# AgentRuntimePort conformance suite (ADR-038 STEP 2)

共通適合試験とFixture。STEP 3以降のRuntime実装・Framework交換時にも同じ試験を通す。**PoC用のFake Gateway/簡略DDLであり、本番実装・本番DDLではない**（正本DDLはCHAT_10）。

| Path | 役割 |
|---|---|
| `aiagent_conformance/control.py`, `schema.sql` | aiagent-md側の正本（Run claim/fence、Goal revision、追記専用Checkpoint HEAD、Approval、QueuePort fixture） |
| `aiagent_conformance/gateways.py` | Fake Model Gateway（admission・credential保持）、Fake Tool Gateway（schema→goal/fence→approval→Effect Ledger→executor）、Framework内Tool stub |
| `aiagent_conformance/fixtures.py` | 両候補共通のFakeModel policy（状態ベース、tool_call_idは毎回新規発行） |
| `aiagent_conformance/worker.py` | Worker 1プロセス。`AIAGENT_FAULT`で指定点`os._exit(137)` |
| `aiagent_conformance/bench.py` | PYRT-T05計測（RSS、CPU、時間、WAL、tuple書込） |
| `candidates/candidate_a.py` / `candidate_b.py` | 候補A（PydanticAI＋DBOS）/ 候補B（DeepAgents＋LangGraph）のAdapter |
| `candidates/corruption.py` | Framework永続状態の破損注入（SQLのみ） |
| `tests/` | PYRT-T00〜T12（同一fixture、実プロセスkill） |
| `requirements-{a,b}.lock.txt` | 検証時の推移依存を固定（Python 3.13） |
| `results/` | 計測結果（環境依存。同一実行内でのみ比較可） |

```bash
python -m venv .venv && .venv/bin/python -m pip install -r requirements-b.lock.txt
AIAGENT_PG_URL=postgresql://postgres:pw@127.0.0.1:5432 AIAGENT_CANDIDATES=b AIAGENT_PY_B=$PWD/.venv/bin/python .venv/bin/python -m pytest tests -q
.venv/bin/python -m aiagent_conformance.bench --candidate b --python .venv/bin/python --pg postgresql://postgres:pw@127.0.0.1:5432 -n 10
```

外部送信・実Provider・実Credentialは使わない。`external_sends`表への1行が「不可逆な外部副作用1回」を表す。
