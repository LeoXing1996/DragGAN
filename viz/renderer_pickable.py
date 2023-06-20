# Copyright (c) 2021, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

"""This is a pickable version of Renderer
"""
from ipdb import iex

from socket import has_dualstack_ipv6
import sys
import copy
import traceback
import math
import numpy as np
import torch
import torch.fft
import torch.nn as nn
import torch.nn.functional as F
import matplotlib.cm
import dnnlib
from torch_utils.ops import upfirdn2d
import legacy # pylint: disable=import-error

#----------------------------------------------------------------------------

class CapturedException(Exception):
    def __init__(self, msg=None):
        if msg is None:
            _type, value, _traceback = sys.exc_info()
            assert value is not None
            if isinstance(value, CapturedException):
                msg = str(value)
            else:
                msg = traceback.format_exc()
        assert isinstance(msg, str)
        super().__init__(msg)

#----------------------------------------------------------------------------

class CaptureSuccess(Exception):
    def __init__(self, out):
        super().__init__()
        self.out = out

#----------------------------------------------------------------------------


class Renderer:

    PRETRAINED_MODELS = {
        "afhqwild": {
            "url": "https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/afhqwild.pkl",
            # "features_extractor_layer": feature_extractor_default_callback,
            "features_extractor_size": 256,
        },
        "afhqcat": {
            "url": "https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/afhqcat.pkl",
            # "features_extractor_layer": feature_extractor_default_callback,
            "features_extractor_size": 256,
        },
        "afhqdog": {
            "url": "https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/afhqdog.pkl",
            # "features_extractor_layer": feature_extractor_default_callback,
            "features_extractor_size": 256,
        },
        "brecahad": {
            "url": "https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/brecahad.pkl",
            # "features_extractor_layer": feature_extractor_default_callback,
            "features_extractor_size": 256,
        },
        "cifar10": {
            "url": "https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/cifar10.pkl",
            # "features_extractor_layer": feature_extractor_default_callback,
            "features_extractor_size": 16,
        },
        "ffhq": {
            "url": "https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/ffhq.pkl",
            # "features_extractor_layer": feature_extractor_default_callback,
            "features_extractor_size": 256,
        },
        "metfaces": {
            "url": "https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/metfaces.pkl",
            # "features_extractor_layer": feature_extractor_default_callback,
            "features_extractor_size": 256,
        },
    }

    def __init__(self):
        self._device        = torch.device('cuda')
        self._pkl_data      = dict()    # {pkl: dict | CapturedException, ...}
        self._networks      = dict()    # {cache_key: torch.nn.Module, ...}
        self._pinned_bufs   = dict()    # {(shape, dtype): torch.Tensor, ...}
        self._cmaps         = dict()    # {name: torch.Tensor, ...}
        # self._is_timing     = False
        # self._start_event   = torch.cuda.Event(enable_timing=True)
        # self._end_event     = torch.cuda.Event(enable_timing=True)
        self._net_layers    = dict()    # {cache_key: [dnnlib.EasyDict, ...], ...}
        self._is_old        = False

    def render(self, **args):
        self._is_timing = True
        # self._start_event.record(torch.cuda.current_stream(self._device))
        res = dnnlib.EasyDict()
        try:
            init_net = False
            if not hasattr(self, 'G'):
                init_net = True
            if hasattr(self, 'pkl'):
                if self.pkl != args['pkl']:
                    init_net = True
            if hasattr(self, 'w0_seed'):
                if self.w0_seed != args['w0_seed']:
                    init_net = True
            if hasattr(self, 'w_plus'):
                if self.w_plus != args['w_plus']:
                    init_net = True
            if args['reset_w']:
                init_net = True
            res.init_net = init_net
            if init_net:
                self.init_network(res, **args)
            self._render_drag_impl(res, **args)
        except:
            res.error = CapturedException()
        # self._end_event.record(torch.cuda.current_stream(self._device))
        if 'image' in res:
            res.image = self.to_cpu(res.image).detach().numpy()
        if 'image_pts' in res:
            res.image_pts = self.to_cpu(res.image_pts).detach().numpy()
        if 'stats' in res:
            res.stats = self.to_cpu(res.stats).detach().numpy()
        if 'error' in res:
            res.error = str(res.error)
        # if 'stop' in res and res.stop:

        # if self._is_timing:
        #     self._end_event.synchronize()
        #     # res.render_time = self._start_event.elapsed_time(self._end_event) * 1e-3
        #     self._is_timing = False
        return res

    def get_network(self, pkl, key, **tweak_kwargs):
        data = self._pkl_data.get(pkl, None)
        self._is_old = True if 'stylegan2-old' in pkl else False
        if data is None:
            print(f'Loading "{pkl}"... ', end='', flush=True)
            try:
                if 'stylegan2-old' in pkl:
                    ckpt = torch.load(pkl)
                    from stylegan2 import Generator
                    net = Generator(512, 512, 8, channel_multiplier=2)
                    net.load_state_dict(ckpt['g_ema'], strict=False)
                    data = {'G_ema': net}
                else:
                    with dnnlib.util.open_url(pkl, verbose=False) as f:
                        data = legacy.load_network_pkl(f)
                print('Done.')
            except:
                data = CapturedException()
                print('Failed!')
            self._pkl_data[pkl] = data
            self._ignore_timing()
        if isinstance(data, CapturedException):
            raise data

        orig_net = data[key]
        cache_key = (orig_net, self._device, tuple(sorted(tweak_kwargs.items())))
        net = self._networks.get(cache_key, None)
        if net is None:
            try:
                if 'stylegan2' in pkl:
                    from training.networks_stylegan2 import Generator
                elif 'stylegan3' in pkl:
                    from training.networks_stylegan3 import Generator
                elif 'stylegan_human' in pkl:
                    from stylegan_human.training_scripts.sg2.training.networks import Generator
                else:
                    raise NameError('Cannot infer model type from pkl name!')
                if 'stylegan2-old' in pkl:
                    # TODO: input image resolution according to pkl
                    net = data[key]
                    with torch.no_grad():
                        self.mean_latent = net.mean_latent(4096).to(self._device)
                else:
                    print(data[key].init_args)
                    print(data[key].init_kwargs)
                    if 'stylegan_human' in pkl:
                        net = Generator(*data[key].init_args, **data[key].init_kwargs, square=False, padding=True)
                    else:
                        net = Generator(*data[key].init_args, **data[key].init_kwargs)
                    net.load_state_dict(data[key].state_dict())
                net.to(self._device)
            except:
                net = CapturedException()
            self._networks[cache_key] = net
            self._ignore_timing()
        if isinstance(net, CapturedException):
            raise net
        return net

    def _get_pinned_buf(self, ref):
        key = (tuple(ref.shape), ref.dtype)
        buf = self._pinned_bufs.get(key, None)
        if buf is None:
            buf = torch.empty(ref.shape, dtype=ref.dtype).pin_memory()
            self._pinned_bufs[key] = buf
        return buf

    def to_device(self, buf):
        return self._get_pinned_buf(buf).copy_(buf).to(self._device)

    def to_cpu(self, buf):
        return self._get_pinned_buf(buf).copy_(buf).clone()

    def _ignore_timing(self):
        self._is_timing = False

    def _apply_cmap(self, x, name='viridis'):
        cmap = self._cmaps.get(name, None)
        if cmap is None:
            cmap = matplotlib.cm.get_cmap(name)
            cmap = cmap(np.linspace(0, 1, num=1024), bytes=True)[:, :3]
            cmap = self.to_device(torch.from_numpy(cmap))
            self._cmaps[name] = cmap
        hi = cmap.shape[0] - 1
        x = (x * hi + 0.5).clamp(0, hi).to(torch.int64)
        x = torch.nn.functional.embedding(x, cmap)
        return x

    def init_network(self, res,
        pkl             = None,
        w0_seed         = 0,
        w_plus          = True,
        noise_mode      = 'const',
        trunc_psi       = 0.7,
        trunc_cutoff    = None,
        input_transform = None,
        lr              = 0.001,
        **kwargs
        ):
        # Dig up network details.
        self.pkl = pkl
        G = self.get_network(pkl, 'G_ema')
        self.G = G
        if self._is_old:
            res.img_resolution = G.img_resolution = 512
            res.num_ws = round(math.log(res.img_resolution, 2)) * 2 - 2
            res.has_input_transform = False
        else:
            res.img_resolution = G.img_resolution
            res.num_ws = G.num_ws
            res.has_noise = any('noise_const' in name for name, _buf in G.synthesis.named_buffers())
            res.has_input_transform = (hasattr(G.synthesis, 'input') and hasattr(G.synthesis.input, 'transform'))

        # Set input transform.
        if res.has_input_transform:
            m = np.eye(3)
            try:
                if input_transform is not None:
                    m = np.linalg.inv(np.asarray(input_transform))
            except np.linalg.LinAlgError:
                res.error = CapturedException()
            G.synthesis.input.transform.copy_(torch.from_numpy(m))

        # Generate random latents.
        self.w0_seed = w0_seed
        z = torch.from_numpy(np.random.RandomState(w0_seed).randn(1, 512)).to(self._device).float()

        # Run mapping network.
        if self._is_old:
            w = G.style_forward(z, None)
            w = w.unsqueeze(1).repeat(1, res.num_ws, 1)
        else:
            label = torch.zeros([1, G.c_dim], device=self._device)
            w = G.mapping(z, label, truncation_psi=trunc_psi, truncation_cutoff=trunc_cutoff)

        self.w0 = w.detach().clone()
        self.w_plus = w_plus
        if w_plus:
            self.w = w.detach()
        else:
            self.w = w[:, 0, :].detach()
        self.w.requires_grad = True
        self.w_optim = torch.optim.Adam([self.w], lr=lr)

        self.feat_refs = None
        self.points0_pt = None

    def update_optim_space(self, w_plus):
        if self.w_plus == w_plus:
            print(f'Do not change optimize space, w_plus: {self.w_plus}')
            return

        self.w_plus = w_plus
        print(f'Change optimize space, w_plus: {self.w_plus}')
        w = self.w0.detach().clone()
        if w_plus:
            self.w = w.detach()
        else:
            self.w = w[:, 0, :].detach()
        self.w.requires_grad = True
        lr = self.w_optim.param_groups[0]['lr']
        self.w_optim = torch.optim.Adam([self.w], lr=lr)
        print(f'    Rebuild optimizer with lr: {lr}')

        self.feat_refs = None
        self.points0_pt = None
        print('    Clear feat_refs and points0_pt')

    def update_lr(self, lr):

        del self.w_optim
        self.w_optim = torch.optim.Adam([self.w], lr=lr)
        print(f'Rebuild optimizer with lr: {lr}')
        print('    Remain feat_refs and points0_pt')

    @iex
    def _render_drag_impl(self, res,
        points          = [],
        targets         = [],
        mask            = None,
        lambda_mask     = 10,
        reg             = 0,
        feature_idx     = 5,
        r1              = 3,
        r2              = 12,
        random_seed     = 0,
        noise_mode      = 'const',
        trunc_psi       = 0.7,
        force_fp32      = True,
        layer_name      = None,
        sel_channels    = 3,
        base_channel    = 0,
        img_scale_db    = 0,
        img_normalize   = False,
        untransform     = False,
        is_drag         = False,
        reset           = False,
        to_pil = True,
        **kwargs
    ):
        G = self.G
        ws = self.w
        if ws.dim() == 2:
            ws = ws.unsqueeze(1).repeat(1,6,1)
        ws = torch.cat([ws[:,:6,:], self.w0[:,6:,:]], dim=1)
        if hasattr(self, 'points'):
            if len(points) != len(self.points):
                reset = True
        if reset:
            self.feat_refs = None
            self.points0_pt = None
        self.points = points

        # Run synthesis network.
        if self._is_old:
            img, feat = G([ws], input_is_w=True, truncation_latent=self.mean_latent, truncation=0.7, randomize_noise=False, return_features=True)
        else:
            label = torch.zeros([1, G.c_dim], device=self._device)
            img, feat = G(ws, label, truncation_psi=trunc_psi, noise_mode=noise_mode, input_is_w=True, return_feature=True, force_fp32=True)

        h, w = G.img_resolution, G.img_resolution

        if is_drag:
            X = torch.linspace(0, h, h)
            Y = torch.linspace(0, w, w)
            xx, yy = torch.meshgrid(X, Y)
            # select the target feature map, (5 by default)
            feat_resize = F.interpolate(feat[feature_idx], [h, w], mode='bilinear')
            if self.feat_refs is None:
                self.feat0_resize = F.interpolate(feat[feature_idx].detach(), [h, w], mode='bilinear')
                self.feat_refs = []
                for point in points:
                    py, px = round(point[0]), round(point[1])
                    self.feat_refs.append(self.feat0_resize[:,:,py,px])
                self.points0_pt = torch.Tensor(points).unsqueeze(0).to(self._device) # 1, N, 2

            # Point tracking with feature matching
            with torch.no_grad():
                for j, point in enumerate(points):
                    r = round(r2 / 512 * h)
                    up = max(point[0] - r, 0)
                    down = min(point[0] + r + 1, h)
                    left = max(point[1] - r, 0)
                    right = min(point[1] + r + 1, w)
                    feat_patch = feat_resize[:,:,up:down,left:right]
                    L2 = torch.linalg.norm(feat_patch - self.feat_refs[j].reshape(1,-1,1,1), dim=1)
                    _, idx = torch.min(L2.view(1,-1), -1)
                    width = right - left
                    point = [idx.item() // width + up, idx.item() % width + left]
                    points[j] = point

            res.points = [[point[0], point[1]] for point in points]

            # Motion supervision
            loss_motion = 0
            res.stop = True
            for j, point in enumerate(points):
                direction = torch.Tensor([targets[j][1] - point[1], targets[j][0] - point[0]])
                if torch.linalg.norm(direction) > max(2 / 512 * h, 2):
                    res.stop = False
                if torch.linalg.norm(direction) > 1:
                    distance = ((xx.to(self._device) - point[0])**2 + (yy.to(self._device) - point[1])**2)**0.5
                    relis, reljs = torch.where(distance < round(r1 / 512 * h))
                    direction = direction / (torch.linalg.norm(direction) + 1e-7)
                    gridh = (relis-direction[1]) / (h-1) * 2 - 1
                    gridw = (reljs-direction[0]) / (w-1) * 2 - 1
                    grid = torch.stack([gridw,gridh], dim=-1).unsqueeze(0).unsqueeze(0)
                    target = F.grid_sample(feat_resize.float(), grid, align_corners=True).squeeze(2)
                    loss_motion += F.l1_loss(feat_resize[:,:,relis,reljs], target.detach())

            loss = loss_motion
            if mask is not None:
                if mask.min() == 0 and mask.max() == 1:
                    mask_usq = mask.to(self._device).unsqueeze(0).unsqueeze(0)
                    loss_fix = F.l1_loss(feat_resize * mask_usq, self.feat0_resize * mask_usq)
                    loss += lambda_mask * loss_fix

            loss += reg * F.l1_loss(ws, self.w0)  # latent code regularization
            if not res.stop:
                self.w_optim.zero_grad()
                loss.backward()
                self.w_optim.step()

        # Scale and convert to uint8.
        img = img[0]
        if img_normalize:
            img = img / img.norm(float('inf'), dim=[1,2], keepdim=True).clip(1e-8, 1e8)
        img = img * (10 ** (img_scale_db / 20))
        img = (img * 127.5 + 128).clamp(0, 255).to(torch.uint8).permute(1, 2, 0)
        if to_pil:
            from PIL import Image
            img = img.cpu().numpy()
            img = Image.fromarray(img)
        res.image = img

#----------------------------------------------------------------------------