# ABOUTME: Typer entrypoint for the slarc command line.
# ABOUTME: Each command takes its adapters from bootstrap and calls the service layer.
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskID, TextColumn
from rich.table import Table
from rich.text import Text

from slarc import __version__, bootstrap
from slarc.config import (
    ARCHIVE_FILE,
    create_archive,
    find_archive,
    load_settings,
    read_global_config,
)
from slarc.domain.links import parse_selector
from slarc.ports import NoArchive, NoSession, SlarcError
from slarc.service.auth import AuthStatus, auth_status, store_pasted
from slarc.service.pull import PullOptions, PullReport, pull
from slarc.unit_of_work import UnitOfWork

app = typer.Typer(
    help="Archive the Slack conversations you can see into searchable, re-renderable files.",
    no_args_is_help=True,
)

out = Console(highlight=False, soft_wrap=True)
err = Console(stderr=True, highlight=False, soft_wrap=True)

NO_SESSIONS = (
    "No Slack sessions found. Sign into the Slack desktop app or run `slarc auth --paste`."
)
RANGE_NEEDS_SELECTORS = "--since and --until apply only to links named on the command line."
INTERRUPTED = "Interrupted; progress so far is saved."
INTERRUPTED_EXIT = 130
MB = 1024 * 1024


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


class RichReporter:
    """Shows a progress bar per item on stderr, with warnings printed above the bars.

    The bars start with the first item, so a run that fails before then prints only its error.
    """

    def __init__(self, console: Console) -> None:
        self._progress = Progress(
            TextColumn("{task.description}", markup=False),
            BarColumn(),
            TextColumn("{task.completed} messages"),
            console=console,
        )
        self._tasks: dict[str, TaskID] = {}

    def __enter__(self) -> RichReporter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._tasks:
            self._progress.stop()

    def info(self, msg: str) -> None:
        self._progress.console.print(msg, markup=False)

    def warn(self, msg: str) -> None:
        self._progress.console.print(f"Warning: {msg}", style="yellow", markup=False)

    def progress(self, label: str, done: int, total: int | None) -> None:
        if label not in self._tasks:
            if not self._tasks:
                self._progress.start()
            self._tasks[label] = self._progress.add_task(label, total=total)
        self._progress.update(self._tasks[label], completed=done, total=total)


def _check_date(flag: str, value: str | None) -> None:
    if value is None:
        return
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        _fail(f"{flag} must be a date like 2026-10-19.")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _summary(report: PullReport) -> str:
    return (
        f"Pulled {_plural(report.items_pulled, 'item')}: "
        f"{_plural(report.lines_appended, 'new line')}, "
        f"{_plural(report.downloads_pending, 'download')} pending."
    )


def _archive_for_pull(
    root: Path | None, env: Mapping[str, str], global_config: Mapping[str, Any], yes: bool
) -> Path:
    """The archive to pull into, created after confirmation when it has no slarc.toml.

    With no archive found, the archive goes in the current directory.
    """
    found = find_archive(Path.cwd(), root, env, global_config)
    if found is not None and (found / ARCHIVE_FILE).is_file():
        return found
    if found is None:
        target, question = Path.cwd(), f"No archive here. Create one in {Path.cwd()}?"
    else:
        target, question = found, f"No archive at {found}. Create one?"
    if not yes and not typer.confirm(question, default=True):
        _fail("No archive created.")
    create_archive(target)
    return target


def _run_args(
    selectors: list[str], since: str | None, until: str | None, flags: dict[str, object]
) -> list[str]:
    """The pull's command line as recorded in its run line, without machine-specific paths."""
    args = list(selectors)
    for flag, value in (("--since", since), ("--until", until)):
        if value is not None:
            args += [flag, value]
    for flag, value in flags.items():
        if value is True:
            args.append(flag)
        elif value not in (None, False):
            args += [flag, str(value)]
    return args


@app.command("pull")
def pull_command(
    selectors: Annotated[
        list[str] | None,
        typer.Argument(
            help="Slack links, conversation ids, #channel-name or @user. "
            "Without any, every tracked item is refreshed.",
            show_default=False,
        ),
    ] = None,
    since: Annotated[
        str | None, typer.Option(metavar="DATE", help="Start of the range, YYYY-MM-DD in UTC.")
    ] = None,
    until: Annotated[
        str | None, typer.Option(metavar="DATE", help="End of the range, YYYY-MM-DD in UTC.")
    ] = None,
    channel: Annotated[
        bool, typer.Option("--channel", help="Pull the whole conversation a message link is in.")
    ] = False,
    full: Annotated[
        bool, typer.Option("--full", help="Re-read everything instead of only what is new.")
    ] = False,
    downloads: Annotated[
        int | None, typer.Option(metavar="N", min=1, help="Parallel file downloads.")
    ] = None,
    max_file_size: Annotated[
        int | None,
        typer.Option(
            "--max-file-size", metavar="MB", min=0, help="Largest file to download, in megabytes."
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Create an archive here without asking.")
    ] = False,
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Pull what the links name into the archive, or refresh everything tracked."""
    texts = selectors or []
    try:
        parsed = [parse_selector(text) for text in texts]
    except ValueError as error:
        _fail(str(error))
    if (since or until) and not parsed:
        _fail(RANGE_NEEDS_SELECTORS)
    _check_date("--since", since)
    _check_date("--until", until)
    services = bootstrap.current_services()
    flags = {"--channel": channel, "--full": full, "--downloads": downloads}
    args = _run_args(texts, since, until, flags | {"--max-file-size": max_file_size})
    try:
        global_config = read_global_config(services.env, services.home)
        archive_root = _archive_for_pull(root, services.env, global_config, yes)
        overrides = {"downloads": downloads, "max_file_size_mb": max_file_size}
        settings = load_settings(archive_root, global_config, services.env, overrides)
        options = PullOptions(
            since=since,
            until=until,
            full=full,
            channel_mode=channel,
            downloads=settings.downloads,
            max_file_bytes=settings.max_file_size_mb * MB,
            active_thread_days=settings.active_thread_days,
        )
        connector = services.connector()
        with (
            bootstrap.archive_stores(archive_root) as (archive, blobs),
            services.downloader(blobs, connector.authenticated) as downloader,
            RichReporter(err) as reporter,
            UnitOfWork(archive, blobs, services.clock, "pull", args, __version__) as uow,
        ):
            report = pull(parsed, options, uow, connector, downloader, reporter)
    except NoSession as error:
        _warn([str(e) for e in services.credentials.errors])
        _fail(str(error))
    except SlarcError as error:
        _fail(str(error))
    except KeyboardInterrupt:
        err.print(INTERRUPTED, markup=False)
        raise typer.Exit(INTERRUPTED_EXIT) from None
    out.print(_summary(report), markup=False)


def _existing_archive(root: Path | None, env: Mapping[str, str], home: Path) -> Path:
    """The archive found from the current directory, --root or the config; raises NoArchive."""
    found = find_archive(Path.cwd(), root, env, read_global_config(env, home))
    if found is None or not (found / ARCHIVE_FILE).is_file():
        raise NoArchive()
    return found


def _cell(value: object) -> Text:
    return Text("NULL", style="dim") if value is None else Text(str(value))


@app.command("sql")
def sql_command(
    query: Annotated[str, typer.Argument(help="A DuckDB query over the archive's views.")],
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Run a DuckDB query against the archive's views."""
    services = bootstrap.current_services()
    try:
        archive_root = _existing_archive(root, services.env, services.home)
        with bootstrap.query_index(archive_root) as index:
            columns, rows = index.sql(query)
    except SlarcError as error:
        _fail(str(error))
    table = Table()
    for column in columns:
        table.add_column(Text(column), overflow="fold")
    for row in rows:
        table.add_row(*(_cell(value) for value in row))
    out.print(table)
