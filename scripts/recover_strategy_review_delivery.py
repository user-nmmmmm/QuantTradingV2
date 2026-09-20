"""Append-only, bounded-memory recovery of primitive frozen research caches.

This reader deliberately does not execute pickle globals/reducers. Large list
tables are counted and omitted; their source cache remains the primary evidence.
It never imports the current or historical trading implementation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pickletools


class ProjectionError(ValueError):
    """The cache cannot be projected without losing a required fact."""


@dataclass
class _Container:
    kind: str
    path: tuple
    keep: bool
    value: object = None
    count: int = 0


_OMITTED = object()
_MARK = object()


@dataclass(frozen=True)
class _Opaque:
    description: str


TABLE_NAMES = frozenset({"candidates", "decisions", "outcomes", "contexts", "actuals",
    "predictions", "evaluations", "calibration_predictions", "cells", "folds",
    "diagnostics", "attribution", "rows", "equity", "fills", "financing", "execution_audit"})


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def project_primitive_pickle(path, *, drop_roots=(), table_names=TABLE_NAMES):
    """Read a protocol 4/5 primitive tree without materializing large tables.

    Memo slots for omitted containers are not retained. A reference from a kept
    field to an omitted container is rejected rather than fabricated. Primitive
    atoms remain memoized, since later metadata may refer to earlier strings.
    """
    stack, marks, memo, omitted = [], [], {}, {}
    memo_size = 0
    opcode_counts = Counter()

    def materialize(item, where):
        if item is _OMITTED or isinstance(item, _Opaque):
            raise ProjectionError(f"Required field references an omitted container: {where}")
        if isinstance(item, _Container):
            if item.keep:
                return item.value
            return {"projection": "omitted_table", "container": item.kind, "count": item.count}
        if isinstance(item, tuple):
            return tuple(materialize(value, where) for value in item)
        return item

    def new_container(kind):
        parent = stack[marks[-1] - 1] if marks else (stack[-1] if stack else None)
        if not isinstance(parent, _Container):
            parent = next((item for item in reversed(stack) if isinstance(item, _Container)), None)
        path = ()
        keep = True
        if parent is not None:
            key = stack[-1] if parent.kind == "dict" and stack and isinstance(stack[-1], str) else "*"
            path = (*parent.path, key)
            keep = parent.keep and not (len(path) == 1 and key in drop_roots)
            if kind == "list" and key in table_names:
                keep = False
        node = _Container(kind, path, keep, {} if kind == "dict" else [])
        if not keep:
            node.value = None
        stack.append(node)

    def attach(node, items):
        if not isinstance(node, _Container):
            if node is _OMITTED:
                return
            raise ProjectionError("Malformed container target")
        if node.kind == "dict":
            if len(items) % 2:
                raise ProjectionError("Odd dictionary item count")
            node.count += len(items) // 2
            if node.keep:
                for key, value in zip(items[::2], items[1::2]):
                    node.value[materialize(key, node.path)] = materialize(value, (*node.path, key))
        else:
            node.count += len(items)
            if node.keep:
                node.value.extend(materialize(item, node.path) for item in items)
        if not node.keep and node.path and (len(node.path) == 1 or node.path[-1] in table_names):
            omitted["/".join(node.path)] = node.count

    with Path(path).open("rb") as stream:
        for opcode, arg, _position in pickletools.genops(stream):
            name = opcode.name
            opcode_counts[name] += 1
            if name in {"PROTO", "FRAME"}:
                continue
            if name == "MARK":
                marks.append(len(stack))
                stack.append(_MARK)
            elif name in {"EMPTY_DICT", "EMPTY_LIST"}:
                new_container("dict" if name == "EMPTY_DICT" else "list")
            elif name in {"SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8", "BININT", "BININT1", "BININT2",
                          "LONG1", "LONG4", "BINFLOAT", "BINBYTES", "SHORT_BINBYTES", "BINBYTES8"}:
                stack.append(arg)
            elif name in {"NONE", "NEWTRUE", "NEWFALSE"}:
                stack.append({"NONE": None, "NEWTRUE": True, "NEWFALSE": False}[name])
            elif name in {"MEMOIZE", "BINPUT", "LONG_BINPUT"}:
                index = memo_size if name == "MEMOIZE" else arg
                value = stack[-1]
                if not (isinstance(value, _Container) and not value.keep):
                    memo[index] = value
                memo_size = max(memo_size, index + 1)
            elif name in {"BINGET", "LONG_BINGET"}:
                if arg >= memo_size:
                    raise ProjectionError("Invalid memo reference")
                stack.append(memo.get(arg, _OMITTED))
            elif name in {"SETITEMS", "APPENDS"}:
                start = marks.pop()
                items = stack[start + 1:]
                del stack[start:]
                attach(stack[-1], items)
            elif name == "SETITEM":
                items = stack[-2:]
                del stack[-2:]
                attach(stack[-1], items)
            elif name == "APPEND":
                item = stack.pop()
                attach(stack[-1], [item])
            elif name == "EMPTY_TUPLE":
                stack.append(())
            elif name in {"TUPLE", "TUPLE1", "TUPLE2", "TUPLE3"}:
                if name == "TUPLE":
                    start = marks.pop()
                    items = stack[start + 1:]
                    del stack[start:]
                else:
                    count = int(name[-1])
                    items = stack[-count:]
                    del stack[-count:]
                stack.append(tuple(items))
            elif name == "STACK_GLOBAL":
                symbol, module = stack.pop(), stack.pop()
                stack.append(_Opaque(f"{module}.{symbol}"))
            elif name == "GLOBAL":
                stack.append(_Opaque(str(arg)))
            elif name in {"REDUCE", "NEWOBJ"}:
                stack.pop()
                symbol = stack.pop()
                stack.append(_Opaque(f"inert {symbol}"))
            elif name == "BUILD":
                stack.pop()
                if not isinstance(stack[-1], _Opaque):
                    raise ProjectionError("BUILD on non-opaque object")
            elif name == "STOP":
                if marks or len(stack) != 1 or stream.read(1):
                    raise ProjectionError("Unexpected trailing or unfinished pickle data")
                return {"payload": materialize(stack[0], ()), "omitted_tables": omitted,
                        "opcode_counts": dict(opcode_counts), "memo_slots": memo_size,
                        "retained_memo_slots": len(memo)}
            else:
                raise ProjectionError(f"Unsupported non-primitive pickle opcode: {name} at {_position}; atoms={stack[-2:] if name == 'STACK_GLOBAL' else ''}")
    raise ProjectionError("Pickle has no STOP")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def verify_cache(path):
    expected = path.with_suffix(".sha256").read_text(encoding="ascii").strip()
    actual = sha256(path)
    if actual != expected:
        raise ProjectionError(f"Cache checksum mismatch: {path.name}")
    return {"file": str(path), "bytes": path.stat().st_size, "sha256": actual}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    batch, output = args.batch.resolve(), args.output.resolve()
    if output == batch or output.is_relative_to(batch):
        raise ValueError("Delivery output must be outside the historical batch")
    output.mkdir(parents=True, exist_ok=True)
    meta = batch / "meta_review"
    facts = {}
    for name in ("official_off", "official_p0"):
        print(f"VERIFY {name}", flush=True)
        cache = meta / f"{name}.pickle"
        identity = verify_cache(cache)
        print(f"PROJECT {name}", flush=True)
        projected = project_primitive_pickle(cache, drop_roots={"p0"})
        write_json(output / f"{name}_projection.json", {"cache": identity, **projected})
        facts[name] = identity
        print(f"DONE {name}: {projected['retained_memo_slots']} retained memo slots", flush=True)
    write_json(output / "cache_identity.json", {"generated_at": datetime.now(timezone.utc).isoformat(),
        "historical_identity": read_json(meta / "identity.json"), "caches": facts,
        "recovery_tool_sha256": sha256(__file__), "engine_or_model_reruns": 0})


if __name__ == "__main__":
    main()
