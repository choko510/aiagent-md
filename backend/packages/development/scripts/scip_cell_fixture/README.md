# Disposable SCIP Cell fixture

This is an **opt-in test image**, not a production image or authenticated
execution environment. `build_fixture.py` explicitly downloads the npm
archive at build time, verifies its published SHA-512, rejects unsafe archive
members, and builds the Dockerfile. The image contains Node.js 22.23.5 and the
bundled `@sourcegraph/scip-python` 0.6.8 (MIT). The Dockerfile also obtains Git
and Python test dependencies during image build. Those transitive packages are
not fully pinned or security-attested, so rebuilding does not guarantee the
same image digest. No npm/pip install occurs during the indexing test.

Source and usage: [scip-python](https://github.com/sourcegraph/scip-python),
[license](https://github.com/sourcegraph/scip-python/blob/scip/LICENSE.txt),
[security policy](https://github.com/sourcegraph/scip-python/blob/scip/SECURITY.md).
The package's documented `--environment` option skips environment discovery;
the fixture uses a fixed empty JSON list. A search of advisories is not proof
that the transitive runtime is vulnerability-free.

Example (Linux/WSL, Docker):

```sh
python3 packages/development/scripts/scip_cell_fixture/build_fixture.py
fixture_image=$(docker image inspect codex-dev13-scip-cell-fixture --format '{{.Id}}')
docker run --rm --read-only --network none --cap-add SYS_ADMIN \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --memory 2g --pids-limit 256 --tmpfs /tmp:exec,mode=1777,size=256m \
  -e AIAGENT_SCIP_FIXTURE=1 -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/w/packages/domain/src:/w/packages/core/src:/w/packages/gateways/src:/w/packages/sandbox/src:/w/packages/development/src \
  -v "$PWD:/w:ro" -w /w "$fixture_image" \
  python -m pytest -q -s --noconftest -p no:cacheprovider -c /dev/null tests/test_scip_supervisor_fixture.py
```

Run from `backend`. The read-only `/w` mount supplies only test harness code;
the source under analysis is a synthetic Git repository created inside the
container and passed to the child through a staged descriptor. Landlock does
not grant the indexer access to `/w`. Docker shares the host kernel, and these
test options are not production isolation or network-policy attestation.

With a disposable PostgreSQL cluster available, the complete canonical test
uses the same fixture image and the real Tool Gateway and broker code:

```sh
AIAGENT_TEST_PG_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres \
  AIAGENT_SCIP_E2E=1 uv run pytest -q tests/test_scip_production_e2e.py
```

The test inspects and runs the fixture image by immutable local image ID. Keys,
operator approvals and source repository are disposable fixtures. The Docker
Supervisor still shares the host kernel and does not attest a production image.
The fixture exercises the normal Supervisor entry point with explicit source
broker configuration; that broker is disabled unless explicitly configured.

On 2026-11-17, local fixture image ID
`sha256:f55bdb467b9004fe256482225ef553ec88dbab3b7c4a71e8462ea403c70689ae`
produced a 1,362-byte index twice with 11 reported occurrences in two source
files; B1 returned PARTIAL because scip-python omitted position encoding. Wall
times were 483 ms and 365 ms; cumulative child CPU deltas were 0.615 s and
0.548 s. The process-wide waited-children RSS high-water was 162,776 KiB, not
an isolated indexer-only peak. These measurements are fixture-specific.
The test bypasses an authenticated Source/Artifact Broker and canonical
Execution authority; its provenance remains `UNVERIFIED_INDEXER_ORIGIN`.
