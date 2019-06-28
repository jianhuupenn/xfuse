from copy import deepcopy

import itertools as it

from typing import Dict, List, Tuple

import numpy as np

import pyro as p
from pyro.distributions import Delta, NegativeBinomial, Normal
from pyro.contrib.autoname import scope

import torch as t

from . import Image
from ...logging import DEBUG, log
from ...utility import center_crop, find_device, sparseonehot


class ST(Image):
    @property
    def tag(self):
        return 'ST'

    def __init__(
            self,
            *args,
            factors: List[Tuple[float, t.Tensor]] = [],
            default_scale: float = 1.,
            **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.__factors: Dict[str, Tuple(float, t.Tensor)] = {}
        self.__factors_counter = it.count()
        for factor in factors:
            self.add_factor(factor)

        self.__default_scale = default_scale

    @property
    def factors(self):
        return deepcopy(self.__factors)

    def add_factor(self, factor=None):
        if factor is None:
            factor = (0., None)
        n = next(self.__factors_counter)
        assert n not in self.__factors
        log(DEBUG, 'adding new factor: %d', n)
        self.__factors.setdefault(n, factor)
        return self

    def remove_factor(self, n):
        log(DEBUG, 'removing factor: %d', n)
        try:
            self.__factors.pop(n)
        except KeyError:
            raise ValueError(
                f'attempted to remove factor {n}, which doesn\'t exist!')
        self.__factors_counter = it.chain([n], self.__factors_counter)

    def _get_scale_decoder(self, in_channels):
        decoder = t.nn.Sequential(
            t.nn.Conv2d(in_channels, in_channels, 3, 1, 1),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(in_channels),
            t.nn.Conv2d(in_channels, 1, 1, 1, 1),
            t.nn.Softplus(),
        )
        t.nn.init.constant_(decoder[-2].weight, 0.)
        t.nn.init.constant_(
            decoder[-2].bias,
            np.log(np.exp(self.__default_scale) - 1),
        )
        return p.module('scale', decoder, update_module_params=True)

    def _get_factor_decoder(self, in_channels, n):
        decoder = t.nn.Sequential(
            t.nn.Conv2d(in_channels, in_channels, 3, 1, 1),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.BatchNorm2d(in_channels),
            t.nn.Conv2d(in_channels, 1, 1, 1, 1),
        )
        t.nn.init.constant_(decoder[-1].weight, 0.)
        t.nn.init.constant_(decoder[-1].bias, self.__factors[n][0])
        return p.module(f'factor{n}', decoder, update_module_params=True)

    def model(self, x, z):
        num_genes = x['data'][0].shape[1]

        decoded = self._decode(z)

        scale = p.sample('scale', Delta(
            center_crop(
                self._get_scale_decoder(decoded.shape[1]).to(decoded)(decoded),
                [None, None, *x['label'].shape[-2:]],
            )
        ))
        rim = t.cat(
            [
                self._get_factor_decoder(decoded.shape[1], n)
                .to(decoded)(decoded)
                for n in self.factors
            ],
            dim=1,
        )
        rim = center_crop(rim, [None, None, *x['label'].shape[-2:]])
        rim = t.nn.functional.softmax(rim, dim=1)
        rim = p.sample('rim', Delta(rim))
        rim = scale * rim

        rmg = p.sample('rmg', Delta(t.stack([
            p.sample(f'factor{n}', (
                Normal(t.tensor(0.).to(z), 1.).expand([num_genes])
            ))
            for n in self.factors
        ])))

        effects = x['effects'].float()
        rgeff = p.sample('rgeff', (
            Normal(t.tensor(0.).to(z), 1)
            .expand([effects.shape[1], num_genes])
        ))
        lgeff = p.sample('lgeff', (
            Normal(t.tensor(0.).to(z), 1)
            .expand([effects.shape[1], num_genes])
        ))

        lg = effects @ lgeff
        rg = effects @ rgeff
        rmg = rg[:, None] + rmg

        with p.poutine.scale(scale=self.n/len(x)):
            with scope(prefix=self.tag):
                image = self._sample_image(x, decoded)

                def _compute_sample_params(label, rim, rmg, lg):
                    labelonehot = sparseonehot(label.flatten())
                    rim = t.sparse.mm(
                        labelonehot.t().float(),
                        rim.permute(1, 2, 0).view(-1, rim.shape[0]),
                    )
                    rgs = t.einsum('im,mg->ig', rim[1:], rmg.exp())
                    return rgs, lg.expand(len(rgs), -1)

                rgs, lg = zip(*it.starmap(
                    _compute_sample_params, zip(x['label'], rim, rmg, lg)))
                expression = p.sample(
                    'xsg',
                    NegativeBinomial(
                        total_count=t.cat(rgs),
                        logits=t.cat(lg),
                    ),
                    obs=t.cat(x['data']),
                )

        return image, expression

    def guide(self, x):
        num_genes = x['data'][0].shape[1]

        for name, dim in [
            ('rgeff', [x['effects'].shape[1], num_genes]),
            ('lgeff', [x['effects'].shape[1], num_genes]),
        ]:
            p.sample(
                name,
                Normal(
                    p.param(
                        f'{name}_mu',
                        t.zeros(dim),
                    ).to(find_device(x)),
                    p.param(
                        f'{name}_sd',
                        1e-2 * t.ones(dim),
                        constraint=t.distributions.constraints.positive,
                    ).to(find_device(x)),
                ),
            )

        for n, (_, factor_default) in self.factors.items():
            if factor_default is None:
                factor_default = t.zeros(num_genes)
            p.sample(
                f'factor{n}',
                Normal(
                    p.param(
                        f'factor{n}_mu',
                        factor_default.float(),
                    ).to(find_device(x)),
                    p.param(
                        f'factor{n}_sd',
                        1e-2 * t.ones_like(factor_default).float(),
                        constraint=t.distributions.constraints.positive,
                    ).to(find_device(x)),
                ),
            )

        image = super().guide(x)

        expression_encoder = p.module(
            'expression_encoder',
            t.nn.Sequential(
                t.nn.Linear(1 + num_genes, 100),
                t.nn.LeakyReLU(0.2, inplace=True),
                t.nn.BatchNorm1d(100),
                t.nn.Linear(100, 100),
            ),
            update_module_params=True,
        ).to(image)

        def encode(data, label):
            missing = t.tensor([1., *[0.] * data.shape[1]]).to(data)
            data_with_missing = t.nn.functional.pad(data, (1, 0, 1, 0))
            data_with_missing[0] = missing
            encoded_data = expression_encoder(data_with_missing)
            labelonehot = sparseonehot(label.flatten(), len(encoded_data))
            expanded = t.sparse.mm(labelonehot.float(), encoded_data)
            return expanded.t().reshape(-1, *label.shape)

        label = (
            t.nn.functional.interpolate(
                x['label'].float().unsqueeze(1),
                image.shape[-2:],
            )
            .squeeze(1)
            .long()
        )
        expression = t.stack([
            encode(data, label) for data, label in zip(x['data'], label)
        ])

        return t.cat([image, expression], dim=1)
