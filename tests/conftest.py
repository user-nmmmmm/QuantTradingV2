"""Shared pytest hooks.

``quarantine`` (docs/quality.md, "偶发失败"): a known-unstable test stays in the suite but its failure does not
block. Collection validates every inherited marker and limits actual cases before selection. Usage::

    @pytest.mark.quarantine(reason="races with the file watcher on slow disks", until="2026-10-22")

Use a fixed deadline for each quarantine so it expires instead of moving with the current date.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

MAX_QUARANTINED_CASES = 10
MAX_QUARANTINE_DAYS = 30


def _marker_problems(marker, today):
    reason = marker.kwargs.get("reason")
    until = marker.kwargs.get("until")
    problems = []
    if not isinstance(reason, str) or len(reason.strip()) < 10:
        problems.append("quarantine reason must describe the cause in at least 10 characters")
    try:
        deadline = date.fromisoformat(until)
        if deadline.isoformat() != until:
            raise ValueError("non-canonical date")
    except (TypeError, ValueError):
        problems.append("quarantine until must be a YYYY-MM-DD date")
        return problems
    if deadline < today:
        problems.append(f"quarantine expired on {until}; fix the test or remove the mark")
    elif deadline > today + timedelta(days=MAX_QUARANTINE_DAYS):
        problems.append(f"quarantine deadline must be at most {MAX_QUARANTINE_DAYS} days from today (UTC)")
    return problems


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    # xdist workers each collect the full suite. Run before -k/-m/--deselect can shrink the item list.
    today = datetime.now(timezone.utc).date()
    quarantined, problems = [], []
    for item in items:
        markers = list(item.iter_markers(name="quarantine"))
        if markers:
            quarantined.append((item, markers[0]))
        for marker in markers:
            problems.extend(f"{item.nodeid}: {message}" for message in _marker_problems(marker, today))
    if len(quarantined) > MAX_QUARANTINED_CASES:
        problems.insert(0, f"{len(quarantined)} quarantined test cases; maximum is {MAX_QUARANTINED_CASES} (after parameter expansion)")
    if problems:
        raise pytest.UsageError("quarantine policy failed:\n" + "\n".join(problems))
    for item, marker in quarantined:
        reason, until = marker.kwargs["reason"], marker.kwargs["until"]
        item.add_marker(pytest.mark.xfail(reason=f"quarantined until {until}: {reason}", strict=False))
