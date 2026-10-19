# ABOUTME: Tests for the export use case over a copy of the recorded archive and its blobs.
# ABOUTME: Pages and assets land in an in-memory export writer, keyed by their output path.
import base64
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.jsonl_store import JsonlArchiveStore
from slarc.domain.envelope import Envelope, Kind, Meta
from slarc.domain.links import parse_selector
from slarc.domain.tracking import DownloadState, RunRecord
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
BROADCAST = "1792437051.005599"
TIM_AVATAR = "087e94c62cad8dfcddba69985fadf8ebfbbf7b4cb40440de4dc5b471ec22b33c"
GIPHY_ICON_72 = "ba81bb4fb2e71c7f50e287551060940989b4f154f392bbf8aa5031ee508191c6"
SEED_RED = "58740087ec4166d3a49129823287f4849866cc82328a5997f6d7ef96fea8800b"
TED = "U0CA538UEN7"
SYNTHETIC = "1792500000.000100"
LATER = "2026-10-20T00:00:00Z"
LATER_RUN = "r_20261020T000000Z"
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


def append_line(
    store: JsonlArchiveStore,
    kind: Kind,
    obj: dict,
    *,
    channel: str | None = None,
    ts: str | None = None,
    id: str | None = None,
    blob: str | None = None,
) -> None:
    meta = Meta(
        kind=kind,
        team=RECORDED_TEAM,
        channel=channel,
        ts=ts,
        id=id,
        fetched_at=LATER,
        run=LATER_RUN,
        hash="synthetic",
        blob=blob,
    )
    store.append(Envelope(meta=meta, object=obj))
    store.flush()


def append_message(store: JsonlArchiveStore, obj: dict) -> None:
    append_line(
        store,
        "message",
        {"type": "message", "ts": SYNTHETIC, **obj},
        channel=FIXTURES,
        ts=SYNTHETIC,
    )


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


def test_broadcast_reply_in_both_places_with_one_anchor(archive, store, blobs) -> None:
    _, writer = run_export(archive, store, blobs, "#slarc-fixtures")
    page = writer.text(OUT / f"{FIXTURES}.html")
    assert page.count("seed: broadcast reply, also sent to the channel") == 2
    assert page.count(f'id="{FIXTURES}-{BROADCAST}"') == 1
    assert "Also sent to the channel" in page
    flow = article(page, FIXTURES, BROADCAST)
    assert "Replied to a thread" in flow


def test_single_file_writes_each_avatar_once(archive, store, blobs) -> None:
    written, writer = run_export(archive, store, blobs, "#all-naleid", single_file=True)
    page = writer.text(written)
    avatar = base64.b64encode(blobs.read(TED_AVATAR)).decode()
    assert page.count(avatar) == 1
    assert page.count(f"blob-{TED_AVATAR}") > 5
    assert f'.blob-{TED_AVATAR}{{background-image:url("data:image/png;base64,' in page
    assert 'role="img" aria-label="Ted Naleid"' in page


def test_every_copied_asset_is_referenced(archive, store, blobs) -> None:
    _, writer = run_export(archive, store, blobs, "#all-naleid", "#slarc-fixtures")
    pages = "".join(writer.text(p) for p in writer.files if p.suffix == ".html")
    assets = [p.name for p in writer.files if p.parent == OUT / "assets"]
    assert assets
    assert [name for name in assets if f"assets/{name}" not in pages] == []
    assert not any(name.startswith(TIM_AVATAR) for name in assets)


def test_thread_export_default_directory_names_the_thread(archive, store, blobs) -> None:
    written, _ = run_export(archive, store, blobs, THREAD_LINK, out=None)
    assert written == Path("slarc-export-all-naleid-1792427542.806979")


def test_bot_avatar_falls_back_to_a_downloaded_icon(archive, store, blobs) -> None:
    append_message(
        store,
        {
            "subtype": "bot_message",
            "bot_id": "B0CA9EBAH1C",
            "text": "a bot post",
            "icons": {"image_48": "https://example.com/not-downloaded.png"},
            "bot_profile": {
                "name": "giphy",
                "icons": {
                    "image_72": "https://a.slack-edge.com/dc483/img/plugins/giphy/service_72.png"
                },
            },
        },
    )
    _, writer = run_export(archive, store, blobs, "#slarc-fixtures")
    message = article(writer.text(OUT / f"{FIXTURES}.html"), FIXTURES, SYNTHETIC)
    assert f'src="assets/{GIPHY_ICON_72}.png"' in message


def test_file_placeholders_and_unsafe_chrome(archive, store, blobs) -> None:
    thumb = "https://example.com/preview.xml"
    append_line(store, "image", {"url": thumb, "size": 153}, id=thumb, blob=SEED_RED)
    store.append_run(
        RunRecord(
            run=LATER_RUN,
            command="pull",
            started=LATER,
            finished=LATER,
            downloads=[
                DownloadState(
                    team_id=RECORDED_TEAM, target_kind="file", target="FBIG", status="too_large"
                )
            ],
        ),
        "0.1.0",
    )
    append_message(
        store,
        {
            "user": TED,
            "text": "synthetic files",
            "files": [
                {"id": "FHIDDEN", "mode": "hidden_by_limit"},
                {
                    "id": "FBIG",
                    "mode": "hosted",
                    "name": "big.mov",
                    "title": "big.mov",
                    "mimetype": "video/quicktime",
                    "size": 999_999_999,
                },
                {
                    "id": "FEXT",
                    "mode": "external",
                    "is_external": True,
                    "title": "Design doc",
                    "external_url": "https://docs.example.com/d",
                },
                {
                    "id": "FXSS",
                    "mode": "hosted",
                    "title": "<script>alert(1)</script>",
                    "mimetype": "text/plain",
                },
            ],
            "attachments": [
                {
                    "color": "red;background:url(x)",
                    "title": "<script>t</script>",
                    "title_link": "javascript:alert(1)",
                    "service_name": "<b>svc</b>",
                    "text": "preview text",
                    "thumb_url": thumb,
                },
                {"color": "good", "title": "named good"},
                {"color": "danger", "title": "named danger"},
            ],
        },
    )
    _, writer = run_export(archive, store, blobs, "#slarc-fixtures")
    message = article(writer.text(OUT / f"{FIXTURES}.html"), FIXTURES, SYNTHETIC)
    assert "This file is hidden by the workspace's message history limit." in message
    assert "big.mov was not downloaded because it is larger than the download limit." in message
    assert 'External file: <a href="https://docs.example.com/d">Design doc</a>' in message
    assert "&lt;script&gt;alert(1)&lt;/script&gt; was not downloaded." in message
    assert "&lt;script&gt;t&lt;/script&gt;" in message
    assert "&lt;b&gt;svc&lt;/b&gt;" in message
    assert "<script>" not in message
    assert "javascript:" not in message
    assert "background:url(x)" not in message
    assert "border-left-color: #2eb886" in message
    assert "border-left-color: #a30200" in message
    assert f'src="assets/{SEED_RED}.bin"' in message
