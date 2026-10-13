"""Environment capabilities that some tests need, probed instead of assumed.

On Windows, creating a symbolic link needs Developer Mode or an elevated shell (``WinError 1314``); on Linux CI it
always works, so a test guarded by ``requires_symlinks`` must never be skipped there
(``tests/test_quarantine_policy.py`` checks that).
"""
from __future__ import annotations

import functools
import tempfile
from pathlib import Path

import pytest


@functools.lru_cache(maxsize=1)
def can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "target"
        target.write_text("x", encoding="utf-8")
        try:
            (Path(directory) / "link").symlink_to(target)
        except (OSError, NotImplementedError):
            return False
    return True


requires_symlinks = pytest.mark.skipif(
    not can_symlink(),
    reason="creating symbolic links needs privilege here (Windows: Developer Mode or an elevated shell)",
)
