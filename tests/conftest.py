"""Shared pytest hooks.

``quarantine`` (docs/quality.md, "偶发失败"): a known-unstable test stays in the suite but its failure does not
block, and ``tests/test_quarantine_policy.py`` makes the deadline binding. Usage::

    @pytest.mark.quarantine(reason="races with the file watcher on slow disks", until="2026-11-30")
"""
from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        marker = item.get_closest_marker("quarantine")
        if marker is not None:
            reason = marker.kwargs.get("reason", "")
            until = marker.kwargs.get("until", "")
            item.add_marker(pytest.mark.xfail(reason=f"quarantined until {until}: {reason}", strict=False))
