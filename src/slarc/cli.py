# ABOUTME: Typer entrypoint for the slarc command line.
# ABOUTME: Each command takes its adapters from bootstrap and calls the service layer.
from typing import Annotated, NoReturn

import typer
from rich.console import Console

from slarc import __version__, bootstrap
from slarc.ports import SlarcError
from slarc.service.auth import AuthStatus, auth_status, store_pasted

app = typer.Typer(
    help="Archive the Slack conversations you can see into searchable, re-renderable files.",
    no_args_is_help=True,
)

out = Console(highlight=False, soft_wrap=True)
err = Console(stderr=True, highlight=False, soft_wrap=True)

NO_SESSIONS = (
    "No Slack sessions found. Sign into the Slack desktop app or run `slarc auth --paste`."
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"slarc {__version__}")
        raise typer.Exit()


def _fail(message: str) -> NoReturn:
    err.print(message, style="red", markup=False)
    raise typer.Exit(1)


def _warn(messages: list[str]) -> None:
    for message in messages:
        err.print(f"Warning: {message}", style="yellow", markup=False)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    pass


def _status_line(status: AuthStatus) -> tuple[str, str | None]:
    if status.domain and status.team_id:
        workspace = f"{status.domain} ({status.team_id})"
    else:
        workspace = status.domain or status.team_id or "any workspace"
    if status.error is not None:
        return f"{workspace}, source {status.source}: {status.error}", "red"
    if status.shadowed_by is not None:
        reason = f"not used while the {status.shadowed_by} session serves every workspace."
        return f"{workspace}, source {status.source}: {reason}", "yellow"
    who = f"user {status.user} ({status.user_id}) on team {status.team}"
    return f"{workspace}: {who}, source {status.source}", None


@app.command()
def auth(
    workspace: Annotated[
        str | None, typer.Argument(help="Workspace domain or team id to check.")
    ] = None,
    paste: Annotated[
        bool,
        typer.Option("--paste", help="Store a browser token and d cookie in the OS keychain."),
    ] = False,
) -> None:
    """List the Slack sessions found and who you are in each workspace."""
    services = bootstrap.current_services()
    if paste:
        xoxc = typer.prompt("Token (xoxc-...)", hide_input=True)
        xoxd = typer.prompt("d cookie (xoxd-...)", hide_input=True)
        try:
            session = store_pasted(services.keychain, xoxc, xoxd, services.gateway_factory)
        except SlarcError as error:
            _fail(str(error))
        out.print(
            f"Stored the token and cookie for {session.domain} ({session.team_id}) "
            "in the OS keychain.",
            markup=False,
        )
        return
    try:
        statuses = auth_status(services.connector(), workspace)
    except SlarcError as error:
        _warn([str(e) for e in services.credentials.errors])
        _fail(str(error))
    if not any(s.error is None and s.shadowed_by is None for s in statuses):
        _warn([str(e) for e in services.credentials.errors])
    if not statuses:
        _fail(NO_SESSIONS)
    for status in statuses:
        line, style = _status_line(status)
        out.print(line, style=style, markup=False)
    if any(s.error is not None for s in statuses):
        raise typer.Exit(1)
