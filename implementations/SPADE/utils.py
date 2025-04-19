
import itertools
import functools

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import random_split
from torch.cuda.amp import autocast, GradScaler
from torchvision.utils import save_image

from dataset import AnimeFaceXDoG, DanbooruPortraitXDoG, to_loader
from utils import Status, save_args, add_args
from nnutils import get_device, sample_nnoise
from nnutils.loss import HingeLoss

from .model import Generator, Discriminator, Encoder, init_weight_xavier

l1 = nn.L1Loss()

def KL_divergence(mu, logvar):
    return - 0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())

def feature_matching(real_feats, fake_feats):
    feat_loss = 0
    layer_weight = 4 / len(real_feats) # 1.
    for real_feat, fake_feat in zip(real_feats, fake_feats):
        feat_loss += l1(real_feat, fake_feat) * layer_weight
    return feat_loss

def train(
    dataset, max_iters, sampler, z_dim, test,
    G, D, E, optimizer_G, optimizer_D,
    kld_lambda, feat_lambda, d_num_scale,
    device, amp, save=1000
):

    status = Status(max_iters)
    scaler = GradScaler() if amp else None
    loss = HingeLoss()
    D_input = lambda x, y: torch.cat([x, y], dim=1)

    while status.batches_done < max_iters:
        for index, (rgb, line) in enumerate(dataset):
            optimizer_G.zero_grad()
            optimizer_D.zero_grad()

            rgb = rgb.to(device)
            line = line.to(device)

            '''Discriminator'''
            with autocast(amp):
                if E is not None:
                    # E(rgb)
                    z, mu, logvar = E(rgb)
                else:
                    # z : N(0, 1)
                    z = sampler((rgb.size(0), z_dim))
                # D(line, rgb)
                real_outs = D(D_input(line, rgb))
                # D(line, G(z, line))
                fake = G(z, line)
                fake_outs = D(D_input(line, fake.detach()))
                # loss
                D_loss = 0
                for real_out, fake_out in zip(real_outs, fake_outs):
                    D_loss = D_loss + loss.d_loss(real_out[0], fake_out[0]) / d_num_scale

            if scaler is not None:
                scaler.scale(D_loss).backward()
                scaler.step(optimizer_D)
            else:
                D_loss.backward()
                optimizer_D.step()

            '''Generator (+ Encoder)'''
            with autocast(amp):
                # D(line, rgb)
                real_outs = D(D_input(line, rgb))
                # D(line, G(z, line))
                fake_outs = D(D_input(line, fake))
                # loss
                G_loss = 0
                for real_out, fake_out in zip(real_outs, fake_outs):
                    # gan loss
                    G_loss = G_loss + loss.g_loss(fake_out[0]) / d_num_scale
                    # feature matching loss
                    if feat_lambda > 0:
                        G_loss = G_loss + feature_matching(real_out[1], fake_out[1]) / d_num_scale * feat_lambda
                # KLD loss if E exists
                if E is not None and kld_lambda > 0:
                    G_loss = G_loss + KL_divergence(mu, logvar) * kld_lambda

            if scaler is not None:
                scaler.scale(G_loss).backward()
                scaler.step(optimizer_G)
            else:
                G_loss.backward()
                optimizer_G.step()

            # save
            if status.batches_done % save == 0:
                # save running samples
                image_grid = _image_grid(line, fake, rgb)
                save_image(image_grid, f'implementations/SPADE/result/recons_{status.batches_done}.jpg', nrow=3*3, normalize=True, value_range=(-1, 1))
                # test
                if E is not None: E.eval()
                G.eval()
                with torch.no_grad():
                    if E is not None: z, _, _ = E(test[0])
                    else: z = sampler((test[0].size(0), z_dim))
                    fake = G(z, test[1])
                if E is not None: E.train()
                G.train()
                # save test samples
                image_grid = _image_grid(test[1], fake, test[0])
                save_image(image_grid, f'implementations/SPADE/result/test_{status.batches_done}.jpg', nrow=3*3, normalize=True, value_range=(-1, 1))
                # save models
                torch.save(G.state_dict(), f'implementations/SPADE/result/G_{status.batches_done}.pt')
                if E is not None:
                    torch.save(E.state_dict(), f'implementations/SPADE/result/E_{status.batches_done}.pt')

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

def _image_grid(line, gen, rgb, num_images=6):
    lines = line.repeat(1, 3, 1, 1).chunk(line.size(0), dim=0) # convert to RGB.
    gens = gen.chunk(gen.size(0), dim=0)
    rgbs = rgb.chunk(rgb.size(0), dim=0)

    images = []
    for index, (line, gen, rgb) in enumerate(zip(lines, gens, rgbs)):
        images.extend([line, gen, rgb])
        if index == num_images-1:
            break

    return torch.cat(images, dim=0)

def main(parser):

    parser = add_args(parser,
        dict(
            line_channels         = [1, 'number of channels of line art images'],
            rgb_channels          = [3, 'number of channels of the generated images'],
            test_images           = [6, 'number of images for test'],
            z_dim                 = [256, 'number of dimensions for input z'],
            channels              = [32, 'channel width multiplier'],
            max_channels          = [1024, 'maximum number of channels'],
            block_num_conv        = [2, 'number of convolution layers per residual block'],
            spade_hidden_channels = [128, 'number of channels in SPADE hidden layers'],
            g_norm_name           = ['bn', 'normalization layer name of G'],
            g_act_name            = ['lrelu', 'activation function name of G'],
            g_disable_bias        = [False, 'do not use bias in G'],
            g_disable_sn          = [False, 'do not use spectral normalization in G'],
            num_scale             = [2, 'number of scales to discriminate'],
            num_layers            = [3, 'number of layers in D'],
            d_norm_name           = ['bn', 'normalization layer name of D'],
            d_act_name            = ['lrelu', 'activation function name of D'],
            d_disable_bias        = [False, 'do not use bias in D'],
            d_disable_sn          = [False, 'do not use spectral normalization in D'],
            no_encoder            = [False, 'do not use encoder'],
            target_resl           = [4, 'to what resolution down-sample to before FC layers in E'],
            e_norm_name           = ['bn', 'normalization layer name of E'],
            e_act_name            = ['lrelu', 'activation function name of E'],
            e_disable_bias        = [False, 'do not use bias in E'],
            e_disable_sn          = [False, 'do not use spectral normalization in E'],
            lr                    = [0.0002, 'learning rate'],
            beta1                 = [0.5, 'beta1'],
            beta2                 = [0.999, 'beta2'],
            ttur                  = [False, 'use TTUR'],
            kld_lambda            = [0.05, 'lambda for KL divergence'],
            feat_lambda           = [10., 'lambda for feature matching loss']))
    args = parser.parse_args()
    save_args(args)

    # # generator
    g_use_sn    = not args.g_disable_sn
    g_use_bias  = not args.g_disable_bias
    # discriminator
    d_use_sn    = not args.d_disable_sn
    d_use_bias  = not args.d_disable_bias
    # encoder
    image_guided = not args.no_encoder # if False, will not use Encoder
    e_use_sn    = not args.e_disable_sn
    e_use_bias  = not args.e_disable_bias

    amp = not args.disable_amp
    device = get_device(not args.disable_gpu)

    # dataset
    # dataset = XDoGAnimeFaceDataset(image_size, min_year)
    dataset = DanbooruPortraitXDoG(args.image_size, num_images=args.num_images+args.test_images)
    dataset, test = random_split(dataset, [len(dataset)-args.test_images, args.test_images])
    ## training dataset
    dataset = to_loader(dataset, args.batch_size)
    ## test batch
    test    = to_loader(test, args.test_images, shuffle=False, pin_memory=False)
    test_batch = next(iter(test))
    test_batch = (test_batch[0].to(device), test_batch[1].to(device))
    if args.max_iters < 0:
        args.max_iters = len(dataset) * 100
    ## noise sampler (ignored when E exists)
    sampler = functools.partial(sample_nnoise, device=device)

    # models
    G = Generator(
        args.image_size, args.z_dim, args.line_channels, args.rgb_channels,
        args.channels, args.max_channels,
        args.block_num_conv, args.spade_hidden_channels,
        args.g_norm_name, args.g_act_name, g_use_sn, g_use_bias
    )
    D = Discriminator(
        args.image_size, args.line_channels+args.rgb_channels,
        args.num_scale, args.num_layers, args.channels,
        args.d_norm_name, args.d_act_name, d_use_sn, d_use_bias
    )
    G.apply(init_weight_xavier)
    D.apply(init_weight_xavier)
    G.to(device)
    D.to(device)
    ## create encoder (optional)
    if image_guided:
        E = Encoder(
            args.image_size, args.z_dim, args.rgb_channels, args.target_resl,
            args.channels, args.max_channels,
            e_use_sn, e_use_bias, args.e_norm_name, args.e_act_name
        )
        E.apply(init_weight_xavier)
        E.to(device)
    else:
        E = None
        args.kld_lambda = 0

    # optimizers
    if args.ttur:
        g_lr, d_lr = args.lr / 2, args.lr * 2
        betas = (0., 0.9)
    else:
        g_lr, d_lr = args.lr, args.lr
        betas = (args.beta1, args.beta2)
    optimizer_D = optim.Adam(D.parameters(), lr=d_lr, betas=betas)
    if E is None:
        g_params = G.parameters()
    else:
        g_params = itertools.chain(
            G.parameters(), E.parameters()
        )
    optimizer_G = optim.Adam(g_params, lr=g_lr, betas=betas)

    train(
        dataset, args.max_iters, sampler, args.z_dim, test_batch,
        G, D, E, optimizer_G, optimizer_D,
        args.kld_lambda, args.feat_lambda, args.num_scale,
        device, amp, 1000
    )
