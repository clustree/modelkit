import json
import logging
import os
import sys
from time import perf_counter, sleep
import multiprocessing
import click
import humanize
from rich.console import Console
from rich.progress import Progress, track
from rich.table import Table
from rich.tree import Tree

from modelkit import ModelLibrary
from modelkit.api import create_modelkit_app
from modelkit.assets.cli import assets_cli
from modelkit.core.errors import ModelsNotFound
from modelkit.core.library import download_assets
from modelkit.core.model_configuration import list_assets
from modelkit.utils.serialization import safe_np_dump


@click.group()
def modelkit_cli():
    sys.path.append(os.getcwd())
    pass


modelkit_cli.add_command(assets_cli)


def _configure_from_cli_arguments(models, required_models, settings):
    models = list(models) or None
    required_models = list(required_models) or None
    if not (models or os.environ.get("MODELKIT_DEFAULT_PACKAGE")):
        raise ModelsNotFound(
            "Please add `your_package` as argument or set the "
            "`MODELKIT_DEFAULT_PACKAGE=your_package` env variable."
        )

    service = ModelLibrary(
        models=models,
        required_models=required_models,
        settings=settings,
    )
    return service


@modelkit_cli.command()
@click.argument("models", type=str, nargs=-1, required=False)
@click.option("--required-models", "-r", multiple=True)
def memory(models, required_models):
    """
    Show memory consumption of modelkit models.
    """
    from memory_profiler import memory_usage

    def _load_model(m, service):
        service._load(m)
        sleep(1)

    service = _configure_from_cli_arguments(
        models, required_models, {"lazy_loading": True}
    )
    grand_total = 0
    stats = {}
    logging.getLogger().setLevel(logging.ERROR)
    if service.required_models:
        with Progress(transient=True) as progress:
            task = progress.add_task("Profiling memory...", total=len(required_models))
            for m in service.required_models:
                deps = service.configuration[m].model_dependencies
                deps = deps.values() if isinstance(deps, dict) else deps
                for dependency in list(deps) + [m]:
                    mu = memory_usage((_load_model, (dependency, service), {}))
                    stats[dependency] = mu[-1] - mu[0]
                    grand_total += mu[-1] - mu[0]
                progress.update(task, advance=1)

    console = Console()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Model")
    table.add_column("Memory", style="dim")

    for k, (m, mc) in enumerate(stats.items()):
        table.add_row(
            m,
            humanize.naturalsize(mc * 10 ** 6, format="%.2f"),
            end_section=k == len(stats) - 1,
        )
    table.add_row("Total", humanize.naturalsize(grand_total * 10 ** 6, format="%.2f"))
    console.print(table)


@modelkit_cli.command("list-assets")
@click.argument("models", type=str, nargs=-1, required=False)
@click.option("--required-models", "-r", multiple=True)
def list_assets_cli(models, required_models):
    """
    List necessary assets.

    List the assets necessary to run a given set of models.
    """
    service = _configure_from_cli_arguments(
        models, required_models, {"lazy_loading": True}
    )

    console = Console()
    if service.configuration:
        for m in service.required_models:
            assets_specs = list_assets(
                configuration=service.configuration, required_models=[m]
            )
            model_tree = Tree(f"[bold]{m}[/bold] ({len(assets_specs)} assets)")
            if assets_specs:
                for asset_spec_string in assets_specs:
                    model_tree.add(asset_spec_string.replace("[", "\["), style="dim")
            console.print(model_tree)


def add_dependencies_to_graph(g, model, configurations):
    g.add_node(
        model,
        type="model",
        fillcolor="/accent3/2",
        style="filled",
        shape="box",
    )
    model_configuration = configurations[model]
    if model_configuration.asset:
        g.add_node(
            model_configuration.asset,
            type="asset",
            fillcolor="/accent3/3",
            style="filled",
        )
        g.add_edge(model, model_configuration.asset)
    for dependent_model in model_configuration.model_dependencies:
        g.add_edge(model, dependent_model)
        add_dependencies_to_graph(g, dependent_model, configurations)


@modelkit_cli.command()
@click.argument("models", type=str, nargs=-1, required=False)
@click.option("--required-models", "-r", multiple=True)
def dependencies_graph(models, required_models):
    import networkx as nx
    from networkx.drawing.nx_agraph import write_dot

    """
    Create a  dependency graph for a library.

    Create a DOT file with the assets and model dependency graph
    from a list of models.
    """
    service = _configure_from_cli_arguments(
        models, required_models, {"lazy_loading": True}
    )
    if service.configuration:
        dependency_graph = nx.DiGraph(overlap="False")
        for m in service.required_models:
            add_dependencies_to_graph(dependency_graph, m, service.configuration)
        write_dot(dependency_graph, "dependencies.dot")


@modelkit_cli.command()
@click.argument("models", type=str, nargs=-1, required=False)
@click.option("--required-models", "-r", multiple=True)
def describe(models, required_models):
    """
    Describe a library.

    Show settings, models and assets for a given library.
    """
    service = _configure_from_cli_arguments(models, required_models, {})
    service.describe()


@modelkit_cli.command()
@click.argument("model")
@click.argument("example")
@click.argument("models", type=str, nargs=-1, required=False)
@click.option("--n", "-n", default=100)
def time(model, example, models, n):
    """
    Benchmark a model on an example.

    Time n iterations of a model's call on an example.
    """
    service = _configure_from_cli_arguments(models, [model], {"lazy_loading": True})

    console = Console()

    t0 = perf_counter()
    model = service.get(model)
    console.print(
        f"{f'Loaded model `{model.configuration_key}` in':50} "
        f"... {f'{perf_counter()-t0:.2f} s':>10}"
    )

    example_deserialized = json.loads(example)
    console.print(f"Calling `predict` {n} times on example:")
    console.print(f"{json.dumps(example_deserialized, indent = 2)}")

    times = []
    for _ in track(range(n)):
        t0 = perf_counter()
        model(example_deserialized)
        times.append(perf_counter() - t0)

    console.print(
        f"Finished in {sum(times):.1f} s, "
        f"approximately {sum(times)/n*1e3:.2f} ms per call"
    )

    t0 = perf_counter()
    model([example_deserialized] * n)
    batch_time = perf_counter() - t0
    console.print(
        f"Finished batching in {batch_time:.1f} s, approximately"
        f" {batch_time/n*1e3:.2f} ms per call"
    )


@modelkit_cli.command("serve")
@click.argument("models", type=str, nargs=-1, required=False)
@click.option("--required-models", "-r", type=str, multiple=True)
@click.option("--host", type=str, default="localhost")
@click.option("--port", type=int, default=8000)
def serve(models, required_models, host, port):
    import uvicorn

    """
    Run a library as a service.

    Run an HTTP server with specified models using FastAPI
    """
    app = create_modelkit_app(models=models, required_models=required_models)
    uvicorn.run(app, host=host, port=port)


@modelkit_cli.command("predict")
@click.argument("model_name", type=str)
@click.argument("models", type=str, nargs=-1, required=False)
def predict(model_name, models):
    """
    Make predictions for a given model.
    """
    lib = _configure_from_cli_arguments(models, [model_name], {})
    model = lib.get(model_name)
    while True:
        r = click.prompt(f"[{model_name}]>")
        if r:
            res = model(json.loads(r))
            click.secho(json.dumps(res, indent=2, default=safe_np_dump))


@modelkit_cli.command("batch")
@click.argument("model_name", type=str)
@click.argument("input", type=str)
@click.argument("output", type=str)
@click.option("--models", type=str, multiple=True)
@click.option("--processes", type=int, default=4)
def batch_predict(model_name, input, output, models, processes):
    """
    Make predictions for a given model.
    """
    lib = _configure_from_cli_arguments(models, [model_name], {})
    model = lib.get(model_name)

    manager = multiprocessing.Manager()
    q = manager.Queue()
    q_in = manager.Queue()

    def worker(q_in, q):
        while True:
            item = q_in.pop()
            res = model.predict(item)
            q.put(res)

    def writer(q):
        with open(output, 'w') as f:
            while True:
                m = q.get()
                f.write(json.dumps(m) + '\n')
                f.flush()

    def reader(q_in):
        with open(input) as f:
            for l in f:
                item = json.loads(json.loads(l.strip()))
                q_in.put(item)

    with multiprocessing.Pool(processes) as p:
        r = p.apply_async(writer, (q,))
        p.apply_async(worker, (q_in, q))
        p.apply_async(reader, (q_in,))
        r.wait()


@modelkit_cli.command("tf-serving")
@click.argument("mode", type=click.Choice(["local-docker", "local-process", "remote"]))
@click.argument("models", type=str, nargs=-1, required=False)
@click.option("--required-models", "-r", multiple=True)
@click.option("--verbose", is_flag=True)
def tf_serving(mode, models, required_models, verbose):
    from modelkit.utils.tensorflow import deploy_tf_models

    service = _configure_from_cli_arguments(
        models, required_models, {"lazy_loading": True}
    )

    deploy_tf_models(service, mode, verbose=verbose)


@modelkit_cli.command("download-assets")
@click.argument("models", type=str, nargs=-1, required=False)
@click.option("--required-models", "-r", multiple=True)
def download(models, required_models):
    """
    Download all assets necessary to run a given set of models
    """
    download_assets(
        models=list(models) or None, required_models=list(required_models) or None
    )
