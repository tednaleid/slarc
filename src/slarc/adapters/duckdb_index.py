# ABOUTME: QueryIndex over an archive's JSONL files: an in-memory DuckDB with one view per entity.
# ABOUTME: Current-state views take the latest line per key by (fetched_at, run).
import json
from pathlib import Path
from types import TracebackType
from typing import Any

import duckdb

from slarc.domain.links import message_link
from slarc.domain.plain import snippet
from slarc.ports import SearchHit, SlarcError

MAXIMUM_OBJECT_SIZE = 67108864

META_TYPE = (
    "STRUCT(schema INTEGER, kind VARCHAR, team VARCHAR, channel VARCHAR, ts VARCHAR, "
    "id VARCHAR, source_team VARCHAR, fetched_at VARCHAR, run VARCHAR, api VARCHAR, "
    "slarc VARCHAR, hash VARCHAR, plain VARCHAR, blob VARCHAR)"
)

# Each source view reads the lines of one file kind; the glob is relative to the archive root.
SOURCES = {
    "raw_line": "raw/*/*/*.jsonl",
    "user_line": "directory/*/users.jsonl",
    "emoji_line": "directory/*/emoji.jsonl",
    "conversation_line": "directory/*/conversations.jsonl",
    "bot_line": "directory/*/bots.jsonl",
    "usergroup_line": "directory/*/usergroups.jsonl",
    "file_line": "directory/*/files.jsonl",
    "image_line": "directory/*/images.jsonl",
    "run_line": "runs.jsonl",
}

# Slack ts strings ordered as numbers.
TS_ORDER = "TRY_CAST({} AS DECIMAL(18, 6))"

# Directory lines reduced to the latest line per (team, id). A blob is only ever added to an
# object, so on a (fetched_at, run) tie the line with a blob is the later one.
LATEST_DIRECTORY_LINE = """
    SELECT meta.team AS team_id, meta.id AS id, meta.blob AS blob, object
    FROM {source}
    WHERE meta.team IS NOT NULL AND meta.id IS NOT NULL AND meta.hash IS NOT NULL
    QUALIFY row_number() OVER (
        PARTITION BY meta.team, meta.id
        ORDER BY meta.fetched_at DESC, meta.run DESC, meta.blob NULLS LAST, meta.hash
    ) = 1
"""

ENTITY_VIEWS = {
    "message_line": """
        SELECT meta.team AS team_id, meta.channel AS channel_id, meta.ts AS ts,
            meta.kind AS kind, meta.fetched_at AS fetched_at, meta.run AS run,
            meta.source_team AS source_team, meta.hash AS hash, meta.plain AS plain, object
        FROM raw_line
        WHERE meta.kind IN ('message', 'deleted')
            AND meta.team IS NOT NULL AND meta.channel IS NOT NULL AND meta.ts IS NOT NULL
        QUALIFY row_number() OVER (
            PARTITION BY meta.team, meta.channel, meta.ts, meta.fetched_at, meta.run
            ORDER BY meta.kind, meta.hash
        ) = 1
    """,
    "message_version": """
        SELECT team_id, channel_id, ts, fetched_at, run, hash,
            object -> 'edited' ->> 'ts' AS edited_ts
        FROM message_line
    """,
    "message_current": """
        WITH latest AS (
            SELECT team_id, channel_id, ts, kind, fetched_at
            FROM message_line
            QUALIFY row_number() OVER (
                PARTITION BY team_id, channel_id, ts ORDER BY fetched_at DESC, run DESC, kind
            ) = 1
        ),
        content AS (
            SELECT * FROM message_line
            WHERE kind = 'message'
            QUALIFY row_number() OVER (
                PARTITION BY team_id, channel_id, ts ORDER BY fetched_at DESC, run DESC, hash
            ) = 1
        ),
        seen AS (
            SELECT team_id, channel_id, ts,
                min(fetched_at) AS first_seen_at, max(fetched_at) AS last_seen_at
            FROM message_line
            WHERE kind = 'message'
            GROUP BY ALL
        )
        SELECT latest.team_id, latest.channel_id, latest.ts,
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
            seen.first_seen_at, seen.last_seen_at,
            CASE WHEN latest.kind = 'deleted' THEN latest.fetched_at END AS deleted_at,
            content.object
        FROM latest
        LEFT JOIN content USING (team_id, channel_id, ts)
        LEFT JOIN seen USING (team_id, channel_id, ts)
    """,
    "message": f"""
        WITH replies AS (
            SELECT team_id, channel_id, thread_ts AS ts, count(*) AS reply_count,
                arg_max(ts, {TS_ORDER.format("ts")}) AS latest_reply
            FROM message_current
            WHERE deleted_at IS NULL AND thread_ts IS NOT NULL AND thread_ts <> ts
            GROUP BY ALL
        )
        -- A parent stored before its first reply has no thread_ts, and thread_ts is not
        -- hashed, so no later line adds it; stored replies make it a thread parent.
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
        FROM ({LATEST_DIRECTORY_LINE.format(source="file_line")})
    """,
    "image": f"""
        SELECT team_id, id AS url,
            object ->> 'content_type' AS content_type,
            CAST(object -> 'size' AS BIGINT) AS size,
            blob
        FROM ({LATEST_DIRECTORY_LINE.format(source="image_line")})
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
        FROM ({LATEST_DIRECTORY_LINE.format(source="user_line")})
    """,
    "user_snapshot": """
        SELECT meta.team AS team_id, meta.id AS id, meta.fetched_at AS fetched_at,
            meta.hash AS hash, object -> 'profile' AS profile_json
        FROM user_line
        WHERE meta.team IS NOT NULL AND meta.id IS NOT NULL AND meta.hash IS NOT NULL
        QUALIFY row_number() OVER (
            PARTITION BY meta.team, meta.id, meta.fetched_at ORDER BY meta.run DESC, meta.hash
        ) = 1
    """,
    "bot": f"""
        SELECT team_id, id,
            object ->> 'app_id' AS app_id,
            object ->> 'name' AS name,
            blob AS icon_blob
        FROM ({LATEST_DIRECTORY_LINE.format(source="bot_line")})
    """,
    "emoji": f"""
        WITH seen AS (
            SELECT meta.team AS team_id, meta.id AS id,
                min(meta.fetched_at) AS first_seen, max(meta.fetched_at) AS last_seen_in_list
            FROM emoji_line
            WHERE meta.team IS NOT NULL AND meta.id IS NOT NULL AND meta.hash IS NOT NULL
            GROUP BY ALL
        )
        SELECT latest.team_id, latest.id AS name,
            CASE WHEN NOT starts_with(object ->> 'url', 'alias:') THEN object ->> 'url' END AS url,
            CASE WHEN starts_with(object ->> 'url', 'alias:')
                THEN substr(object ->> 'url', length('alias:') + 1) END AS alias_of,
            coalesce(lower(object ->> 'url') LIKE '%.gif', false) AS is_animated,
            latest.blob, seen.first_seen, seen.last_seen_in_list
        FROM ({LATEST_DIRECTORY_LINE.format(source="emoji_line")}) latest
        JOIN seen USING (team_id, id)
    """,
    "usergroup": f"""
        SELECT team_id, id,
            object ->> 'handle' AS handle,
            object ->> 'name' AS name,
            CAST(object -> 'users' AS VARCHAR[]) AS user_ids
        FROM ({LATEST_DIRECTORY_LINE.format(source="usergroup_line")})
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
        FROM ({LATEST_DIRECTORY_LINE.format(source="conversation_line")})
    """,
    "run": """
        SELECT meta.run AS run,
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
            object -> 'errors' AS errors,
            object -> 'tracked' AS tracked,
            object -> 'downloads' AS downloads
        FROM run_line
        WHERE meta.kind = 'run' AND meta.run IS NOT NULL
        QUALIFY row_number() OVER (
            PARTITION BY meta.run ORDER BY meta.fetched_at DESC, md5(CAST(object AS VARCHAR))
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
        SELECT latest.team_id, latest.domain, latest.name, seen.user_ids,
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
}

# Matches newest first. Authors and conversations are named as DirectoryView names them, and a
# link's domain falls back to the team id for a workspace no run line has authenticated.
SEARCH = f"""
    SELECT m.team_id, m.channel_id, m.ts, m.thread_ts, m.plain,
        CASE WHEN m.user_id IS NOT NULL THEN coalesce(author.display, m.user_id)
            ELSE coalesce(
                m.object -> 'bot_profile' ->> 'name', m.object ->> 'username', 'unknown'
            )
        END AS author,
        coalesce(
            CASE c.kind
                WHEN 'dm' THEN '@' || coalesce(dm_user.display, c.dm_user_id)
                WHEN 'channel' THEN '#' || c.name
                ELSE c.name
            END,
            m.channel_id
        ) AS conversation,
        coalesce(w.domain, m.team_id) AS domain
    FROM message m
    LEFT JOIN "user" author ON author.team_id = m.team_id AND author.id = m.user_id
    LEFT JOIN conversation c ON c.team_id = m.team_id AND c.id = m.channel_id
    LEFT JOIN "user" dm_user ON dm_user.team_id = c.team_id AND dm_user.id = c.dm_user_id
    LEFT JOIN workspace w ON w.team_id = m.team_id
    WHERE m.deleted_at IS NULL
        AND m.plain ILIKE $pattern ESCAPE '\\'
        AND ($in_channel IS NULL OR m.channel_id = $in_channel OR c.name = $in_channel)
        AND ($from_user IS NULL OR m.user_id = $from_user OR lower($from_user) IN (
            lower(author.name), lower(author.display_name), lower(author.real_name)
        ))
    ORDER BY {TS_ORDER.format("m.ts")} DESC, m.team_id, m.channel_id
    LIMIT $limit
"""

MESSAGE_COLUMNS = """
    team_id, channel_id, ts, thread_ts, is_thread_parent, is_broadcast, reply_count,
    latest_reply, author_kind, user_id, bot_id, subtype, edited_ts, edited_by, plain,
    deleted_at, object
"""


def _sql_string(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _source_query(root: Path, pattern: str) -> str:
    return _read_query(sorted(str(path) for path in root.glob(pattern)))


def _read_query(paths: list[str]) -> str:
    """read_ndjson over paths, or no rows when there are none.

    Truncated lines from a crash read as rows with a null meta, which every view filters out.
    """
    if not paths:
        return f"SELECT CAST(NULL AS {META_TYPE}) AS meta, CAST(NULL AS JSON) AS object WHERE false"
    files = "[" + ", ".join(_sql_string(path) for path in paths) + "]"
    return (
        f"SELECT meta, object FROM read_ndjson({files}, "
        f"columns = {{'meta': '{META_TYPE}', 'object': 'JSON'}}, "
        f"ignore_errors = true, maximum_object_size = {MAXIMUM_OBJECT_SIZE})"
    )


def _like_pattern(text: str) -> str:
    """An ILIKE pattern matching text anywhere, with its wildcard characters taken literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _error_message(error: duckdb.Error) -> str:
    """DuckDB's message up to its first blank line, which starts the query excerpt."""
    head = str(error).split("\n\n", 1)[0]
    return " ".join(line.strip() for line in head.splitlines() if line.strip())


class DuckDbQueryIndex:
    """Views over the JSONL files present under root when the index is created."""

    def __init__(self, root: Path) -> None:
        self._db = duckdb.connect(":memory:")
        self._channel_files: dict[tuple[str, str], list[str]] = {}
        for path in sorted(root.glob(SOURCES["raw_line"])):
            channel_dir = path.parent
            key = (channel_dir.parent.name, channel_dir.name)
            self._channel_files.setdefault(key, []).append(str(path))
        for name, pattern in SOURCES.items():
            self._db.execute(f"CREATE VIEW {name} AS {_source_query(root, pattern)}")
        for name, query in ENTITY_VIEWS.items():
            self._db.execute(f'CREATE VIEW "{name}" AS {query}')

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
        self._db.close()

    def sql(self, query: str) -> tuple[list[str], list[tuple]]:
        try:
            cursor = self._db.execute(query)
            columns = [column[0] for column in cursor.description or []]
            return columns, cursor.fetchall()
        except duckdb.Error as error:
            raise SlarcError(_error_message(error)) from None

    def search(
        self, text: str, in_channel: str | None, from_user: str | None, limit: int
    ) -> list[SearchHit]:
        """Current messages whose plain text contains text, ignoring case, newest first.

        in_channel is a conversation id or name; from_user a user id, name, display name or
        real name, compared ignoring case.
        """
        params = {
            "pattern": _like_pattern(text),
            "in_channel": in_channel,
            "from_user": from_user,
            "limit": limit,
        }
        rows = self._db.execute(SEARCH, params).fetchall()
        return [
            SearchHit(
                author=author,
                conversation=conversation,
                ts=ts,
                snippet=snippet(plain, text),
                link=message_link(domain, channel, ts, thread_ts if thread_ts != ts else None),
            )
            for _, channel, ts, thread_ts, plain, author, conversation, domain in rows
        ]

    def conversation_counts(self) -> dict[tuple[str, str], int]:
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
        """The thread's parent and replies; a message outside any thread comes back alone."""
        return self._messages(
            "(ts = $thread_ts OR thread_ts = $thread_ts)",
            {"team": team, "channel": channel, "thread_ts": thread_ts},
            include_deleted,
        )

    def _messages(
        self, condition: str, params: dict[str, Any], include_deleted: bool
    ) -> list[dict]:
        """Runs the message view over the channel's own month files only, so a loop over
        conversations reads each file once instead of the whole archive per conversation."""
        deleted = "" if include_deleted else "AND deleted_at IS NULL"
        source = _read_query(self._channel_files.get((params["team"], params["channel"]), []))
        cursor = self._db.execute(
            f"""
            WITH raw_line AS ({source}),
                message_line AS ({ENTITY_VIEWS["message_line"]}),
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
