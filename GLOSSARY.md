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
_Avoid_: revision, history entry

**Tracked item**:
A conversation, thread or message the archive keeps pulling. The first pull of it starts tracking it, and removing it stops.
_Avoid_: target, subscription, watched item

**Tag**:
One stretch of tracking a tracked item, from the pull that starts it to the removal that ends it. Tracking the same item again after a removal starts a new tag.
_Avoid_: tracking id, generation

**Run**:
One invocation of a command that writes to the archive, such as a pull or a removal, with a record of when it started, what it did and how it ended.
_Avoid_: job, session, execution

**Batch**:
The lines one run writes for one unit of its work, such as refreshing one tracked item or one page of a backfill.
_Avoid_: chunk, transaction

**Commit**:
The record that makes a batch part of the archive, together with the progress that batch earned. A batch without a commit is not part of the archive. Git's commits are always called "git commits".
_Avoid_: checkpoint, save, flush

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

**Deleted message**:
A message the archive still holds after Slack stopped returning it, marked with when slarc noticed.
_Avoid_: removed message, tombstone (except for Slack's own placeholder for a deleted parent that still has replies)
