# ABOUTME: Typer entrypoint for the slarc command line.
# ABOUTME: Each command composes real adapters and calls the service layer.
from typing import Annotated

import typer

from slarc import __version__

app = typer.Typer(
    help="Archive the Slack conversations you can see into searchable, re-renderable files.",
    no_args_is_help=True,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"slarc {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    pass
