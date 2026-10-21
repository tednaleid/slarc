# Ports and adapters with in-memory fakes, a service layer and a unit of work, and no message bus

slarc's domain and service layer reach Slack, the filesystem, DuckDB, HTTP and the OS keychain only through ports in `src/slarc/ports.py`. Each port has a real adapter and, except `QueryIndex`, an in-memory fake in `tests/fakes.py`. The service layer has one function per use case that takes its ports as arguments, a unit of work scopes each pull run and writes its run row however the run ends, and `bootstrap.py` is the one place real adapters are built. We chose this so that pull's hard parts (resuming, reply discovery, delete inference, multi-workspace skips) can be tested in milliseconds against fakes with no network or disk, and so that a change in how Slack is reached touches one adapter. We left out the message bus and domain events of the Cosmic Python shape: a pull is one straight sequence (directory, items, downloads, run row), and the one deferred step, downloading, is a list of jobs the service collects and hands to the `Downloader` at the end.

## Considered Options

- Service code calling `slack_sdk`, DuckDB and the filesystem directly, tested with HTTP mocks and temp directories: slower tests, and every test would depend on the exact shape of each library call.
- A message bus with commands, events and handlers: indirection with no second consumer of any event, which makes a single pull harder to follow.
- A per-adapter fake for `QueryIndex`: it would have to reimplement the SQL views, and a fake that disagrees with DuckDB hides the bugs worth catching. DuckDB over a temp archive is fast enough to use directly.

## Consequences

- Every new outside dependency gets a port, a real adapter and a fake, and the fake must honor the port's errors and paging so service tests stay honest. Contract tests run the archive and blob stores' real adapters and fakes through the same tests.
- Domain and service modules import no `slack_sdk`, `duckdb`, `httpx` or `keyring` and do no file or environment I/O; reading immutable package data with `importlib.resources` is the recorded exception (`docs/architecture.md`).
- The unit of work is one class used with real or in-memory stores; there is no separate in-memory unit of work.
- If a later feature needs several independent reactions to one event (for example `serve` updating a view while a pull runs), revisit the bus then rather than adding it ahead of need.
