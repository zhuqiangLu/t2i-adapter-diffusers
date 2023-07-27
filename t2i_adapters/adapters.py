# from https://github.com/TencentARC/T2I-Adapter/blob/main/ldm/modules/encoders/adapter.py

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Literal
from . import T2IAdapterUNet2DConditionModel
from einops import rearrange


def conv_nd(dims, *args, **kwargs):
    """
    Create a 1D, 2D, or 3D convolution module.
    """
    if dims == 1:
        return nn.Conv1d(*args, **kwargs)
    elif dims == 2:
        return nn.Conv2d(*args, **kwargs)
    elif dims == 3:
        return nn.Conv3d(*args, **kwargs)
    raise ValueError(f"unsupported dimensions: {dims}")


def avg_pool_nd(dims, *args, **kwargs):
    """
    Create a 1D, 2D, or 3D average pooling module.
    """
    if dims == 1:
        return nn.AvgPool1d(*args, **kwargs)
    elif dims == 2:
        return nn.AvgPool2d(*args, **kwargs)
    elif dims == 3:
        return nn.AvgPool3d(*args, **kwargs)
    raise ValueError(f"unsupported dimensions: {dims}")


class Downsample(nn.Module):
    """
    A downsampling layer with an optional convolution.
    :param channels: channels in the inputs and outputs.
    :param use_conv: a bool determining if a convolution is applied.
    :param dims: determines if the signal is 1D, 2D, or 3D. If 3D, then
                 downsampling occurs in the inner-two dimensions.
    """

    def __init__(self, channels, use_conv, dims=2, out_channels=None, padding=1):
        super().__init__()
        self.channels = channels
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.dims = dims
        stride = 2 if dims != 3 else (1, 2, 2)
        if use_conv:
            self.op = conv_nd(
                dims,
                self.channels,
                self.out_channels,
                3,
                stride=stride,
                padding=padding,
            )
        else:
            assert self.channels == self.out_channels
            self.op = avg_pool_nd(dims, kernel_size=stride, stride=stride)

    def forward(self, x):
        assert x.shape[1] == self.channels
        return self.op(x)


class ResnetBlock(nn.Module):
    def __init__(self, in_c, out_c, down, ksize=3, sk=False, use_conv=True):
        super().__init__()
        ps = ksize // 2
        if in_c != out_c or sk == False:
            self.in_conv = nn.Conv2d(in_c, out_c, ksize, 1, ps)
        else:
            # print('n_in')
            self.in_conv = None
        self.block1 = nn.Conv2d(out_c, out_c, 3, 1, 1)
        self.act = nn.ReLU()
        self.block2 = nn.Conv2d(out_c, out_c, ksize, 1, ps)
        if self.in_conv is None:
            self.skep = nn.Conv2d(in_c, out_c, ksize, 1, ps)
        else:
            self.skep = None

        self.down = down
        if self.down == True:
            self.down_opt = Downsample(in_c, use_conv=use_conv)

    def forward(self, x):
        if self.down == True:
            x = self.down_opt(x)
        if self.in_conv is not None:  # edit
            x = self.in_conv(x)

        h = self.block1(x)
        h = self.act(h)
        h = self.block2(h)
        if self.skep is not None:
            return h + self.skep(x)
        else:
            return h + x


class Adapter(nn.Module):
    def __init__(
        self,
        channels=[320, 640, 1280, 1280],
        nums_rb=3,
        cin=64,
        ksize=3,
        sk=False,
        use_conv=True,
    ):
        super(Adapter, self).__init__()
        self.unshuffle = nn.PixelUnshuffle(8)
        self.channels = channels
        self.nums_rb = nums_rb
        self.body = []
        for i in range(len(channels)):
            for j in range(nums_rb):
                if (i != 0) and (j == 0):
                    self.body.append(
                        ResnetBlock(
                            channels[i - 1],
                            channels[i],
                            down=True,
                            ksize=ksize,
                            sk=sk,
                            use_conv=use_conv,
                        )
                    )
                else:
                    self.body.append(
                        ResnetBlock(
                            channels[i],
                            channels[i],
                            down=False,
                            ksize=ksize,
                            sk=sk,
                            use_conv=use_conv,
                        )
                    )
        self.body = nn.ModuleList(self.body)
        self.conv_in = nn.Conv2d(cin, channels[0], 3, 1, 1)

    def extract_patch(self, x):
        # unshuffle
        x = self.unshuffle(x)
        # extract features
        features = []
        x = self.conv_in(x)
        for i in range(len(self.channels)):
            for j in range(self.nums_rb):
                idx = i * self.nums_rb + j
                x = self.body[idx](x)
            features.append(x)

        return features

    def forward(self, cube, pers, process_idxs=None):
        b, n, c, _, _ = cube.shape 
        cube = rearrange(cube, 'b n c h w -> (b n) c h w') 
        cube_feats = self.extract_patch(cube) 
        reshape_cube_feats = list() 
        for cube_feat in cube_feats:
            reshape_cube_feats.append(rearrange(cube_feat, '(b n) c h w -> b n c h w', b=b, n=n))

        pers_feat = self.extract_patch(pers)
        return reshape_cube_feats, pers_feat

        
        



    @classmethod
    def from_pretrained(
        cls, adapter_type: Literal["sketch", "seg", "keypose", "depth"]
    ):

        WEB_PATH = {
            "sketch": "https://huggingface.co/TencentARC/T2I-Adapter/resolve/main/models/t2iadapter_sketch_sd14v1.pth",
            "depth": "https://huggingface.co/TencentARC/T2I-Adapter/resolve/main/models/t2iadapter_depth_sd14v1.pth",
            "seg": "https://huggingface.co/TencentARC/T2I-Adapter/resolve/main/models/t2iadapter_seg_sd14v1.pth",
            "keypose": "https://huggingface.co/TencentARC/T2I-Adapter/resolve/main/models/t2iadapter_keypose_sd14v1.pth",
        }

        if adapter_type == "sketch":
            mod = Adapter(
                channels=[320, 640, 1280, 1280],
                nums_rb=2,
                ksize=1,
                sk=True,
                use_conv=False,
            )

        else:
            # all other models are in:
            mod = Adapter(
                cin=64 * 3,
                channels=[320, 640, 1280, 1280],
                nums_rb=2,
                ksize=1,
                sk=True,
                use_conv=False,
            )

        mod.load_state_dict(
            torch.hub.load_state_dict_from_url(
                WEB_PATH[adapter_type], map_location="cpu"
            )
        )

        return mod


def patch_pipe(pipe):

    a_unet = T2IAdapterUNet2DConditionModel.from_config(pipe.unet.config)
    a_unet.load_state_dict(pipe.unet.state_dict(), strict=False)
    a_unet.to(pipe.unet.device).to(pipe.unet.dtype).eval()
    pipe.unet = a_unet


if __name__ == "__main__":

    model = Adapter.from_pretrained("sketch")
    print(model)
