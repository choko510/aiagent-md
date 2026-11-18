"""FA-T04..T07, T29..T35: CLI gates, local quota, logging and production isolation."""

from __future__ import annotations

import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NoReturn

import httpx
import pytest

from devtools.free_ai import catalog, runner, transport

_REAL_NEW_CLIENT = transport._new_client
_REPO_ROOT = Path(__file__).resolve().parents[3]
_HOSTS = (
    "ai-gateway.kilo.ai",
    # The retired Kilo host must not reappear in production wiring either.
    "api.kilo.ai",
    "oai.endpoints.kepler.ai.cloud.ovh.net",
    "opencode.ai",
    "text.pollinations.ai",
)
_SCAN_TEXT_SUFFIXES = {
    ".py",
    ".toml",
    ".lock",
    ".yml",
    ".yaml",
    ".json",
    ".sh",
    ".ts",
    ".tsx",
    ".js",
    ".md",
    ".dockerfile",
    ".txt",
}
_SKIP_DIRS = {".venv", "node_modules", "__pycache__", ".git", ".runs", "dist", "build"}


def _blocked(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("offline test attempted to open a network socket")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)


def _temp_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    runs = tmp_path / ".runs"
    monkeypatch.setattr(runner, "RUNS_DIR", runs)
    monkeypatch.setattr(runner, "QUOTA_FILE", runs / "quota.json")
    monkeypatch.setattr(runner, "LOCK_FILE", runs / "lock")
    monkeypatch.setattr(runner, "LOG_FILE", runs / "probes.jsonl")
    monkeypatch.setattr(runner, "REVIEW_SCORE_LOG_FILE", runs / "review_scores.jsonl")
    return runs


def _mock_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(transport, "_new_client", lambda: _REAL_NEW_CLIENT(httpx.MockTransport(recording)))
    return requests


def _json_ok(content: str = "こんにちは") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "model": "nvidia/nemotron-3.5-super-vl:free",
            "choices": [{"message": {"role": "assistant", "content": content}}],
        },
    )


def _probe_argv(*extra: str, provider: str = "kilo", fixture: str = "jp_hello") -> list[str]:
    return [
        "probe",
        "--provider",
        provider,
        "--fixture",
        fixture,
        "--ack-synthetic-only",
        "--ack-external-request",
        *extra,
    ]


def _last_record(out: str) -> dict[str, object]:
    lines = [line for line in out.splitlines() if line.startswith("{")]
    assert lines, "probe must print one JSON record"
    parsed = json.loads(lines[-1])
    assert isinstance(parsed, dict)
    return parsed


def test_fa_t04_missing_acknowledgements_send_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok())
    without_both = ["probe", "--provider", "kilo", "--fixture", "jp_hello"]
    only_one = ["--ack-synthetic-only"]
    only_other = ["--ack-external-request"]
    for extra in ([], only_one, only_other):
        with pytest.raises(SystemExit) as excinfo:
            runner.main([*without_both, *extra])
        assert excinfo.value.code == 2
    assert requests == []
    assert not runs.exists()
    capsys.readouterr()


@pytest.mark.parametrize("provider", ["jev_legacy"])
def test_fa_t05_chat_fixture_is_never_sent_to_jev(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    provider: str,
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok())
    code = runner.main(_probe_argv("--allow-legacy", provider=provider, fixture="jp_hello"))
    record = _last_record(capsys.readouterr().out)
    assert code == 2
    assert record["status"] == "LOCAL_INPUT_INVALID"
    assert requests == []


def test_fa_t06_refund_fixture_is_never_sent_to_chat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok())
    code = runner.main(_probe_argv(provider="kilo", fixture="refund_noul"))
    record = _last_record(capsys.readouterr().out)
    assert code == 2
    assert record["status"] == "LOCAL_INPUT_INVALID"
    assert requests == []


@pytest.mark.parametrize(
    ("provider", "fixture"),
    [("jev_legacy", "refund_noul"), ("pollinations_legacy", "jp_hello")],
)
def test_fa_t07_legacy_services_are_skipped_without_confirmed_terms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    provider: str,
    fixture: str,
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok())
    plain = runner.main(_probe_argv(provider=provider, fixture=fixture))
    plain_record = _last_record(capsys.readouterr().out)
    assert plain == 2
    assert plain_record["status"] == "UNVERIFIED_TERMS"
    assert plain_record["reason_code"] == "LEGACY_DISABLED"

    legacy = runner.main(_probe_argv("--allow-legacy", provider=provider, fixture=fixture))
    legacy_record = _last_record(capsys.readouterr().out)
    assert legacy == 2
    assert legacy_record["status"] == "UNVERIFIED_TERMS"
    assert requests == []


def test_fa_t29_held_lock_blocks_the_second_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    runs.mkdir(parents=True)
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok())

    with runner._exclusive_run():
        code = runner.main(_probe_argv())
    in_process = _last_record(capsys.readouterr().out)
    assert code == 5
    assert in_process["status"] == "SAFETY_BLOCKED"
    assert requests == []

    holder = tmp_path / "hold_lock.py"
    holder.write_text(
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(holder), str(runs / "lock")],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "locked"
        code = runner.main(_probe_argv())
        assert code == 5
        assert requests == []
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    capsys.readouterr()


def _write_quota(path: Path, provider: str, *, last_utc_epoch: float, local_day: str, day_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {
                    provider: {
                        "last_utc_epoch": last_utc_epoch,
                        "local_day": local_day,
                        "day_count": day_count,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_fa_t30_daily_cap_and_interval_block_sends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok())
    today = time.strftime("%Y-%m-%d", time.gmtime())

    _write_quota(runs / "quota.json", "kilo", last_utc_epoch=time.time() - 1000, local_day=today, day_count=16)
    code = runner.main(_probe_argv())
    capped = _last_record(capsys.readouterr().out)
    assert code == 2
    assert capped["status"] == "RATE_LIMITED"
    assert requests == []

    _write_quota(runs / "quota.json", "kilo", last_utc_epoch=time.time() - 1, local_day=today, day_count=3)
    code = runner.main(_probe_argv())
    too_soon = _last_record(capsys.readouterr().out)
    assert code == 2
    assert too_soon["status"] == "RATE_LIMITED"
    assert requests == []

    _write_quota(runs / "quota.json", "kilo", last_utc_epoch=time.time() + 3600, local_day=today, day_count=1)
    code = runner.main(_probe_argv())
    backwards = _last_record(capsys.readouterr().out)
    assert code == 5
    assert backwards["status"] == "SAFETY_BLOCKED"
    assert requests == []

    (runs / "quota.json").write_text("{not json", encoding="utf-8")
    code = runner.main(_probe_argv())
    corrupt = _last_record(capsys.readouterr().out)
    assert code == 5
    assert corrupt["status"] == "SAFETY_BLOCKED"
    assert requests == []


def test_fa_t30_unwritable_quota_state_blocks_before_sending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    (runs / "quota.json").mkdir(parents=True)
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok())
    code = runner.main(_probe_argv())
    record = _last_record(capsys.readouterr().out)
    assert code == 5
    assert record["status"] == "SAFETY_BLOCKED"
    assert requests == []


def test_fa_t30_a_failed_send_still_consumes_the_local_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)

    def connection_failure(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    requests = _mock_transport(monkeypatch, connection_failure)
    assert runner.main(_probe_argv()) == 3
    record = _last_record(capsys.readouterr().out)
    assert record["status"] == "NETWORK_ERROR"
    assert len(requests) == 1

    quota = json.loads((runs / "quota.json").read_text(encoding="utf-8"))
    assert quota["providers"]["kilo"]["day_count"] == 1

    assert runner.main(_probe_argv()) == 2
    second = _last_record(capsys.readouterr().out)
    assert second["status"] == "RATE_LIMITED"
    assert len(requests) == 1


def test_fa_t31_log_is_minimal_and_runs_directory_is_git_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    _mock_transport(monkeypatch, lambda _request: _json_ok())
    assert runner.main(_probe_argv()) == 0
    capsys.readouterr()

    lines = (runs / "probes.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert list(record) == [
        "schema_version",
        "provider_id",
        "model_requested",
        "model_observed",
        "fixture_id",
        "observed_at_utc",
        "http_status",
        "status",
        "reason_code",
        "elapsed_ms",
        "input_tokens",
        "output_tokens",
        "cost_reported_usd",
    ]
    for forbidden in ("日本語", "こんにちは", "authorization", "cookie", "prompt", "reasoning", "response"):
        assert forbidden not in lines[0].lower()

    gitignore = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "devtools/free_ai/.runs/" in gitignore
    git = shutil.which("git")
    if git is None:  # pragma: no cover - git is present in CI
        pytest.skip("git is unavailable")
    ignored = subprocess.run(  # noqa: S603
        [git, "check-ignore", "-q", "devtools/free_ai/.runs/probes.jsonl"],
        cwd=_REPO_ROOT,
        check=False,
    )
    assert ignored.returncode == 0
    tracked = subprocess.run(  # noqa: S603
        [git, "ls-files", "devtools/free_ai/.runs"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.stdout.strip() == ""


def test_fa_t32_production_code_contains_no_lab_wiring() -> None:
    production_roots = [_REPO_ROOT / "backend" / "packages", _REPO_ROOT / "backend" / "tools"]
    py_files: list[Path] = []
    for root in production_roots:
        py_files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    assert py_files
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        assert "devtools" not in roots, f"{path} imports devtools"

    scanned_roots = [_REPO_ROOT / "backend", _REPO_ROOT / "deploy", _REPO_ROOT / "web" / "src"]
    for root in scanned_roots:
        for path in root.rglob("*"):
            if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.suffix not in _SCAN_TEXT_SUFFIXES and path.name != "Dockerfile":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for host in _HOSTS:
                assert host not in text, f"{path} references {host}"

    dockerfile = (_REPO_ROOT / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "devtools" not in dockerfile
    assert "free_ai" not in dockerfile

    for workflow in (_REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        for host in _HOSTS:
            assert host not in text, f"{workflow} references {host}"
        assert "devtools.free_ai probe" not in text
        assert "devtools.free_ai probe \\" not in text

    lab_workflow = (_REPO_ROOT / ".github" / "workflows" / "dev-free-ai-offline.yml").read_text(encoding="utf-8")
    assert "devtools/free_ai/tests" in lab_workflow
    assert "schedule:" not in lab_workflow
    assert "workflow_dispatch" not in lab_workflow
    assert "secrets." not in lab_workflow


@pytest.mark.parametrize(
    "flag",
    [
        "--url",
        "--base-url",
        "--model",
        "--prompt",
        "--file",
        "--repo",
        "--all",
        "--repeat",
        "--concurrency",
        "--api-key",
        "--token",
        "--proxy",
        "--execute-generated-code",
        "--fallback-to-paid",
    ],
)
def test_fa_t33_forbidden_flags_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok())
    with pytest.raises(SystemExit) as excinfo:
        runner.main([*_probe_argv(), flag, "value"])
    assert excinfo.value.code == 2
    assert requests == []
    assert not runs.exists()
    capsys.readouterr()


def test_fa_t35_no_subcommand_prints_help_and_exits_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    assert runner.main([]) == 2
    out = capsys.readouterr().out
    assert "usage:" in out
    assert not runs.exists()


def test_fa_t35_cli_subprocess_ignores_decoy_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    decoy = "decoy-value-must-not-appear"
    environment = {
        **os.environ,
        "AIAGENT_API_KEY": decoy,
        "OPENAI_API_KEY": decoy,
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "devtools.free_ai", "list"],
        cwd=_REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0
    assert "kilo" in completed.stdout
    assert decoy not in completed.stdout
    assert decoy not in completed.stderr
    assert not (tmp_path / ".runs").exists()


def test_every_provider_stays_unavailable_until_probed() -> None:
    for spec in catalog.catalog_specs():
        assert spec.provider_id in catalog.PROVIDERS
        catalog.validate_spec(spec)


def _iter_lab_sources() -> Iterator[Path]:
    yield from (_REPO_ROOT / "devtools" / "free_ai").rglob("*.py")


def test_lab_modules_do_not_read_files_or_environment_outside_runner() -> None:
    for path in _iter_lab_sources():
        if path.name in {"runner.py", "transport.py"} or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "subprocess" for alias in node.names), path
            if isinstance(node, ast.ImportFrom):
                assert node.module != "subprocess", path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "open", f"{path} opens a file"
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert not (node.value.id == "os" and node.attr in {"environ", "getenv"}), (
                    f"{path} reads the environment"
                )


_TODAY = time.strftime("%Y-%m-%d", time.gmtime())
_HUGE_DIGITS = "9" * 309


def _quota_text(*, timestamp: str, day_count: str = "1", extra_provider: bool = False) -> str:
    entry = f'{{"last_utc_epoch":{timestamp},"local_day":"{_TODAY}","day_count":{day_count}}}'
    providers = f'"kilo":{entry}' + (f',"kilo":{entry}' if extra_provider else "")
    return f'{{"version":1,"providers":{{{providers}}}}}'


@pytest.mark.parametrize(
    "quota_text",
    [
        _quota_text(timestamp="NaN"),
        _quota_text(timestamp="Infinity"),
        _quota_text(timestamp="-Infinity"),
        _quota_text(timestamp="1e309"),
        _quota_text(timestamp=_HUGE_DIGITS),
        _quota_text(timestamp="1799999999", day_count=_HUGE_DIGITS),
        _quota_text(timestamp="1799999999", day_count="1.5"),
        _quota_text(timestamp="1799999999", extra_provider=True),
        '{"version":1,"version":1,"providers":{}}',
    ],
)
def test_quota_non_finite_or_absurd_values_block_every_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    quota_text: str,
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    runs.mkdir(parents=True)
    (runs / "quota.json").write_text(quota_text, encoding="utf-8")
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok())

    code = runner.main(_probe_argv())
    printed = capsys.readouterr().out
    record = _last_record(printed)
    assert code == 5
    assert record["status"] == "SAFETY_BLOCKED"
    assert requests == []
    for forbidden in ("Overflow", "invalid literal", "traceback", "Error"):
        assert forbidden not in printed
