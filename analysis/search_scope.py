"""Search-scope and source-closure checks on top of ``ResearchEvidenceRun`` (PR25-02).

``ResearchEvidenceRun`` already records every attempt; this module audits that record. It states whether
the *local* family of a run is accounted for (every registered arm has a terminal status, the log is
intact) and always reports ``historical_global_search_complete=False``: a new registration cannot
reconstruct earlier search history. Failed, interrupted and abstained arms are kept, never dropped.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from analysis.research_evidence import source_identity
from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_file

TERMINAL = frozenset({"completed", "failed", "interrupted", "abstained"})
ACTIVE = frozenset({"planned", "started"})
MODEL_SUFFIXES = frozenset({".pt", ".pth", ".pkl", ".joblib", ".npz", ".onnx", ".safetensors"})


def source_closure(root: str | Path, *, extra_files: Iterable[str] = ()) -> dict[str, str]:
    """Content hashes of the run's executable closure: the default source identity plus test
    definitions, model artifacts under ``research/`` and explicitly listed extra files (data, protocol)."""
    root = Path(root)
    closure = dict(source_identity(root))
    paths = set()
    if (root / "tests").is_dir():
        paths.update(p for p in (root / "tests").rglob("*.py") if p.is_file())
    if (root / "research").is_dir():
        paths.update(p for p in (root / "research").rglob("*") if p.is_file() and p.suffix in MODEL_SUFFIXES)
    for name in extra_files:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"closure file not found: {name}")
        paths.add(path)
    closure.update({p.relative_to(root).as_posix(): sha256_file(p) for p in paths})
    return dict(sorted(closure.items()))


def verify_source_closure(recorded: Mapping[str, str], root: str | Path, *, extra_files: Iterable[str] = ()) -> dict[str, Any]:
    """Recompute the closure from local bytes and compare; a recorded hash is never trusted on its own."""
    current = source_closure(root, extra_files=extra_files)
    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))
    changed = sorted(p for p in set(recorded) & set(current) if recorded[p] != current[p])
    return {"unchanged": not (added or removed or changed), "added": added, "removed": removed, "changed": changed,
            "file_count": len(current)}


def read_run(run_dir: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Read registration.json and attempts.jsonl; unreadable or truncated lines become problems."""
    run_dir = Path(run_dir)
    problems: list[str] = []
    registration = json.loads((run_dir / "registration.json").read_text(encoding="utf-8"))
    events: list[dict[str, Any]] = []
    log = run_dir / "attempts.jsonl"
    if not log.is_file():
        return registration, events, ["attempts.jsonl missing"]
    text = log.read_text(encoding="utf-8")
    lines = text.split("\n")
    if text and not text.endswith("\n"):
        problems.append("attempts.jsonl does not end with a newline (possible truncated write)")
    for number, line in enumerate(lines, 1):
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"attempts.jsonl line {number} is not valid JSON")
            continue
        if not isinstance(row, dict):
            problems.append(f"attempts.jsonl line {number} is not an object")
            continue
        events.append(row)
    return registration, events, problems


def audit_attempts(candidates: Sequence[str], events: Sequence[Mapping[str, Any]], *, read_problems: Sequence[str] = (),
                   expected_identity: str | None = None) -> dict[str, Any]:
    """Account for every registered arm against the recorded events."""
    problems = list(read_problems)
    duplicates = sorted(c for c, n in Counter(candidates).items() if n > 1)
    registered = list(dict.fromkeys(candidates))
    if not registered:
        problems.append("no candidates registered")
    if duplicates:
        problems.append(f"duplicate registered candidates: {duplicates}")
    sequences = [e.get("sequence") for e in events]
    if sequences != list(range(len(events))):
        problems.append("event sequence is not 0..n-1 (gap, reorder or duplicate)")
    if expected_identity is not None and any(e.get("identity") not in (None, expected_identity) for e in events):
        problems.append("events carry a different run identity")
    by_candidate: dict[str, list[Mapping[str, Any]]] = {c: [] for c in registered}
    unknown: set[str] = set()
    unrecognized: list[dict[str, Any]] = []
    for event in events:
        candidate = event.get("candidate")
        status = event.get("status")
        if status not in TERMINAL | ACTIVE | {"changed", "checked"}:
            unrecognized.append({"sequence": event.get("sequence"), "candidate": candidate, "status": status})
        if candidate is None:
            continue
        if candidate in by_candidate:
            by_candidate[candidate].append(event)
        else:
            unknown.add(candidate)
    identity_changed = [e.get("sequence") for e in events if e.get("phase") == "identity" and e.get("status") == "changed"]
    final: dict[str, str | None] = {}
    never_planned, not_terminal = [], []
    for candidate, rows in by_candidate.items():
        statuses = [r.get("status") for r in rows if r.get("status") in TERMINAL | ACTIVE]
        final[candidate] = statuses[-1] if statuses else None
        if "planned" not in statuses:
            never_planned.append(candidate)
        if final[candidate] not in TERMINAL:
            not_terminal.append(candidate)
    if unknown:
        problems.append(f"events for unregistered candidates: {sorted(unknown)}")
    if never_planned:
        problems.append(f"registered candidates without a planned event: {never_planned}")
    if not_terminal:
        problems.append(f"candidates without a terminal status: {not_terminal}")
    if unrecognized:
        problems.append(f"{len(unrecognized)} events with unrecognized status")
    if identity_changed:
        problems.append("identity changed after registration")
    return {
        "scope": "current_local_family", "local_complete": not problems and bool(registered),
        "historical_global_search_complete": False,
        "registered": registered, "final_status": final,
        "status_counts": dict(Counter(s for s in final.values() if s)),
        "kept_unsuccessful": sorted(c for c, s in final.items() if s in {"failed", "interrupted", "abstained"}),
        "attempt_counts": {c: sum(1 for r in rows if r.get("status") == "started") for c, rows in by_candidate.items()},
        "unregistered_candidates": sorted(unknown), "missing_planned": never_planned, "not_terminal": not_terminal,
        "unrecognized_status_events": unrecognized, "identity_changed_events": identity_changed,
        "problems": problems,
        "interpretation": "local arm accounting only; does not reconstruct earlier search history or certify admission",
    }


def audit_run(run_dir: str | Path) -> dict[str, Any]:
    registration, events, problems = read_run(run_dir)
    identity = sha256_bytes(canonical_json(registration).encode())
    return audit_attempts(registration.get("candidates", []), events, read_problems=problems, expected_identity=identity)
