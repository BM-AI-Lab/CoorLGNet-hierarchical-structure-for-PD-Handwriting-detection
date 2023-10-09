import math
import logging
from functools import partial
from collections import OrderedDict
from torch import Tensor
import torch
import torch.nn as nn
import torch.nn.functional as F

from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from timm.models.helpers import load_pretrained
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from timm.models.resnet import resnet26d, resnet50d
from timm.models.registry import register_model
from CA_Block import CoordAtt
from eca_module import eca_layer
# from dynamic_conv import DynamicConv
from cbam_module import SpatialAttention, ChannelAttention

_logger = logging.getLogger(__name__)


def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 2, 'input_size': (3, 224, 224), 'pool_size': None,
        'crop_pct': .9, 'interpolation': 'bicubic',
        'mean': IMAGENET_DEFAULT_MEAN, 'std': IMAGENET_DEFAULT_STD,
        'first_conv': 'patch_embed.proj', 'classifier': 'head',
        **kwargs
    }


def window_partition(x, window_size: int):
    """
    将feature map按照window_size划分成一个个没有重叠的window
    Args:
        x: (B, H, W, C)
        window_size (int): window size(M)

    Returns:
        windows: (num_windows*B, window_size, window_size, C)
    """
    B, H, W, C = x.shape
    x = x.view(B, H // window_size, window_size, W // window_size, window_size, C)
    # permute: [B, H//Mh, Mh, W//Mw, Mw, C] -> [B, H//Mh, W//Mh, Mw, Mw, C]
    # view: [B, H//Mh, W//Mw, Mh, Mw, C] -> [B*num_windows, Mh, Mw, C]
    windows = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size, window_size, C)
    return windows


def window_reverse(windows, window_size: int, H: int, W: int):
    """
    将一个个window还原成一个feature map
    Args:
        windows: (num_windows*B, window_size, window_size, C)
        window_size (int): Window size(M)
        H (int): Height of image
        W (int): Width of image

    Returns:
        x: (B, H, W, C)
    """
    B = int(windows.shape[0] / (H * W / window_size / window_size))
    # view: [B*num_windows, Mh, Mw, C] -> [B, H//Mh, W//Mw, Mh, Mw, C]
    x = windows.view(B, H // window_size, W // window_size, window_size, window_size, -1)
    # permute: [B, H//Mh, W//Mw, Mh, Mw, C] -> [B, H//Mh, Mh, W//Mw, Mw, C]
    # view: [B, H//Mh, Mh, W//Mw, Mw, C] -> [B, H, W, C]
    x = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(B, H, W, -1)
    return x


# A memory-efficient implementation of Swish function
class SwishImplementation(torch.autograd.Function):
    @staticmethod
    def forward(ctx, i):
        result = i * torch.sigmoid(i)
        ctx.save_for_backward(i)
        return result

    @staticmethod
    def backward(ctx, grad_output):
        i = ctx.saved_tensors[0]
        sigmoid_i = torch.sigmoid(i)
        return grad_output * (sigmoid_i * (1 + i * (1 - sigmoid_i)))


class MemoryEfficientSwish(nn.Module):
    def forward(self, x):
        return SwishImplementation.apply(x)


# class SqueezeExcitation(nn.Module):
#     def __init__(self,
#                  input_c: int,   # block input channel
#                  expand_c: int,  # block expand channel
#                  squeeze_factor: int = 4):
#         super(SqueezeExcitation, self).__init__()
#         squeeze_c = input_c // squeeze_factor
#         self.fc1 = nn.Conv2d(expand_c, squeeze_c, 1)
#         self.ac1 = nn.SiLU()  # alias Swish
#         self.fc2 = nn.Conv2d(squeeze_c, expand_c, 1)
#         self.ac2 = nn.Sigmoid()
#
#     def forward(self, x: Tensor) -> Tensor:
#         scale = F.adaptive_avg_pool2d(x, output_size=(1, 1))
#         scale = self.fc1(scale)
#         scale = self.ac1(scale)
#         scale = self.fc2(scale)
#         scale = self.ac2(scale)
#         return x * scale.expand_as(x)





# class MHCA(nn.Module):
#     """
#     Multi-Head Convolutional Attention
#     """
#     def __init__(self, out_channels, head_dim):
#         super(MHCA, self).__init__()
#         norm_layer = partial(nn.BatchNorm2d, eps=1e-5)
#         self.group_conv3x3 = nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1,
#                                        padding=1, groups=out_channels // head_dim, bias=False)
#         self.norm = norm_layer(out_channels)
#         # self.act = nn.ReLU(inplace=True)
#         self.act = nn.GELU()
#         self.projection = nn.Conv2d(out_channels, out_channels, kernel_size=1, bias=False)
#
#     def forward(self, x):
#         out = self.group_conv3x3(x)
#         out = self.norm(out)
#         out = self.act(out)
#         out = self.projection(out)
#         return out


# class NCB(nn.Module):
#     """
#     Next Convolution Block
#     """
#     def __init__(self, in_channels, out_channels, stride=1, path_dropout=0,
#                  drop=0, head_dim=32, mlp_ratio=3):
#         super(NCB, self).__init__()
#         self.in_channels = in_channels
#         self.out_channels = out_channels
#         norm_layer = partial(nn.BatchNorm2d, eps=1e-5)
#         assert out_channels % head_dim == 0
#
#         self.patch_embed = PatchEmbed(in_channels, out_channels, stride)
#         self.mhca = MHCA(out_channels, head_dim)
#         self.attention_path_dropout = DropPath(path_dropout)
#
#         self.norm = norm_layer(out_channels)
#         self.mlp = Mlp(out_channels, mlp_ratio=mlp_ratio, drop=drop, bias=True)
#         self.mlp_path_dropout = DropPath(path_dropout)
#         self.is_bn_merged = False
#
#     def merge_bn(self):
#         if not self.is_bn_merged:
#             self.mlp.merge_bn(self.norm)
#             self.is_bn_merged = True
#
#     def forward(self, x):
#         x = self.patch_embed(x)
#         x = x + self.attention_path_dropout(self.mhca(x))
#         if not torch.onnx.is_in_onnx_export() and not self.is_bn_merged:
#             out = self.norm(x)
#         else:
#             out = x
#         x = x + self.mlp_path_dropout(self.mlp(out))
#         return x


class FFN(nn.Module):
    """ MLP as used in Vision Transformer, MLP-Mixer and related networks
    """
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features

        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.drop1 = nn.Dropout(drop)
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop2 = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop1(x)
        x = self.fc2(x)
        x = self.drop2(x)
        return x


class Mlp(nn.Module):
    # def __init__(self, in_features, hidden_features=None, out_features=None, baseWidth=26, scale = 4, stride = 1, act_layer=nn.GELU, drop=0.):
    def __init__(self, in_features, hidden_features=None, out_features=None, baseWidth=26, scale=4, stride=1, act_layer=nn.GELU, drop=0.2):
        super().__init__()

        width = int(math.floor(hidden_features * (baseWidth / 64.0)))  # 256 * (26 / 64) = 104

        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.conv1 = nn.Sequential(
            nn.Conv2d(in_features, width * scale, 1, 1, 0, bias=True),  # in_features = 64, width * scale = 104*4 = 416
            nn.GELU(),
            nn.BatchNorm2d(width * scale, eps=1e-5),
        )
        self.proj = nn.Conv2d(hidden_features, hidden_features, 3, 1, 1, groups=hidden_features)  # DW卷积
        self.proj_act = nn.GELU()
        self.proj_bn = nn.BatchNorm2d(hidden_features, eps=1e-5)
        self.conv2 = nn.Sequential(
            nn.Conv2d(width * scale, out_features, 1, 1, 0, bias=True),
            nn.BatchNorm2d(out_features, eps=1e-5),
        )
        if scale == 1:
            self.nums = 1
        else:
            self.nums = scale - 1   # 3

        self.relu = nn.GELU()
        # self.eca_layer = eca_layer(channel=width * scale, k_size=3)

        self.drop = nn.Dropout(drop)

        convs = []
        bns = []
        for i in range(self.nums):  # 3个 3 × 3卷积
            convs.append(nn.Conv2d(width, width, kernel_size=3, stride=stride, padding=1, bias=False, groups=width))
            bns.append(nn.BatchNorm2d(width))
        self.convs = nn.ModuleList(convs)
        self.bns = nn.ModuleList(bns)

        self.width = width
        self.scale = scale


    def forward(self, x, H, W):
        B, N, C = x.shape
        x = x.permute(0, 2, 1).reshape(B, C, H, W)
        residual = x

        x = self.conv1(x)

        spx = torch.split(x, self.width, 1)  # 按照第1个维度划分，即通道维度,划分为4个, 每个通道104  # [416, 56, 56] --> [104, 56, 56] × 4
        for i in range(self.nums):
            if i == 0:
                sp = spx[i]
            else:
                sp = sp + spx[i]
            sp = self.convs[i](sp)  # 3 × 3卷积
            sp = self.relu(self.bns[i](sp))
            if i == 0:
                x = sp
            else:
                x = torch.cat((x, sp), 1)
        if self.scale != 1:
            x = torch.cat((x, spx[self.nums]), 1)  # 拼上没有经过3 × 3卷积的


        # x = self.eca_layer(x)
        x = self.conv2(x)
        x = x + residual
        x = self.relu(x)

        x = x.flatten(2).permute(0, 2, 1)
        x = self.drop(x)
        return x


class WindowAttention(nn.Module):
    r""" Window based multi-head self attention (W-MSA) module with relative position bias.
    It supports both of shifted and non-shifted window.

    Args:
        dim (int): Number of input channels.
        window_size (tuple[int]): The height and width of the window.
        num_heads (int): Number of attention heads.
        qkv_bias (bool, optional):  If True, add a learnable bias to query, key, value. Default: True
        attn_drop (float, optional): Dropout ratio of attention weight. Default: 0.0
        proj_drop (float, optional): Dropout ratio of output. Default: 0.0
    """

    def __init__(self, dim, window_size, num_heads, qkv_bias=True, attn_drop=0., proj_drop=0., sr_ratio=1, qk_ratio=1):

        super().__init__()
        self.dim = dim
        self.window_size = window_size  # [Mh, Mw]
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.sr_ratio = sr_ratio
        self.qk_dim = dim // qk_ratio
        self.scale = head_dim ** -0.5

        # define a parameter table of relative position bias
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros((2 * window_size[0] - 1) * (2 * window_size[1] - 1), num_heads))  # [2*Mh-1 * 2*Mw-1, nH]

        # get pair-wise relative position index for each token inside the window
        coords_h = torch.arange(self.window_size[0])
        coords_w = torch.arange(self.window_size[1])
        coords = torch.stack(torch.meshgrid([coords_h, coords_w], indexing="ij"))  # [2, Mh, Mw]
        coords_flatten = torch.flatten(coords, 1)  # [2, Mh*Mw]
        # [2, Mh*Mw, 1] - [2, 1, Mh*Mw]
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # [2, Mh*Mw, Mh*Mw]
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # [Mh*Mw, Mh*Mw, 2]
        relative_coords[:, :, 0] += self.window_size[0] - 1  # shift to start from 0
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1
        relative_position_index = relative_coords.sum(-1)  # [Mh*Mw, Mh*Mw]
        self.register_buffer("relative_position_index", relative_position_index)

        self.q = nn.Linear(dim, self.qk_dim, bias=qkv_bias)
        self.k = nn.Linear(dim, self.qk_dim, bias=qkv_bias)
        self.v = nn.Linear(dim, dim, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.sr_ratio = sr_ratio
        # Exactly same as PVTv1

        nn.init.trunc_normal_(self.relative_position_bias_table, std=.02)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x):
        B_, N, C = x.shape
        q = self.q(x).reshape(B_, N, self.num_heads, self.qk_dim // self.num_heads).permute(0, 2, 1, 3)  # self.qk_dim // self.num_heads表示多头时，分出来的头的dimension
        k = self.k(x).reshape(B_, N, self.num_heads, self.qk_dim // self.num_heads).permute(0, 2, 1, 3)
        v = self.v(x).reshape(B_, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)


        # transpose: -> [batch_size*num_windows, num_heads, embed_dim_per_head, Mh*Mw]
        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, Mh*Mw]
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))

        # relative_position_bias_table.view: [Mh*Mw*Mh*Mw,nH] -> [Mh*Mw,Mh*Mw,nH]
        relative_position_bias = self.relative_position_bias_table[self.relative_position_index.view(-1)].view(
            self.window_size[0] * self.window_size[1], self.window_size[0] * self.window_size[1], -1)
        relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # [nH, Mh*Mw, Mh*Mw]
        attn = attn + relative_position_bias.unsqueeze(0)
        attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        # transpose: -> [batch_size*num_windows, Mh*Mw, num_heads, embed_dim_per_head]
        # reshape: -> [batch_size*num_windows, Mh*Mw, total_embed_dim]
        x = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x



class Attention(nn.Module):
    # def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None,
    #              attn_drop=0., proj_drop=0., qk_ratio=1, sr_ratio=1):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None,
                 attn_drop=0., proj_drop=0.2, qk_ratio=1, sr_ratio=1):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5    # scale对应根号下dk分之一
        self.qk_dim = dim // qk_ratio

        self.q = nn.Linear(dim, self.qk_dim, bias=qkv_bias)
        self.k = nn.Linear(dim, self.qk_dim, bias=qkv_bias)
        self.v = nn.Linear(dim, dim, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.sr_ratio = sr_ratio
        # Exactly same as PVTv1
        if self.sr_ratio > 1:
            self.sr = nn.Sequential(
                nn.Conv2d(dim, dim, kernel_size=sr_ratio, stride=sr_ratio, groups=dim, bias=True),
                nn.BatchNorm2d(dim, eps=1e-5),
            )

    def forward(self, x, H, W, relative_pos):
        B, N, C = x.shape
        q = self.q(x).reshape(B, N, self.num_heads, self.qk_dim // self.num_heads).permute(0, 2, 1, 3)   # self.qk_dim // self.num_heads表示多头时，分出来的头的dimension

        if self.sr_ratio > 1:
            x_ = x.permute(0, 2, 1).reshape(B, C, H, W)
            x_ = self.sr(x_).reshape(B, C, -1).permute(0, 2, 1)
            k = self.k(x_).reshape(B, -1, self.num_heads, self.qk_dim // self.num_heads).permute(0, 2, 1, 3)
            v = self.v(x_).reshape(B, -1, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        else:
            k = self.k(x).reshape(B, N, self.num_heads, self.qk_dim // self.num_heads).permute(0, 2, 1, 3)
            v = self.v(x).reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)

        attn = (q @ k.transpose(-2, -1)) * self.scale + relative_pos   # q × k的转置  @表示矩阵乘法   此处是矩阵乘法
        attn = attn.softmax(dim=-1)   # 对得到结果的每一行进行softmax处理  dim=-1代表最后一个维度  即每一行
        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x




class Block(nn.Module):
    def __init__(self, dim, num_heads, mlp_ratio=1., qkv_bias=False, qk_scale=None, drop=0.2, attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, qk_ratio=1, sr_ratio=1, window_size=7):
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.mlp_ratio = mlp_ratio
        self.norm1 = norm_layer(dim)

        self.win_attn = WindowAttention(
            dim, window_size=(self.window_size, self.window_size), num_heads=num_heads, qkv_bias=qkv_bias,
            attn_drop=attn_drop, proj_drop=drop, qk_ratio=qk_ratio, sr_ratio=sr_ratio)

        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, qk_ratio=qk_ratio, sr_ratio=sr_ratio)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.ffn = FFN(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.proj = nn.Conv2d(dim, dim, 3, 1, 1, groups=dim)  # DW卷积



        # self.bn = nn.BatchNorm2d(dim, eps=1e-5)
        # self.gelu = nn.GELU()
        # self.mhca = MHCA(dim, head_dim=dim // num_heads)
        self.ca_att = CoordAtt(dim, dim)      # CA Attention
        # self.dropout = nn.Dropout(drop)

    def forward(self, x, H, W, relative_pos):
        B, N, C = x.shape   # [B, 3136, 64]
        cnn_feat = x.permute(0, 2, 1).reshape(B, C, H, W)   # [B, 64, 56, 56]
        # x = self.proj(cnn_feat) + cnn_feat       # [B, 64, 56, 56]
        # x = self.mhca(cnn_feat) + cnn_feat

        x = self.proj(cnn_feat)
        # x = self.gelu(x)
        # x = self.bn(x)
        x = self.ca_att(x) + cnn_feat

        # fuse_conv = x      # [B, 64, 56, 56]

        x = x.flatten(2).permute(0, 2, 1)
        shortcut = x
        x = self.norm1(x)
        x = x.reshape(B, H, W, C)      # [B, 56, 56, 64]

        # pad feature maps to multiples of window size
        # 把feature map给pad到window size的整数倍
        pad_l = pad_t = 0
        pad_r = (self.window_size - W % self.window_size) % self.window_size
        pad_b = (self.window_size - H % self.window_size) % self.window_size
        x = F.pad(x, (0, 0, pad_l, pad_r, pad_t, pad_b))
        _, Hp, Wp, _ = x.shape

        shifted_x = x

        # partition windows
        x_windows = window_partition(shifted_x, self.window_size)  # [nW*B, Mh, Mw, C]
        x_windows = x_windows.view(-1, self.window_size * self.window_size, C)  # [nW*B, Mh*Mw, C]

        # W-MSA
        attn_windows = self.win_attn(x_windows)  # [nW*B, Mh*Mw, C]

        # merge windows
        attn_windows = attn_windows.view(-1, self.window_size, self.window_size, C)  # [nW*B, Mh, Mw, C]
        x = window_reverse(attn_windows, self.window_size, Hp, Wp)  # [B, H', W', C]

        if pad_r > 0 or pad_b > 0:
            # 把前面pad的数据移除掉
            x = x[:, :H, :W, :].contiguous()

        x = x.view(B, H * W, C)
        x = shortcut + self.drop_path(x)
        x = x + self.drop_path(self.ffn(self.norm2(x)))


        x = x + self.drop_path(self.attn(self.norm1(x), H, W, relative_pos))
        x = x + self.drop_path(self.mlp(self.norm2(x), H, W))    # [B, 3136, 64]


        return x




class PatchEmbed(nn.Module):
    """ Image to Patch Embedding
    """

    def __init__(self, img_size=224, patch_size=16, in_chans=3, embed_dim=768):
        super().__init__()
        img_size = to_2tuple(img_size)    # img_size = [112, 112]
        patch_size = to_2tuple(patch_size)  # patch_size = [2, 2]
        num_patches = (img_size[1] // patch_size[1]) * (img_size[0] // patch_size[0])   # 56 * 56 = 3136

        assert img_size[0] % patch_size[0] == 0 and img_size[1] % patch_size[1] == 0, \
            f"img_size {img_size} should be divided by patch_size {patch_size}."

        self.img_size = img_size   # 112
        self.patch_size = patch_size  # 2
        self.num_patches = num_patches   # 3136

        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)
        self.norm = nn.LayerNorm(embed_dim)

    def forward(self, x):
        B, C, H, W = x.shape
        # FIXME look at relaxing size constraints
        assert H == self.img_size[0] and W == self.img_size[1], \
            f"Input image size ({H}*{W}) doesn't match model ({self.img_size[0]}*{self.img_size[1]})."
        x = self.proj(x).flatten(2).transpose(1, 2)
        x = self.norm(x)

        H, W = H // self.patch_size[0], W // self.patch_size[1]
        return x, (H, W)




class CoorLGNet(nn.Module):
    def __init__(self, img_size=224, in_chans=3, num_classes=2, embed_dims=[46, 92, 184, 368], stem_channel=16,
                 fc_dim=1280,
                 num_heads=[1, 2, 4, 8], mlp_ratios=[3.6, 3.6, 3.6, 3.6], qkv_bias=True, qk_scale=None,
                 representation_size=None,
                 drop_rate=0.2, attn_drop_rate=0., drop_path_rate=0., hybrid_backbone=None, norm_layer=None,
                 depths=[2, 2, 10, 2], qk_ratio=1, sr_ratios=[8, 4, 2, 1], dp=0.1):
        super().__init__()
        self.num_classes = num_classes
        self.num_features = self.embed_dim = embed_dims[-1]
        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)

        self.stem_conv1 = nn.Conv2d(3, stem_channel, kernel_size=3, stride=2, padding=1, bias=True)
        self.stem_relu1 = nn.GELU()
        self.stem_norm1 = nn.BatchNorm2d(stem_channel, eps=1e-5)

        self.stem_conv2 = nn.Conv2d(stem_channel, stem_channel, kernel_size=3, stride=1, padding=1, bias=True)
        self.stem_relu2 = nn.GELU()
        self.stem_norm2 = nn.BatchNorm2d(stem_channel, eps=1e-5)

        self.stem_conv3 = nn.Conv2d(stem_channel, stem_channel, kernel_size=3, stride=1, padding=1, bias=True)  # 224-->112
        self.stem_relu3 = nn.GELU()
        self.stem_norm3 = nn.BatchNorm2d(stem_channel, eps=1e-5)

        # self.eca = eca_layer(channel=stem_channel, k_size=3)

        self.patch_embed_a = PatchEmbed(
            img_size=img_size // 2, patch_size=2, in_chans=stem_channel, embed_dim=embed_dims[0])    # 112 -->56
        self.patch_embed_b = PatchEmbed(
            img_size=img_size // 4, patch_size=2, in_chans=embed_dims[0], embed_dim=embed_dims[1])   # 56 -->28
        self.patch_embed_c = PatchEmbed(
            img_size=img_size // 8, patch_size=2, in_chans=embed_dims[1], embed_dim=embed_dims[2])   # 28 -->14
        self.patch_embed_d = PatchEmbed(
            img_size=img_size // 16, patch_size=2, in_chans=embed_dims[2], embed_dim=embed_dims[3])

        self.relative_pos_a = nn.Parameter(torch.randn(
            num_heads[0], self.patch_embed_a.num_patches,     #  torch.randn(1, 3136, 49)
            self.patch_embed_a.num_patches // sr_ratios[0] // sr_ratios[0]))
        self.relative_pos_b = nn.Parameter(torch.randn(
            num_heads[1], self.patch_embed_b.num_patches,
            self.patch_embed_b.num_patches // sr_ratios[1] // sr_ratios[1]))
        self.relative_pos_c = nn.Parameter(torch.randn(
            num_heads[2], self.patch_embed_c.num_patches,
            self.patch_embed_c.num_patches // sr_ratios[2] // sr_ratios[2]))
        self.relative_pos_d = nn.Parameter(torch.randn(
            num_heads[3], self.patch_embed_d.num_patches,
            self.patch_embed_d.num_patches // sr_ratios[3] // sr_ratios[3]))

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule
        cur = 0
        self.blocks_a = nn.ModuleList([
            Block(
                dim=embed_dims[0], num_heads=num_heads[0], mlp_ratio=mlp_ratios[0], qkv_bias=qkv_bias,
                qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i],
                norm_layer=norm_layer, qk_ratio=qk_ratio, sr_ratio=sr_ratios[0], window_size=7)
            for i in range(depths[0])])
        cur += depths[0]
        self.blocks_b = nn.ModuleList([
            Block(
                dim=embed_dims[1], num_heads=num_heads[1], mlp_ratio=mlp_ratios[1], qkv_bias=qkv_bias,
                qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i],
                norm_layer=norm_layer, qk_ratio=qk_ratio, sr_ratio=sr_ratios[1], window_size=7)
            for i in range(depths[1])])
        cur += depths[1]
        self.blocks_c = nn.ModuleList([
            Block(
                dim=embed_dims[2], num_heads=num_heads[2], mlp_ratio=mlp_ratios[2], qkv_bias=qkv_bias,
                qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i],
                norm_layer=norm_layer, qk_ratio=qk_ratio, sr_ratio=sr_ratios[2], window_size=7)
            for i in range(depths[2])])
        cur += depths[2]
        self.blocks_d = nn.ModuleList([
            Block(
                dim=embed_dims[3], num_heads=num_heads[3], mlp_ratio=mlp_ratios[3], qkv_bias=qkv_bias,
                qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i],
                norm_layer=norm_layer, qk_ratio=qk_ratio, sr_ratio=sr_ratios[3], window_size=7)
            for i in range(depths[3])])

        # Representation layer
        if representation_size:
            self.num_features = representation_size
            self.pre_logits = nn.Sequential(OrderedDict([
                ('fc', nn.Linear(self.embed_dim, representation_size)),
                ('act', nn.Tanh())
            ]))
        else:
            self.pre_logits = nn.Identity()

        # Classifier head
        self._fc = nn.Conv2d(embed_dims[-1], fc_dim, kernel_size=1)
        self._bn = nn.BatchNorm2d(fc_dim, eps=1e-5)
        self._swish = MemoryEfficientSwish()
        self._avg_pooling = nn.AdaptiveAvgPool2d(1)
        self._drop = nn.Dropout(dp)
        self.head = nn.Linear(fc_dim, num_classes) if num_classes > 0 else nn.Identity()
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.Conv2d):
            nn.init.kaiming_normal_(m.weight, mode='fan_out')
            if isinstance(m, nn.Conv2d) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.BatchNorm2d):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def update_temperature(self):
        for m in self.modules():
            if isinstance(m, Attention):
                m.update_temperature()

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dims[-1], num_classes) if num_classes > 0 else nn.Identity()

    def forward_features(self, x):
        B = x.shape[0]
        x = self.stem_conv1(x)      # [3, 224, 224] --> [32, 112, 112]
        x = self.stem_relu1(x)
        x = self.stem_norm1(x)

        x = self.stem_conv2(x)      # [32, 112, 112] --> [32, 112, 112]
        x = self.stem_relu2(x)
        x = self.stem_norm2(x)


        x = self.stem_conv3(x)      # [32, 112, 112] --> [32, 112, 112]
        x = self.stem_relu3(x)
        x = self.stem_norm3(x)      # bn

        # x = self.stem_conv1(x)  # [3, 224, 224] --> [32, 112, 112]
        # x = self.stem_norm1(x)
        # x = self.stem_relu1(x)
        #
        #
        # x = self.stem_conv2(x)  # [32, 112, 112] --> [32, 112, 112]
        # x = self.stem_norm2(x)
        # x = self.stem_relu2(x)
        #
        # downsample = x
        # x = self.stem_conv3(x)  # [32, 112, 112] --> [32, 112, 112]
        # x = self.stem_norm3(x)  # bn
        # x = self.eca(x)
        # x = x + downsample
        # x = self.stem_relu3(x)



        x, (H, W) = self.patch_embed_a(x)       # [B, 32, 112, 112] --> [B, 3136, 64]  (H , W) = (56, 56)
        for i, blk in enumerate(self.blocks_a):
            x = blk(x, H, W, self.relative_pos_a)

        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        x, (H, W) = self.patch_embed_b(x)
        for i, blk in enumerate(self.blocks_b):
            x = blk(x, H, W, self.relative_pos_b)

        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        x, (H, W) = self.patch_embed_c(x)
        for i, blk in enumerate(self.blocks_c):
            x = blk(x, H, W, self.relative_pos_c)

        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        x, (H, W) = self.patch_embed_d(x)
        for i, blk in enumerate(self.blocks_d):
            x = blk(x, H, W, self.relative_pos_d)

        B, N, C = x.shape
        x = self._fc(x.permute(0, 2, 1).reshape(B, C, H, W))
        x = self._bn(x)
        x = self._swish(x)
        x = self._avg_pooling(x).flatten(start_dim=1)
        x = self._drop(x)
        x = self.pre_logits(x)
        return x

    def forward(self, x):
        x = self.forward_features(x)
        x = self.head(x)
        return x


def resize_pos_embed(posemb, posemb_new):
    # Rescale the grid of position embeddings when loading from state_dict. Adapted from
    # https://github.com/google-research/vision_transformer/blob/00883dd691c63a6830751563748663526e811cee/vit_jax/checkpoint.py#L224
    _logger.info('Resized position embedding: %s to %s', posemb.shape, posemb_new.shape)
    ntok_new = posemb_new.shape[1]
    if True:
        posemb_tok, posemb_grid = posemb[:, :1], posemb[0, 1:]
        ntok_new -= 1
    else:
        posemb_tok, posemb_grid = posemb[:, :0], posemb[0]
    gs_old = int(math.sqrt(len(posemb_grid)))
    gs_new = int(math.sqrt(ntok_new))
    _logger.info('Position embedding grid-size from %s to %s', gs_old, gs_new)
    posemb_grid = posemb_grid.reshape(1, gs_old, gs_old, -1).permute(0, 3, 1, 2)
    posemb_grid = F.interpolate(posemb_grid, size=(gs_new, gs_new), mode='bilinear')
    posemb_grid = posemb_grid.permute(0, 2, 3, 1).reshape(1, gs_new * gs_new, -1)
    posemb = torch.cat([posemb_tok, posemb_grid], dim=1)
    return posemb


def checkpoint_filter_fn(state_dict, model):
    """ convert patch embedding weight from manual patchify + linear proj to conv"""
    out_dict = {}
    if 'model' in state_dict:
        # For deit models
        state_dict = state_dict['model']
    for k, v in state_dict.items():
        if 'patch_embed.proj.weight' in k and len(v.shape) < 4:
            # For old models that I trained prior to conv based patchification
            O, I, H, W = model.patch_embed.proj.weight.shape
            v = v.reshape(O, -1, H, W)
        elif k == 'pos_embed' and v.shape != model.pos_embed.shape:
            # To resize pos embedding when using model at different size from pretrained weights
            v = resize_pos_embed(v, model.pos_embed)
        out_dict[k] = v
    return out_dict


def _create_model(pretrained=False, distilled=False, **kwargs):
    default_cfg = _cfg()
    default_num_classes = default_cfg['num_classes']
    default_img_size = default_cfg['input_size'][-1]

    num_classes = kwargs.pop('num_classes', default_num_classes)
    print("num_classes:", num_classes)
    img_size = kwargs.pop('img_size', default_img_size)
    repr_size = kwargs.pop('representation_size', None)
    if repr_size is not None and num_classes != default_num_classes:
        # Remove representation layer if fine-tuning. This may not always be the desired action,
        # but I feel better than doing nothing by default for fine-tuning. Perhaps a better interface?
        _logger.warning("Removing representation layer for fine-tuning.")
        repr_size = None

    model = CoorLGNet(img_size=img_size, num_classes=num_classes, representation_size=repr_size, **kwargs)
    model.default_cfg = default_cfg

    if pretrained:
        load_pretrained(
            model, num_classes=num_classes, in_chans=kwargs.get('in_chans', 3),
            filter_fn=partial(checkpoint_filter_fn, model=model))
    return model





@register_model
def coorlgnet(pretrained=False, **kwargs):

    print("Constructing CoorLGNet......")
    model_kwargs = dict(
        qkv_bias=True, embed_dims=[64, 128, 256, 512], stem_channel=32, num_heads=[1, 2, 4, 8],
        depths=[3, 3, 16, 3], mlp_ratios=[4, 4, 4, 4], qk_ratio=1, sr_ratios=[8, 4, 2, 1], **kwargs)
    model = _create_model(pretrained=pretrained, **model_kwargs)
    return model


