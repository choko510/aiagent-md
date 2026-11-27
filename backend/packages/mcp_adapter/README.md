# aiagent-mcp-adapter

Generic, provider-agnostic MCP adapter foundation for aiagent-md.

The canonical behaviour is `docs/03-tools/mcp-adapter.md`; the implementation record and
verification status are in `docs/03-tools/mcp-adapter-implementation.md` and
`docs/11-qa/mcp-adapter-verification.md`.

Boundaries enforced by this package:

* MCP metadata (descriptions, annotations, `_meta`, server instructions, schemas) never grants authority.
* Every HTTP byte goes through an injected `McpEgress` port. This package contains no direct-origin
  network path and no stdio transport.
* `tools/list` is traversed page by page with finite limits and an all-or-nothing publish.
* Registry snapshots are immutable, generation-fenced and expire; stale ToolRefs cannot execute.
* Tools execute only with a Gateway-issued binding (intent, permit, generation, schema digest, args digest).
* A dispatched non-read call whose outcome is unknown is reported as UNKNOWN, never retried here.

Feature flag: the adapter is disabled unless `MCP_ADAPTER_ENABLED=1` **and** an admin-approved provider
policy exists. Tests use explicit test-only egress implementations under `backend/tests/`.