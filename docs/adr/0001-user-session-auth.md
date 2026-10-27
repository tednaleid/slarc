# Authenticate with the user's own Slack session, not a Slack app

slarc reads the `xoxc` token and `d` cookie that the Slack desktop app (or a browser) already holds, and calls the documented Web API with them. It sees exactly what the user sees, with nothing installed in the workspace and no admin approval needed. Slack app tokens were rejected: they need a workspace install that a regular member often can't get, bot tokens can't see DMs, and since 2025 apps outside the Marketplace are limited to a handful of history requests per minute. slarc is unsanctioned by Slack on purpose; if the client protocol changes, only the Slack adapter changes.

## Consequences

- slarc never calls a method that changes anything in Slack. Posting to the test workspace happens only in a separate script that is not part of the tool.
- slarc uses documented Web API methods only, with an honest User-Agent: no `client.*` endpoints and no browser impersonation. For pushed events it uses the documented `rtm.connect` and the websocket URL it returns, never the desktop client's own socket URL.
- Credentials are read fresh from the desktop app on every run and are never written into an archive, log or fixture.
