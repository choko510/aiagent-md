# Round 4 actual-execution audit harness — TEST BRANCH ONLY

This branch branches off **PR #10 HEAD**, not the current main. It does not represent the implementation of the Personal Agent Harness.

The GitHub Actions workflow runs a real `postgres:16` instance and extracts the **proposed SQL from the Markdown** verbatim, then checks whether it executes and required tables exist. Failures are valid audit findings. It also runs a **generic Docker isolation primitive test** and **Chromium local navigation fixture**; these cannot certify the future Harness's Docker isolation, SSRF protection, tool/browser network gateway, or real deployment.

Do not merge this branch into the specs without review. Avoid embedding secrets. Tests are isolated local fixtures and no external buying/posts/actions occur.

A pass (if obtained) means precisely what each test asserts; a fail must not be silently marked pass.
