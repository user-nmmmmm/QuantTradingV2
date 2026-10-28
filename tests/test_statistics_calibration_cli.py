"""Calibration startup and immutable source-snapshot failure boundaries."""

import pytest

from core.foundation.reproducibility import sha256_file
from scripts.calibrate_paper_statistics import calibrate, snapshot_sources


@pytest.mark.parametrize("invalid", [{}, False, 0, []])
def test_explicit_invalid_design_cannot_silently_expand_to_default_full_grid(tmp_path, invalid):
    output = tmp_path / "invalid-calibration"
    with pytest.raises(ValueError, match="design|schema"):
        calibrate(output, design=invalid)
    assert not (output / "evidence").exists()
    assert not (output / "design.json").exists()


def test_snapshot_verifies_actual_bytes_and_fails_on_drift_without_overwriting(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    value = root / "research.py"
    value.write_bytes(b"version = 1\n")
    hashes = {"research.py": sha256_file(value)}
    result = snapshot_sources(root, tmp_path / "snapshot", hashes)
    assert result["files_verified"] == 1
    assert (tmp_path / "snapshot/research.py").read_bytes() == value.read_bytes()
    with pytest.raises(FileExistsError):
        snapshot_sources(root, tmp_path / "snapshot", hashes)
    value.write_bytes(b"version = 2\n")
    with pytest.raises(ValueError, match="changed while snapshotting"):
        snapshot_sources(root, tmp_path / "drift", hashes)
