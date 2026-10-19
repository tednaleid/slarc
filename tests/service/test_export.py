# ABOUTME: Tests for the export use case over a copy of the recorded archive and its blobs.
# ABOUTME: Pages and assets land in an in-memory export writer, keyed by their output path.
import base64
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.jsonl_store import JsonlArchiveStore
from slarc.domain.envelope import Envelope, Meta
from slarc.domain.links import parse_selector
from slarc.service.export import export
from tests.fakes import InMemoryBlobStore, InMemoryExportWriter
from tests.recorded_fixtures import RECORDED_TEAM, copy_recorded, recorded_blobs

OUT = Path("exported")
ALL_NALEID = "C0CA5396G03"
FIXTURES = "C0CA7SNHTSP"
DM = "D0CAD7JE1QR"
GROUP_DM = "C0CAG251117"
ARCHIVED = "C0CAF9YVDMY"
THREAD_LINK = (
    "https://naleidworkspace.slack.com/archives/C0CA5396G03/p1792427577619569"
    "?thread_ts=1792427542.806979"
)
SCREENSHOT = "ca3f1d09a38b3f0dc102411b3c732a8cabc6cdb275e88457417ae32d098578fd"
GIPHY = "99e5f805f84886ded9071cb7680b7846fa6b634d74eae01e15fc4c322d7d807f"
TEAMWORK = "851a83125d6f54e37eedeab4e47e2502b05f07e48295870df540743c84749b04"
TED_AVATAR = "1f367ba1455a68c9828dad535ff2fc69a875c75712404ab79a3bca54b4a7c319"
TEN_K_SNIPPET = "72e62bcee33d30d5fffe5178c6b982bc9670abbbe1d99665dd572de094af20b4"
PINNED = "1792437054.532049"
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+")


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return copy_recorded(tmp_path / "archive")


@pytest.fixture
def store(archive: Path) -> Iterator[JsonlArchiveStore]:
    store = JsonlArchiveStore(archive)
    yield store
    store.close()


@pytest.fixture(scope="module")
def blobs() -> InMemoryBlobStore:
    return recorded_blobs()


def run_export(
    archive: Path,
    store: JsonlArchiveStore,
    blobs: InMemoryBlobStore,
    *selectors: str,
    out: Path | None = OUT,
    single_file: bool = False,
    include_deleted: bool = False,
    tz: str | None = None,
) -> tuple[Path, InMemoryExportWriter]:
    writer = InMemoryExportWriter()
    parsed = [parse_selector(text) for text in selectors]
    with DuckDbQueryIndex(archive) as index:
        written = export(parsed, out, single_file, include_deleted, tz, index, blobs, store, writer)
    return written, writer


def article(page: str, channel: str, ts: str) -> str:
    """The HTML of one message, from its anchor up to the next message anchor."""
    start = page.index(f'id="{channel}-{ts}"')
    following = page.find('<article class="message', start)
    return page[start : following if following != -1 else len(page)]


def test_export_conversation_writes_index_and_page(archive, store, blobs) -> None:
    written, writer = run_export(archive, store, blobs, "#all-naleid")
    assert written == OUT
    pages = {path for path in writer.files if path.suffix == ".html"}
    assert pages == {OUT / "index.html", OUT / f"{ALL_NALEID}.html"}
    index = writer.text(OUT / "index.html")
    assert f'href="{ALL_NALEID}.html"' in index
    assert "#all-naleid" in index
    page = writer.text(OUT / f"{ALL_NALEID}.html")
    assert "<title>#all-naleid</title>" in page
    assert 'class="day-divider"' in page
    assert '<time datetime="2026-10-19T' in page
    parent = article(page, ALL_NALEID, "1792427542.806979")
    assert "<details" in parent
    assert "3 replies" in parent
    assert f'id="{ALL_NALEID}-1792427577.619569"' in page
    assert "reply from Jim" in page
    assert (OUT / "viewer.css") in writer.files
    assert (OUT / "viewer.js") in writer.files


def test_export_thread_link_writes_only_thread(archive, store, blobs) -> None:
    written, writer = run_export(archive, store, blobs, THREAD_LINK)
    pages = [path for path in writer.files if path.suffix == ".html"]
    thread_page = OUT / f"{ALL_NALEID}-1792427542.806979.html"
    assert sorted(pages) == sorted([OUT / "index.html", thread_page])
    page = writer.text(thread_page)
    assert "starting a" in page
    assert "reply from Jim" in page
    assert "alias:" in page
    assert "Hello!" not in page
    assert "has joined the channel" not in page
    assert written == OUT


def test_assets_copied_by_hash(archive, store, blobs) -> None:
    _, writer = run_export(archive, store, blobs, "#all-naleid")
    page = writer.text(OUT / f"{ALL_NALEID}.html")
    for sha, ext in (
        (SCREENSHOT, "png"),
        (GIPHY, "gif"),
        (TEAMWORK, "gif"),
        (TED_AVATAR, "png"),
    ):
        path = OUT / "assets" / f"{sha}.{ext}"
        assert writer.files[path] == blobs.read(sha)
        assert f'src="assets/{sha}.{ext}"' in page
    assets = {path.name for path in writer.files if path.parent == OUT / "assets"}
    assert f"{TEN_K_SNIPPET}.txt" not in assets
    assert 'src="http' not in page


def test_single_file_inlines_assets(archive, store, blobs) -> None:
    written, writer = run_export(
        archive, store, blobs, "#all-naleid", "#slarc-fixtures", single_file=True
    )
    assert written == Path("exported.html")
    assert list(writer.files) == [written]
    page = writer.text(written)
    gif = base64.b64encode(blobs.read(TEAMWORK)).decode()
    assert f'src="data:image/gif;base64,{gif}"' in page
    assert 'src="assets/' not in page
    assert "<style>" in page
    assert "<script>" in page
    assert 'href="viewer.css"' not in page
    assert "reply from Jim" in page
    assert "seed: three images" in page


def test_single_file_default_name_comes_from_conversation(archive, store, blobs) -> None:
    written, _ = run_export(archive, store, blobs, "#all-naleid", out=None, single_file=True)
    assert written == Path("slarc-export-all-naleid.html")
    folder, _ = run_export(archive, store, blobs, "@jim", out=None)
    assert folder == Path("slarc-export-Jim")


def test_deleted_hidden_unless_flag(archive, store, blobs) -> None:
    store.append(
        Envelope(
            meta=Meta(
                kind="deleted",
                team=RECORDED_TEAM,
                channel=FIXTURES,
                ts=PINNED,
                fetched_at="2026-10-20T00:00:00Z",
                run="r_20261020T000000Z",
                slarc="0.1.0",
            )
        )
    )
    store.flush()
    _, hidden = run_export(archive, store, blobs, "#slarc-fixtures")
    page = hidden.text(OUT / f"{FIXTURES}.html")
    assert "seed: pinned message" not in page
    assert "seed: reactions" in page
    _, shown = run_export(archive, store, blobs, "#slarc-fixtures", include_deleted=True)
    message = article(shown.text(OUT / f"{FIXTURES}.html"), FIXTURES, PINNED)
    assert "seed: pinned message" in message
    assert 'class="deleted-marker"' in message


def test_no_email_in_output(archive, store, blobs) -> None:
    selectors = ("#all-naleid", "#slarc-fixtures", ARCHIVED, GROUP_DM, "@jim")
    _, folder = run_export(archive, store, blobs, *selectors)
    _, single = run_export(archive, store, blobs, *selectors, single_file=True)
    texts = [
        data.decode("utf-8")
        for path, data in (folder.files | single.files).items()
        if path.suffix in (".html", ".css", ".js")
    ]
    assert len(texts) == 9
    for text in texts:
        assert "example.invalid" not in text
        assert EMAIL.search(text) is None


def test_snippet_rendered_in_full_and_highlighted(archive, store, blobs) -> None:
    _, writer = run_export(archive, store, blobs, "#all-naleid", "#slarc-fixtures")
    page = writer.text(OUT / f"{ALL_NALEID}.html")
    snippet = article(page, ALL_NALEID, "1792422552.941749")
    assert "10k-snippet.txt" in snippet
    assert 'class="highlight"' in snippet
    lines = blobs.read(TEN_K_SNIPPET).decode().splitlines()
    assert len(lines) == 10000
    assert lines[-1] in snippet
    assert lines[0] in snippet
    html_snippet = article(writer.text(OUT / f"{FIXTURES}.html"), FIXTURES, "1792437058.938609")
    assert "seed.html" in html_snippet
    assert '<span class="nt">title</span>' in html_snippet
    assert "<title>slarc seed" not in html_snippet


def test_tombstone_file_placeholder(archive, store, blobs) -> None:
    _, writer = run_export(archive, store, blobs, "#all-naleid")
    message = article(writer.text(OUT / f"{ALL_NALEID}.html"), ALL_NALEID, "1792422363.271059")
    assert "This file was deleted." in message
    assert "(edited)" in message


def test_reactions_grouped_by_base_name(archive, store, blobs) -> None:
    _, writer = run_export(archive, store, blobs, "#all-naleid")
    message = article(writer.text(OUT / f"{ALL_NALEID}.html"), ALL_NALEID, "1792410693.663869")
    reactions = re.findall(
        r'<span class="reaction" title="([^"]*)">(.*?<span class="count">\d+</span>)</span>',
        message,
    )
    assert len(reactions) == 1
    title, content = reactions[0]
    assert "Jim" in title
    assert "Ted Naleid" in title
    assert ":wave:" in title
    assert ":wave::skin-tone-3:" in title
    assert "\U0001f44b\U0001f3fc" in content
    assert content.count('class="emoji"') == 2
    assert '<span class="count">2</span>' in content


def test_named_dm_included(archive, store, blobs) -> None:
    written, writer = run_export(archive, store, blobs, "@jim", GROUP_DM)
    pages = {path for path in writer.files if path.suffix == ".html"}
    assert pages == {OUT / "index.html", OUT / f"{DM}.html", OUT / f"{GROUP_DM}.html"}
    dm = writer.text(OUT / f"{DM}.html")
    assert "<title>@Jim</title>" in dm
    assert "Direct message to Jim from Ted" in dm
    assert "Reply from Jim" in writer.text(OUT / f"{GROUP_DM}.html")
    assert written == OUT
