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
slarc pull acme                                                               # refresh everything tracked in workspace acme
slarc pull --full                                                             # re-read everything tracked, catching edits and deletes
slarc list                                                                    # the archive, its config files and each tracked item
slarc search "deploy" --in '#general'                                         # archived messages, newest first, with their Slack links
slarc sql "SELECT channel_id, count(*) FROM message GROUP BY 1"               # DuckDB over the archive's views
slarc export https://yourworkspace.slack.com/archives/C0123456789 --out ./share  # static HTML
slarc export acme                                                             # static HTML of everything tracked in workspace acme
slarc rm https://yourworkspace.slack.com/archives/C0123456789                 # stop tracking it and delete what only it kept (asks first)
slarc --take-ownership list                                                   # take the archive over from the machine that last used it, once it has synced
```

The first `pull` offers to create an archive (a directory with `slarc.toml`) in the current directory. Every command finds the archive from `--root`, then `SLARC_ARCHIVE`, then the current directory or its parents, then `default_archive` in the global config.

Automated access can trigger security alerts in managed Slack workspaces. Check your organization's policies before using slarc there.

## License

MIT
