# slarc

slarc keeps a local, searchable, re-renderable copy of the Slack conversations a person can see, using their own Slack session.

## Storage

**Archive**:
A directory holding slarc's config file and everything pulled into it. One archive can hold several workspaces.
_Avoid_: store, vault, repository, root (except when naming the directory path)

**Pull**:
Copying content from Slack into an archive. The first pull of a conversation, thread or message starts tracking it; later pulls refresh what is tracked.
_Avoid_: sync, archive (as a verb), download, fetch

**Version**:
One observed state of a message. An edit, a reaction change or a removed link preview produces a new version; earlier versions are kept.
_Avoid_: revision (for a message version), history entry

**Tracked item**:
A conversation, thread or message the archive keeps pulling. The first pull of it starts tracking it, and removing it stops.
_Avoid_: target, subscription, watched item

**Run**:
One invocation of a command that writes to the archive, such as a pull or a removal, with a record of when it started, what it did and how it ended.
_Avoid_: job, session, execution

**Batch**:
The rows one run writes for one unit of its work, such as refreshing one tracked item or one page of a backfill.
_Avoid_: chunk, transaction

**Commit**:
The record that makes a batch part of the archive, together with the progress that batch earned. A batch without a commit is not part of the archive. Git's commits are always called "git commits".
_Avoid_: checkpoint, save, flush

**Directory**:
What an archive knows about a workspace besides its messages: users, custom emoji, conversations, bots, usergroups, files and images.
_Avoid_: metadata, catalog (the archive's catalog is a storage detail)

**Maintenance**:
The step at the end of a pull or removal that tidies the archive's files and, when no other command has the archive open, frees the space of what nothing needs any more.
_Avoid_: cleanup, garbage collection, vacuum (except for the database step of that name)

**Window**:
The span of posting times a command limits messages to, from a start to an end, either of which may be open.
_Avoid_: range, date range, period

**Owning machine**:
The one machine allowed to open an archive. Another machine must take ownership, once a synced folder has finished syncing, before it opens the archive.
_Avoid_: host (except in messages naming it), owner (unqualified)

## Slack

**Workspace**:
One Slack instance, identified by its team id and reached at its own `*.slack.com` domain.
_Avoid_: team, instance, org, server

**Conversation**:
Any place messages are posted: a channel, a DM or a group DM.
_Avoid_: channel (when the kind doesn't matter), room, chat

**Channel**:
A named conversation, public or private.
_Avoid_: group

**DM**:
A conversation between the user and exactly one other person or app.
_Avoid_: IM, direct message channel

**Group DM**:
An unnamed conversation between three or more people.
_Avoid_: MPIM, private group

**Slack-archived**:
A conversation that Slack has made read-only. Unrelated to an Archive.
_Avoid_: archived (unqualified), closed

**Thread**:
A parent message plus every reply to it.
_Avoid_: conversation, reply chain

**Broadcast reply**:
A thread reply that was also sent to the conversation, so it appears in both places.
_Avoid_: thread broadcast, cross-post

**Snippet**:
A text file posted to Slack as a snippet, which slarc keeps in full and shows as text.
_Avoid_: snippet for the text search shows around a match; that is the excerpt

**Deleted message**:
A message the archive still holds after Slack stopped returning it, marked with when slarc noticed.
_Avoid_: removed message, tombstone (except for Slack's own placeholder for a deleted parent that still has replies)
