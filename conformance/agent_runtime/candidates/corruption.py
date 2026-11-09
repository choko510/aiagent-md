"""Fault injection on persisted framework state (PYRT-T04). Pure SQL: imports neither framework.

garbage: undecodable bytes. swap: well-formed state whose recorded tool call differs from HEAD
("widget" -> "wodget", same length so pickle / msgpack framing stays valid).
"""

from __future__ import annotations

import base64

from aiagent_conformance.control import Control


class CorruptA:
    """DBOS system tables (schema `dbos`), outputs are base64 pickles by default."""

    @staticmethod
    def corrupt_static(ctl: Control, run_id: str, mode: str) -> int:
        ref = ctl.run(run_id)["framework_ref"]
        rows = ctl.db.execute(
            "SELECT function_id, output FROM dbos.operation_outputs WHERE workflow_uuid = %s AND output IS NOT NULL", (ref,)
        ).fetchall()
        n = 0
        for r in rows:
            raw = base64.b64decode(r["output"])
            if mode == "garbage":
                new = "Z2FyYmFnZQ=="  # b"garbage"
            elif b"widget" in raw:
                new = base64.b64encode(raw.replace(b"widget", b"wodget")).decode()
            else:
                continue
            ctl.db.execute("UPDATE dbos.operation_outputs SET output=%s WHERE workflow_uuid=%s AND function_id=%s",
                           (new, ref, r["function_id"]))
            n += 1
        return n


class CorruptB:
    """langgraph-checkpoint-postgres tables (schema `public`), msgpack blobs."""

    @staticmethod
    def corrupt_static(ctl: Control, run_id: str, mode: str) -> int:
        ref = ctl.run(run_id)["framework_ref"]
        n = 0
        for table, keys in (("checkpoint_blobs", ("checkpoint_ns", "channel", "version")),
                            ("checkpoint_writes", ("checkpoint_ns", "checkpoint_id", "task_id", "idx"))):
            rows = ctl.db.execute(f"SELECT {', '.join(keys)}, blob FROM public.{table} WHERE thread_id = %s", (ref,)).fetchall()
            for r in rows:
                blob = bytes(r["blob"]) if r["blob"] is not None else None
                if blob is None:
                    continue
                if mode == "garbage":
                    new = b"\xc1\xde\xad\xbe\xef"  # 0xc1 is never valid msgpack
                elif b"widget" in blob:
                    new = blob.replace(b"widget", b"wodget")
                else:
                    continue
                where = " AND ".join(f"{k} = %s" for k in keys)
                ctl.db.execute(f"UPDATE public.{table} SET blob = %s WHERE thread_id = %s AND {where}",
                               (new, ref, *(r[k] for k in keys)))
                n += 1
        return n
