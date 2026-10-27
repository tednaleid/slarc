# Slack push events and cheap change detection

Research date: 2026-10-27. Question: can a `slarc listen` command receive changes pushed by Slack
over the user's desktop session (xoxc token plus `d` cookie), and are there cheap "what changed"
calls that would let `pull` skip unchanged conversations?

Method: Slack's developer docs, Slack help center and engineering blog, and the source and issue
trackers of open-source clients that use xoxc + `d` sessions. No Slack API call was made and no
token, cookie or keychain item was read. Claims marked **unconfirmed** have no primary source or
rest on one user report.

Client source read at these commits (links below are permalinks to them):

| Client | Repo | Commit (date) |
|---|---|---|
| wee-slack 3.x (`master`) | https://github.com/wee-slack/wee-slack | `aa904c2` (2026-08-17) |
| wee-slack 2.x | same repo, branch `2.x` | `d4b6887` |
| emacs-slack | https://github.com/emacs-slack/emacs-slack | `1486b47` (2026-10-22) |
| slack-libpurple | https://github.com/dylex/slack-libpurple | `1115ec1` (2025-07-26) |
| mautrix-slack (Matrix bridge) | https://github.com/mautrix/slack | `4204d8b` (2026-10-27) |
| beeper/slackgo (slack-go fork used by mautrix-slack) | https://github.com/beeper/slackgo | `4b61f4f` (2026-10-27) |
| slackdump | https://github.com/rusq/slackdump | `4593753` (2026-10-28) |

slackdump has no websocket code; it only uses `client.counts` and `client.userBoot` (section 5).
slack-term vendors an old slack-go and uses documented `rtm.connect`/`rtm.start` with app tokens;
it adds nothing here.

## Summary

| Question | Answer |
|---|---|
| 1. What the client opens | A websocket to `wss://wss-primary.slack.com/?token=<xoxc>&...` with the `d` cookie on the handshake. Third-party clients reach the same message server either through the documented `rtm.connect` (returns a single-use URL) or by building the `wss-primary` URL directly. Both work with xoxc + `d` per current client code and 2026 user reports; not verified by slarc. Socket Mode and the Events API need an installed Slack app and are ruled out. |
| 2. What arrives | The legacy RTM event stream: `message` with subtypes `message_changed`, `message_deleted`, `message_replied`, thread replies as `message` with `thread_ts`, `reaction_added/removed`, `file_*`, `channel_joined`, `member_joined_channel`, `*_marked`, `thread_*` and more. The gateway subscribes the connection to every conversation the user is a member of; no per-channel subscribe is needed. Events for public channels the user is not in, and for followed threads outside member channels, are unconfirmed. |
| 3. Side effects | The connection counts as a connected client (`online: true`, `connection_count` +1, visible only to the user). It does not mark anything read. Third-party sockets that send only pings leave the user `away`; clients that want "active" must send `tickle` or call `users.setPresence`/`presence.set`. No flag hides the connection; `presence_sub` and `batch_presence_aware` only govern receiving others' presence. |
| 4. Reliability | URL valid 30 s; ping every few seconds; `goodbye` before server close; 8 h maximum connection; `reconnect_url` (experimental) allows a resume; otherwise there is no replay, so clients catch up with Web API reads after a reconnect. `rtm.connect` is Tier 1. Session tokens are unsupported by Slack and may stop working. |
| 5. Cheap change detection | Undocumented `client.counts` returns `latest`, `last_read`, `has_unreads` for every member channel, group DM and DM in one call. Undocumented `users.counts` is older and similar. Documented `conversations.info` returns no `latest` for channels with slarc's session (fixture). Documented options cost one call per conversation. |

## 1. How the client receives real-time events

### The message server and how clients reach it

- Slack's architecture post: "On boot up, the client fetches the user token and websocket connection
  setup information from the Webapp backend"; the websocket goes to a Gateway Server (GS), which
  "fetches the user information, including all the user's channels, from Webapp" and "then
  subscribes to all the channel servers that hold those channels". Sameera Thangudu, "Real-time
  Messaging", Slack Engineering, 2023-04-11, https://slack.engineering/real-time-messaging/
- Flannel is Slack's edge cache that serves boot data and holds a websocket to the core region for
  the client. https://slack.engineering/flannel-an-application-level-edge-cache-to-make-slack-scale/
  (2017-05-31). The `flannel=3` and `flannel_api_ver=4` parameters below refer to it.
- The web client's websocket URL, captured from the browser network tab in October 2024 and used by
  emacs-slack today:
  `wss://wss-primary.slack.com/?token=<xoxc>&sync_desync=1&slack_client=desktop&start_args=...&no_query_on_subscribe=1&flannel=3&lazy_channels=1&gateway_server=<team id>-4&batch_presence_aware=1`.
  emacs-slack `slack-websocket.el`
  [L72-L83](https://github.com/emacs-slack/emacs-slack/blob/1486b4710f151260f3c1c2acecc5bde552089584/slack-websocket.el#L72-L83);
  commit message of [b710c9c](https://github.com/emacs-slack/emacs-slack/commit/b710c9c4ea8d0c47c41675be345d800c5684316a)
  ("I checked the network tab of the Slack web app ... and I could find the call to establish the
  websocket").
- wee-slack 3.x opens `wss://wss-primary.slack.com/?token=...&gateway_server=<team id>-1&slack_client=desktop&batch_presence_aware=1`
  for session tokens and uses `rtm.connect` only for OAuth tokens.
  [slack_workspace.py L376-L396](https://github.com/wee-slack/wee-slack/blob/aa904c24a85a50278e271fa6d4ac29838186c800/slack/slack_workspace.py#L376-L396).
  The reason, from commit [3b98fbe](https://github.com/wee-slack/wee-slack/commit/3b98fbeff044299104f7d00a3c9b56d9a53b0df0)
  (2023-12): "Some enterprise workspaces are restricted in which API methods they can use ... for me
  it seems to be at least rtm.connect, users.conversations, conversations.list and
  conversations.members".
- beeper/slackgo (used by mautrix-slack) no longer calls `rtm.connect`; it builds
  `wss://wss-primary.slack.com/?token=<token>`, sends the session cookies and
  `Origin: https://app.slack.com` on the handshake, and has a TODO "replace rtm.connect with
  /api/client.boot".
  [websocket_managed_conn.go L216-L263](https://github.com/beeper/slackgo/blob/4b61f4fcd5e0aef337cd9d3a3e1f03aece47478e/websocket_managed_conn.go#L216-L263).
- `client.boot` and `client.userBoot` are the web client's undocumented boot calls (users, channels,
  prefs). slackgo calls `client.boot` with `flannel_api_ver=4`
  ([users.go L608-L626](https://github.com/beeper/slackgo/blob/4b61f4fcd5e0aef337cd9d3a3e1f03aece47478e/users.go#L608-L626))
  and `client.userBoot` with `_x_reason=initial-data`
  ([users.go L627-L665](https://github.com/beeper/slackgo/blob/4b61f4fcd5e0aef337cd9d3a3e1f03aece47478e/users.go#L627-L665)).
  They return data, not the socket; no source shows either returning a websocket URL.
  **Unconfirmed** whether the real client gets its gateway URL from `client.boot`.

### Authentication on the socket

- The token is in the URL query (`token=xoxc-...`). The `d` cookie must be sent on the websocket
  handshake: in September 2023 emacs-slack's socket started answering
  `{"type":"error","error":{"msg":"invalid_auth","code":401,...}}` until the `d` cookie was added to
  the websocket request. https://github.com/emacs-slack/emacs-slack/issues/584 (comments of
  2023-09-20). wee-slack commit `435ca06` "Include cookie when making the websocket connection";
  slack-libpurple passes `d=` on the handshake
  ([slack-rtm.c L191-L196](https://github.com/dylex/slack-libpurple/blob/1115ec117bcddb38ee0ec649f139d04ab3b53942/slack-rtm.c#L191-L196)).
- emacs-slack additionally stores `d-s` and `lc` cookies for the socket
  ([b710c9c](https://github.com/emacs-slack/emacs-slack/commit/b710c9c4ea8d0c47c41675be345d800c5684316a)).
  wee-slack and slack-libpurple send only `d`. **Unconfirmed** whether `d-s` is needed.

### Does `rtm.connect` still work with xoxc + `d` in 2025-2026?

- Docs: `rtm.connect` needs "No scopes required", is "Tier 1: 1+ per minute", returns URLs that "are
  only valid for 30 seconds", and states "New Slack apps may not use any Real Time Messaging API
  method." Its error list includes `not_allowed_token_type` and `enterprise_is_restricted`.
  https://docs.slack.dev/reference/methods/rtm.connect
- The RTM page: "Granular permission Slack apps cannot use the RTM API. Classic apps can, but be
  warned that they may no longer be created and are soon to be deprecated."
  https://docs.slack.dev/legacy/legacy-rtm-api
- Classic app retirement was moved several times (March 2026, May 2026, November 2026) and then,
  on 2025-12-08: "we have decided to pause this migration. Classic apps will continue to work for
  the foreseeable future". https://docs.slack.dev/changelog/tags/deprecation and
  https://docs.slack.dev/changelog/2024-09-legacy-custom-bots-classic-apps-deprecation
- `rtm.start` now behaves like `rtm.connect` (since 2022-09-27); "The RTM API remains available to
  developers using rtm.connect." https://docs.slack.dev/changelog/2021-10-rtm-start-to-stop
- None of these notices mention xoxc session tokens, which belong to Slack's own client, not to an
  app. Evidence that `rtm.connect` answers xoxc + `d` today:
  - slack-libpurple's login calls `rtm.connect` with `batch_presence_aware=1&presence_sub=true`
    and connects to the returned URL
    ([slack.c L351-L354](https://github.com/dylex/slack-libpurple/blob/1115ec117bcddb38ee0ec649f139d04ab3b53942/slack.c#L351-L354),
    [slack-rtm.c L241-L243](https://github.com/dylex/slack-libpurple/blob/1115ec117bcddb38ee0ec649f139d04ab3b53942/slack-rtm.c#L241-L243)).
    A user with `xoxc-... xoxd-...` reported a working login on 2026-01-12 after updating
    (https://github.com/dylex/slack-libpurple/issues/200).
  - emacs-slack still calls `rtm.connect` in `slack-authorize` and fails the login if it errors
    ([slack-websocket.el L1014-L1027](https://github.com/emacs-slack/emacs-slack/blob/1486b4710f151260f3c1c2acecc5bde552089584/slack-websocket.el#L1014-L1027)),
    though it then ignores the returned URL (the `let` in `slack-ws-open` shadows the `ws-url`
    argument, L72-L83).
  - In 2023 `rtm.connect` with xoxc returned a URL of the form
    `wss://wss-primary.slack.com/websocket/LEGACY_BOT:<hex>` (log in issue #584 above).
  - Enterprise Grid orgs can restrict it (wee-slack 3b98fbe above; https://github.com/emacs-slack/emacs-slack/issues/543
    `enterprise_is_restricted`).
- Conclusion: works for xoxc + `d` per 2026 client code and one 2026 user report, **not verified by
  slarc**. A check on `naleidworkspace` would settle it: `rtm.connect` is a documented, read-only
  Web API method.

### Socket Mode and the Events API: ruled out

- Socket Mode: "The URL is created at runtime by calling the apps.connections.open method";
  "Socket Mode is only available for apps using granular permissions"; you must "Generate an
  app-level token" (`xapp-...`). https://docs.slack.dev/apis/events-api/using-socket-mode
- `apps.connections.open`: "This API method uses an app-level token to generate a dynamic WebSocket
  URL." https://docs.slack.dev/reference/methods/apps.connections.open
- Events API message events need a subscription per type (`message.channels`, `message.im`, ...)
  and scopes in an installed app. https://docs.slack.dev/reference/events/message
- Both need a Slack app installed in the workspace, which ADR 0001 rejects
  (`docs/adr/0001-user-session-auth.md`). mautrix-slack supports Socket Mode only for app-token
  logins ([handleslack.go L162-L189](https://github.com/mautrix/slack/blob/4204d8bd2c4f04d907d7e022465b04e65c47589b/pkg/connector/handleslack.go#L162-L189)).

## 2. What the websocket delivers

### Event types

- RTM: "Almost everything that happens in Slack will result in an event being sent to all connected
  clients." The first frame is `{"type": "hello"}`.
  https://docs.slack.dev/legacy/legacy-rtm-api
- Message subtypes (from https://docs.slack.dev/reference/events/message):
  - `message_changed`: "The message property contains the updated message object." Also sent when
    automatic language detection updates metadata.
    https://docs.slack.dev/reference/events/message/message_changed
  - `message_deleted`: carries `deleted_ts`; "The original message will no longer return in history
    calls." It is a hidden subtype.
    https://docs.slack.dev/reference/events/message/message_deleted
  - `message_replied`: "sent when a channel's message is a reply to another message"; a thread reply
    itself arrives as a `message` whose `thread_ts` differs from its `ts`.
    https://docs.slack.dev/reference/events/message/message_replied
  - `thread_broadcast`, `file_share`, `channel_join`, `channel_topic` and the rest of the subtype
    table.
- `reaction_added` "is sent to all connected clients for users who can see the content that was
  reacted to". https://docs.slack.dev/reference/events/reaction_added
- `file_shared` "is sent to all connected clients for all users that have permission to see the
  file". https://docs.slack.dev/reference/events/file_shared
- `channel_joined` "is sent to all connections for a user when that user joins a channel".
  https://docs.slack.dev/reference/events/channel_joined
- Event types handled by clients that use the session socket:
  - wee-slack 3.x: `message` with `message_changed`, `message_deleted`, `message_replied`;
    `reaction_added/removed`; `channel_joined`, `group_joined`, `*_marked`, `thread_marked`,
    `thread_subscribed/unsubscribed`, `channel_rename`, `emoji_changed`, `subteam_*`, `pref_change`,
    `user_status_changed`, `user_invalidated`, `sh_room_*` (huddles), and ignores `file_public`,
    `file_shared`, `file_deleted`, `dnd_updated_user`, `pong`.
    [slack_workspace.py L676-L878](https://github.com/wee-slack/wee-slack/blob/aa904c24a85a50278e271fa6d4ac29838186c800/slack/slack_workspace.py#L676-L878)
  - mautrix-slack: `message`, reactions, typing, `*_marked`, joined/left, `member_joined_channel`,
    `channel_update`, `emoji_changed`, `file_created`, `user_change`, `user_profile_changed`; it
    ignores `file_shared/public/private/change/deleted`, `desktop_notification`, `reconnect_url`
    "intentionally, these are duplicates or do not contain useful information".
    [handleslack.go L113-L160](https://github.com/mautrix/slack/blob/4204d8bd2c4f04d907d7e022465b04e65c47589b/pkg/connector/handleslack.go#L113-L160)
  - emacs-slack's dispatch table lists `channel_archive/unarchive/deleted/rename`, `file_created`,
    `file_deleted`, `file_unshared`, `pin_added/removed`, `star_*`, `team_join`, `user_change`,
    `update_thread_state` and others
    ([slack-websocket.el L435-L533](https://github.com/emacs-slack/emacs-slack/blob/1486b4710f151260f3c1c2acecc5bde552089584/slack-websocket.el#L435-L533)).

### Which conversations

- Member conversations: the Gateway Server subscribes the connection to "all the user's channels"
  on connect (engineering post above). No client in this survey sends a per-channel subscribe, and
  wee-slack receives messages for conversations it has not opened and logs "received websocket
  message for not open conversation, discarding" (L800-L805). So every conversation the user is a
  member of (public, private, DM, group DM) streams without a subscribe step.
- Threads: thread replies in member conversations arrive as `message` events with `thread_ts`,
  whether or not the user follows the thread (they are channel messages fanned out by channel).
  `thread_subscribed`, `thread_unsubscribed` and `thread_marked` report follow and read state.
  **Unconfirmed**: replies to a followed thread in a conversation the user is not a member of.
- Public channels the user is not a member of: **unconfirmed**. The gateway subscribes to the
  user's channels; the web client URL carries `lazy_channels=1` and `no_query_on_subscribe=1`,
  which suggests the client can subscribe to more channels, but no source documents the message.
- Presence: "presence_change events are not dispatched without presence subscriptions established
  with presence_sub" (since January 2018). https://docs.slack.dev/apis/web-api/user-presence-and-status
- Enterprise Grid shared channels can deliver the same message once per connected workspace; dedupe
  by `ts` (RTM page, "Be careful with messages").

## 3. Side effects of connecting

### Presence

- Docs: "A user is active if they have at least one client connected to Slack, and they are not
  marked as 'away'." "After 10 minutes with no activity, the user is automatically marked as away."
  "There's no way to force a user status to active." https://docs.slack.dev/apis/web-api/user-presence-and-status
- Help center: "you're set to active when Slack is open on your desktop or mobile device. You're set
  to away after 10 minutes of desktop inactivity". https://slack.com/help/articles/201864558
- `users.getPresence` for yourself returns `online` ("true if they have a client currently connected
  to Slack"), `auto_away`, `manual_away`, `connection_count` and `last_activity`; for anyone else
  "this method just returns the current presence (either active or away)".
  https://docs.slack.dev/reference/methods/users.getPresence
- Observed with third-party sockets: with only emacs-slack connected for 30 minutes,
  `users.getPresence` returned `"presence": "away", "online": true, "auto_away": true,
  "connection_count": 1`; opening the official client made it `active` with `connection_count: 2`.
  The maintainer: "there was no special API or websocket message, so the official client seems to
  be treated specially." https://github.com/emacs-slack/emacs-slack/issues/506 (2020).
  The same was reported on 2026-04-19 with the current `wss-primary` URL that includes
  `slack_client=desktop`: "the current user continues to appear as 'away' to teammates".
  https://github.com/emacs-slack/emacs-slack/issues/650
- What does make a session active, per client code:
  - `{"type": "tickle"}` over the socket. wee-slack sends it on key presses, or every 20 s with
    `keep_active = always` ("Slack sets you away after 30 minutes of inactivity" in its option text)
    ([register.py L92-L118](https://github.com/wee-slack/wee-slack/blob/aa904c24a85a50278e271fa6d4ac29838186c800/slack/register.py#L92-L118),
    [slack_workspace.py L916-L919](https://github.com/wee-slack/wee-slack/blob/aa904c24a85a50278e271fa6d4ac29838186c800/slack/slack_workspace.py#L916-L919)).
    slack-libpurple sends `tickle` every 60 s when the user is not idle and a bare websocket PONG
    frame otherwise ([slack-rtm.c L138-L148](https://github.com/dylex/slack-libpurple/blob/1115ec117bcddb38ee0ec649f139d04ab3b53942/slack-rtm.c#L138-L148)).
  - `users.setPresence presence=auto` every 7 s, opt-in in emacs-slack
    ([slack-websocket.el L948-L959](https://github.com/emacs-slack/emacs-slack/blob/1486b4710f151260f3c1c2acecc5bde552089584/slack-websocket.el#L948-L959),
    commit [869dc1d](https://github.com/emacs-slack/emacs-slack/commit/869dc1d64d355b2f99a6d9f990ef87e77447788c)).
  - Typing indicators (`typing` / `user_typing` sent by the client) are user actions.
- wee-slack sends a JSON `ping` every 5 s
  ([register.py L176](https://github.com/wee-slack/wee-slack/blob/aa904c24a85a50278e271fa6d4ac29838186c800/slack/register.py#L176))
  and still needs `tickle` to stay active, so a JSON `ping` is not counted as activity. This is an
  inference from client design, **not tested**.
- No connection flag hides the session. `presence_sub` means "Only deliver presence events when
  requested by subscription" and `batch_presence_aware` changes "the shape of presence_change
  events" (rtm.connect docs); both concern other users' presence delivered to you. The only way to
  appear away is `users.setPresence away`, which changes account state and is out of bounds for
  slarc.
- Net effect of a listen-only socket that sends only pings: the user's own `online` and
  `connection_count` change, which only the user can read. Others see `active` or `away`; the
  evidence above says a third-party socket alone leaves it `away`, but whether the first moments of
  a connection count as activity is **unconfirmed**. It should be measured on `naleidworkspace`
  with `users.getPresence` (read-only) before and after connecting.

### Notifications

- "By default, you'll receive mobile notifications one minute after locking your desktop screen" or
  "10 minutes after Slack stops detecting cursor activity." https://slack.com/help/articles/201355156
  Activity there is cursor and screen-lock activity in the official client. **Unconfirmed** whether
  a third-party socket affects mobile push routing; the presence evidence above suggests it does not.
- The socket receives `desktop_notification` events (mautrix-slack ignores them, L144-L146). They are
  data for the client to display; receiving them is not known to change anything.

### Read state

- Nothing in the socket protocol marks messages read. Clients mark read with explicit Web API calls:
  wee-slack `conversations.mark` and `subscriptions.thread.mark`
  ([slack_api.py L441-L456](https://github.com/wee-slack/wee-slack/blob/aa904c24a85a50278e271fa6d4ac29838186c800/slack/slack_api.py#L441-L456)),
  slack-libpurple `conversations.mark`
  ([slack-conversation.c L177](https://github.com/dylex/slack-libpurple/blob/1115ec117bcddb38ee0ec649f139d04ab3b53942/slack-conversation.c#L177)).
  emacs-slack marks read on receipt only when its `:mark-as-read-immediately` option is set
  (https://github.com/emacs-slack/emacs-slack/issues/629). A listener that never calls a `*.mark`
  method changes no read state.

### Logged-in client and access logs

- Access logs record "a user accessing Slack from a specific user, IP address, and user agent
  combination", including "actual logins as well as other API calls that are typically made when
  accessing Slack", with `date_first`, `date_last` and `count`; admins on paid plans can read them.
  https://docs.slack.dev/reference/methods/team.accessLogs
  slarc's Web API calls already appear there. A socket with the same User-Agent and IP should fold
  into the same row; **unconfirmed** whether the socket handshake is logged separately.
- Reusing the desktop session creates no new sign-in, so no "new login" email or session list entry
  is expected. **Unconfirmed**.

### Things a listener must never send

`tickle`, `typing`, `message`, `presence_sub` (harmless but unneeded), and any Web API write
(`users.setPresence`, `presence.set`, `conversations.mark`, `subscriptions.thread.mark`). The RTM
protocol lets a client post messages over the socket (RTM page, "Sending messages"), so the socket
writer should accept only `ping`.

## 4. Reliability

- Connect: `rtm.connect` URLs are "single-use and are only valid for 30 seconds"; an expired URL
  yields `{"type":"error","error":{"code":1,"msg":"Socket URL has expired"}}`. After `hello`, the
  server sends "any events that occurred between the call to rtm.connect and the connection to the
  message server". https://docs.slack.dev/legacy/legacy-rtm-api
- Ping: "When there is no other activity clients should send a ping every few seconds", as JSON
  `{"id": n, "type": "ping"}`; the server answers `pong` with `reply_to` (same page). Intervals in
  practice: wee-slack 5 s and reconnects when nothing has arrived within `network_timeout`
  ([slack_workspace.py L899-L914](https://github.com/wee-slack/wee-slack/blob/aa904c24a85a50278e271fa6d4ac29838186c800/slack/slack_workspace.py#L899-L914));
  slackgo 30 s ([rtm.go L14](https://github.com/beeper/slackgo/blob/4b61f4fcd5e0aef337cd9d3a3e1f03aece47478e/rtm.go#L14));
  emacs-slack 20 s pong timeout, 10 s reconnect delay, up to 360 attempts
  ([slack-team-ws.el L35-L41](https://github.com/emacs-slack/emacs-slack/blob/1486b4710f151260f3c1c2acecc5bde552089584/slack-team-ws.el#L35-L41));
  slack-libpurple 60 s.
- Lifetime: `goodbye` "may be sent by a server that expects it will close the connection after an
  unspecified amount of time. A well formed client should reconnect to avoid data loss." Causes
  listed: "reaching the maximum duration of a RTM web socket connection (8 hours)" and "your
  workspace has been inactive for over two minutes". https://docs.slack.dev/reference/events/goodbye
- Resume: the server sends `reconnect_url` events; the docs say "The reconnect_url event is currently
  unsupported and experimental." https://docs.slack.dev/reference/events/reconnect_url
  wee-slack reconnects to the last `reconnect_url` and, when the new `hello` has
  `fast_reconnect: true`, skips reloading state (commit `bf4940c`, "if fast_reconnect is true after
  reconnecting, it doesn't need to update any state";
  [slack_workspace.py L367-L374, L678-L693](https://github.com/wee-slack/wee-slack/blob/aa904c24a85a50278e271fa6d4ac29838186c800/slack/slack_workspace.py#L678-L693)).
  How long the server keeps a resumable session is **unconfirmed**.
- Missed events: there is no documented replay. Clients catch up with Web API reads. mautrix-slack,
  on a `hello` after a reconnect that was not a resume, calls `client.counts` and backfills every
  conversation whose `latest` is newer than the last bridged message
  ([handleslack.go L89-L98](https://github.com/mautrix/slack/blob/4204d8bd2c4f04d907d7e022465b04e65c47589b/pkg/connector/handleslack.go#L89-L98),
  [client.go L640-L656](https://github.com/mautrix/slack/blob/4204d8bd2c4f04d907d7e022465b04e65c47589b/pkg/connector/client.go#L640-L656),
  [handleslack.go L672-L678](https://github.com/mautrix/slack/blob/4204d8bd2c4f04d907d7e022465b04e65c47589b/pkg/connector/handleslack.go#L672-L678)).
  For slarc this means a listener still needs `pull` after any gap: edits, deletes and reactions on
  old messages that happen while disconnected are not reflected in `latest`.
- Error codes seen on the socket (undocumented): `1` URL expired, `17` rate limited (slackgo sleeps
  one minute), `401` invalid auth
  ([websocket_managed_conn.go L517-L536](https://github.com/beeper/slackgo/blob/4b61f4fcd5e0aef337cd9d3a3e1f03aece47478e/websocket_managed_conn.go#L517-L536)).
- Rate limits: `rtm.connect` is Tier 1, so reconnect loops need backoff. "Clients should not send
  more than one message per second sustained"; a frame over 16 KB disconnects the client (RTM page).
  `presence_sub` and `presence_query` are rate limited (presence page).
- Terms:
  - wee-slack's README on session tokens: "They are not officially supported, and may stop working
    at any time."
    ([README.md, 2.x branch](https://github.com/wee-slack/wee-slack/blob/d4b68872b0e4c493279b3e4de47e4589bd938f28/README.md)).
  - An archived 2019 version of Slack's Acceptable Use Policy forbids accessing the Services "by any
    means other than our publicly supported interfaces (e.g. 'scraping' ...)"
    (https://tostracker.app/document/slack-aup/version/737). The current AUP (last updated
    2025-07-08) is a Salesforce PDF linked from https://slack.com/acceptable-use-policy; its wording
    on this point is **unconfirmed**.
  - The 2025 rate-limit cut for `conversations.history` and `conversations.replies` (1 request per
    minute, 15 objects) applies to commercially distributed non-Marketplace apps.
    https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps
    How Slack limits xoxc client tokens is not documented.

## 5. Cheap change detection for pull

| Call | Documented | Coverage per call | What it gives |
|---|---|---|---|
| `client.counts` | No | All member channels, private channels, group DMs, DMs | Per conversation: `id`, `last_read`, `latest`, `updated`, `history_invalid`, `mention_count`, `has_unreads`; plus `threads {has_unreads, mention_count}`, `channel_badges`, `saved` |
| `users.counts` | No | All member conversations | Older equivalent; per conversation `has_unreads`/`unread_count`, `last_read`, `is_muted` |
| `client.userBoot` | No | Member conversations | Channel and DM objects with `last_read`; its `latest` is reported wrong |
| `conversations.info` | Yes | One conversation | `last_read`, `updated`; `latest` for DMs only |
| `conversations.history` `oldest=<newest stored ts>` `limit=1` | Yes | One conversation | Whether anything newer exists at top level |

- `client.counts`: field list from wee-slack's typings
  ([slack_client_counts.pyi L13-L49](https://github.com/wee-slack/wee-slack/blob/aa904c24a85a50278e271fa6d4ac29838186c800/typings/slack_api/slack_client_counts.pyi#L13-L49))
  and slackgo
  ([users.go L700-L745](https://github.com/beeper/slackgo/blob/4b61f4fcd5e0aef337cd9d3a3e1f03aece47478e/users.go#L700-L745)).
  Parameters used: `thread_counts_by_channel=true`, `org_wide_aware=true`,
  `include_file_channels=true` (slackdump
  [internal/edge/client.go L52-L76](https://github.com/rusq/slackdump/blob/4593753e08deb07dc105365cf9991825398eae87/internal/edge/client.go#L52-L76),
  mautrix-slack [client.go L466-L494](https://github.com/mautrix/slack/blob/4204d8bd2c4f04d907d7e022465b04e65c47589b/pkg/connector/client.go#L466-L494)).
  mautrix-slack calls it only for real-user (session) logins and uses `latest` to decide which
  conversations need backfill. One call covers every member conversation; its rate-limit tier is
  **unconfirmed**.
  Caveats:
  - It can omit DMs with deactivated users (wee-slack commit `74562a3`).
  - It covers only conversations the user is a member of.
  - Whether `latest` moves on a thread reply, an edit, a delete or a reaction is **unconfirmed**.
    The documented `message` page says hidden subtypes such as `message_deleted` "will not appear
    as the latest property on channel, group or im objects", so edits and deletes most likely do
    not move it.
  - What `updated` and `history_invalid` track is **unconfirmed**.
- `users.counts`: slack-libpurple calls it with `mpim_aware=true&only_relevant_ims=true&simple_unreads=true`
  and comments "Private API, not documented. Found by EionRobb (Github)."
  ([slack-conversation.c L102-L105](https://github.com/dylex/slack-libpurple/blob/1115ec117bcddb38ee0ec649f139d04ab3b53942/slack-conversation.c#L102-L105)).
  It is on that client's login path (`slack.c` L375-L378), which a user reported working in January
  2026 (issue #200 above).
- `client.userBoot`: wee-slack: "latest seems to always be present, but is incorrectly set to the
  current timestamp for all conversations, so we delete the key and set it from client.counts
  instead" ([slack_client_userboot.pyi L40](https://github.com/wee-slack/wee-slack/blob/aa904c24a85a50278e271fa6d4ac29838186c800/typings/slack_api/slack_client_userboot.pyi#L40)).
  emacs-slack passes `min_channel_updated` to limit it to recently updated channels
  ([slack-websocket.el L1029-L1045](https://github.com/emacs-slack/emacs-slack/blob/1486b4710f151260f3c1c2acecc5bde552089584/slack-websocket.el#L1029-L1045)).
- `conversations.info`: documented as Tier 3. The conversation object docs say `latest` is "The
  latest message in the channel. no_latest=true for non-first party requests", and that
  `unread_count` is "included for DM conversations only".
  https://docs.slack.dev/reference/objects/conversation-object,
  https://docs.slack.dev/reference/methods/conversations.info
  slarc's own fixture for a channel, taken with the session, has `last_read` and `updated` but no
  `latest` (`tests/fixtures/spike/conversations.info.json`). So it cannot detect new channel
  messages.
- `search.messages` with `after:` (what pull already uses for replies under old parents) is
  documented and already covers new replies per conversation; it is one call per conversation per
  page.

## What this means for slarc

- ADR 0001 says slarc "uses documented Web API methods only ... no `client.*` endpoints". Within
  that rule, a listener is possible only through `rtm.connect` and the URL it returns, and change
  detection stays one documented call per conversation. Using the `wss-primary` URL directly,
  `client.counts` or `users.counts` would need a change to ADR 0001.
- A listener would still need `pull` for correctness: there is no replay after a gap, `latest`
  ignores edits and deletes, and events for non-member public channels are unconfirmed. A
  reasonable shape is "pull on start and after every reconnect that was not a resume, then apply
  events", which is what mautrix-slack does.
- Before any design work, three read-only checks on `naleidworkspace` would turn the main
  unconfirmed points into facts for `docs/slack-facts.md`: `rtm.connect` with the session (and
  whether its URL accepts the `d` cookie), `users.getPresence` before and after a ping-only
  connection, and whether `client.counts` `latest` moves on a thread reply.
