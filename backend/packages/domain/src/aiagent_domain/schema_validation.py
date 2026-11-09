"""Canonical JSON Schema validator with the x-int64-min / x-int64-max extension keywords.

A regex cannot express the int64 range on decimal strings ("9223372036854775808" matches ^[1-9][0-9]{0,18}$), so
every non-Pydantic consumer of contracts/domain must validate with these keywords (the TS UI codec does the same).
Requires the optional `jsonschema` dependency (dev/test, contract tooling).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import validators
from referencing import Registry, Resource


def _range(kind: str) -> Any:
    def check(_v: Any, bound: str, instance: Any, _schema: Any) -> Iterator[jsonschema.ValidationError]:
        if isinstance(instance, str) and instance.lstrip("-").isdigit() and instance.isascii():
            n, b = int(instance), int(bound)
            if (kind == "min" and n < b) or (kind == "max" and n > b):
                yield jsonschema.ValidationError(f"{instance} outside int64 {kind} {bound}")
    return check


Int64Validator = validators.extend(
    jsonschema.Draft202012Validator, {"x-int64-min": _range("min"), "x-int64-max": _range("max")}
)


def load_registry(schema_dir: Path) -> Registry:
    reg: Registry = Registry()
    for p in schema_dir.glob("*.schema.json"):
        s = json.loads(p.read_text(encoding="utf-8"))
        res = Resource.from_contents(s)
        reg = reg.with_resource(s["$id"], res).with_resource(p.name, res)
    return reg


def validator_for(schema_dir: Path, fname: str) -> Any:
    s = json.loads((schema_dir / fname).read_text(encoding="utf-8"))
    return Int64Validator(s, registry=load_registry(schema_dir))
