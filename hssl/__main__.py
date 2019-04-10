# pylint: disable=missing-docstring, invalid-name, too-many-instance-attributes

from datetime import datetime as dt

from functools import wraps

import itertools as it

import os

import sys

import click

import numpy as np

import pandas as pd

from pyvips import Image

import torch as t

from . import __version__
from .analyze import (
    analyze as default_analysis,
    analyze_gene_profiles,
    analyze_genes,
    impute_counts,
)
from .dataset import Dataset, RandomSlide, spot_size
from .logging import (
    DEBUG,
    ERROR,
    INFO,
    WARNING,
    LoggedExecution,
    log,
    set_level,
)
from .network import Histonet, STD
from .optimizer import create_optimizer
from .train import train as _train
from .utility import (
    design_matrix_from,
    lazify,
    read_data,
    set_rng_seed,
)
from .utility.state import (
    State,
    load_state,
    save_state,
    to_device,
)


DEVICE = t.device('cuda' if t.cuda.is_available() else 'cpu')


def _logged_command(get_output_dir):
    def _decorator(f):
        @wraps(f)
        def _wrapper(*args, **kwargs):
            output_dir = get_output_dir(*args, **kwargs)

            if os.path.exists(output_dir):
                log(ERROR, 'output directory %s already exists', output_dir)
                sys.exit(1)

            os.makedirs(output_dir)

            with LoggedExecution(os.path.join(output_dir, 'log')):
                log(INFO, 'this is %s %s', __package__, __version__)
                log(DEBUG, 'invoked by %s', ' '.join(sys.argv))
                log(INFO, 'device: %s', str(DEVICE))

                f(*args, **kwargs)
        return _wrapper
    return _decorator


@click.group()
@click.option('-v', '--verbose', is_flag=True)
@click.version_option()
def cli(verbose):
    if verbose:
        set_level(DEBUG)
    else:
        set_level(INFO)


@click.command()
@click.argument('design-file', type=click.File('rb'))
@click.option('--factors', type=int, default=50)
@click.option('--patch-size', type=int, default=512)
@click.option('--lr', type=float, default=1e-3)
@click.option('--batch-size', type=int, default=8)
@click.option('--workers', type=int)
@click.option('--seed', type=int)
@click.option(
    '--restore',
    'state_file',
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
)
@click.option(
    '-o', '--output',
    type=click.Path(resolve_path=True),
    default=f'{__package__}-{dt.now().isoformat()}',
)
@click.option('--checkpoint', 'chkpt_interval', type=int)
@click.option('--image', 'image_interval', type=int, default=1000)
@click.option('--epochs', type=int)
@_logged_command(lambda *_, **args: args['output'])
def train(
        design_file,
        factors,
        patch_size,
        lr,
        output,
        state_file,
        workers,
        seed,
        **kwargs,
):
    if seed is not None:
        set_rng_seed(seed)

        if workers is None:
            log(WARNING,
                'setting workers to 0 to avoid race conditions '
                '(set --workers explicitly to override)')
            workers = 0

    design = pd.read_csv(design_file)
    design_dir = os.path.dirname(design_file.name)

    def _path(p):
        return (
            p
            if os.path.isabs(p) else
            os.path.join(design_dir, p)
        )

    count_data = read_data(map(_path, design.data))

    design_matrix = design_matrix_from(design[[
        x for x in design.columns
        if x not in ['image', 'labels', 'validation', 'data']
    ]])

    dataset = Dataset(
        [
            RandomSlide(
                data=counts,
                image=Image.new_from_file(_path(image)),
                label=Image.new_from_file(_path(labels)),
                patch_size=patch_size,
            )
            for image, labels, counts in zip(
                design.image,
                design.labels,
                (count_data.loc[x] for x in count_data.index.levels[0]),
            )
        ],
        design_matrix,
    )
    try:
        dataset_validation = Dataset(
            [
                RandomSlide(
                    data=counts,
                    image=Image.new_from_file(_path(image)),
                    label=Image.new_from_file(_path(labels)),
                    patch_size=patch_size,
                )
                for image, labels, counts in zip(
                    design.image,
                    design.validation,
                    (count_data.loc[x] for x in count_data.index.levels[0]),
                )
            ],
            design_matrix,
        )
    except AttributeError:
        dataset_validation = None

    state: State
    if state_file is not None:
        state = load_state(state_file)
    else:
        histonet = Histonet(
            num_factors=factors,
        )
        t.nn.init.normal_(
            histonet.mixture_loadings[-1].weight,
            std=1e-5,
        )
        t.nn.init.normal_(
            histonet.mixture_loadings[-1].bias,
            mean=-np.log(spot_size(dataset) * factors),
            std=1e-5,
        )

        std = STD(
            genes=count_data.columns,
            num_factors=factors,
            covariates=[
                (
                    design_matrix.index.levels[0][k],
                    [v for _, v in vs],
                )
                for k, vs in it.groupby(
                    enumerate(design_matrix.index.levels[1]),
                    lambda x: design_matrix.index.codes[0][x[0]],
                )
            ],
            gene_baseline=count_data.mean(0).values,
        )

        state = State(
            histonet=histonet,
            std=std,
            optimizer=create_optimizer(
                histonet,
                std,
                learning_rate=lr,
            ),
            epoch=0,
        )

    state = _train(
        state=state,
        output_prefix=output,
        dataset=dataset,
        dataset_validation=dataset_validation,
        workers=workers,
        **kwargs,
    )

    save_state(state, os.path.join(output, 'final-state.pkl'))


cli.add_command(train)


@click.group(chain=True)
@click.argument(
    'state-file',
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
)
@click.option(
    '--image',
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
)
@click.option(
    '--label',
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
)
@click.option(
    '-o', '--output',
    type=click.Path(resolve_path=True),
    default=f'{__package__}-{dt.now().isoformat()}',
)
def analyze(**_):
    pass


@analyze.resultcallback()
@_logged_command(lambda *_, **args: args['output'])
def _run_analysis(analyses, state_file, image, label, output):
    state = load_state(state_file)
    to_device(state, DEVICE)

    @lazify
    def _image():
        if image is None:
            raise ValueError('no image has been provided')
        return Image.new_from_file(image)

    @lazify
    def _label():
        if label is None:
            raise ValueError('no labels have been provided')
        return Image.new_from_file(label)

    for name, analysis in analyses:
        log(INFO, 'performing analysis: %s', name)
        analysis(
            state=state,
            image_provider=_image,
            label_provider=_label,
            output=output,
        )


cli.add_command(analyze)


@click.command()
@click.argument('gene-list', nargs=-1)
def genes(gene_list):
    def _analysis(state, image_provider, label_provider, output, **_):
        try:
            labels = label_provider()
        except ValueError:
            labels = None
        analyze_genes(
            state.histonet,
            state.std,
            image_provider(),
            labels,
            gene_list,
            output_prefix=output,
            device=DEVICE,
        )
    return 'gene list', _analysis


analyze.add_command(genes)


@click.command()
@click.argument('gene-list', nargs=-1)
@click.option('--factor', type=int, multiple=True)
@click.option('--truncate', type=int, default=25)
@click.option('--regex/--no-regex', default=True)
def gene_profiles(gene_list, factor, truncate, regex):
    def _analysis(state, output, **_):
        analyze_gene_profiles(
            std=state.std,
            genes=gene_list,
            factors=factor,
            truncate=truncate,
            regex=regex,
            output_prefix=output,
        )
    return 'gene profiles', _analysis


analyze.add_command(gene_profiles)


@click.command()
def default():
    def _analysis(state, image_provider, label_provider, output):
        try:
            labels = label_provider()
        except ValueError:
            labels = None
        default_analysis(
            state.histonet,
            state.std,
            image_provider(),
            labels,
            output_prefix=output,
            device=DEVICE,
        )
    return 'default', _analysis


analyze.add_command(default)


@click.command()
@click.argument('data-file', type=click.File('rb'))
def impute(data_file):
    data = pd.read_csv(data_file)
    data_dir = os.path.dirname(data_file.name)

    def _analysis(state, output, **_):
        output_dir = os.path.join(output, 'imputed-counts')
        os.makedirs(output_dir)

        design_matrix = design_matrix_from(
            data[[
                x for x in data.columns
                if x not in ['name', 'image', 'labels']
            ]],
            covariates=state.std._covariates,
        )

        for name, image, label, effects in zip(
                data.name,
                data.image,
                data.labels,
                t.tensor(np.transpose(design_matrix.values)),
        ):
            log(INFO, 'imputing counts for %s', name)
            d, labels = impute_counts(
                state.histonet,
                state.std,
                Image.new_from_file(os.path.join(data_dir, image)),
                Image.new_from_file(os.path.join(data_dir, label)),
                effects,
                device=DEVICE,
            )
            result = pd.DataFrame(
                d.mean.detach().cpu().numpy(),
                index=pd.Index(labels, name='n'),
                columns=state.std.genes,
            )
            result.to_csv(os.path.join(output_dir, f'{name}.csv.gz'))

    return 'imputation', _analysis


analyze.add_command(impute)


if __name__ == '__main__':
    cli()
