# ABOUTME: Tests for the export use case over a lake of the recorded archive and its blobs.
# ABOUTME: Pages and assets land in an in-memory export writer, keyed by their output path.
import base64
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.lake import extension_directory
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.domain.envelope import Envelope, Kind, Meta
from slarc.domain.links import parse_selector
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem
from slarc.ports import Commit, SlarcError
from slarc.service.export import Exported, export
from tests.fakes import FakeArchiveLock, InMemoryBlobStore, InMemoryExportWriter
from tests.recorded_fixtures import RECORDED_TEAM, recorded_blobs, recorded_lake

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
MESSAGE_LINK = "https://naleidworkspace.slack.com/archives/C0CA5396G03/p1792427577619569"
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
JIM = "U0CA61XJF2R"
ITS_TRUE = "985b5d5aa6b5f64d2822b031a1f94f15ee2f46b5bd46282061e4dd2f0cc722da"
SYNTHETIC = "1792500000.000100"
ALL_NALEID_MESSAGES = 14
ALL_NALEID_FILES = 4
ALL_NALEID_REACTIONS = 6
LATER = "2026-10-20T00:00:00Z"
LATER_RUN = "r_20261020T000000Z"
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+")
THREADS = {
    ALL_NALEID: {
        "1792427542.806979": ["1792427577.619569", "1792427679.271249", "1792427779.502989"],
        "1792410693.663869": ["1792410743.787639", "1792411464.844209", "1792422623.464629"],
    },
    FIXTURES: {
        "1792437051.761119": ["1792437052.019739"],
        "1792437050.459469": ["1792437050.716469", BROADCAST],
        "1792427960.377109": ["1792427974.789599"],
    },
}


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return recorded_lake(tmp_path / "archive")


@pytest.fixture
def store(archive: Path, ext_dir: Path) -> Iterator[DuckLakeArchiveStore]:
    store = DuckLakeArchiveStore(archive, ext_dir, FakeArchiveLock())
    yield store
    store.close()


@pytest.fixture(scope="module")
def blobs() -> InMemoryBlobStore:
    return recorded_blobs()


def run_export(
    archive: Path,
    store: DuckLakeArchiveStore,
    blobs: InMemoryBlobStore,
    *selectors: str,
    out: Path | None = OUT,
    single_file: bool = False,
    include_deleted: bool = False,
    tz: str | None = None,
) -> tuple[Path, InMemoryExportWriter]:
    exported, writer = counted_export(
        archive,
        store,
        blobs,
        *selectors,
        out=out,
        single_file=single_file,
        include_deleted=include_deleted,
        tz=tz,
    )
    return exported.path, writer


def counted_export(
    archive: Path,
    store: DuckLakeArchiveStore,
    blobs: InMemoryBlobStore,
    *selectors: str,
    out: Path | None = OUT,
    single_file: bool = False,
    include_deleted: bool = False,
    tz: str | None = None,
) -> tuple[Exported, InMemoryExportWriter]:
    writer = InMemoryExportWriter()
    parsed = [parse_selector(text) for text in selectors]
    ext_dir = extension_directory(os.environ, Path.home())
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        exported = export(
            parsed, out, single_file, include_deleted, tz, index, blobs, store, writer
        )
    return exported, writer


def append_line(
    store: DuckLakeArchiveStore,
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
    commit(store)


def commit(store: DuckLakeArchiveStore, *tracked: TrackedItem) -> None:
    """Commits the store's open batch with the tracked items, then a run."""
    store.commit(Commit(items=list(tracked)))
    store.append_run(
        RunRecord(run=LATER_RUN, command="pull", started=LATER, finished=LATER), "0.1.0"
    )


def append_message(store: DuckLakeArchiveStore, obj: dict) -> None:
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


def panel(page: str, channel: str, ts: str) -> str:
    """The HTML of one thread's panel."""
    start = page.index(f'<aside class="thread-panel" id="thread-{channel}-{ts}"')
    return page[start : page.index("</aside>", start)]


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
    assert f'href="#thread-{ALL_NALEID}-1792427542.806979"' in parent
    assert "3 replies" in parent
    assert f'id="{ALL_NALEID}-1792427577.619569"' in page
    assert "reply from Jim" in page
    assert (OUT / "viewer.css") in writer.files
    assert (OUT / "viewer.js") in writer.files


def test_export_counts_what_it_wrote(archive, store, blobs) -> None:
    exported, _ = counted_export(archive, store, blobs, "#all-naleid")

    assert exported == Exported(
        path=OUT,
        workspaces=1,
        conversations=1,
        threads=len(THREADS[ALL_NALEID]),
        messages=ALL_NALEID_MESSAGES,
        files=ALL_NALEID_FILES,
        reactions=ALL_NALEID_REACTIONS,
    )


def test_export_counts_a_thread_on_its_own_page(archive, store, blobs) -> None:
    exported, _ = counted_export(archive, store, blobs, THREAD_LINK)

    assert (exported.conversations, exported.threads, exported.messages) == (0, 1, 4)


def test_export_counts_a_message_on_two_pages_once(archive, store, blobs) -> None:
    alone, _ = counted_export(archive, store, blobs, "#all-naleid")
    both, _ = counted_export(archive, store, blobs, "#all-naleid", THREAD_LINK)

    assert both == alone


def test_export_counts_each_workspace_page(archive, store, blobs) -> None:
    exported, _ = counted_export(archive, store, blobs, "naleidworkspace")

    assert exported.workspaces == 1
    assert exported.conversations >= 3
    assert exported.threads >= len(THREADS[ALL_NALEID]) + len(THREADS[FIXTURES])


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


@pytest.mark.parametrize("channel", [ALL_NALEID, FIXTURES])
def test_thread_summary_links_to_its_panel(archive, store, blobs, channel) -> None:
    _, writer = run_export(archive, store, blobs, "#all-naleid", "#slarc-fixtures")
    page = writer.text(OUT / f"{channel}.html")
    assert '<details class="thread"' not in page
    assert page.count('<aside class="thread-panel"') == len(THREADS[channel])
    for parent in THREADS[channel]:
        flow = article(page, channel, parent)
        assert f'<a class="thread-summary" href="#thread-{channel}-{parent}">' in flow
        assert page.count(f'id="thread-{channel}-{parent}"') == 1


@pytest.mark.parametrize("channel", [ALL_NALEID, FIXTURES])
def test_thread_panel_holds_parent_and_each_reply_once(archive, store, blobs, channel) -> None:
    _, writer = run_export(archive, store, blobs, "#all-naleid", "#slarc-fixtures")
    page = writer.text(OUT / f"{channel}.html")
    for parent, replies in THREADS[channel].items():
        html = panel(page, channel, parent)
        assert html.count('<article class="message') == 1 + len(replies)
        assert html.count(f'data-ts="{parent}"') == 1
        assert f'id="{channel}-{parent}-thread"' in html
        assert 'class="thread-close" href="#thread-closed"' in html
        for reply in replies:
            assert html.count(f'data-ts="{reply}"') == 1
            suffix = "-thread" if reply == BROADCAST else ""
            assert f'id="{channel}-{reply}{suffix}"' in html
    ids = re.findall(r' id="([^"]+)"', page)
    assert len(ids) == len(set(ids))
    assert "thread-closed" not in ids


def test_broadcast_reply_links_to_its_thread_panel(archive, store, blobs) -> None:
    _, writer = run_export(archive, store, blobs, "#slarc-fixtures")
    page = writer.text(OUT / f"{FIXTURES}.html")
    flow = article(page, FIXTURES, BROADCAST)
    assert f'<a href="#thread-{FIXTURES}-1792437050.459469">view the thread</a>' in flow


@pytest.mark.parametrize("link", [THREAD_LINK, MESSAGE_LINK])
def test_thread_export_shows_replies_without_a_panel(archive, store, blobs, link) -> None:
    _, writer = run_export(archive, store, blobs, link)
    page = writer.text(OUT / f"{ALL_NALEID}-1792427542.806979.html")
    assert "thread-panel" not in page.split("<body", 1)[1]
    assert "<details" not in page
    assert '<a class="thread-summary"' not in page
    for reply in THREADS[ALL_NALEID]["1792427542.806979"]:
        assert page.count(f'id="{ALL_NALEID}-{reply}"') == 1
    assert "3 replies" in page


def test_single_file_holds_the_panel_css(archive, store, blobs) -> None:
    written, writer = run_export(archive, store, blobs, "#all-naleid", single_file=True)
    page = writer.text(written)
    assert ".thread-panel:target" in page
    assert ".thread-panel:has(:target)" in page
    assert page.count('<aside class="thread-panel"') == 2


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
    commit(store)
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


def reactions_html(message: str) -> str:
    """The reactions row of one message's HTML."""
    start = message.index('<div class="reactions">')
    return message[start : message.index("</div>", start)]


def test_reactions_grouped_by_base_name(archive, store, blobs) -> None:
    _, writer = run_export(archive, store, blobs, "#all-naleid")
    message = article(writer.text(OUT / f"{ALL_NALEID}.html"), ALL_NALEID, "1792410693.663869")
    reactions = reactions_html(message)
    assert reactions.count('<span class="reaction"') == 1
    assert "\U0001f44b\U0001f3fc" in reactions
    assert reactions.count('class="emoji"') == 4
    assert '<span class="count">2</span>' in reactions


def test_reaction_popup_names_each_variant(archive, store, blobs) -> None:
    _, writer = run_export(archive, store, blobs, "#all-naleid")
    message = article(writer.text(OUT / f"{ALL_NALEID}.html"), ALL_NALEID, "1792410693.663869")
    reactions = reactions_html(message)
    toned = "Ted Naleid reacted with :wave::skin-tone-3:"
    plain = "Jim reacted with :wave:"
    assert f'<span class="reaction-popup-line">{toned}</span>' in reactions
    assert f'<span class="reaction-popup-line">{plain}</span>' in reactions
    assert f'tabindex="0" role="img" aria-label="{toned}. {plain}"' in reactions
    assert (
        '<span class="reaction-popup" aria-hidden="true"><span class="reaction-popup-card">'
        '<span class="reaction-popup-emoji">'
    ) in reactions
    assert "title=" not in reactions


def test_reaction_popup_joins_names_and_shows_custom_emoji(archive, store, blobs) -> None:
    _, writer = run_export(archive, store, blobs, "#all-naleid")
    message = article(writer.text(OUT / f"{ALL_NALEID}.html"), ALL_NALEID, "1792410743.787639")
    reactions = reactions_html(message)
    assert reactions.count('<span class="reaction"') == 2
    assert "Jim reacted with :100:" in reactions
    assert "Jim and Ted Naleid reacted with :its-true-all-of-it:" in reactions
    assert (
        '<span class="reaction-popup-emoji"><img class="emoji" '
        f'src="assets/{ITS_TRUE}.png" alt=":its-true-all-of-it:"></span>'
    ) in reactions


def test_reaction_popup_counts_others_and_escapes_names(archive, store, blobs) -> None:
    append_line(
        store, "user", {"id": "UXSS", "profile": {"display_name": "<script>x</script>"}}, id="UXSS"
    )
    append_message(
        store,
        {
            "user": TED,
            "text": ":tada: synthetic reactions",
            "reactions": [
                {"name": "eyes", "users": [TED, JIM, "UXSS"], "count": 5},
                {"name": "tada", "users": [TED], "count": 2},
            ],
        },
    )
    _, writer = run_export(archive, store, blobs, "#slarc-fixtures")
    message = article(writer.text(OUT / f"{FIXTURES}.html"), FIXTURES, SYNTHETIC)
    reactions = reactions_html(message)
    eyes = "Ted Naleid, Jim, &lt;script&gt;x&lt;/script&gt; and 2 others reacted with :eyes:"
    assert f'<span class="reaction-popup-line">{eyes}</span>' in reactions
    assert f'aria-label="{eyes}"' in reactions
    assert "Ted Naleid and 1 other reacted with :tada:" in reactions
    assert "<script>" not in message
    assert '<span class="emoji" title=":tada:">' in message


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
    too_large = DownloadState(
        team_id=RECORDED_TEAM, target_kind="file", target="FBIG", status="too_large"
    )
    store.commit(Commit(downloads=[too_large]))
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


def html_pages(writer: InMemoryExportWriter) -> set[str]:
    return {p.name for p in writer.files if p.suffix == ".html" and p.name != "index.html"}


def test_workspace_exports_one_page_per_tracked_conversation(archive, store, blobs) -> None:
    _, writer = run_export(archive, store, blobs, "naleidworkspace")
    assert html_pages(writer) == {
        f"{channel}.html" for channel in (ALL_NALEID, FIXTURES, DM, GROUP_DM, ARCHIVED)
    }


def test_workspace_export_adds_no_page_for_a_thread_in_a_tracked_conversation(
    archive, store, blobs
) -> None:
    commit(
        store,
        TrackedItem(
            team_id=RECORDED_TEAM, kind="thread", channel=ALL_NALEID, ts="1792427542.806979"
        ),
    )
    _, writer = run_export(archive, store, blobs, "naleidworkspace")
    assert not any("-" in name for name in html_pages(writer))


def test_workspace_export_adds_a_page_for_a_thread_in_an_untracked_conversation(
    archive, store, blobs
) -> None:
    thread_ts = "1792500000.000200"
    append_line(
        store,
        "message",
        {"type": "message", "ts": thread_ts, "user": TED, "text": "lone thread"},
        channel="C0THREADONLY",
        ts=thread_ts,
    )
    commit(
        store,
        TrackedItem(team_id=RECORDED_TEAM, kind="thread", channel="C0THREADONLY", ts=thread_ts),
    )
    _, writer = run_export(archive, store, blobs, "naleidworkspace")
    assert f"C0THREADONLY-{thread_ts}.html" in html_pages(writer)


def test_workspace_export_skips_an_item_without_messages(archive, store, blobs) -> None:
    commit(store, TrackedItem(team_id=RECORDED_TEAM, kind="conversation", channel="C0NOMESSAGE"))
    _, writer = run_export(archive, store, blobs, "naleidworkspace")
    assert "C0NOMESSAGE.html" not in html_pages(writer)
    assert len(html_pages(writer)) == 5


def test_workspace_export_default_directory_names_the_workspace(archive, store, blobs) -> None:
    written, _ = run_export(archive, store, blobs, "naleidworkspace", out=None)
    assert written == Path("slarc-export-naleidworkspace")
    single, _ = run_export(archive, store, blobs, "naleidworkspace", out=None, single_file=True)
    assert single == Path("slarc-export-naleidworkspace.html")


def test_unknown_workspace_lists_the_workspaces_the_archive_holds(archive, store, blobs) -> None:
    message = "This archive holds no workspace acme. Workspaces here: naleidworkspace."
    with pytest.raises(SlarcError) as caught:
        run_export(archive, store, blobs, "acme")
    assert str(caught.value) == message


def test_workspace_holding_nothing_exportable_says_to_pull(archive, store, blobs) -> None:
    for channel in store.channels(RECORDED_TEAM):
        store.drop_messages(
            RECORDED_TEAM, channel, set(store.stored_messages(RECORDED_TEAM, channel))
        )
    store.commit(Commit())
    with pytest.raises(SlarcError) as caught:
        run_export(archive, store, blobs, "naleidworkspace")
    assert str(caught.value) == (
        "The archive holds no messages for workspace naleidworkspace. Pull it first."
    )
