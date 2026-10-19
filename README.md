# slarc

Archive the Slack conversations you can see into files you own: searchable with `rg` or DuckDB, re-renderable as HTML, and suited to keeping in git.

slarc logs in with the session your Slack desktop app already holds, so it needs no Slack app install and no admin approval. It only reads from Slack; it never posts, reacts or marks anything read.

Status: early development. See `docs/specs/detailed-requirements.md` for the design.

## Planned usage

```sh
uv tool install git+https://github.com/tednaleid/slarc

slarc pull https://yourworkspace.slack.com/archives/C0123456789              # a channel
slarc pull https://yourworkspace.slack.com/archives/C0123456789/p1712345678000100  # a thread
slarc pull                                                                    # refresh everything tracked
slarc search "deploy"
slarc export https://yourworkspace.slack.com/archives/C0123456789 --out ./share
```

Automated access can trigger security alerts in managed Slack workspaces. Check your organization's policies before using slarc there.

## License

MIT
