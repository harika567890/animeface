
import itertools

import torch
import torch.nn as nn
import torch.optim as optim
from torch.cuda.amp import autocast, GradScaler
from torch.utils.data import random_split
from torchvision.utils import save_image

from dataset import AnimeFaceCelebA, DanbooruPortraitCelebA, to_loader
from utils import Status, save_args, add_args
from nnutils import get_device, init
from nnutils.loss import LSGANLoss

from .model import Generator, Discriminator

l1 = nn.L1Loss()

def train(
    max_iters, dataset, test,
    GA, GH, DA, DH, optimizer_G, optimizer_D,
    cycle_lambda,
    amp, device, save
):

    status = Status(max_iters)
    loss   = LSGANLoss()
    scaler = GradScaler() if amp else None

    while status.batches_done < max_iters:
        for anime, human in dataset:
            optimizer_D.zero_grad()
            optimizer_G.zero_grad()

            anime = anime.to(device)
            human = human.to(device)

            with autocast(amp):
                '''generate images'''
                AH = GH(anime)
                HA = GA(human)
                AHA = GA(AH)
                HAH = GH(HA)

                '''discriminator'''
                real_anime, _ = DA(anime)
                real_human, _ = DH(human)
                fake_anime, _ = DA(HA.detach())
                fake_human, _ = DH(AH.detach())

                # loss
                adv_anime = loss.d_loss(real_anime, fake_anime)
                adv_human = loss.d_loss(real_human, fake_human)
                D_loss = adv_anime + adv_human

            if scaler is not None:
                scaler.scale(D_loss).backward()
                scaler.step(optimizer_D)
            else:
                D_loss.backward()
                optimizer_D.step()

            with autocast(amp):
                '''generator'''
                fake_anime, _ = DA(HA)
                fake_human, _ = DH(AH)

                # loss
                adv_anime = loss.g_loss(fake_anime)
                adv_human = loss.g_loss(fake_human)
                cycle_anime = l1(AHA, anime)
                cycle_human = l1(HAH, human)
                G_loss = adv_anime + adv_human \
                    + (cycle_anime + cycle_human) * cycle_lambda

            if scaler is not None:
                scaler.scale(G_loss).backward()
                scaler.step(optimizer_G)
            else:
                G_loss.backward()
                optimizer_G.step()

            # save
            if status.batches_done % save == 0:
                with torch.no_grad():
                    GA.eval()
                    GH.eval()
                    ah = GH(test[0])
                    ha = GH(test[1])
                    GA.train()
                    GH.train()
                image_grid = _image_grid(test[0], test[1], ah, ha)
                save_image(image_grid, f'implementations/GANILLA/result/{status.batches_done}.jpg',
                    nrow=4*3, normalize=True, value_range=(-1, 1))
                ckpt = dict(ga=GA.state_dict(), gh=GH.state_dict())
                torch.save(ckpt, f'implementations/GANILLA/result/G_{status.batches_done}.pt')
            save_image(AH, 'running_AH.jpg', normalize=True, value_range=(-1, 1))
            save_image(HA, 'running_HA.jpg', normalize=True, value_range=(-1, 1))

            # updates
            loss_dict = dict(
                G=G_loss.item() if not torch.isnan(G_loss).any() else 0,
                D=D_loss.item() if not torch.isnan(D_loss).any() else 0
            )
            status.update(**loss_dict)
            if scaler is not None:
                scaler.update()

            if status.batches_done == max_iters:
                break
    status.plot_loss()

def _image_grid(a, h, ah, ha):
    _split = lambda x: x.chunk(x.size(0), dim=0)
    a_s = _split(a)
    hs  = _split(h)
    ahs = _split(ah)
    has = _split(ha)
    images = []
    for a, h, ah, ha in zip(a_s, hs, ahs, has):
        images.extend([a, h, ha, ah])
    return torch.cat(images, dim=0)

def main(parser):
    parser = add_args(parser,
        dict(
            num_test         = [6, 'number of images for eval'],
            image_channels   = [3, 'image channels'],
            bottom_width     = [8, 'bottom width'],
            num_downs        = [int, 'number of up/down sampling'],
            num_feats        = [3, 'number of features to return from the encoder'],
            g_channels       = [32, 'channel_width multiplier'],
            hid_channels     = [128, 'channels in decoder'],
            layer_num_blocks = [2, 'number of blocks in one GANILLA layer'],
            g_disable_sn     = [False, 'disable spectral norm'],
            g_bias           = [False, 'enable bias'],
            g_norm_name      = ['in', 'normalization layer name'],
            g_act_name       = ['lrelu', 'activation function name'],
            num_layers       = [3, 'number of layers'],
            d_channels       = [32, 'channel width multiplier'],
            d_disable_sn     = [False, 'disable spectral norm'],
            d_disable_bias   = [False, 'disable bias'],
            d_norm_name      = ['in', 'normalization layer name'],
            d_act_name       = ['relu', 'activation function name'],
            lr               = [0.0002, 'learning rate'],
            betas            = [[0.5, 0.999], 'betas'],
            cycle_lambda     = [10., 'lambda for cycle consistency loss']))
    args = parser.parse_args()
    save_args(args)

    amp = not args.disable_amp and not args.disable_gpu
    device = get_device(not args.disable_gpu)

    # dataset
    if args.dataset == 'animeface':
        dataset = AnimeFaceCelebA(args.image_size, args.min_year)
    elif args.dataset == 'danbooru':
        dataset = DanbooruPortraitCelebA(args.image_size, num_images=args.num_images+args.num_test)
    dataset, test = random_split(dataset, [len(dataset)-args.num_test, args.num_test])
    # train
    dataset = to_loader(dataset, args.batch_size)
    # test
    test = to_loader(test, args.num_test, shuffle=False, pin_memory=False)
    test_batch = next(iter(test))
    test_batch = (test_batch[0].to(device), test_batch[1].to(device))

    if args.max_iters < 0:
        args.max_iters = len(dataset) * args.default_epochs

    # models
    GA = Generator(
        args.image_size, args.image_channels, args.bottom_width,
        args.num_downs, args.num_feats, args.g_channels, args.hid_channels,
        args.layer_num_blocks, not args.g_disable_sn, args.g_bias,
        args.g_norm_name, args.g_act_name
    )
    GH = Generator(
        args.image_size, args.image_channels, args.bottom_width,
        args.num_downs, args.num_feats, args.g_channels, args.hid_channels,
        args.layer_num_blocks, not args.g_disable_sn, args.g_bias,
        args.g_norm_name, args.g_act_name
    )
    DA = Discriminator(
        args.image_size, args.image_channels, args.num_layers,
        args.d_channels, not args.d_disable_sn, not args.d_disable_bias,
        args.d_norm_name, args.d_act_name
    )
    DH = Discriminator(
        args.image_size, args.image_channels, args.num_layers,
        args.d_channels, not args.d_disable_sn, not args.d_disable_bias,
        args.d_norm_name, args.d_act_name
    )
    GA.apply(init().N002)
    GH.apply(init().N002)
    DA.apply(init().N002)
    DH.apply(init().N002)
    GA.to(device)
    GH.to(device)
    DA.to(device)
    DH.to(device)

    # optimizers
    optimizer_G = optim.Adam(
        itertools.chain(GA.parameters(), GH.parameters()),
        lr=args.lr, betas=args.betas
    )
    optimizer_D = optim.Adam(
        itertools.chain(DA.parameters(), DH.parameters()),
        lr=args.lr, betas=args.betas
    )

    train(
        args.max_iters, dataset, test_batch,
        GA, GH, DA, DH,
        optimizer_G, optimizer_D,
        args.cycle_lambda,
        amp, device, args.save
    )
