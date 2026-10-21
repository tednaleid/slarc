# ABOUTME: Typer entrypoint for the slarc command line.
# ABOUTME: Each command takes its adapters from bootstrap and calls the service layer.
import dataclasses
import signal
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType, TracebackType
from typing import Annotated, Any, NoReturn

import typer
from rich import box
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
from slarc.domain.links import WorkspaceSel, parse_selector
from slarc.ports import ItemProgress, NoArchive, NoSession, SlarcError
lazy from slarc.service.auth import AuthStatus, auth_status, store_pasted
lazy from slarc.service.export import export
lazy from slarc.service.listing import ArchiveListing, RemoteRow, list_archive, list_remote
lazy from slarc.service.pull import PullOptions, PullReport, pull
lazy from slarc.service.remove import Removal, apply_removal, plan_removal
lazy from slarc.service.search import search
lazy from slarc.unit_of_work import UnitOfWork

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
RANGE_NOT_FOR_WORKSPACE = (
    "--since and --until apply to links and conversations, not to a whole workspace."
)
INTERRUPTED = "Interrupted; progress so far is saved."
INTERRUPTED_EXIT = 130
MB = 1024 * 1024
NO_WORKSPACE = "This archive tracks no workspace yet. Pull a Slack link first."
NO_TRACKED_ITEMS = "No tracked items yet. Pull a Slack link to start tracking it."
NO_MATCHES = "No messages match."
LOCAL_TIME = "%Y-%m-%d %H:%M"
LOCAL_DATE = "%Y-%m-%d"
TARGET = "⌖"  # crosshairs, before the date history reads back to
FETCHED_AT = "%Y-%m-%dT%H:%M:%SZ"
DOWNLOADS_ROW = "Downloads"
ARCHIVE_ROW = "Archive"
WAITING_ROW = "Slack"
DIRECTORY_KINDS = "(users, emoji, files)"
NOTHING_TO_REMOVE = "Nothing to remove."
NOTHING_REMOVED = "Nothing removed."
CANNOT_PULL_AGAIN = "Messages deleted from Slack since they were pulled cannot be pulled again."
LEFTOVERS = "what is left of removed items"
SPACE_FREED_LATER = "the space is freed when the running slarc command finishes"


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
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show version."),
    ] = False,
    take_ownership: Annotated[
        bool,
        typer.Option(
            "--take-ownership",
            help="Take this archive over from the machine that last used it, once your synced "
            "folder has finished syncing.",
        ),
    ] = False,
) -> None:
    signal.signal(signal.SIGTERM, _interrupt)
    # Every command takes its services from here, so they carry the global options.
    ctx.obj = dataclasses.replace(bootstrap.current_services(), take_ownership=take_ownership)


def _interrupt(signum: int, frame: FrameType | None) -> None:
    """Turns SIGTERM (cron timeouts, kill, shutdown) into the Ctrl-C path, which records the run."""
    raise KeyboardInterrupt


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
    ctx: typer.Context,
    workspace: Annotated[
        str | None, typer.Argument(help="Workspace domain or team id to check.")
    ] = None,
    paste: Annotated[
        bool,
        typer.Option("--paste", help="Store a browser token and d cookie in the OS keychain."),
    ] = False,
) -> None:
    """List the Slack sessions found and who you are in each workspace."""
    services: bootstrap.Services = ctx.obj
    if paste:
        xoxc = typer.prompt("Token (xoxc-...)", hide_input=True)
        xoxd = typer.prompt("d cookie (xoxd-...)", hide_input=True)
        try:
            session = store_pasted(services.keychain, xoxc, xoxd, services.gateway)
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
    """Shows a progress bar per item, one for downloads and one for saving the archive on
    stderr, with warnings above them.

    The bars start with the first item or downloads row, so a run that fails before then prints
    only its error. Rich shares columns across rows, so each row carries its own detail text.
    """

    def __init__(self, console: Console) -> None:
        self._progress = Progress(
            TextColumn("{task.description}", markup=False),
            BarColumn(),
            TextColumn("{task.fields[detail]}", markup=False),
            console=console,
        )
        self._tasks: dict[str, TaskID] = {}
        self._started = False

    def __enter__(self) -> RichReporter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._started:
            self._progress.stop()

    def info(self, msg: str) -> None:
        self._progress.console.print(msg, markup=False)

    def warn(self, msg: str) -> None:
        self._progress.console.print(f"Warning: {msg}", style="yellow", markup=False)

    def progress(self, label: str, update: ItemProgress) -> None:
        parts = [f"{update.read:,} read, {update.new:,} new"]
        if update.at is not None:
            parts.append(f"@ {_local_date(update.at)}")
        if update.target is not None:
            parts.append(f"{TARGET} {_local_date(update.target)}")
        total = None if update.share is None else 1
        self._show(label, update.share or 0, total, "  ".join(parts))

    def downloads(self, done: int, failed: int, skipped: int, total: int) -> None:
        parts = [f"{done}/{total}"]
        if failed:
            parts.append(f"{failed} failed")
        if skipped:
            parts.append(f"{skipped} skipped")
        self._show(DOWNLOADS_ROW, done, total, ", ".join(parts))

    def archive(self, step: str, *, finished: bool = False) -> None:
        if self._tasks.keys() - {WAITING_ROW}:
            self._show(ARCHIVE_ROW, 1 if finished else 0, 1 if finished else None, step)

    def waiting(self, seconds: int) -> None:
        """Counts down a wait Slack asked for; 0 ends the wait and drops its row."""
        if seconds:
            self._show(WAITING_ROW, 0, None, f"rate limited, waiting {seconds}s")
        elif WAITING_ROW in self._tasks:
            self._progress.remove_task(self._tasks.pop(WAITING_ROW))

    def _show(self, label: str, done: float, total: float | None, detail: str) -> None:
        if label not in self._tasks:
            if not self._started:
                self._progress.start()
                self._started = True
            self._tasks[label] = self._progress.add_task(label, total=total, detail=detail)
        self._progress.update(self._tasks[label], completed=done, total=total, detail=detail)


def _local_date(ts: str) -> str:
    return datetime.fromtimestamp(float(ts)).strftime(LOCAL_DATE)


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
    """The items pulled and lines appended, then this run's downloads and not-visible items."""
    directory = report.lines_appended - report.messages_new
    pulled = (
        f"Pulled {_plural(report.items_pulled, 'item')}: "
        f"{_plural(report.messages_new, 'new message')}, "
        f"{_plural(directory, 'new directory line')}"
    )
    lines = [f"{pulled} {DIRECTORY_KINDS}." if directory else f"{pulled}."]
    saved = [
        _plural(count, noun)
        for count, noun in ((report.files_saved, "file"), (report.images_saved, "image"))
        if count
    ]
    parts = [f"{' and '.join(saved)} saved"] if saved else []
    if report.downloads_pending:
        parts.append(f"{report.downloads_pending} pending for the next pull")
    if report.downloads_skipped:
        parts.append(f"{report.downloads_skipped} skipped")
    if parts:
        lines.append(f"Downloads: {', '.join(parts)}.")
    if report.not_visible:
        lines.append(
            f"{_plural(report.not_visible, 'item')} not visible to this account; see slarc list."
        )
    return "\n".join(lines)


def _archive_for_pull(
    root: Path | None,
    env: Mapping[str, str],
    global_config: Mapping[str, Any],
    yes: bool,
    bare: bool,
) -> Path | None:
    """The archive to pull into, created after confirmation when it has no slarc.toml.

    With no archive found, the archive goes in the current directory. A bare pull never creates
    one, since an empty archive tracks nothing, and gets None; so does a pull of only workspaces.
    """
    found = find_archive(Path.cwd(), root, env, global_config)
    if found is not None and (found / ARCHIVE_FILE).is_file():
        return found
    if bare:
        return None
    if found is None:
        target, question = Path.cwd(), f"No archive here. Create one in {Path.cwd()}?"
    else:
        target, question = found, f"No archive at {found}. Create one?"
    try:
        declined = not yes and not typer.confirm(question, default=True)
    except typer.Abort:
        declined = True
    if declined:
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
    ctx: typer.Context,
    selectors: Annotated[
        list[str] | None,
        typer.Argument(
            help="Slack links, conversation ids, #channel-name, @user or a workspace name. "
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
    if (since or until) and any(isinstance(selector, WorkspaceSel) for selector in parsed):
        _fail(RANGE_NOT_FOR_WORKSPACE)
    _check_date("--since", since)
    _check_date("--until", until)
    all_workspaces = all(isinstance(selector, WorkspaceSel) for selector in parsed)
    services: bootstrap.Services = ctx.obj
    flags = {"--channel": channel, "--full": full, "--downloads": downloads}
    args = _run_args(texts, since, until, flags | {"--max-file-size": max_file_size})
    try:
        global_config = read_global_config(services.env, services.home)
        archive_root = _archive_for_pull(root, services.env, global_config, yes, all_workspaces)
        if archive_root is None:
            out.print(NO_TRACKED_ITEMS, markup=False)
            return
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
        with RichReporter(err) as reporter:
            connector = services.connector(reporter.waiting)
            with (
                bootstrap.archive_stores(archive_root, services.take_ownership) as (
                    archive,
                    blobs,
                ),
                services.downloader(blobs, connector.authenticated) as downloader,
                UnitOfWork(
                    archive,
                    blobs,
                    services.clock,
                    "pull",
                    args,
                    __version__,
                    services.run_suffix,
                    reporter,
                ) as uow,
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
    if any(isinstance(error, NoSession) for error in report.unreachable):
        _warn([str(e) for e in services.credentials.errors])
    if not parsed and report.items_selected == 0:
        out.print(NO_TRACKED_ITEMS, markup=False)
    else:
        out.print(_summary(report), markup=False)
    if report.failed:
        raise typer.Exit(1)


def _existing_archive(root: Path | None, env: Mapping[str, str], home: Path) -> Path:
    """The archive found from the current directory, --root or the config.

    Raises NoArchive when there is none.
    """
    found = find_archive(Path.cwd(), root, env, read_global_config(env, home))
    if found is None or not (found / ARCHIVE_FILE).is_file():
        raise NoArchive()
    return found


def _cell(value: object) -> Text:
    return Text("NULL", style="dim") if value is None else Text(str(value))


@app.command("sql")
def sql_command(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="A DuckDB query over the archive's views.")],
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Run a DuckDB query against the archive's views."""
    services: bootstrap.Services = ctx.obj
    try:
        archive_root = _existing_archive(root, services.env, services.home)
        with bootstrap.query_index(archive_root, services.take_ownership) as index:
            columns, rows = index.sql(query)
    except SlarcError as error:
        _fail(str(error))
    table = Table()
    for column in columns:
        table.add_column(Text(column), overflow="fold")
    for row in rows:
        table.add_row(*(_cell(value) for value in row))
    out.print(table)


def _local_time(moment: datetime) -> str:
    return moment.astimezone().strftime(LOCAL_TIME)


def _ts_time(ts: str) -> str:
    return _local_time(datetime.fromtimestamp(int(ts.split(".")[0]), UTC))


def _fetched_time(fetched_at: str | None) -> str:
    if fetched_at is None:
        return ""
    return _local_time(datetime.strptime(fetched_at, FETCHED_AT).replace(tzinfo=UTC))


def _table(headers: list[str], rows: list[list[object]]) -> Table:
    table = Table(box=box.SIMPLE_HEAD)
    for header in headers:
        table.add_column(Text(header), overflow="fold")
    for row in rows:
        table.add_row(*(Text(str(value)) for value in row))
    return table


def _print_listing(listing: ArchiveListing) -> None:
    out.print(f"Archive: {listing.archive}", markup=False)
    config = ", ".join(str(path) for path in listing.config_files) or "none, built-in defaults"
    out.print(f"Config: {config}", markup=False)
    if listing.rows:
        headers = [
            "Workspace",
            "Conversation",
            "Kind",
            "Messages",
            "Last pull",
            "Pending",
            "Skipped",
            "Status",
        ]
        rows: list[list[object]] = [
            [
                row.workspace,
                row.conversation,
                row.kind,
                row.messages,
                _fetched_time(row.last_pull),
                row.pending,
                row.skipped,
                row.status,
            ]
            for row in listing.rows
        ]
        out.print(_table(headers, rows))
    else:
        out.print(NO_TRACKED_ITEMS, markup=False)
    out.print(f"Downloads: {listing.pending} pending, {listing.skipped} skipped.", markup=False)


def _print_remote(rows: list[RemoteRow]) -> None:
    headers = ["Workspace", "Conversation", "Kind", "Id", "Tracked"]
    cells: list[list[object]] = [
        [row.workspace, row.conversation, row.kind, row.channel, "tracked" if row.tracked else ""]
        for row in rows
    ]
    out.print(_table(headers, cells))


@app.command("list")
def list_command(
    ctx: typer.Context,
    remote: Annotated[
        bool,
        typer.Option(
            "--remote", help="List every conversation Slack shows you, marking tracked ones."
        ),
    ] = False,
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Show the archive, its config files and each tracked item."""
    services: bootstrap.Services = ctx.obj
    try:
        archive_root = _existing_archive(root, services.env, services.home)
        if remote:
            connector = services.connector()
            with bootstrap.archive_stores(archive_root, services.take_ownership) as (archive, _):
                teams = sorted({team_id for team_id, _, _, _ in archive.tracked_items()})
                if not teams:
                    _fail(NO_WORKSPACE)
                remote_rows = [
                    row for team in teams for row in list_remote(connector, team, archive)
                ]
            _print_remote(remote_rows)
            return
        global_config = read_global_config(services.env, services.home)
        settings = load_settings(archive_root, global_config, services.env, {})
        with (
            bootstrap.archive_stores(archive_root, services.take_ownership) as (archive, _),
            bootstrap.shared_index(archive_root, archive) as index,
        ):
            listing = list_archive(settings, archive, index)
    except NoSession as error:
        _warn([str(e) for e in services.credentials.errors])
        _fail(str(error))
    except SlarcError as error:
        _fail(str(error))
    _print_listing(listing)


@app.command("search")
def search_command(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Text to find in messages, ignoring case.")],
    in_conv: Annotated[
        str | None,
        typer.Option(
            "--in", metavar="CONV", help="Only this conversation: #channel-name or its id."
        ),
    ] = None,
    from_user: Annotated[
        str | None,
        typer.Option("--from", metavar="USER", help="Only messages by this user: a name or id."),
    ] = None,
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Find archived messages containing the text, newest first, with their Slack links."""
    services: bootstrap.Services = ctx.obj
    try:
        archive_root = _existing_archive(root, services.env, services.home)
        with bootstrap.query_index(archive_root, services.take_ownership) as index:
            hits = search(index, query, in_conv, from_user)
    except SlarcError as error:
        _fail(str(error))
    if not hits:
        out.print(NO_MATCHES, markup=False)
    for number, hit in enumerate(hits):
        if number:
            out.print()
        out.print(f"{hit.author} in {hit.conversation}, {_ts_time(hit.ts)}", markup=False)
        out.print(f"  {hit.snippet}", markup=False)
        out.print(f"  {hit.link}", markup=False)


@app.command("export")
def export_command(
    ctx: typer.Context,
    selectors: Annotated[
        list[str],
        typer.Argument(
            help="Slack links, conversation ids, #channel-name, @user or a workspace name "
            "to export.",
            show_default=False,
        ),
    ],
    out_path: Annotated[
        Path | None,
        typer.Option(
            "--out",
            metavar="DIR",
            help="Directory to write, or the HTML file with --single-file. "
            "Default: slarc-export-<conversation name or workspace> here.",
        ),
    ] = None,
    single_file: Annotated[
        bool,
        typer.Option("--single-file", help="Write one HTML file with every asset inlined."),
    ] = False,
    include_deleted: Annotated[
        bool,
        typer.Option("--include-deleted", help="Include messages deleted from Slack, marked."),
    ] = False,
    tz: Annotated[
        str | None,
        typer.Option(
            metavar="ZONE",
            help="Show times in this zone, such as America/Chicago, instead of the viewer's.",
        ),
    ] = None,
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Write static HTML of the archived conversations and threads the links name."""
    try:
        parsed = [parse_selector(text) for text in selectors]
    except ValueError as error:
        _fail(str(error))
    services: bootstrap.Services = ctx.obj
    try:
        archive_root = _existing_archive(root, services.env, services.home)
        global_config = read_global_config(services.env, services.home)
        settings = load_settings(archive_root, global_config, services.env, {"tz": tz})
        with (
            bootstrap.archive_stores(archive_root, services.take_ownership) as (archive, blobs),
            bootstrap.shared_index(archive_root, archive) as index,
        ):
            written = export(
                parsed,
                out_path,
                single_file,
                include_deleted,
                settings.tz,
                index,
                blobs,
                archive,
                bootstrap.export_writer(),
            )
    except SlarcError as error:
        _fail(str(error))
    out.print(f"Exported to {written.absolute()}.", markup=False)


def _deleted_parts(removal: Removal, line: str = "line") -> str:
    """The lines, files and images a removal deletes, such as "2 lines and 1 file"; may be empty.

    line names what a removed line is called.
    """
    parts = [
        _plural(count, noun)
        for count, noun in (
            (removal.lines, line),
            (removal.files, "file"),
            (removal.images, "image"),
        )
        if count
    ]
    return " and ".join([", ".join(parts[:-1]), parts[-1]] if len(parts) > 1 else parts)


def _removal_question(removal: Removal) -> str:
    """The prompt: what is untracked and deleted, then a warning when anything is deleted."""
    deleted = _deleted_parts(removal)
    tracking = f"Stop tracking {_plural(len(removal.items), 'item')}"
    if removal.items and not deleted:
        return f"{tracking}?"
    if removal.items:
        question = f"{tracking} and delete {deleted}?"
    else:
        question = f"Delete {deleted or LEFTOVERS}?"
    return f"{question}\n{CANNOT_PULL_AGAIN}"


def _removal_result(removal: Removal, maintained: bool | None) -> str:
    """What the removal did. When maintenance found the archive in use, the bytes are still on
    disk, so the message says they are out of the archive and freed later."""
    deleted = _deleted_parts(removal)
    if not removal.items:
        if maintained:
            return f"Cleaned up {deleted or LEFTOVERS}."
        return f"Cleaned up {deleted or LEFTOVERS}; {SPACE_FREED_LATER}."
    result = f"Stopped tracking {_plural(len(removal.items), 'item')}"
    if deleted and not maintained:
        gone = _deleted_parts(removal, "message")
        result += f". Their {gone} are no longer in the archive; {SPACE_FREED_LATER}."
    else:
        result += f"; deleted {deleted}." if deleted else "."
    if removal.kept_messages:
        kept = _plural(removal.kept_messages, "message")
        result += f" Kept {kept} that other tracked items cover."
    return result


@app.command("rm")
def rm_command(
    ctx: typer.Context,
    selectors: Annotated[
        list[str],
        typer.Argument(
            help="Slack links, conversation ids, #channel-name, @user or a workspace name "
            "to stop tracking.",
            show_default=False,
        ),
    ],
    channel: Annotated[
        bool,
        typer.Option("--channel", help="Remove the whole conversation a message link is in."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Remove without asking.")] = False,
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Stop tracking what the links name and delete what only they kept in the archive."""
    try:
        parsed = [(text, parse_selector(text)) for text in selectors]
    except ValueError as error:
        _fail(str(error))
    services: bootstrap.Services = ctx.obj
    args = _run_args(selectors, None, None, {"--channel": channel})
    try:
        archive_root = _existing_archive(root, services.env, services.home)
        with bootstrap.archive_stores(archive_root, services.take_ownership) as (archive, blobs):
            with bootstrap.shared_index(archive_root, archive) as index:
                removal = plan_removal(parsed, channel, archive, blobs, index)
            if removal.nothing_to_do:
                out.print(NOTHING_TO_REMOVE, markup=False)
                return
            try:
                declined = not yes and not typer.confirm(_removal_question(removal), default=False)
            except typer.Abort:
                declined = True
            if declined:
                _fail(NOTHING_REMOVED)
            # The index is closed by now: maintenance needs the store's hold to be this
            # process's only hold on the read lock.
            with UnitOfWork(
                archive, blobs, services.clock, "rm", args, __version__, services.run_suffix
            ) as uow:
                removal = apply_removal(removal, uow, blobs)
    except SlarcError as error:
        _fail(str(error))
    except KeyboardInterrupt:
        err.print(INTERRUPTED, markup=False)
        raise typer.Exit(INTERRUPTED_EXIT) from None
    out.print(_removal_result(removal, uow.maintained), markup=False)
