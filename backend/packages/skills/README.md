# aiagent-skills

Immutable, content-addressed Agent Skill lifecycle for aiagent-md (MKT-2).

The package owns manifest parsing, bundle/archive inspection, quarantine, security findings,
release/installation state, capability intersection, run pins and progressive disclosure.
It never executes Skill content, never grants tools and never talks to the network directly:
source adapters require an injected egress port.

See `docs/06-memory-skills/skill-lifecycle-implementation.md` for the operational contract.
