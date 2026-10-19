I'd like to make an app that can authorize with a slack account that I have and create a browsable archive of content from an existing slack instance.

This is for slack apps where I'm a regular member and not an admin will be able to create the full archive. I have that on many slack instances I'm on, but it's very clunky. I want something easier to use and more targeted that does not require that full archive to be used.

For language, I lean towards python with uv to start, but am open to other options (I've written apps in many different languages including golang, rust, zig, java, kotlin, typescript, and javascript). First goal is a working app with decent speed.

Initially looking to create some sort of CLI that is able to do the authorization/archiving and then might be able to do something like have a `serve` subcommand that'll open up a browser app that lets me browse through the messages similar to slack itself. This can be a separate deliverable but would be the goal after we've downloaded and proven we have the content archived in a way that can later be rendered.

Could also be nice to render it as a static html page (or PDF?) that could be shared/sent. I'd want to talk through options there for rich content (like attachments).

Requirements:
- ability to download a channel (including a DM conversation) possibly given a link like https://naleidworkspace.slack.com/archives/C0CA5396G03
- possibly limited to date range
- possibly just a given thread within a channel like https://naleidworkspace.slack.com/archives/C0CA5396G03/p1792410693663869

initial thoughts about data format and what we'd want to be able to store and later recreate:

Necessary content attributes:
- top level messages in the channel
- read/unread status (I might not care much about this if this is expensive to store, maybe this is more of a high watermark on a thread/channel than a per message status?)
- full message threads
- channel
    - name
    - id
    - public/private status
    - is it a DM (direct message)
        - if so is the channel name the concatenation of those in it? does it have a unique ID?
- threads within a channel should be supported
- message attributes:
    - message text
        - not sure what the native format is, can it be markdown? if not, leaving whatever slack has as the format
        - should support all formatting that slack messages can support, code blocks, inline code, italics, links with link text, bold, emojis
    - edit status (whether it was edited)
    - snippet 
        - mime type
        - full text of snippet, not just first 500 lines
        - file name
    - attachments 
        - download blob
        - reference to blob
        - filename
        - mime type?
        - giphy? "posted using /giphy" or other slack app attributes?
    - link preview? (if the user kept it, it's possible for the poster to delete it, which we'd want to honor)
    - author
    - date/timestamp
    - reactions
        - emoji text (ex: :heart:, or :joy:)
        - number of reactions for each emoji
        - reaction author names (maybe number of reactions can be derived?)
    - inline emojis within the text
    - inline mentions knowing who the target person is and we should have access to their user profile
- user profiles
    - name
    - full name
    - profile picture/avatar
    - email address
    - time zone
- emojis
    - emoji text name (ex: :heart: or :custom-emoji:)
    - skin tone (default is a yellow, but many have alternate skin tones that are able to be distinguished)
    - image (might be animated)
    - custom uploaded emojis should be supported

Things I'm not currently worried about for MVP, but might want to expand to later:
- canvas content
- treating apps as anything other than possible authors for messages or something you can have a channel/DM with
- channel organization (whether I have it in a group or not)
- huddles

possible storage/query engines:
- duckdb - this could be great for queries and searching through things
- sqlite3 

generally thinking of treating things as immutable (including edits), but it'd be worth understanding how slack treats edits of messages, do they share an ID and are they then versioned?

I strongly lean towards hexagonal/ports and adapters architecture and would want this to be developed in that style so its very easy to unit test and interactions with real adapters are able to be swapped out in tests. 

I also like the patterns in the Cosmic Python book (repository, service layer, unit of work) and have it checked out at .llm/cosmic-python-book for your reference.

I've downloaded some projects that have some slack integration that I think might be useful to help us figure out some good patterns, these are in the gitignored .llm directory:
- slarchive - an html only "app" that can look at a slack archive file and turn it into something that can be browsed through
- hermes-agent - a full featured agentic platform that has slack integration
- slk - a TUI for slack that could demonstrate good patterns for integrating with slack and getting messages/content
- slack-mcp-server - git@github.com:korotovsky/slack-mcp-server.git 

It'd be worth digging into each of these to see their strengths and if there are any patterns we can gather from them that we can reuse