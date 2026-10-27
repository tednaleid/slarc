# ABOUTME: QueryIndex over an archive's lake: the lake attached read-only plus one view per entity.
# ABOUTME: Current-state views take the latest row per key by (fetched_at, run).
import json
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

import duckdb

from slarc.adapters.lake import CATALOG, connect_lake, lake_errors
from slarc.domain.links import SLACK_HOST_SUFFIX
from slarc.domain.plain import excerpt
from slarc.ports import ArchiveLock, ArchiveReadFailed, SearchFilter, SearchHit, SlarcError

# Slack ts strings ordered as numbers.
TS_ORDER = "TRY_CAST({} AS DECIMAL(18, 6))"

# Directory entries of one kind reduced to the latest row per (team, id). A blob is only ever
# added to an object, so on a (fetched_at, run) tie the row with a blob is the later one.
LATEST_DIRECTORY_ROW = """
    SELECT team AS team_id, id, blob, object
    FROM lake.main.directory_entry
    WHERE kind = '{kind}' AND team IS NOT NULL AND id IS NOT NULL AND hash IS NOT NULL
    QUALIFY row_number() OVER (
        PARTITION BY team, id ORDER BY fetched_at DESC, run DESC, blob NULLS LAST, hash
    ) = 1
"""

# message_readable's columns over `message m` and the joins they need; SEARCH selects the same.
AUTHOR = """CASE WHEN m.user_id IS NOT NULL THEN coalesce(author.display, m.user_id)
        ELSE coalesce(m.object -> 'bot_profile' ->> 'name', m.object ->> 'username', 'unknown')
    END"""
READABLE_COLUMNS = f"""
    m.team_id, coalesce(w.domain, m.team_id) AS workspace, m.channel_id,
    coalesce(
        CASE c.kind
            WHEN 'dm' THEN '@' || coalesce(dm_user.display, c.dm_user_id)
            WHEN 'channel' THEN '#' || c.name
            ELSE c.name
        END,
        m.channel_id
    ) AS conversation,
    m.ts, m.thread_ts, m.posted_at,
    {AUTHOR} AS author,
    coalesce(nullif(author.real_name, ''), {AUTHOR}) AS author_real_name,
    regexp_replace(m.plain, '^.*?:( |$)', '') AS text,
    'https://' || coalesce(w.domain, m.team_id) || '{SLACK_HOST_SUFFIX}/archives/'
        || m.channel_id || '/p' || replace(m.ts, '.', '')
        || CASE WHEN m.is_reply
            THEN '?thread_ts=' || m.thread_ts || '&cid=' || m.channel_id
            ELSE '' END AS link,
    nullif(array_to_string(list_transform(
        CAST(m.object -> 'reactions' AS JSON[]),
        lambda r: ':' || (r ->> 'name') || ': ' || coalesce(r ->> 'count', '1')
    ), ', '), '') AS reactions,
    list_transform(
        CAST(m.object -> 'files' AS JSON[]),
        lambda f: struct_pack(id := f ->> 'id', name := coalesce(f ->> 'name', f ->> 'title'))
    ) AS files,
    m.deleted_at, m.first_seen_at
"""
READABLE_JOINS = """
    LEFT JOIN "user" author ON author.team_id = m.team_id AND author.id = m.user_id
    LEFT JOIN conversation c ON c.team_id = m.team_id AND c.id = m.channel_id
    LEFT JOIN "user" dm_user ON dm_user.team_id = c.team_id AND dm_user.id = c.dm_user_id
    LEFT JOIN workspace w ON w.team_id = m.team_id
"""

# The message views read the lake only through message_row, which messages() and thread()
# replace with a query over one conversation's rows.
ENTITY_VIEWS = {
    "message_row": "SELECT * FROM lake.main.message_version",
    "message_version": """
        SELECT team AS team_id, channel AS channel_id, ts, fetched_at, run, hash,
            object -> 'edited' ->> 'ts' AS edited_ts
        FROM message_row
    """,
    # One pass per key: the latest row decides deleted_at, and the latest message row gives
    # the content, so a deleted message keeps its last content.
    "message_current": """
        WITH latest AS (
            SELECT team AS team_id, channel AS channel_id, ts, kind, fetched_at,
                first_value(
                    CASE WHEN kind = 'message' THEN {
                        'source_team': source_team, 'plain': plain, 'hash': hash,
                        'object': object
                    } END IGNORE NULLS
                ) OVER newest AS content,
                min(fetched_at) FILTER (WHERE kind = 'message') OVER message_key
                    AS first_seen_at,
                max(fetched_at) FILTER (WHERE kind = 'message') OVER message_key
                    AS last_seen_at
            FROM message_row
            WINDOW message_key AS (PARTITION BY team, channel, ts),
                newest AS (
                    PARTITION BY team, channel, ts
                    ORDER BY fetched_at DESC, run DESC, kind, hash
                    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                )
            QUALIFY row_number() OVER newest = 1
        )
        SELECT team_id, channel_id, ts,
            content.source_team,
            content.object ->> 'thread_ts' AS thread_ts,
            content.object ->> 'user' AS user_id,
            content.object ->> 'bot_id' AS bot_id,
            content.object ->> 'subtype' AS subtype,
            content.object ->> 'text' AS text_mrkdwn,
            content.object -> 'blocks' AS blocks_json,
            content.object -> 'attachments' AS attachments_json,
            content.object -> 'edited' ->> 'ts' AS edited_ts,
            content.object -> 'edited' ->> 'user' AS edited_by,
            content.plain, content.hash,
            first_seen_at, last_seen_at,
            CASE WHEN kind = 'deleted' THEN fetched_at END AS deleted_at,
            content.object
        FROM latest
    """,
    "message": f"""
        WITH replies AS (
            SELECT team_id, channel_id, thread_ts AS ts, count(*) AS reply_count,
                arg_max(ts, {TS_ORDER.format("ts")}) AS latest_reply
            FROM message_current
            WHERE deleted_at IS NULL AND thread_ts IS NOT NULL AND thread_ts <> ts
            GROUP BY ALL
        ),
        -- A parent stored before its first reply has no thread_ts, and thread_ts is not
        -- hashed, so no later line adds it; stored replies make it a thread parent.
        computed AS (
        SELECT m.team_id, m.channel_id, m.ts, m.source_team,
            CASE WHEN replies.reply_count IS NOT NULL THEN m.ts ELSE m.thread_ts END AS thread_ts,
            coalesce(m.thread_ts = m.ts OR replies.reply_count IS NOT NULL, false)
                AS is_thread_parent,
            coalesce(replies.reply_count, 0) AS reply_count,
            replies.latest_reply,
            CASE
                WHEN m.object IS NULL THEN NULL
                WHEN m.subtype IS NULL
                    OR m.subtype IN ('thread_broadcast', 'file_share', 'me_message')
                    THEN CASE
                        WHEN m.user_id IS NULL AND m.bot_id IS NOT NULL THEN 'bot' ELSE 'user'
                    END
                WHEN m.subtype = 'bot_message' THEN 'bot'
                ELSE 'system'
            END AS author_kind,
            m.user_id, m.bot_id, m.subtype, m.text_mrkdwn, m.blocks_json, m.attachments_json,
            m.edited_ts, m.edited_by,
            coalesce(m.subtype = 'thread_broadcast', false) AS is_broadcast,
            m.plain, m.hash, m.first_seen_at, m.last_seen_at, m.deleted_at, m.object
        FROM message_current m
        LEFT JOIN replies USING (team_id, channel_id, ts)
        )
        SELECT * EXCLUDE (object),
            to_timestamp(TRY_CAST(ts AS DOUBLE)) AS posted_at,
            to_timestamp(TRY_CAST(edited_ts AS DOUBLE)) AS edited_at,
            coalesce(thread_ts, ts) AS thread_root_ts,
            coalesce(thread_ts <> ts, false) AS is_reply,
            object
        FROM computed
    """,
    "reaction": """
        SELECT team_id, channel_id, ts,
            r ->> 'name' AS name,
            regexp_replace(r ->> 'name', '::skin-tone-[0-9]+$', '') AS base_name,
            CAST(r -> 'count' AS INTEGER) AS count,
            CAST(r -> 'users' AS VARCHAR[]) AS user_ids
        FROM (
            SELECT team_id, channel_id, ts,
                unnest(CAST(object -> 'reactions' AS JSON[])) AS r
            FROM message
            WHERE json_type(object -> 'reactions') = 'ARRAY'
        )
    """,
    "message_file": """
        SELECT m.team_id, m.channel_id, m.ts, f.value ->> 'id' AS file_id,
            CAST(f.key AS INTEGER) AS position
        FROM message m, json_each(m.object -> 'files') f
        WHERE json_type(m.object -> 'files') = 'ARRAY'
    """,
    "file": f"""
        SELECT team_id, id,
            object ->> 'name' AS name,
            object ->> 'title' AS title,
            object ->> 'mimetype' AS mimetype,
            object ->> 'filetype' AS filetype,
            object ->> 'mode' AS mode,
            CAST(object -> 'size' AS BIGINT) AS size,
            CAST(object -> 'is_external' AS BOOLEAN) AS is_external,
            object ->> 'external_url' AS external_url,
            object ->> 'permalink' AS permalink,
            blob,
            object ->> 'preview' AS preview_text,
            CAST(object -> 'original_w' AS INTEGER) AS width,
            CAST(object -> 'original_h' AS INTEGER) AS height
        FROM ({LATEST_DIRECTORY_ROW.format(kind="file")})
    """,
    "image": f"""
        SELECT team_id, id AS url,
            object ->> 'content_type' AS content_type,
            CAST(object -> 'size' AS BIGINT) AS size,
            blob
        FROM ({LATEST_DIRECTORY_ROW.format(kind="image")})
    """,
    "user": f"""
        SELECT team_id, id,
            object ->> 'name' AS name,
            object ->> 'real_name' AS real_name,
            object -> 'profile' ->> 'display_name' AS display_name,
            coalesce(
                nullif(object -> 'profile' ->> 'display_name', ''),
                nullif(object -> 'profile' ->> 'real_name', ''),
                nullif(object ->> 'real_name', ''),
                object ->> 'name'
            ) AS display,
            object -> 'profile' ->> 'email' AS email,
            object ->> 'tz' AS tz,
            object ->> 'tz_label' AS tz_label,
            CAST(object -> 'tz_offset' AS INTEGER) AS tz_offset,
            CAST(object -> 'is_bot' AS BOOLEAN) AS is_bot,
            CAST(object -> 'is_app_user' AS BOOLEAN) AS is_app_user,
            CAST(object -> 'deleted' AS BOOLEAN) AS deleted,
            object ->> 'team_id' AS home_team_id,
            blob AS avatar_blob
        FROM ({LATEST_DIRECTORY_ROW.format(kind="user")})
    """,
    "user_snapshot": """
        SELECT team AS team_id, id, fetched_at, hash, object -> 'profile' AS profile_json
        FROM lake.main.directory_entry
        WHERE kind = 'user' AND team IS NOT NULL AND id IS NOT NULL AND hash IS NOT NULL
        QUALIFY row_number() OVER (
            PARTITION BY team, id, fetched_at ORDER BY run DESC, hash
        ) = 1
    """,
    "bot": f"""
        SELECT team_id, id,
            object ->> 'app_id' AS app_id,
            object ->> 'name' AS name,
            blob AS icon_blob
        FROM ({LATEST_DIRECTORY_ROW.format(kind="bot")})
    """,
    "emoji": f"""
        WITH seen AS (
            SELECT team AS team_id, id,
                min(fetched_at) AS first_seen, max(fetched_at) AS last_seen_in_list
            FROM lake.main.directory_entry
            WHERE kind = 'emoji' AND team IS NOT NULL AND id IS NOT NULL AND hash IS NOT NULL
            GROUP BY ALL
        )
        SELECT latest.team_id, latest.id AS name,
            CASE WHEN NOT starts_with(object ->> 'url', 'alias:') THEN object ->> 'url' END AS url,
            CASE WHEN starts_with(object ->> 'url', 'alias:')
                THEN substr(object ->> 'url', length('alias:') + 1) END AS alias_of,
            coalesce(lower(object ->> 'url') LIKE '%.gif', false) AS is_animated,
            latest.blob, seen.first_seen, seen.last_seen_in_list
        FROM ({LATEST_DIRECTORY_ROW.format(kind="emoji")}) latest
        JOIN seen USING (team_id, id)
    """,
    "usergroup": f"""
        SELECT team_id, id,
            object ->> 'handle' AS handle,
            object ->> 'name' AS name,
            CAST(object -> 'users' AS VARCHAR[]) AS user_ids
        FROM ({LATEST_DIRECTORY_ROW.format(kind="usergroup")})
    """,
    "conversation": f"""
        SELECT team_id, id,
            CASE
                WHEN CAST(object -> 'is_im' AS BOOLEAN) THEN 'dm'
                WHEN CAST(object -> 'is_mpim' AS BOOLEAN) THEN 'group_dm'
                ELSE 'channel'
            END AS kind,
            CAST(object -> 'is_private' AS BOOLEAN) AS is_private,
            CASE WHEN NOT coalesce(CAST(object -> 'is_im' AS BOOLEAN), false)
                THEN object ->> 'name' END AS name,
            object ->> 'user' AS dm_user_id,
            CAST(object -> 'is_archived' AS BOOLEAN) AS is_archived,
            CAST(object -> 'is_shared' AS BOOLEAN) AS is_shared,
            CAST(object -> 'created' AS BIGINT) AS created,
            object ->> 'creator' AS creator,
            object -> 'topic' ->> 'value' AS topic,
            object -> 'purpose' ->> 'value' AS purpose,
            CAST(object -> 'members' AS VARCHAR[]) AS member_ids
        FROM ({LATEST_DIRECTORY_ROW.format(kind="conversation")})
    """,
    "run": """
        SELECT run,
            object ->> 'command' AS command,
            CAST(object -> 'args' AS VARCHAR[]) AS args,
            object ->> 'started' AS started,
            object ->> 'finished' AS finished,
            CAST(object -> 'teams' AS VARCHAR[]) AS teams,
            object -> 'auth' AS auth,
            CAST(object -> 'counts' -> 'messages_seen' AS BIGINT) AS messages_seen,
            CAST(object -> 'counts' -> 'lines_appended' AS BIGINT) AS lines_appended,
            CAST(object -> 'counts' -> 'rate_limit_wait_s' AS DOUBLE) AS rate_limit_wait_s,
            object -> 'counts' -> 'api_calls' AS api_calls,
            object -> 'counts' -> 'threads_read' AS threads_read,
            object -> 'errors' AS errors
        FROM lake.main.run
        WHERE run IS NOT NULL
        QUALIFY row_number() OVER (
            PARTITION BY run ORDER BY fetched_at DESC, md5(CAST(object AS VARCHAR))
        ) = 1
    """,
    "workspace": """
        WITH entry AS (
            SELECT a ->> 'team_id' AS team_id, a ->> 'domain' AS domain, a ->> 'name' AS name,
                a ->> 'user_id' AS user_id, a ->> 'credential_source' AS credential_source,
                a ->> 'auth_test_at' AS auth_test_at
            FROM (
                SELECT unnest(CAST(auth AS JSON[])) AS a FROM run
                WHERE json_type(auth) = 'ARRAY'
            )
        ),
        seen AS (
            SELECT team_id, list(DISTINCT user_id ORDER BY user_id) AS user_ids
            FROM entry
            WHERE user_id IS NOT NULL
            GROUP BY team_id
        )
        SELECT latest.team_id, latest.domain, latest.name, latest.user_id, seen.user_ids,
            latest.credential_source, latest.auth_test_at AS last_auth_test
        FROM entry latest
        LEFT JOIN seen USING (team_id)
        WHERE latest.team_id IS NOT NULL
        QUALIFY row_number() OVER (
            PARTITION BY latest.team_id
            ORDER BY latest.auth_test_at DESC NULLS LAST, latest.domain, latest.credential_source,
                latest.user_id
        ) = 1
    """,
    "tracked_item": """
        SELECT team AS team_id, kind, channel AS channel_id, nullif(ts, '') AS ts,
            since, until, newest_ts, oldest_ts, backfill_complete, last_search_at, last_pull_at,
            status, archived, last_read, retry_threads
        FROM lake.main.tracked_item
    """,
    "download": """
        SELECT team AS team_id, target_kind, target, url, status, attempts, last_error
        FROM lake.main.download_state
    """,
    # Names and links resolved for reading. text is plain without its "Author: " prefix,
    # taken through the first ": ", so a display name holding ": " keeps its later part.
    "message_readable": f"SELECT {READABLE_COLUMNS} FROM message m {READABLE_JOINS}",
}

# One line per view and per column a reader may not guess, shown by duckdb_views() and
# duckdb_columns(). Times named *_at without a type are ISO 8601 UTC strings.
VIEW_COMMENTS = {
    "message_row": "Every stored message row; the message views read the lake through it.",
    "message_version": "Every stored version of every message, one row per observed change.",
    "message_current": "The newest version of each message; message builds on it.",
    "message": "The current version of each message, with thread and author facts.",
    "message_readable": "Each current message with names, text and its Slack link resolved.",
    "reaction": "One row per emoji reaction on a current message.",
    "message_file": "The files attached to each current message, in order.",
    "file": "Each file a message referenced, with its metadata and blob.",
    "image": "Each image URL (previews, avatars, icons) with its downloaded blob.",
    "user": "Each user of a workspace, newest profile.",
    "user_snapshot": "Every stored version of each user's profile.",
    "bot": "Each bot of a workspace.",
    "emoji": "Each custom emoji of a workspace.",
    "usergroup": "Each usergroup (@handle) of a workspace, with its members.",
    "conversation": "Each channel, DM and group DM a workspace's directory holds.",
    "run": "Each command that wrote to the archive, such as a pull or rm.",
    "workspace": "Each workspace in the archive, from its latest auth.",
    "tracked_item": "Each conversation, thread or message the archive keeps pulling.",
    "download": "Each file or image download not yet saved, and why.",
}
COLUMN_COMMENTS = {
    "message": {
        "ts": "Slack's message id and posting time, as seconds.micros text.",
        "thread_ts": "The thread parent's ts for a reply or a parent; null outside threads.",
        "is_thread_parent": "True when the message starts a thread with stored replies.",
        "author_kind": "user, bot or system (joins, topic changes and the like).",
        "text_mrkdwn": "Slack's raw mrkdwn, with <@U...> mention tokens.",
        "plain": "Plain text with names resolved, prefixed by the author and a colon.",
        "first_seen_at": "When a pull first stored the message.",
        "last_seen_at": "When a pull last stored a version of the message.",
        "deleted_at": "When slarc noticed Slack no longer returns the message; else null.",
        "posted_at": "When the message was posted.",
        "edited_at": "When the message was last edited; else null.",
        "thread_root_ts": "thread_ts, else ts: group by it to gather each thread.",
        "is_reply": "True for a reply inside a thread.",
    },
    "message_readable": {
        "team_id": "The workspace's team id.",
        "workspace": "The workspace's domain, as in its slack.com address.",
        "channel_id": "The conversation's id.",
        "conversation": "#channel, @user for a DM, or the group DM's name.",
        "ts": "Slack's message id and posting time, as seconds.micros text.",
        "thread_ts": "The thread parent's ts for a reply or a parent; null outside threads.",
        "posted_at": "When the message was posted.",
        "author": "The author's display name, else the bot or app name.",
        "author_real_name": "The author's real name, else the author column.",
        "text": "The message as plain text with names resolved.",
        "link": "The message's Slack link.",
        "reactions": "Each reaction as :name: count, joined by commas; null without any.",
        "files": "The id and name of each file on the message; slarc file F... gives one.",
        "deleted_at": "When slarc noticed Slack no longer returns the message; else null.",
        "first_seen_at": "When a pull first stored the message.",
    },
    "workspace": {"user_id": "The user of the latest auth, whom me names in search."},
    "run": {"started": "When the run started; first_seen_at at or after it is new in it."},
}

# Matches newest first, the plain text ILIKE $pattern when one is given. A user filter names
# a user by id, name, display name or real name, or "me", each workspace's latest auth user.
# Mentions match <@ID> and <!subteam^ID> tokens of the user's usergroups, not system messages.
USER_MATCH = """
    SELECT team_id, id FROM "user"
    WHERE {param} = id OR lower({param}) IN (lower(name), lower(display_name), lower(real_name))
    UNION
    SELECT team_id, user_id FROM workspace WHERE lower({param}) = 'me' AND user_id IS NOT NULL
"""
SEARCH = f"""
    WITH sender AS ({USER_MATCH.format(param="$from_user")}),
        mentioned AS ({USER_MATCH.format(param="$mentions")}),
        mention_token AS (
            SELECT team_id, '<@' || id || '>' AS token FROM mentioned
            UNION SELECT team_id, '<@' || id || '|' FROM mentioned
            UNION SELECT g.team_id, '<!subteam^' || g.id || suffix
                FROM usergroup g, (VALUES ('>'), ('|')) AS ends(suffix)
                JOIN mentioned u ON u.team_id = g.team_id AND list_contains(g.user_ids, u.id)
        )
    SELECT {READABLE_COLUMNS}, m.plain
    FROM message m {READABLE_JOINS}
    WHERE m.deleted_at IS NULL
        AND ($pattern IS NULL OR m.plain ILIKE $pattern ESCAPE '\\')
        AND ($in_channel IS NULL OR m.channel_id = $in_channel OR c.name = $in_channel)
        AND ($in_dm_user IS NULL OR c.kind = 'dm' AND (c.dm_user_id = $in_dm_user
            OR lower($in_dm_user) IN (
                lower(dm_user.name), lower(dm_user.display_name), lower(dm_user.real_name)
            )))
        AND ($from_user IS NULL OR EXISTS (
            SELECT 1 FROM sender WHERE sender.team_id = m.team_id AND sender.id = m.user_id
        ))
        AND ($mentions IS NULL OR m.author_kind <> 'system' AND EXISTS (
            SELECT 1 FROM mention_token t
            WHERE t.team_id = m.team_id AND contains(m.text_mrkdwn, t.token)
        ))
        AND ($since IS NULL OR {TS_ORDER.format("m.ts")} > {TS_ORDER.format("$since")})
        AND ($until IS NULL OR {TS_ORDER.format("m.ts")} < {TS_ORDER.format("$until")})
    ORDER BY {TS_ORDER.format("m.ts")} DESC, m.team_id, m.channel_id
    {{limit}}
"""

# Whether the directory knows the conversation or user a search filter names, matched as SEARCH
# matches them.
KNOWS_CHANNEL = """
    SELECT EXISTS (SELECT 1 FROM conversation WHERE id = $channel OR name = $channel)
        OR EXISTS (SELECT 1 FROM message WHERE channel_id = $channel)
"""
KNOWS_DM_USER = """
    SELECT EXISTS (
        SELECT 1 FROM conversation c
        LEFT JOIN "user" u ON u.team_id = c.team_id AND u.id = c.dm_user_id
        WHERE c.kind = 'dm' AND (c.dm_user_id = $user
            OR lower($user) IN (lower(u.name), lower(u.display_name), lower(u.real_name)))
    )
"""
KNOWS_USER = f"SELECT EXISTS ({USER_MATCH.format(param='$user')})"

MESSAGE_COLUMNS = """
    team_id, channel_id, ts, thread_ts, is_thread_parent, is_broadcast, reply_count,
    latest_reply, author_kind, user_id, bot_id, subtype, edited_ts, edited_by, plain,
    deleted_at, object
"""


def _comments() -> list[str]:
    statements = [
        f'COMMENT ON VIEW "{name}" IS {_quoted(text)}' for name, text in VIEW_COMMENTS.items()
    ]
    statements += [
        f'COMMENT ON COLUMN "{view}"."{column}" IS {_quoted(text)}'
        for view, columns in COLUMN_COMMENTS.items()
        for column, text in columns.items()
    ]
    return statements


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _like_pattern(text: str) -> str:
    """An ILIKE pattern matching text anywhere, with its wildcard characters taken literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _error_message(error: duckdb.Error) -> str:
    """DuckDB's message up to its first blank line, which starts the query excerpt."""
    head = str(error).split("\n\n", 1)[0]
    return " ".join(line.strip() for line in head.splitlines() if line.strip())


class DuckDbQueryIndex:
    """Views over the archive's lake, which is attached read-only as `lake`.

    The views live in the in-memory database, which queries use by default, so a view and the
    lake table it reads can share a name. The lake's catalog must exist. The index holds the
    archive's read lock from open to close. A lake failure in any query but sql raises
    ArchiveReadFailed.

    connection, when given, is a connection to a DuckDB instance that already has the lake
    attached, such as a store's shared_connection; otherwise the index attaches the lake
    read-only. owner checks the archive's owner record on that connection.
    """

    def __init__(
        self,
        root: Path,
        ext_dir: Path,
        lock: ArchiveLock,
        *,
        connection: duckdb.DuckDBPyConnection | None = None,
        owner: Callable[[duckdb.DuckDBPyConnection], object] = lambda con: None,
    ) -> None:
        self._catalog = root / CATALOG
        self._lock = lock
        lock.hold_shared()
        try:
            self._db = connection or connect_lake(root, ext_dir, read_only=True)
        except BaseException:
            lock.release_shared()
            raise
        try:
            owner(self._db)
            self._db.execute("USE memory")
            self._db.execute(
                ";\n".join(
                    [
                        *(
                            f'CREATE VIEW "{name}" AS {query}'
                            for name, query in ENTITY_VIEWS.items()
                        ),
                        *_comments(),
                    ]
                )
            )
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> DuckDbQueryIndex:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._db.close()
        finally:
            self._lock.release_shared()

    def sql(self, query: str) -> tuple[list[str], list[tuple]]:
        try:
            cursor = self._db.execute(query)
            columns = [column[0] for column in cursor.description or []]
            return columns, cursor.fetchall()
        except duckdb.Error as error:
            raise SlarcError(_error_message(error)) from None

    def search(self, query: SearchFilter) -> list[SearchHit]:
        params = {
            "pattern": _like_pattern(query.text) if query.text else None,
            "in_channel": query.in_channel,
            "in_dm_user": query.in_dm_user,
            "from_user": query.from_user,
            "mentions": query.mentions,
            "since": query.window.since,
            "until": query.window.until,
        }
        limit = f"LIMIT {int(query.limit)}" if query.limit else ""
        with lake_errors(self._catalog, ArchiveReadFailed):
            rows = self._db.execute(SEARCH.format(limit=limit), params).fetchall()
        return [SearchHit(*row[:-1], excerpt=excerpt(row[-1], query.text or "")) for row in rows]

    def knows_conversation(self, channel: str | None, dm_user: str | None) -> bool:
        if dm_user is not None:
            return self._exists(KNOWS_DM_USER, {"user": dm_user})
        return self._exists(KNOWS_CHANNEL, {"channel": channel})

    def knows_user(self, user: str) -> bool:
        return self._exists(KNOWS_USER, {"user": user})

    def _exists(self, query: str, params: dict[str, str | None]) -> bool:
        with lake_errors(self._catalog, ArchiveReadFailed):
            row = self._db.execute(query, params).fetchone()
        return bool(row and row[0])

    def conversation_counts(self) -> dict[tuple[str, str], int]:
        with lake_errors(self._catalog, ArchiveReadFailed):
            rows = self._db.execute(
                "SELECT team_id, channel_id, count(*) FROM message "
                "WHERE deleted_at IS NULL GROUP BY ALL"
            ).fetchall()
        return {(team, channel): count for team, channel, count in rows}

    def messages(
        self,
        team: str,
        channel: str,
        since: str | None,
        until: str | None,
        include_deleted: bool,
    ) -> list[dict]:
        """Current messages with since < ts < until, the exclusive bounds of a tracked item."""
        return self._messages(
            f"""
            ($since IS NULL OR {TS_ORDER.format("ts")} > {TS_ORDER.format("$since")})
            AND ($until IS NULL OR {TS_ORDER.format("ts")} < {TS_ORDER.format("$until")})
            """,
            {"team": team, "channel": channel, "since": since, "until": until},
            include_deleted,
        )

    def thread(self, team: str, channel: str, thread_ts: str, include_deleted: bool) -> list[dict]:
        """The thread's parent and replies; a message outside any thread comes back alone.

        The rows read are every version of each message whose ts or thread_ts names the thread
        in any version, so a deleted reply's marker, which has no thread_ts, comes along.
        """
        return self._messages(
            "(ts = $thread_ts OR thread_ts = $thread_ts)",
            {"team": team, "channel": channel, "thread_ts": thread_ts},
            include_deleted,
            in_thread="""
                AND ts IN (
                    SELECT ts FROM lake.main.message_version
                    WHERE team = $team AND channel = $channel
                        AND (ts = $thread_ts OR thread_ts = $thread_ts)
                )
            """,
        )

    def _messages(
        self, condition: str, params: dict[str, Any], include_deleted: bool, in_thread: str = ""
    ) -> list[dict]:
        """Runs the message views over the channel's own rows only, so a loop over
        conversations reads each conversation once instead of the whole archive each time."""
        deleted = "" if include_deleted else "AND deleted_at IS NULL"
        with lake_errors(self._catalog, ArchiveReadFailed):
            cursor = self._db.execute(
                f"""
                WITH message_row AS (
                        SELECT * FROM lake.main.message_version
                        WHERE team = $team AND channel = $channel {in_thread}
                    ),
                    message_current AS ({ENTITY_VIEWS["message_current"]}),
                    message AS ({ENTITY_VIEWS["message"]})
                SELECT {MESSAGE_COLUMNS} FROM message
                WHERE team_id = $team AND channel_id = $channel AND {condition} {deleted}
                ORDER BY {TS_ORDER.format("ts")}
                """,
                params,
            )
            columns = [column[0] for column in cursor.description or []]
            rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        for row in rows:
            if row["object"] is not None:
                row["object"] = json.loads(row["object"])
        return rows
