# ABOUTME: Tests for queueing, draining and recording downloads of files and images during a pull.
# ABOUTME: A fake downloader serves bytes per URL into an in-memory blob store.
import hashlib

import pytest

from slarc import __version__
from slarc.domain.directory import DirectoryView
from slarc.domain.hashing import content_hash
from slarc.domain.tracking import DownloadState
from slarc.ports import AuthFailed, DownloadJob
from slarc.service.downloads import (
    drain_downloads,
    image_urls,
    outstanding_downloads,
    queue_downloads,
)
from slarc.service.pull import PullOptions
from slarc.unit_of_work import UnitOfWork
from tests.service.pull_harness import BASE, CHANNEL, ME, TEAM, TOMBSTONE_FILE, Harness, spike

THREAD_LINK = f"{BASE}/p1792410693663869"
SNIPPET_LINK = f"{BASE}/p1792422552941749"
SNIPPET_TS = "1792422552.941749"
TOMBSTONE_LINK = f"{BASE}/p1792422363271059"
SNIPPET = "F0CA7TKM285"
SNIPPET_URL = "https://files.slack.com/files-pri/T0CB5LYPVFA-F0CA7TKM285/download/10k-snippet.txt"
SNIPPET_SIZE = 228_893
GIPHY_URL = (
    "https://media4.giphy.com/media/F9yAvk7Xpr0c/giphy.gif"
    "?cid=6104955ee4adf136dcbbrjmn0qdsjgxzsga2bbxafvz2c6fe&ep=v1_gifs_translate&rid=giphy.gif&ct=g"
)
GIPHY_LOGO = "https://a.slack-edge.com/dc483/img/plugins/giphy/service_32.png"
GIPHY_ICON_72 = "https://a.slack-edge.com/dc483/img/plugins/giphy/service_72.png"
GIPHY_ICON_48 = "https://a.slack-edge.com/dc483/img/plugins/giphy/service_48.png"
GITHUB_THUMB = "https://avatars.githubusercontent.com/u/5231?v=4?s=400"
GITHUB_ICON = "https://a.slack-edge.com/80588/img/unfurl_icons/github.png"
CUSTOM_EMOJI = "its-true-all-of-it"
CUSTOM_EMOJI_URL = (
    "https://emoji.slack-edge.com/T0CB5LYPVFA/its-true-all-of-it/cdea6811dcb186a7.png"
)


@pytest.fixture
def h() -> Harness:
    return Harness()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snippet_bytes() -> bytes:
    data = b"".join(f"line {n} of the snippet \xe2\x9c\x93\n".encode() for n in range(10_000))
    return (data * 2)[:SNIPPET_SIZE]


def avatar(user_id: str) -> str:
    [user] = [u for u in spike("users.list")["members"] if u["id"] == user_id]
    return user["profile"]["image_512"]


def thread_messages() -> list[dict]:
    return spike("conversations.replies")["messages"]


def run_states(h: Harness, kind: str) -> list[DownloadState]:
    return [d for d in h.last_run().downloads if d.target_kind == kind]


def serve_every_job(h: Harness) -> None:
    for job in h.downloader.jobs:
        h.downloader.serve(job.url, job.url.encode(), "image/png")


def non_run_lines(h: Harness) -> int:
    return sum(1 for env in h.archive.lines if env.meta.kind != "run")


def test_giphy_and_preview_images_queued(h: Harness):
    giphy, preview = thread_messages()[2], thread_messages()[3]

    assert image_urls(giphy) == [GIPHY_URL, GIPHY_LOGO, GIPHY_ICON_72, GIPHY_ICON_48]
    assert image_urls(preview) == [GITHUB_THUMB, GITHUB_ICON]

    h.pull(THREAD_LINK)

    images = {d.target: d for d in run_states(h, "image")}
    for url in [GIPHY_URL, GIPHY_LOGO, GIPHY_ICON_72, GIPHY_ICON_48, GITHUB_THUMB, GITHUB_ICON]:
        assert images[url] == DownloadState(
            team_id=TEAM, target_kind="image", target=url, url=url, status="pending"
        )
    jobs = {job.target: job for job in h.downloader.jobs}
    assert jobs[GIPHY_URL] == DownloadJob(
        TEAM, "image", GIPHY_URL, GIPHY_URL, PullOptions().max_file_bytes, slack_auth=False
    )


def test_custom_emoji_and_avatars_queued(h: Harness):
    h.pull(THREAD_LINK)

    targets = {d.target for d in run_states(h, "image")}
    assert {CUSTOM_EMOJI_URL, avatar(ME), avatar("U0CA61XJF2R")} <= targets
    assert not any("emoji.slack-edge.com" in t and CUSTOM_EMOJI not in t for t in targets)


def test_custom_emoji_in_mrkdwn_text_queued_through_aliases(h: Harness):
    emoji = {
        "teamwork": "https://emoji.slack-edge.com/T/teamwork/a.gif",
        "tw2": "alias:teamwork",
        "tw3": "alias:tw2",
        "status": "https://emoji.slack-edge.com/T/status/b.png",
    }
    view = DirectoryView(users={}, conversations={}, usergroups={}, emoji=emoji, bots={})
    message = {
        "ts": SNIPPET_TS,
        "bot_id": "B1",
        "text": "deployed :tw3: :smile:",
        "attachments": [{"text": "build :status::skin-tone-2:"}],
    }

    with UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__) as uow:
        jobs = queue_downloads(message, TEAM, uow, PullOptions().max_file_bytes, view=view)

    assert [job.target for job in jobs] == [emoji["teamwork"], emoji["status"]]


def test_only_slack_file_urls_carry_auth(h: Harness):
    h.pull(THREAD_LINK)

    auth = {job.target_kind: set() for job in h.downloader.jobs}
    for job in h.downloader.jobs:
        auth[job.target_kind].add(job.slack_auth)
    assert auth == {"file": {True}, "image": {False}}


def test_drain_uses_the_download_parallelism(h: Harness):
    h.pull(THREAD_LINK, options=PullOptions(downloads=7))

    assert h.downloader.parallelism == [7]


def test_snippet_downloaded_and_file_line_gets_blob(h: Harness):
    data = snippet_bytes()
    assert len(data) == SNIPPET_SIZE
    h.downloader.serve(SNIPPET_URL, data, "application/force-download")

    report = h.pull(SNIPPET_LINK)

    info = spike(f"files.info.{SNIPPET}")["file"]
    inline, downloaded = h.lines("file")
    assert (inline.meta.blob, downloaded.meta.blob) == (None, sha256(data))
    assert downloaded.object == info
    assert "content" not in (downloaded.object or {})
    assert (downloaded.meta.id, downloaded.meta.api) == (SNIPPET, "files.info")
    assert downloaded.meta.hash == content_hash("file", info)
    assert h.blobs.read(sha256(data)) == data
    assert ("files.info", SNIPPET) in h.gateway.calls
    [job] = [job for job in h.downloader.jobs if job.target_kind == "file"]
    assert (job.target, job.url, job.slack_auth) == (SNIPPET, SNIPPET_URL, True)
    assert run_states(h, "file") == []
    assert (TEAM, SNIPPET) not in outstanding_downloads(h.archive)
    assert report.downloads_pending == len(run_states(h, "image"))


def test_tombstone_file_marked_deleted_without_fetch(h: Harness):
    h.pull(TOMBSTONE_LINK)

    assert run_states(h, "file") == [
        DownloadState(team_id=TEAM, target_kind="file", target=TOMBSTONE_FILE, status="deleted")
    ]
    assert not [job for job in h.downloader.jobs if job.target_kind == "file"]
    assert ("files.info", TOMBSTONE_FILE) not in h.gateway.calls

    h.pull()
    assert run_states(h, "file") == []


def test_file_deleted_after_download_marked_deleted(h: Harness):
    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    h.gateway.deleted_files.add(SNIPPET)

    h.pull(SNIPPET_LINK)

    [state] = run_states(h, "file")
    assert (state.target, state.status) == (SNIPPET, "deleted")
    assert [env.meta.blob for env in h.lines("file")] == [None]


def test_external_file_not_downloaded(h: Harness):
    external = {
        "id": "F0EXTERNAL1",
        "mode": "external",
        "is_external": True,
        "external_type": "gdrive",
        "url_private": "https://docs.google.com/document/d/abc",
    }
    h.change_message(SNIPPET_TS, files=[external])

    h.pull(SNIPPET_LINK)

    assert run_states(h, "file") == [
        DownloadState(
            team_id=TEAM,
            target_kind="file",
            target="F0EXTERNAL1",
            url="https://docs.google.com/document/d/abc",
            status="external",
        )
    ]
    assert not [job for job in h.downloader.jobs if job.target_kind == "file"]


def test_too_large_recorded_and_listed(h: Harness):
    h.downloader.serve(SNIPPET_URL, snippet_bytes())

    h.pull(SNIPPET_LINK, options=PullOptions(max_file_bytes=100_000))

    [state] = run_states(h, "file")
    assert (state.target, state.status) == (SNIPPET, "too_large")
    assert not [job for job in h.downloader.jobs if job.target_kind == "file"]
    assert outstanding_downloads(h.archive)[(TEAM, SNIPPET)].status == "too_large"

    h.pull()

    assert h.lines("file")[-1].meta.blob == sha256(snippet_bytes())
    assert (TEAM, SNIPPET) not in outstanding_downloads(h.archive)


def test_image_over_the_limit_recorded_as_too_large(h: Harness):
    h.downloader.serve(avatar(ME), b"x" * 2000)

    h.pull(SNIPPET_LINK, options=PullOptions(max_file_bytes=1000))

    [state] = [d for d in run_states(h, "image") if d.target == avatar(ME)]
    assert (state.status, state.attempts) == ("too_large", 1)


def test_failed_download_retried_next_pull(h: Harness):
    h.downloader.fail(SNIPPET_URL, error="HTTP 500")

    report = h.pull(SNIPPET_LINK)

    [state] = run_states(h, "file")
    assert (state.status, state.attempts, state.last_error) == ("failed", 1, "HTTP 500")
    assert report.downloads_pending == len(h.last_run().downloads)

    h.pull()
    [state] = run_states(h, "file")
    assert (state.status, state.attempts) == ("failed", 2)

    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    h.pull()

    assert run_states(h, "file") == []
    assert h.lines("file")[-1].meta.blob == sha256(snippet_bytes())


def test_drain_retries_earlier_runs_queue(h: Harness):
    h.pull(SNIPPET_LINK)
    serve_every_job(h)
    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    h.downloader.jobs.clear()

    with UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__) as uow:
        drained = drain_downloads(
            uow, h.downloader, 4, PullOptions().max_file_bytes, h.connector, h.reporter
        )

    assert drained.pending == 0
    assert {job.target for job in h.downloader.jobs} == {SNIPPET, avatar(ME)}
    assert h.lines("file")[-1].meta.blob == sha256(snippet_bytes())
    assert outstanding_downloads(h.archive) == {}


@pytest.mark.parametrize("failure", ["no_session", "auth_failed"])
def test_drain_keeps_slack_files_queued_when_their_workspace_cannot_connect(
    h: Harness, failure: str
):
    h.pull(SNIPPET_LINK)
    if failure == "no_session":
        h.connector.gateways = {}
    else:
        h.gateway.auth_error = AuthFailed("desktop", "naleidworkspace")
    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    h.downloader.jobs.clear()
    h.reporter.warnings.clear()

    with UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__) as uow:
        drain_downloads(uow, h.downloader, 4, PullOptions().max_file_bytes, h.connector, h.reporter)

    assert SNIPPET not in {job.target for job in h.downloader.jobs}
    assert avatar(ME) in {job.target for job in h.downloader.jobs}
    [snippet] = [state for state in run_states(h, "file") if state.target == SNIPPET]
    assert (snippet.status, snippet.attempts, snippet.last_error) == ("pending", 0, None)
    assert len(h.reporter.warnings) == 1
    assert h.reporter.warnings[0].startswith(f"Could not connect to workspace {TEAM}")


def test_drain_retries_too_large_file_once_it_fits(h: Harness):
    h.pull(SNIPPET_LINK, options=PullOptions(max_file_bytes=100_000))
    h.downloader.serve(SNIPPET_URL, snippet_bytes())

    with UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__) as uow:
        drain_downloads(uow, h.downloader, 4, 100_000, h.connector, h.reporter)
    assert h.lines("file")[-1].meta.blob is None

    with UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__) as uow:
        drain_downloads(uow, h.downloader, 4, 300_000, h.connector, h.reporter)
    assert h.lines("file")[-1].meta.blob == sha256(snippet_bytes())


def test_existing_blob_not_refetched(h: Harness):
    h.pull(THREAD_LINK)
    serve_every_job(h)
    h.pull()
    h.downloader.jobs.clear()

    h.pull()

    assert h.downloader.jobs == []
    assert h.last_run().downloads == []


def test_recorded_blob_missing_from_the_store_is_fetched_again(h: Harness):
    data = snippet_bytes()
    h.downloader.serve(SNIPPET_URL, data)
    h.pull(SNIPPET_LINK)
    del h.blobs.blobs[sha256(data)]
    h.downloader.jobs.clear()

    h.pull()

    assert [job.target for job in h.downloader.jobs if job.target_kind == "file"] == [SNIPPET]
    assert h.blobs.read(sha256(data)) == data


def test_image_download_records_image_emoji_and_user_lines(h: Harness):
    h.downloader.serve(GIPHY_URL, b"GIF89a-giphy", "image/gif")
    h.downloader.serve(CUSTOM_EMOJI_URL, b"emoji-png", "image/png")
    h.downloader.serve(avatar(ME), b"avatar-jpg", "image/jpeg")

    h.pull(THREAD_LINK)

    images = {env.meta.id: env for env in h.lines("image")}
    giphy = images[GIPHY_URL]
    expected = {"url": GIPHY_URL, "content_type": "image/gif", "size": 12}
    assert giphy.object == expected
    assert (giphy.meta.api, giphy.meta.team) == ("download", TEAM)
    assert giphy.meta.blob == sha256(b"GIF89a-giphy")
    assert giphy.meta.hash == content_hash("image", expected)
    assert set(images) == {GIPHY_URL, CUSTOM_EMOJI_URL, avatar(ME)}

    emoji = [env for env in h.lines("emoji") if env.meta.id == CUSTOM_EMOJI]
    assert [env.meta.blob for env in emoji] == [None, sha256(b"emoji-png")]
    assert emoji[0].object == emoji[1].object
    assert emoji[1].meta.hash == emoji[0].meta.hash
    users = [env for env in h.lines("user") if env.meta.id == ME]
    assert [env.meta.blob for env in users] == [None, sha256(b"avatar-jpg")]
    assert users[1].meta.api == "users.list"


def test_pull_download_pull_appends_nothing(h: Harness):
    h.pull(THREAD_LINK)
    serve_every_job(h)
    h.pull()
    assert {env.meta.blob is not None for env in h.lines("file")[-1:]} == {True}
    written = non_run_lines(h)

    h.pull()

    assert non_run_lines(h) == written


def test_downloaded_target_is_not_outstanding(h: Harness):
    h.pull(SNIPPET_LINK)
    assert outstanding_downloads(h.archive)[(TEAM, SNIPPET)].status == "pending"

    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    h.pull()

    assert h.archive.downloads()[(TEAM, SNIPPET)].status == "pending"
    assert (TEAM, SNIPPET) not in outstanding_downloads(h.archive)


def test_edited_snippet_downloaded_again(h: Harness):
    first, second = snippet_bytes(), snippet_bytes()[:200_000]
    h.downloader.serve(SNIPPET_URL, first)
    h.pull(SNIPPET_LINK)
    messages = h.gateway.conversations[CHANNEL]
    [inline] = next(m for m in messages if m["ts"] == SNIPPET_TS)["files"]
    edited = {**inline, "size": len(second), "preview": "edited"}
    h.change_message(SNIPPET_TS, files=[edited])
    h.gateway.files[SNIPPET] = {
        **h.gateway.files[SNIPPET],
        "size": len(second),
        "preview": "edited",
    }
    h.downloader.serve(SNIPPET_URL, second)
    h.downloader.jobs.clear()

    h.pull()

    blobs = [env.meta.blob for env in h.lines("file")]
    assert blobs == [None, sha256(first), None, sha256(second)]
    assert [job.target for job in h.downloader.jobs if job.target_kind == "file"] == [SNIPPET]
    assert h.blobs.read(sha256(second)) == second


def test_malformed_image_url_does_not_fail_the_pull(h: Harness):
    bad = "https://[::1/direct"
    h.change_message(SNIPPET_TS, attachments=[{"image_url": bad}])

    report = h.pull(SNIPPET_LINK)

    [state] = [d for d in h.last_run().downloads if d.target == bad]
    assert state.status == "pending"
    [job] = [job for job in h.downloader.jobs if job.target == bad]
    assert job.slack_auth is False
    assert report.downloads_pending >= 1


def test_drain_reports_progress_before_and_after_each_result(h: Harness):
    h.change_message(SNIPPET_TS, attachments=[{"image_url": GIPHY_URL}])
    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    del h.gateway.files[SNIPPET]
    h.downloader.serve(GIPHY_URL, b"giphy", "image/gif")
    h.downloader.fail(avatar(ME), status="forbidden", error="HTTP 403")

    h.pull(SNIPPET_LINK)

    assert [job.target for job in h.downloader.jobs] == [SNIPPET, GIPHY_URL, avatar(ME)]
    total = len(h.downloader.jobs)
    assert h.reporter.download_updates == [
        (0, 0, 0, total),
        (1, 1, 0, total),
        (2, 1, 0, total),
        (3, 1, 1, total),
    ]


def test_drain_reports_no_progress_without_jobs(h: Harness):
    with UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__) as uow:
        drain_downloads(uow, h.downloader, 4, PullOptions().max_file_bytes, h.connector, h.reporter)

    assert h.reporter.download_updates == []


def test_report_counts_this_runs_saved_and_skipped_downloads(h: Harness):
    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    h.downloader.fail(avatar(ME), status="forbidden", error="HTTP 403")

    first = h.pull(SNIPPET_LINK)

    assert (first.files_saved, first.images_saved) == (1, 0)
    assert (first.downloads_pending, first.downloads_skipped) == (0, 1)

    second = h.pull()

    assert (second.files_saved, second.images_saved) == (0, 0)
    assert (second.downloads_pending, second.downloads_skipped) == (0, 0)


def test_report_counts_too_large_as_skipped_and_images_saved(h: Harness):
    h.downloader.serve(avatar(ME), b"avatar", "image/png")

    report = h.pull(SNIPPET_LINK, options=PullOptions(max_file_bytes=100_000))

    assert (report.files_saved, report.images_saved) == (0, 1)
    assert (report.downloads_pending, report.downloads_skipped) == (0, 1)
