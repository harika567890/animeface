
# Labeling images using illustration2vec

## `label.py`

Labeling images for conditional models.
I labeled the images by using [illustration2vec](https://github.com/rezoo/illustration2vec) [1].  
I used the tags that are introduced in the work by Yanghua Jin et al [2] for the categories to give for each image.

## `color_label.py`

Labeling images for HiSD using [1].
Three categories (i.e. hair color, eye color and w/wo glasses)
No overlap.

## Reference

```
[1] Masaki, S., Yusuke M.,
    "Illustration2Vec: a semantic vector representation of illustrations},"
    in proceedings of SIGGRAPH Asia Technical Briefs
[2] Jin, Y., Zhang, J., Li, M., Tian, Y., Zhu, H., Fang, Z.  
    "Towards the Automatic Anime Characters Creation with Generative Adversarial Networks,"  
    arXiv 2017, arXiv:1708.05509,  
    https://arxiv.org/abs/1708.05509
```