# slarc

Archive the Slack conversations you can see into files you own: searchable with `rg` or DuckDB, re-renderable as HTML, and suited to keeping in git.

slarc logs in with the session your Slack desktop app already holds, so it needs no Slack app install and no admin approval. It only reads from Slack; it never posts, reacts or marks anything read.

Status: early development. See `docs/specs/detailed-requirements.md` for the design.

## Usage

```sh
uv tool install git+https://github.com/tednaleid/slarc

slarc auth                                                                    # the Slack sessions found, and who you are in each workspace
slarc pull https://yourworkspace.slack.com/archives/C0123456789              # pull a conversation and start tracking it
slarc pull https://yourworkspace.slack.com/archives/C0123456789/p1712345678000100  # a thread or single message
slarc pull                                                                    # refresh everything tracked
slarc pull --full                                                             # re-read everything tracked, catching edits and deletes
slarc list                                                                    # the archive, its config files and each tracked item
slarc search "deploy" --in '#general'                                         # archived messages, newest first, with their Slack links
slarc sql "SELECT channel_id, count(*) FROM message GROUP BY 1"               # DuckDB over the archive's views
slarc export https://yourworkspace.slack.com/archives/C0123456789 --out ./share  # static HTML
```

The first `pull` offers to create an archive (a directory with `slarc.toml`) in the current directory. Other commands find it from the current directory, `--root` or `SLARC_ARCHIVE`.

Automated access can trigger security alerts in managed Slack workspaces. Check your organization's policies before using slarc there.

## License

MIT
