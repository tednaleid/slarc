# ABOUTME: Typer entrypoint for the slarc command line.
# ABOUTME: Each command takes its adapters from bootstrap and calls the service layer.
import dataclasses
import secrets
import shlex
import signal
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, tzinfo
from importlib.resources import files
from pathlib import Path
from types import FrameType, TracebackType
from typing import Annotated, Any, NoReturn

import typer
from rich import box
from rich.cells import cell_len
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskID, TextColumn
from rich.style import Style
from rich.table import Table
from rich.text import Text

from slarc import __version__, bootstrap
from slarc.config import (
    ARCHIVE_FILE,
    Settings,
    create_archive,
    find_archive,
    load_settings,
    read_global_config,
)
from slarc.domain.links import WorkspaceSel, parse_selector
from slarc.domain.retention import parse_retention
from slarc.domain.window import Window
from slarc.formats import ExportFormat, OutputFormat, write_rows
from slarc.ports import (
    SEARCH_LIMIT,
    Clock,
    ItemProgress,
    NoArchive,
    NoSession,
    SearchHit,
    SlarcError,
)
lazy from slarc.service.auth import AuthStatus, auth_status, store_pasted
lazy from slarc.service.export import STDOUT, EmptyWindow, Exported, export
lazy from slarc.service.files import FileNotArchived, file_bytes
lazy from slarc.service.listen import ListenOptions, listen
lazy from slarc.service.listing import (
    ArchiveListing,
    ListedDownload,
    ListRow,
    RemoteRow,
    list_archive,
    list_remote,
)
lazy from slarc.service.pull import PullOptions, PullReport, pull
lazy from slarc.service.remove import Removal, apply_removal, plan_removal
lazy from slarc.service.search import search
lazy from slarc.unit_of_work import UnitOfWork

app = typer.Typer(
    help="Archive the Slack conversations you can see into searchable, re-renderable files.\n\n"
    "Agents and scripts: run slarc guide for workflows, output formats and exit codes.",
    no_args_is_help=True,
)

out = Console(highlight=False, soft_wrap=True)
err = Console(stderr=True, highlight=False, soft_wrap=True)

NO_SESSIONS = (
    "No Slack sessions found. Sign into the Slack desktop app or run `slarc auth --paste`."
)
RETENTION_NEEDS_SELECTORS = "--retention applies to links and workspace names."
RANGE_NEEDS_SELECTORS = "--since and --until apply only to links named on the command line."
RANGE_NOT_FOR_WORKSPACE = (
    "--since and --until apply to links and conversations, not to a whole workspace."
)
INTERRUPTED = "Interrupted; progress so far is saved."
INTERRUPTED_EXIT = 130
STOPPING = "Stopping once the current step ends; press Ctrl-C again to stop now."
# Like grep: 1 when a search finds nothing, 2 when a command fails.
NO_MATCH_EXIT = 1
ERROR_EXIT = 2
MB = 1024 * 1024
NO_WORKSPACE = "This archive tracks no workspace yet. Pull a Slack link first."
NO_TRACKED_ITEMS = "No tracked items yet. Pull a Slack link to start tracking it."
NO_MATCHES = "No messages match."
LOCAL_TIME = "%Y-%m-%d %H:%M"
LOCAL_DATE = "%Y-%m-%d"
# Crosshairs, before the date history reads back to. Terminal fonts often draw it over the
# space after it, so `@` takes no space before its date either.
TARGET = "⌖"
FETCHED_AT = "%Y-%m-%dT%H:%M:%SZ"
DOWNLOADS_ROW = "Downloads"
ARCHIVE_ROW = "Archive"
REMOTE_TAKES_NO_LINKS = "slarc list --remote lists every conversation; it takes no links."
WAITING_ROW = "Slack"
DIRECTORY_KINDS = "(users, emoji, files)"
NOTHING_TO_REMOVE = "Nothing to remove."
NOTHING_REMOVED = "Nothing removed."
CANNOT_PULL_AGAIN = "Messages deleted from Slack since they were pulled cannot be pulled again."
LEFTOVERS = "what is left of removed items"
SPACE_FREED_LATER = "the space is freed when the running slarc command finishes"
SINCE_HELP = (
    "Start of the window: 2026-10-19, 2026-10-19T14:30 (local unless it has an offset) "
    "or an age such as 7d."
)
UNTIL_HELP = "End of the window, in the same forms; a date alone runs through that day."
FORMAT_OPTION = typer.Option(
    "--format",
    help="table for people; jsonl or csv for scripts, never truncated.",
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"slarc {__version__}")
        raise typer.Exit()


def _fail(message: str) -> NoReturn:
    err.print(message, style="red", markup=False)
    raise typer.Exit(ERROR_EXIT)


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
        raise typer.Exit(ERROR_EXIT)


class RichReporter:
    """Shows a progress bar per item, one for downloads and one for saving the archive on
    stderr, with warnings above them.

    The bars start with the first item or downloads row, so a run that fails before then prints
    only its error. Rich shares columns across rows, so each row carries its own detail text.
    """

    def __init__(self, console: Console, *, transient: bool = False) -> None:
        self._progress = Progress(
            TextColumn("{task.description}", markup=False),
            BarColumn(),
            TextColumn("{task.fields[detail]}", markup=False),
            console=console,
            transient=transient,
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
        self.close()

    def close(self) -> None:
        if self._started:
            self._progress.stop()

    def info(self, msg: str) -> None:
        self._progress.console.print(msg, markup=False)

    def warn(self, msg: str) -> None:
        self._progress.console.print(f"Warning: {msg}", style="yellow", markup=False)

    def progress(self, label: str, update: ItemProgress) -> None:
        parts = [f"{update.read:,} read, {update.new:,} new"]
        # A target alone, when history returned nothing new, would only look like a gap.
        if update.at is not None:
            parts.append(f"@{_local_date(update.at)}")
            if update.target is not None:
                parts.append(f"{TARGET} {_local_date(update.target)}")
        total = None if update.share is None else 1
        self._show(label, update.share or 0, total, "  ".join(parts))

    def downloads(self, done: int, failed: int, skipped: int, total: int) -> None:
        parts = [f"{done:,}/{total:,}"]
        if failed:
            parts.append(f"{failed:,} failed")
        if skipped:
            parts.append(f"{skipped:,} skipped")
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


def _window(since: str | None, until: str | None, now: datetime, zone: tzinfo) -> Window:
    """The window --since and --until give, read in zone, the machine's."""
    try:
        return Window.parse(since, until, now, zone)
    except ValueError as error:
        _fail(str(error))


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def _summary(report: PullReport, selectors: list[str] | None = None) -> str:
    """The items pulled and lines appended, then this run's downloads and not-visible items.

    When downloads are left unsaved, it names the slarc list that shows why: of the pulled
    selectors, or for a bare pull, of a link or workspace."""
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
        parts.append(f"{report.downloads_pending:,} pending for the next pull")
    if report.downloads_skipped:
        parts.append(f"{report.downloads_skipped:,} skipped")
    if parts:
        lines.append(f"Downloads: {', '.join(parts)}.")
    if report.downloads_pending or report.downloads_skipped:
        if selectors:
            lines.append(f"Run slarc list {shlex.join(selectors)} to see why.")
        else:
            lines.append("Run slarc list with a link or workspace to see why.")
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
    since: Annotated[str | None, typer.Option(metavar="WHEN", help=SINCE_HELP)] = None,
    until: Annotated[str | None, typer.Option(metavar="WHEN", help=UNTIL_HELP)] = None,
    channel: Annotated[
        bool, typer.Option("--channel", help="Pull the whole conversation a message link is in.")
    ] = False,
    full: Annotated[
        bool, typer.Option("--full", help="Re-read everything instead of only what is new.")
    ] = False,
    retry_skipped: Annotated[
        bool,
        typer.Option(
            "--retry-skipped",
            help="Try again the downloads skipped as forbidden, not found, given up or too large.",
        ),
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
    retention: Annotated[
        str | None,
        typer.Option(
            metavar="DAYS",
            help="How long to keep threads, counted from each thread's newest message: 30d, 12w "
            "or forever. Given links keep it as their own; a workspace name sets it for every "
            "item there without its own. Later pulls and listen remove older threads and the "
            "files only they use.",
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
    if retention is not None and not parsed:
        _fail(RETENTION_NEEDS_SELECTORS)
    try:
        kept = parse_retention(retention) if retention is not None else None
    except ValueError as error:
        _fail(str(error))
    services: bootstrap.Services = ctx.obj
    window = _window(since, until, services.clock.now(), bootstrap.zone(services.env))
    all_workspaces = all(isinstance(selector, WorkspaceSel) for selector in parsed)
    flags = {
        "--channel": channel,
        "--full": full,
        "--retry-skipped": retry_skipped,
        "--downloads": downloads,
        "--retention": retention,
    }
    args = _run_args(texts, since, until, flags | {"--max-file-size": max_file_size})
    try:
        global_config = read_global_config(services.env, services.home)
        archive_root = _archive_for_pull(root, services.env, global_config, yes, all_workspaces)
        if archive_root is None:
            err.print(NO_TRACKED_ITEMS, markup=False)
            return
        overrides = {"downloads": downloads, "max_file_size_mb": max_file_size}
        settings = load_settings(archive_root, global_config, services.env, overrides)
        options = PullOptions(
            since=window.since,
            until=window.until,
            full=full,
            retry_skipped=retry_skipped,
            channel_mode=channel,
            downloads=settings.downloads,
            max_file_bytes=settings.max_file_size_mb * MB,
            active_thread_days=settings.active_thread_days,
            retention=kept,
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
        err.print(NO_TRACKED_ITEMS, markup=False)
    else:
        out.print(_summary(report, texts), markup=False)
    if report.failed:
        raise typer.Exit(ERROR_EXIT)


def _existing_archive(root: Path | None, env: Mapping[str, str], home: Path) -> Path:
    """The archive found from the current directory, --root or the config.

    Raises NoArchive when there is none.
    """
    found = find_archive(Path.cwd(), root, env, read_global_config(env, home))
    if found is None or not (found / ARCHIVE_FILE).is_file():
        raise NoArchive()
    return found


def _write_dataclasses(
    fmt: OutputFormat, kind: Any, items: Sequence[object], omit: frozenset[str] = frozenset()
) -> None:
    """Writes dataclass instances of kind as rows keyed by its field names, but those in omit."""
    columns = [f.name for f in dataclasses.fields(kind) if f.name not in omit]
    rows = [[getattr(item, column) for column in columns] for item in items]
    write_rows(sys.stdout, fmt, columns, rows)


def _cell(value: object) -> Text:
    return Text("NULL", style="dim") if value is None else Text(str(value))


SQL_EXAMPLES = """Examples:

  # the views, then one view's columns, each with a description
  slarc sql "SELECT view_name, comment FROM duckdb_views() WHERE NOT internal"
  slarc sql "SELECT column_name, data_type, comment FROM duckdb_columns()
    WHERE table_name = 'message_readable'"

  # a conversation since a date, oldest first, as JSON Lines
  slarc sql --format jsonl "SELECT posted_at, author, text, link FROM message_readable
    WHERE conversation = '#general' AND posted_at >= DATE '2026-10-19' ORDER BY posted_at"

  # messages that mention me, the user of each workspace's latest auth
  slarc sql "SELECT r.posted_at, r.conversation, r.author, r.text, r.link
    FROM message_readable r JOIN message m USING (team_id, channel_id, ts)
    JOIN workspace w USING (team_id)
    WHERE contains(m.text_mrkdwn, '<@' || w.user_id) AND m.author_kind <> 'system'"

  # what slarc stored since a time (UTC), whenever the messages were posted
  slarc sql "SELECT conversation, author_real_name, text, link FROM message_readable
    WHERE first_seen_at >= '2026-10-20T14:00:00Z' ORDER BY posted_at"
"""


@app.command("sql", epilog=SQL_EXAMPLES)
def sql_command(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="A DuckDB query over the archive's views.")],
    fmt: Annotated[OutputFormat, FORMAT_OPTION] = "table",
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
    if fmt != "table":
        write_rows(sys.stdout, fmt, columns, rows)
        return
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


def _print_listing(listing: ArchiveListing, settings: Settings) -> None:
    out.print(f"Archive: {listing.archive}", markup=False)
    config = ", ".join(str(path) for path in listing.config_files) or "none, built-in defaults"
    out.print(f"Config: {config}", markup=False)
    out.print(
        f"Settings: downloads = {settings.downloads}, "
        f"max_file_size_mb = {settings.max_file_size_mb}, "
        f"active_thread_days = {settings.active_thread_days}, "
        f"unheard_pull_minutes = {settings.unheard_pull_minutes}, tz = {settings.tz or 'local'}",
        markup=False,
    )
    if listing.retention:
        kept = ", ".join(f"{name} {value}" for name, value in listing.retention.items())
        out.print(f"Retention: {kept}", markup=False)
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
            "Retention",
        ]
        rows: list[list[object]] = [
            [
                row.workspace,
                row.conversation,
                row.kind,
                f"{row.messages:,}",
                _fetched_time(row.last_pull),
                f"{row.pending:,}",
                f"{row.skipped:,}",
                row.status,
                f"{row.retention} (own)" if row.retention_from == "item" else row.retention,
            ]
            for row in listing.rows
        ]
        out.print(_table(headers, rows))
        if listing.downloads:
            _print_downloads(listing.downloads)
            out.print()
    else:
        err.print(NO_TRACKED_ITEMS, markup=False)
    out.print(f"Downloads: {listing.pending:,} pending, {listing.skipped:,} skipped.", markup=False)


def _print_downloads(downloads: list[ListedDownload]) -> None:
    """One line per download, columns aligned as in _table. The URL comes last, unpadded and as
    a terminal link, so it is never wrapped or cut and copies and opens whole."""
    headers = ["Workspace", "Conversation", "Type", "Status", "Attempts", "Error"]
    cells = [
        [
            d.workspace,
            d.conversation,
            d.type,
            d.status.replace("_", " "),
            "" if d.attempts is None else str(d.attempts),
            d.error or "",
        ]
        for d in downloads
    ]
    widths = [max(cell_len(row[i]) for row in [headers, *cells]) for i in range(len(headers))]

    def columns(values: list[str]) -> str:
        padded = (
            value + " " * (width - cell_len(value))
            for value, width in zip(values, widths, strict=True)
        )
        return "  " + "   ".join(padded) + "   "

    heading = columns(headers) + "URL"
    out.print()
    out.print(heading, markup=False, soft_wrap=True)
    out.print(" " + "\u2500" * (cell_len(heading) - 1), markup=False, soft_wrap=True)
    for values, download in zip(cells, downloads, strict=True):
        line = Text.assemble(columns(values), (download.url, Style(link=download.url)))
        out.print(line, soft_wrap=True)


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
    selectors: Annotated[
        list[str] | None,
        typer.Argument(
            help="Slack links, conversation ids, #channel-name, @user or a workspace name, to "
            "show only what they name, with each download not saved.",
            show_default=False,
        ),
    ] = None,
    remote: Annotated[
        bool,
        typer.Option(
            "--remote", help="List every conversation Slack shows you, marking tracked ones."
        ),
    ] = False,
    fmt: Annotated[OutputFormat, FORMAT_OPTION] = "table",
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Show the archive, its config files and each tracked item."""
    texts = selectors or []
    if remote and texts:
        _fail(REMOTE_TAKES_NO_LINKS)
    try:
        named = [(text, parse_selector(text)) for text in texts]
    except ValueError as error:
        _fail(str(error))
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
            if fmt == "table":
                _print_remote(remote_rows)
            else:
                _write_dataclasses(fmt, RemoteRow, remote_rows)
            return
        global_config = read_global_config(services.env, services.home)
        settings = load_settings(archive_root, global_config, services.env, {})
        with (
            bootstrap.archive_stores(archive_root, services.take_ownership) as (archive, _),
            bootstrap.shared_index(archive_root, archive) as index,
        ):
            listing = list_archive(settings, archive, index, named)
    except NoSession as error:
        _warn([str(e) for e in services.credentials.errors])
        _fail(str(error))
    except SlarcError as error:
        _fail(str(error))
    if fmt == "table":
        _print_listing(listing, settings)
    else:
        _write_dataclasses(fmt, ListRow, listing.rows)


@app.command("search")
def search_command(
    ctx: typer.Context,
    query: Annotated[
        str | None,
        typer.Argument(help="Text to find in messages, ignoring case. Optional with a filter."),
    ] = None,
    in_conv: Annotated[
        str | None,
        typer.Option(
            "--in", metavar="CONV", help="Only this conversation: #channel-name, @user or its id."
        ),
    ] = None,
    from_user: Annotated[
        str | None,
        typer.Option(
            "--from", metavar="USER", help="Only messages by this user: a name, an id or me."
        ),
    ] = None,
    mentions: Annotated[
        str | None,
        typer.Option(
            metavar="USER",
            help="Only messages mentioning this user or a usergroup they are in: a name, an id "
            "or me.",
        ),
    ] = None,
    since: Annotated[str | None, typer.Option(metavar="WHEN", help=SINCE_HELP)] = None,
    until: Annotated[str | None, typer.Option(metavar="WHEN", help=UNTIL_HELP)] = None,
    limit: Annotated[
        int, typer.Option(metavar="N", min=0, help="Most matches to show; 0 shows every one.")
    ] = SEARCH_LIMIT,
    fmt: Annotated[OutputFormat, FORMAT_OPTION] = "table",
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Find archived messages by text, conversation, author, mention or window, newest first."""
    services: bootstrap.Services = ctx.obj
    window = _window(since, until, services.clock.now(), bootstrap.zone(services.env))
    try:
        archive_root = _existing_archive(root, services.env, services.home)
        with bootstrap.query_index(archive_root, services.take_ownership) as index:
            found = search(
                index, query, in_conv, from_user, mentions=mentions, window=window, limit=limit
            )
    except SlarcError as error:
        _fail(str(error))
    if not found.hits:
        err.print(NO_MATCHES, markup=False)
        raise typer.Exit(NO_MATCH_EXIT)
    if fmt != "table":
        # text holds the whole message and matched_file names a file the match was in, so the
        # excerpt only serves the table.
        _write_dataclasses(fmt, SearchHit, found.hits, omit=frozenset({"excerpt"}))
    else:
        for number, hit in enumerate(found.hits):
            if number:
                out.print()
            out.print(f"{hit.author} in {hit.conversation}, {_ts_time(hit.ts)}", markup=False)
            out.print(f"  {hit.excerpt}", markup=False)
            out.print(f"  {hit.link}", markup=False)
    if found.more:
        err.print(f"Showing {limit:,}; --limit for more.", markup=False)


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
            help="Directory to write, or the file with --single-file; - writes one file to "
            "stdout. Default: slarc-export-<conversation name or workspace> here.",
        ),
    ] = None,
    fmt: Annotated[
        ExportFormat,
        typer.Option(
            "--format", help="html for a browser; markdown to read as text, without assets."
        ),
    ] = "html",
    channel: Annotated[
        bool,
        typer.Option("--channel", help="Export the whole conversation a message link is in."),
    ] = False,
    since: Annotated[str | None, typer.Option(metavar="WHEN", help=SINCE_HELP)] = None,
    until: Annotated[str | None, typer.Option(metavar="WHEN", help=UNTIL_HELP)] = None,
    single_file: Annotated[
        bool,
        typer.Option("--single-file", help="Write one file, with every HTML asset inlined."),
    ] = False,
    include_deleted: Annotated[
        bool,
        typer.Option("--include-deleted", help="Include messages deleted from Slack, marked."),
    ] = False,
    tz: Annotated[
        str | None,
        typer.Option(
            metavar="ZONE",
            help="Show times in this zone, such as America/Chicago, instead of the viewer's "
            "(HTML) or this machine's (markdown).",
        ),
    ] = None,
    open_page: Annotated[
        bool, typer.Option("--open", help="Open the export in the default browser.")
    ] = False,
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Write static HTML or markdown of the archived conversations and threads the links name."""
    try:
        parsed = [parse_selector(text) for text in selectors]
    except ValueError as error:
        _fail(str(error))
    services: bootstrap.Services = ctx.obj
    zone = bootstrap.zone(services.env)
    window = _window(since, until, services.clock.now(), zone)
    to_stdout = out_path == STDOUT
    try:
        archive_root = _existing_archive(root, services.env, services.home)
        global_config = read_global_config(services.env, services.home)
        settings = load_settings(archive_root, global_config, services.env, {"tz": tz})
        with (
            bootstrap.archive_stores(archive_root, services.take_ownership) as (archive, blobs),
            bootstrap.shared_index(archive_root, archive) as index,
        ):
            exported = export(
                parsed,
                out_path,
                single_file,
                include_deleted,
                settings.tz,
                index,
                blobs,
                archive,
                bootstrap.stdout_writer() if to_stdout else bootstrap.export_writer(),
                fmt=fmt,
                window=window,
                whole_conversation=channel,
                local_zone=zone,
            )
    except EmptyWindow as error:
        err.print(str(error), markup=False)
        raise typer.Exit(NO_MATCH_EXIT) from None
    except SlarcError as error:
        _fail(str(error))
    if to_stdout:
        err.print(f"Exported {_exported_parts(exported)} to stdout.", markup=False)
        page = None
    else:
        out.print(f"Exported {_exported_parts(exported)} to", markup=False)
        is_dir = not single_file and fmt == "html"
        page = (exported.path / "index.html" if is_dir else exported.path).absolute()
        out.print(Text(str(page), style=Style(link=page.as_uri())))
    if exported.files_missing:
        missing = _plural(exported.files_missing, "file")
        verb = "is" if exported.files_missing == 1 else "are"
        listed = shlex.join(selectors)
        err.print(
            f"{missing} {verb} not in the export; run slarc list {listed} to see why.",
            markup=False,
        )
    if open_page and page is not None and not services.open_page(page.as_uri()):
        err.print("No browser could be opened; open the page above instead.", markup=False)


class LineReporter:
    """Prints each info line on stdout and each warning on stderr, each after the local time.

    Between pull_started and pull_ended, the pull's rows show on a terminal's stderr as
    `slarc pull` shows them, and clear when it ends; outside a pull, progress, downloads, saving
    and rate limit waits are not shown."""

    def __init__(self, clock: Clock, zone: tzinfo) -> None:
        self._clock = clock
        self._zone = zone
        self._rows: RichReporter | None = None

    def info(self, msg: str) -> None:
        out.print(f"{self._time()} {msg}", markup=False, soft_wrap=True)

    def warn(self, msg: str) -> None:
        err.print(f"{self._time()} Warning: {msg}", style="yellow", markup=False)

    def progress(self, label: str, update: ItemProgress) -> None:
        if self._rows is not None:
            self._rows.progress(label, update)

    def downloads(self, done: int, failed: int, skipped: int, total: int) -> None:
        if self._rows is not None:
            self._rows.downloads(done, failed, skipped, total)

    def archive(self, step: str, *, finished: bool = False) -> None:
        if self._rows is not None:
            self._rows.archive(step, finished=finished)

    def waiting(self, seconds: int) -> None:
        if self._rows is not None:
            self._rows.waiting(seconds)

    def pull_started(self) -> None:
        # Rows cleared from a file or pipe would leave a blank line behind.
        if err.is_terminal:
            self._rows = RichReporter(err, transient=True)

    def pull_ended(self) -> None:
        if self._rows is not None:
            self._rows.close()
            self._rows = None

    def _time(self) -> str:
        return self._clock.now().astimezone(self._zone).strftime("%H:%M")


@app.command("listen")
def listen_command(
    ctx: typer.Context,
    workspaces: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[WORKSPACE]...",
            help="Workspace names. Without any, every workspace with a tracked item.",
            show_default=False,
        ),
    ] = None,
    downloads: Annotated[
        int | None, typer.Option(metavar="N", min=1, help="Parallel file downloads.")
    ] = None,
    max_file_size: Annotated[
        int | None,
        typer.Option(
            "--max-file-size", metavar="MB", min=0, help="Largest file to download, in megabytes."
        ),
    ] = None,
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Keep the archive current from Slack's pushed events until Ctrl-C.

    Pulls first, showing its progress as `slarc pull` does, then stores new messages, edits,
    deletes and reactions on tracked items as they happen, printing a line for each.
    """
    services: bootstrap.Services = ctx.obj
    names = workspaces or []
    reporter = LineReporter(services.clock, bootstrap.zone(services.env))
    stopping: list[int] = []

    def stop(signum: int, frame: FrameType | None) -> None:
        if stopping:
            raise KeyboardInterrupt
        stopping.append(signum)
        err.print(STOPPING, markup=False)

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        global_config = read_global_config(services.env, services.home)
        archive_root = _existing_archive(root, services.env, services.home)
        overrides = {"downloads": downloads, "max_file_size_mb": max_file_size}
        settings = load_settings(archive_root, global_config, services.env, overrides)
        options = ListenOptions(
            downloads=settings.downloads,
            max_file_bytes=settings.max_file_size_mb * MB,
            active_thread_days=settings.active_thread_days,
            unheard_pull_minutes=settings.unheard_pull_minutes,
        )
        connector = services.connector(reporter.waiting)
        with (
            bootstrap.archive_stores(archive_root, services.take_ownership) as (archive, blobs),
            services.downloader(blobs, connector.authenticated) as downloader,
        ):
            if not archive.tracked_items():
                _fail(NO_TRACKED_ITEMS)

            def open_uow(command: str) -> UnitOfWork:
                # A catch-up pull and the segment after it can start in the same second.
                suffix = secrets.token_hex(2) if services.run_suffix else ""
                return UnitOfWork(
                    archive, blobs, services.clock, command, names, __version__, suffix, reporter
                )

            listen(
                names,
                options,
                open_uow,
                connector,
                services.event_connector(connector),
                downloader,
                reporter,
                services.clock,
                lambda: bool(stopping),
            )
    except NoSession as error:
        _warn([str(e) for e in services.credentials.errors])
        _fail(str(error))
    except SlarcError as error:
        _fail(str(error))
    except KeyboardInterrupt:
        err.print(INTERRUPTED, markup=False)
        raise typer.Exit(INTERRUPTED_EXIT) from None
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    reporter.info("Stopped.")


@app.command("file")
def file_command(
    ctx: typer.Context,
    selector: Annotated[
        str,
        typer.Argument(
            metavar="FILE", help="A file id such as F0123456789, or the file's Slack link."
        ),
    ],
    out_path: Annotated[
        Path | None,
        typer.Option(
            "--out",
            metavar="PATH",
            help="Write to this file path, creating its directories. A different file "
            "already there is never replaced. Without it the bytes go to stdout.",
        ),
    ] = None,
    root: Annotated[
        Path | None, typer.Option(metavar="DIR", help="The archive directory to use.")
    ] = None,
) -> None:
    """Write one archived file's bytes, such as a PDF or a snippet's full text."""
    services: bootstrap.Services = ctx.obj
    try:
        archive_root = _existing_archive(root, services.env, services.home)
        with bootstrap.query_index(archive_root, services.take_ownership) as index:
            data = file_bytes(index, bootstrap.blob_store(archive_root), selector)
    except FileNotArchived as error:
        err.print(str(error), markup=False)
        raise typer.Exit(NO_MATCH_EXIT) from None
    except SlarcError as error:
        _fail(str(error))
    if out_path is None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    _write_new_file(out_path, data)
    out.print(str(out_path), markup=False, soft_wrap=True)


def _write_new_file(path: Path, data: bytes) -> None:
    """Writes data at path, creating its parents. A file already there with the same bytes is
    left as it is; anything else at path is never replaced."""
    if path.is_dir():
        _fail(f"--out takes a file path; {path} is a directory.")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as file:
                file.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                _fail(f"{path} already exists; remove it or choose another path.")
    except OSError as error:
        _fail(f"Could not write {path}: {error.strerror}.")


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
    return _and_joined(parts)


def _exported_parts(exported: Exported) -> str:
    """What an export holds, such as "1 workspace, 2 threads and 14 messages"."""
    return _and_joined(
        [
            _plural(count, noun)
            for count, noun in (
                (exported.workspaces, "workspace"),
                (exported.conversations, "conversation"),
                (exported.threads, "thread"),
                (exported.messages, "message"),
                (exported.files, "file"),
                (exported.reactions, "reaction"),
            )
            if count
        ]
    )


def _and_joined(parts: list[str]) -> str:
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


@app.command("guide")
def guide_command() -> None:
    """Explain to agents and scripts how to read the archive: recipes, formats, exit codes."""
    sys.stdout.write(files("slarc").joinpath("data/guide.md").read_text(encoding="utf-8"))


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
                removal = plan_removal(parsed, channel, archive, blobs, index, services.clock.now())
            if removal.nothing_to_do:
                err.print(NOTHING_TO_REMOVE, markup=False)
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
