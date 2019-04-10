from functools import reduce

import operator as op

from typing import List, Optional, Tuple

import numpy as np

import torch as t

from .distributions import Distribution, Normal, Variable
from .logging import DEBUG, log
from .utility import center_crop
from .utility.init_args import store_init_args


class Variational(t.nn.Module):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._latents = []

    def _register_latent(
            self,
            variable: Variable,
            prior: Distribution,
            id: str,
            is_global: bool = False,
    ):
        if id in self._latents:
            raise RuntimeError(f'variable {id} has already been registered')

        log(DEBUG, 'registering latent variable %s', id)
        setattr(self, f'{id}', variable)
        setattr(self, f'{id}_p', prior)
        setattr(self, f'{id}_is_global', is_global)
        self._latents.append(id)

    def _get_latent(self, latent_id: int):
        return (
            getattr(self, f'{latent_id}'),
            getattr(self, f'{latent_id}_p'),
            getattr(self, f'{latent_id}_is_global'),
        )

    def complexity_cost(self, batch_fraction):
        return sum([
            t.sum(x.distribution.log_prob(x.value) - p.log_prob(x.value))
            * (batch_fraction if g else 1.)
            for x, p, g in map(self._get_latent, self._latents)
        ])


class Unpool(t.nn.Module):
    def __init__(
            self,
            in_channels,
            out_channels=None,
            kernel_size=3,
            stride=2,
            padding=None,
    ):
        super().__init__()

        if out_channels is None:
            out_channels = in_channels

        if padding is None:
            padding = kernel_size // 2

        self.conv = t.nn.Conv2d(
            in_channels, out_channels, kernel_size, padding=padding)
        self.scale_factor = stride

    def forward(self, x):
        x = t.nn.functional.interpolate(x, scale_factor=self.scale_factor)
        x = self.conv(x)
        return x


@store_init_args
class Histonet(Variational):
    def __init__(
            self,
            num_factors=50,
            latent_size=96,
            nf=16,
    ):
        super().__init__()

        self.encoder = t.nn.Sequential(
            # x1
            t.nn.Conv2d(3, 2 * nf, 4, 2, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(2 * nf),
            # x2
            t.nn.Conv2d(2 * nf, 4 * nf, 4, 2, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(4 * nf),
            # x4
            t.nn.Conv2d(4 * nf, 8 * nf, 4, 2, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(8 * nf),
            # x8
            t.nn.Conv2d(8 * nf, 16 * nf, 4, 2, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(16 * nf),
            # x16
        )

        self.z_mu = t.nn.Sequential(
            t.nn.Conv2d(16 * nf, 16 * nf, 3, 1, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(16 * nf),
            t.nn.Conv2d(16 * nf, latent_size, 3, 1, 1, bias=True),
        )
        self.z_sd = t.nn.Sequential(
            t.nn.Conv2d(16 * nf, 16 * nf, 3, 1, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(16 * nf),
            t.nn.Conv2d(16 * nf, latent_size, 3, 1, 1, bias=True),
        )
        self.z = Variable(Normal())
        self._register_latent(self.z, Normal(), 'z')

        self.decoder = t.nn.Sequential(
            t.nn.Conv2d(latent_size, 16 * nf, 5, padding=4),
            # x16
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(16 * nf),
            Unpool(16 * nf, 8 * nf, 5),
            # x8
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(8 * nf),
            Unpool(8 * nf, 4 * nf, 5),
            # x4
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(4 * nf),
            Unpool(4 * nf, 2 * nf, 5),
            # x2
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(2 * nf),
            Unpool(2 * nf, nf, 5),
            # x1
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(nf),
        )

        self.img_mu = t.nn.Sequential(
            t.nn.Conv2d(nf, nf, 3, 1, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(nf),
            t.nn.Conv2d(nf, 3, 3, 1, 1, bias=True),
            t.nn.Tanh(),
        )
        self.img_sd = t.nn.Sequential(
            t.nn.Conv2d(nf, nf, 3, 1, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(nf),
            t.nn.Conv2d(nf, 3, 3, 1, 1, bias=True),
            t.nn.Softplus(),
        )

        self.mixture_loadings = t.nn.Sequential(
            t.nn.Conv2d(nf, nf, 3, 1, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(nf),
            t.nn.Conv2d(nf, num_factors, 3, 1, 1, bias=True),
        )

    @property
    def init_args(self):
        return self._init_args

    def encode(self, x):
        x = self.encoder(x)
        z_mu = self.z_mu(x)
        z_sd = self.z_sd(x)

        self.z.distribution.set(
            loc=z_mu,
            scale=z_sd,
            r_transform=True,
        )
        z = self.z.sample().value

        return (
            z,
            z_mu,
            z_sd,
        )

    def decode(self, z):
        state = self.decoder(z)

        img_mu = self.img_mu(state)
        img_sd = self.img_sd(state)

        mixture_loadings = self.mixture_loadings(state)

        return (
            t.distributions.Normal(img_mu, img_sd),
            mixture_loadings,
            state,
        )

    def forward(self, x):
        z, z_mu, z_sd = self.encode(x)

        def _crop(y):
            if isinstance(y, t.Tensor):
                return center_crop(y, [None, None, *x.shape[-2:]])
            elif isinstance(y, t.distributions.Distribution):
                return type(y)(**{
                    k: center_crop(v, [None, None, *x.shape[-2:]])
                    for k, v in y.__dict__.items() if k[0] != '_'

                })
            return y

        return (z, *map(_crop, self.decode(z)))


@store_init_args
class STD(Variational):
    def __init__(
            self,
            genes: List[str],
            num_factors: int = 50,
            gene_baseline: Optional[np.ndarray] = None,
            covariates: Optional[List[Tuple[str, List[str]]]] = None,
    ):
        super().__init__()

        self.genes = list(genes)

        def _make_covariate(name, shape, learn_prior):
            mu = t.nn.Parameter(t.zeros(shape))
            sd = t.nn.Parameter(-5 * t.ones(shape))
            self.register_parameter(f'{name}_q_mu', mu)
            self.register_parameter(f'{name}_q_sd', sd)
            q = Normal().set(loc=mu, scale=sd, r_transform=True)
            v = Variable(q)

            p_mu = t.nn.Parameter(t.tensor(0.), requires_grad=learn_prior)
            p_sd = t.nn.Parameter(t.tensor(0.), requires_grad=learn_prior)
            self.register_parameter(f'{name}_p_mu', p_mu)
            self.register_parameter(f'{name}_p_sd', p_sd)
            p = Normal().set(loc=p_mu, scale=p_sd, r_transform=True)

            self._register_latent(v, p, name, True)

        _make_covariate('r', (1, ), True)
        _make_covariate('rg', (len(genes), ), True)
        _make_covariate('rt', (num_factors, ), False)
        _make_covariate('rgt', (len(genes), num_factors), False)

        _make_covariate('l', (1, ), True)
        _make_covariate('lg', (len(genes), ), True)

        if covariates is not None and len(covariates) > 0:
            self._covariates = covariates
            n_fe = reduce(op.add, map(lambda x: len(x[1]), covariates))
            _make_covariate('reff', (n_fe,), True)
            _make_covariate('leff', (n_fe,), True)
            _make_covariate('rgeff', (n_fe, len(genes)), False)
            _make_covariate('lgeff', (n_fe, len(genes)), False)
        else:
            self._covariates = []

        if gene_baseline is not None:
            if len(gene_baseline) != len(genes):
                raise ValueError(
                    'size of `gene_baseline` does not match `genes`'
                    f' ({gene_baseline.shape[1]} vs. {len(genes)})'
                )
            lgb = t.tensor(np.log(gene_baseline)).float()
            lgb_mean = lgb.mean()[None]
            self.r_q_mu.data = lgb_mean
            self.r_p_mu.data = lgb_mean
            self.rg_q_mu.data = lgb - lgb_mean
            self.rg_p_mu.data = lgb - lgb_mean

    @property
    def rate_gt(self):
        return t.exp(
            self.r.value
            + self.rg.value[..., None]
            + self.rt.value[None, ...]
            + self.rgt.value
        )

    @property
    def logit(self):
        return self.l.value + self.lg.value

    def resample(self):
        for v, *_ in map(self._get_latent, self._latents):
            v.sample()
        return self

    def forward(self, x, effects=None):
        self.resample()
        rate = x @ self.rate_gt.t()
        logit = self.logit[None]
        if effects is not None:
            effects = effects.float()
            rate = rate * t.exp(
                effects @ (self.reff.value[..., None] + self.rgeff.value))
            logit = logit + (
                effects @ (self.leff.value[..., None] + self.lgeff.value))
        return t.distributions.NegativeBinomial(rate, logits=logit)
