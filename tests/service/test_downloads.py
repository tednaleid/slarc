# ABOUTME: Tests for queueing, draining and recording downloads of files and images during a pull.
# ABOUTME: A fake downloader serves bytes per URL into an in-memory blob store.
import hashlib
from collections.abc import Iterator

import pytest

from slarc import __version__
from slarc.domain.directory import DirectoryView
from slarc.domain.envelope import Kind
from slarc.domain.hashing import content_hash
from slarc.domain.tracking import DownloadState
from slarc.ports import (
    AuthFailed,
    Commit,
    CommitConflict,
    DirectoryState,
    DownloadJob,
    DownloadResult,
)
from slarc.service.downloads import (
    drain_downloads,
    image_types,
    image_urls,
    outstanding_downloads,
    queue_downloads,
)
from slarc.service.pull import PullOptions
from slarc.unit_of_work import UnitOfWork
from tests.fakes import FailedStatus
from tests.service.pull_harness import BASE, CHANNEL, ME, TEAM, TOMBSTONE_FILE, Harness, spike
from tests.service.test_pull_commits import remove_workspace

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
    return [d for d in h.download_states() if d.target_kind == kind]


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
    assert not [job for job in h.downloader.jobs if job.target_kind == "file"]


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
    assert report.downloads_pending == len(h.download_states())

    h.pull()
    [state] = run_states(h, "file")
    assert (state.status, state.attempts) == ("failed", 2)

    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    h.pull()

    assert run_states(h, "file") == []
    assert h.lines("file")[-1].meta.blob == sha256(snippet_bytes())


def image_status(h: Harness, url: str) -> tuple[str, int]:
    state = outstanding_downloads(h.archive)[(TEAM, url)]
    return state.status, state.attempts


def test_outside_image_gives_up_after_three_failures(h: Harness):
    h.downloader.fail(GIPHY_URL, error="timed out")

    statuses = []
    h.pull(THREAD_LINK)
    statuses.append(image_status(h, GIPHY_URL))
    for _ in range(2):
        h.pull()
        statuses.append(image_status(h, GIPHY_URL))

    assert statuses == [("failed", 1), ("failed", 2), ("gave_up", 3)]
    h.downloader.jobs.clear()
    h.pull()
    assert GIPHY_URL not in {job.target for job in h.downloader.jobs}
    assert image_status(h, GIPHY_URL) == ("gave_up", 3)


def test_gave_up_counts_as_skipped(h: Harness):
    h.downloader.fail(GIPHY_URL, error="timed out")
    for _ in range(3):
        h.pull(THREAD_LINK)

    assert h.reporter.download_updates[-1][2] >= 1


def test_slack_file_failures_never_give_up(h: Harness):
    h.downloader.fail(SNIPPET_URL, error="HTTP 500")

    h.pull(SNIPPET_LINK)
    for _ in range(4):
        h.pull()

    assert image_status(h, SNIPPET) == ("failed", 5)


@pytest.mark.parametrize(
    ("status", "error"),
    [("forbidden", "HTTP 403 Forbidden"), ("not_found", "HTTP 404 Not Found")],
)
def test_full_pull_retries_given_up_forbidden_and_not_found_outside_images(
    h: Harness, status: FailedStatus, error: str
):
    h.downloader.fail(GIPHY_URL, error="timed out")
    h.downloader.fail(GITHUB_THUMB, status=status, error=error)
    h.pull(THREAD_LINK)
    h.pull()
    h.pull()
    assert image_status(h, GIPHY_URL) == ("gave_up", 3)
    assert image_status(h, GITHUB_THUMB) == (status, 1)

    h.downloader.jobs.clear()
    h.pull(options=PullOptions(full=True))

    retried = {job.target for job in h.downloader.jobs}
    assert {GIPHY_URL, GITHUB_THUMB} <= retried
    assert image_status(h, GIPHY_URL) == ("failed", 1)
    assert image_status(h, GITHUB_THUMB) == (status, 1)


def test_a_retry_not_tried_stays_queued_with_its_attempts(h: Harness):
    h.downloader.fail(GIPHY_URL, error="timed out")
    h.pull(THREAD_LINK)
    assert image_status(h, GIPHY_URL) == ("failed", 1)
    held = "Not tried: media4.giphy.com asked to wait 900s"
    h.downloader.fail(GIPHY_URL, status="not_tried", error=held)

    h.pull()

    state = outstanding_downloads(h.archive)[(TEAM, GIPHY_URL)]
    assert (state.status, state.attempts, state.last_error) == ("pending", 1, held)


def test_a_first_download_not_tried_stays_pending(h: Harness):
    held = "Not tried: media4.giphy.com asked to wait 900s"
    h.downloader.fail(GIPHY_URL, status="not_tried", error=held)

    report = h.pull(THREAD_LINK)

    state = outstanding_downloads(h.archive)[(TEAM, GIPHY_URL)]
    assert (state.status, state.attempts, state.last_error) == ("pending", 0, held)
    assert report.downloads_pending >= 1


def test_retry_skipped_queues_given_up_and_not_found_images_afresh(h: Harness):
    h.downloader.fail(GIPHY_URL, error="timed out")
    h.downloader.fail(GITHUB_THUMB, status="not_found", error="HTTP 404 Not Found")
    h.pull(THREAD_LINK)
    h.pull()
    h.pull()
    assert image_status(h, GIPHY_URL) == ("gave_up", 3)
    assert image_status(h, GITHUB_THUMB) == ("not_found", 1)
    h.downloader.fail(GIPHY_URL, error="timed out")
    h.downloader.serve(GITHUB_THUMB, b"thumb", "image/png")

    h.pull(options=PullOptions(retry_skipped=True))

    assert image_status(h, GIPHY_URL) == ("failed", 1)
    assert (TEAM, GITHUB_THUMB) not in outstanding_downloads(h.archive)


def test_retry_skipped_retries_only_what_the_selectors_name(h: Harness):
    h.downloader.fail(GIPHY_URL, error="timed out")
    h.downloader.fail(SNIPPET_URL, status="forbidden", error="HTTP 403 Forbidden")
    h.pull(THREAD_LINK, SNIPPET_LINK)
    h.pull()
    h.pull()
    assert image_status(h, GIPHY_URL) == ("gave_up", 3)
    assert image_status(h, SNIPPET) == ("forbidden", 1)
    h.downloader.serve(GIPHY_URL, b"gif", "image/gif")
    h.downloader.serve(SNIPPET_URL, snippet_bytes(), "text/plain")

    h.pull(SNIPPET_LINK, options=PullOptions(retry_skipped=True))

    assert (TEAM, SNIPPET) not in outstanding_downloads(h.archive)
    assert image_status(h, GIPHY_URL) == ("gave_up", 3)


def test_retry_skipped_retries_an_image_that_was_too_large(h: Harness):
    h.downloader.serve(GIPHY_URL, b"x" * 100, "image/gif")
    h.pull(THREAD_LINK, options=PullOptions(max_file_bytes=10))
    assert image_status(h, GIPHY_URL) == ("too_large", 1)

    h.pull(options=PullOptions(retry_skipped=True))

    assert (TEAM, GIPHY_URL) not in outstanding_downloads(h.archive)


def test_without_retry_skipped_skipped_downloads_stay_skipped(h: Harness):
    h.downloader.fail(GITHUB_THUMB, status="not_found", error="HTTP 404 Not Found")
    h.pull(THREAD_LINK)
    h.downloader.serve(GITHUB_THUMB, b"thumb", "image/png")
    h.downloader.jobs.clear()

    h.pull()

    assert GITHUB_THUMB not in {job.target for job in h.downloader.jobs}
    assert image_status(h, GITHUB_THUMB) == ("not_found", 1)


@pytest.mark.parametrize(
    "shown",
    [
        {"files": [{"id": SNIPPET, "url_private_download": SNIPPET_URL, "size": 10}]},
        {"attachments": [{"image_url": GIPHY_URL}]},
    ],
)
def test_targets_found_downloaded_must_still_be_stored_at_the_next_commit(h: Harness, shown: dict):
    blob, _ = h.blobs.write([b"stored bytes"])
    stored = DirectoryState(hash="h1", blob=blob)
    message = {"ts": SNIPPET_TS, **shown}

    with UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__) as uow:
        jobs = queue_downloads(
            message,
            TEAM,
            uow,
            PullOptions().max_file_bytes,
            files={SNIPPET: stored},
            images={GIPHY_URL: stored},
            known={},
        )
        with pytest.raises(CommitConflict):
            uow.commit()

    assert jobs == []


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
    assert h.download_states() == []


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


def test_downloaded_state_row_is_deleted(h: Harness):
    h.pull(SNIPPET_LINK)
    assert h.archive.downloads()[(TEAM, SNIPPET)].status == "pending"

    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    h.pull()

    assert (TEAM, SNIPPET) not in h.archive.downloads()


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

    [state] = [d for d in h.download_states() if d.target == bad]
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


def test_drain_commits_each_result(h: Harness, monkeypatch: pytest.MonkeyPatch):
    h.change_message(SNIPPET_TS, attachments=[{"image_url": GIPHY_URL}])
    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    h.downloader.serve(GIPHY_URL, b"giphy", "image/gif")
    h.downloader.serve(avatar(ME), b"avatar", "image/png")
    fetch = h.downloader.fetch

    def interrupted_after_two(
        jobs: list[DownloadJob], parallelism: int
    ) -> Iterator[DownloadResult]:
        results = list(fetch(jobs, parallelism))
        yield from results[:2]
        raise KeyboardInterrupt

    monkeypatch.setattr(h.downloader, "fetch", interrupted_after_two)

    with pytest.raises(KeyboardInterrupt):
        h.pull(SNIPPET_LINK)

    assert [job.target for job in h.downloader.jobs] == [SNIPPET, GIPHY_URL, avatar(ME)]
    assert h.lines("file")[-1].meta.blob == sha256(snippet_bytes())
    images = h.archive.directory_state(TEAM, "image")
    assert images[GIPHY_URL].blob == sha256(b"giphy")
    assert avatar(ME) not in images
    assert [(key, state.status) for key, state in h.archive.downloads().items()] == [
        ((TEAM, avatar(ME)), "pending")
    ]


@pytest.mark.parametrize("idle", [True, False])
def test_result_for_a_removed_target_is_dropped(
    h: Harness, monkeypatch: pytest.MonkeyPatch, idle: bool
):
    """The dropped result's blob is unreferenced, so the run's maintenance deletes it if no
    other command has the archive open."""
    h.archive.idle = idle
    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    fetch = h.downloader.fetch

    def rm_drops_the_snippet_first(
        jobs: list[DownloadJob], parallelism: int
    ) -> Iterator[DownloadResult]:
        for result in fetch(jobs, parallelism):
            if result.job.target == SNIPPET:
                with h.archive.elsewhere() as archive:
                    archive.drop_downloads(TEAM, {SNIPPET})
                    archive.commit(Commit())
            yield result

    monkeypatch.setattr(h.downloader, "fetch", rm_drops_the_snippet_first)

    report = h.pull(SNIPPET_LINK)

    assert [env.meta.blob for env in h.lines("file")] == [None]
    assert (TEAM, SNIPPET) not in h.archive.downloads()
    assert run_states(h, "file") == []
    assert report.files_saved == 0
    assert h.blobs.exists(sha256(snippet_bytes())) is not idle


@pytest.mark.parametrize("interrupted", [False, True])
def test_a_workspace_removed_during_the_drain_leaves_its_images_unlinked(
    h: Harness, monkeypatch: pytest.MonkeyPatch, interrupted: bool
):
    """rm wipes the workspace after the drain reads its users and before it commits them."""
    h.downloader.serve(avatar(ME), b"avatar", "image/png")
    fetch, read = h.downloader.fetch, h.archive.directory_objects
    drained: list[bool] = []

    def fetch_then_stop(jobs: list[DownloadJob], parallelism: int) -> Iterator[DownloadResult]:
        yield from fetch(jobs, parallelism)
        drained.append(True)
        if interrupted:
            raise KeyboardInterrupt

    def rm_commits_after_the_read(team: str, kind: Kind) -> dict[str, dict]:
        objects = read(team, kind)
        if drained == [True]:
            drained.append(True)
            remove_workspace(h, TEAM)
        return objects

    monkeypatch.setattr(h.downloader, "fetch", fetch_then_stop)
    monkeypatch.setattr(h.archive, "directory_objects", rm_commits_after_the_read)

    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            h.pull(SNIPPET_LINK)
    else:
        h.pull(SNIPPET_LINK)

    assert h.reporter.warnings[-1] == (
        f"Workspace {TEAM} was removed while this pull was running, so its avatars and emoji "
        "were not linked."
    )
    assert h.lines("user") == []


def drop_download(h: Harness, target: str) -> None:
    """Deletes the target's download state, as an rm committed by another command does."""
    with h.archive.elsewhere() as archive:
        archive.drop_downloads(TEAM, {target})
        archive.commit(Commit())


@pytest.mark.parametrize("second_result", ["ok", "failed"])
def test_requeued_target_removed_before_the_first_result_commits_is_dropped(
    h: Harness, monkeypatch: pytest.MonkeyPatch, second_result: str
):
    h.pull(SNIPPET_LINK)
    h.downloader.serve(SNIPPET_URL, snippet_bytes())
    if second_result == "ok":
        h.downloader.serve(avatar(ME), b"avatar", "image/png")
    else:
        h.downloader.fail(avatar(ME), error="HTTP 500")
    fetch = h.downloader.fetch

    def rm_drops_the_second_target_first(
        jobs: list[DownloadJob], parallelism: int
    ) -> Iterator[DownloadResult]:
        assert [job.target for job in jobs] == [SNIPPET, avatar(ME)]
        results = list(fetch(jobs, parallelism))
        drop_download(h, avatar(ME))
        yield from results

    monkeypatch.setattr(h.downloader, "fetch", rm_drops_the_second_target_first)

    with UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__) as uow:
        drained = drain_downloads(
            uow, h.downloader, 4, PullOptions().max_file_bytes, h.connector, h.reporter
        )

    assert (drained.files_saved, drained.images_saved) == (1, 0)
    assert avatar(ME) not in h.archive.directory_state(TEAM, "image")
    assert h.archive.downloads() == {}


def test_requeued_state_removed_while_no_job_can_run_stays_removed(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    h.downloader.fail(SNIPPET_URL, error="HTTP 500")
    h.downloader.serve(avatar(ME), b"avatar", "image/png")
    h.pull(SNIPPET_LINK)
    assert list(h.archive.downloads()) == [(TEAM, SNIPPET)]

    def rm_commits_while_connecting(workspace: str) -> None:
        drop_download(h, SNIPPET)
        raise AuthFailed("desktop", workspace)

    monkeypatch.setattr(h.connector, "connect", rm_commits_while_connecting)

    with UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__) as uow:
        drain_downloads(uow, h.downloader, 4, PullOptions().max_file_bytes, h.connector, h.reporter)

    assert h.archive.downloads() == {}


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


def test_image_types_tells_apart_custom_emoji_and_avatars():
    view = DirectoryView(
        users={"U0TIM000001": {"profile": {"display_name": "Tim", "image_512": "https://a/t.png"}}},
        conversations={},
        usergroups={},
        emoji={"party": "https://e/party.png"},
        bots={},
    )
    message = {
        "user": "U0TIM000001",
        "text": "done :party:",
        "attachments": [{"image_url": "https://i/preview.png"}],
    }

    assert image_types(message, view) == {
        "https://i/preview.png": "image",
        "https://e/party.png": "emoji",
        "https://a/t.png": "avatar",
    }
