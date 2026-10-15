"""Frozen legacy/current bundles cannot borrow project code from a checkout or bytecode cache."""
import hashlib
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys

import pytest

from scripts.run_frozen_replay import RUNNERS, contained

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts/run_frozen_replay.py"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def bundle(tmp_path, code, modules=None, runner=RUNNERS[0]):
    batch = tmp_path / "batch"
    source = batch / "revised_source"
    files = {"scripts/" + runner: code, **(modules or {})}
    hashes = {}
    for relative, text in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        hashes[relative] = sha(path.read_bytes())
    inputs = batch / "frozen_inputs/example.csv"
    inputs.parent.mkdir(parents=True)
    inputs.write_bytes(b"value\n1\n")
    manifest = {"schema": "strategy_review_revised/v1", "source_hashes": hashes,
                "input_hashes": {"example.csv": sha(inputs.read_bytes())}}
    (batch / "revised_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return batch


def launch(batch, tmp_path, *, runner=RUNNERS[0], verify_only=False, cwd=None):
    provenance = tmp_path / "imports.json"
    command = [sys.executable, str(LAUNCHER), "--batch", str(batch), "--runner", runner]
    if verify_only:
        command += ["--verify-only"]
    else:
        command += ["--provenance", str(provenance)]
        if runner == RUNNERS[1]:
            command += ["--output", str(tmp_path / "control-output")]
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=30)
    return result, provenance


@pytest.mark.parametrize("legacy", [True, False])
def test_frozen_driver_uses_its_own_layout_and_records_policy_imports(tmp_path, legacy):
    policy = "core.signal_adaptive_types" if legacy else "signal_meta.signal_adaptive_types"
    market = "core.market_data" if legacy else "core.market.market_data"
    modules = {policy.replace(".", "/") + ".py": "VALUE = 'frozen-policy'\n",
               market.replace(".", "/") + ".py": "VALUE = 'frozen-market'\n"}
    code = (f"from {policy} import VALUE as policy\nfrom {market} import VALUE as market\n"
            "assert (policy, market) == ('frozen-policy', 'frozen-market')\n")
    batch = bundle(tmp_path, code, modules)
    before = {p: p.read_bytes() for p in batch.rglob("*") if p.is_file()}
    result, provenance = launch(batch, tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(provenance.read_text())
    assert report["status"] == "completed" and report["runner_sha256"] == sha(code.encode())
    assert set(report["modules"]) == {policy, market}
    assert report["modules"][policy]["path"] == policy.replace(".", "/") + ".py"
    assert all(path.read_bytes() == data for path, data in before.items())


def test_a_policy_missing_from_frozen_tree_cannot_fall_back_to_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    policy = checkout / "signal_meta/signal_adaptive_types.py"
    policy.parent.mkdir(parents=True)
    marker = tmp_path / "current-code-executed"
    policy.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    code = f"import sys\nsys.path.insert(0, {str(checkout)!r})\nimport signal_meta.signal_adaptive_types\n"
    batch = bundle(tmp_path, code, {"core/old_policy.py": "VALUE = 'old'\n"})
    result, provenance = launch(batch, tmp_path, cwd=checkout)
    assert result.returncode != 0 and "required frozen module is unavailable" in result.stderr
    assert not marker.exists()
    assert json.loads(provenance.read_text())["status"] == "failed"


def test_frozen_namespace_stays_anchored_after_path_changes_and_cache_invalidation(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "core").mkdir(parents=True)
    (checkout / "core/policy.py").write_text("VALUE = 'checkout'\n", encoding="utf-8")
    code = ("import core\nimport sys\nimport importlib\n"
            f"sys.path.insert(0, {str(checkout)!r})\nimportlib.invalidate_caches()\n"
            "from core.policy import VALUE\nassert VALUE == 'frozen'\n")
    batch = bundle(tmp_path, code, {"core/policy.py": "VALUE = 'frozen'\n"})
    result, provenance = launch(batch, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(provenance.read_text())["modules"]["core.policy"]["path"] == "core/policy.py"


def test_unrecorded_source_module_is_not_executed(tmp_path):
    marker = tmp_path / "unrecorded-code-executed"
    batch = bundle(tmp_path, "import core.unrecorded\n", {"core/recorded.py": "A = 1\n"})
    (batch / "revised_source/core/unrecorded.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    result, provenance = launch(batch, tmp_path)
    assert result.returncode != 0 and "not recorded" in result.stderr
    assert not marker.exists() and json.loads(provenance.read_text())["status"] == "failed"


@pytest.mark.parametrize("changed", ["source", "input"])
def test_changed_bundle_is_refused_before_execution(tmp_path, changed):
    marker = tmp_path / "driver-executed"
    batch = bundle(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
                   {"core/a.py": "A = 1\n"})
    path = batch / ("revised_source/core/a.py" if changed == "source" else "frozen_inputs/example.csv")
    path.write_bytes(b"changed")
    result, provenance = launch(batch, tmp_path)
    assert result.returncode != 0 and "hash mismatch" in result.stderr
    assert not marker.exists() and not provenance.exists()


def test_source_changed_after_preflight_is_rechecked_at_import(tmp_path):
    code = ("from pathlib import Path\n"
            "path = Path(__file__).parents[1] / 'core/a.py'\n"
            "path.write_text('A = 2\\n')\nimport core.a\n")
    batch = bundle(tmp_path, code, {"core/a.py": "A = 1\n"})
    result, provenance = launch(batch, tmp_path)
    assert result.returncode != 0 and "hash mismatch: core/a.py" in result.stderr
    assert json.loads(provenance.read_text())["status"] == "failed"


def test_matching_timestamp_bytecode_cannot_replace_verified_source(tmp_path):
    batch = bundle(tmp_path, "from core.a import VALUE\nassert VALUE == 'frozen'\n",
                   {"core/a.py": "VALUE = 'frozen'\n"})
    module = batch / "revised_source/core/a.py"
    original = module.read_bytes()
    module.write_bytes(b"VALUE = 'poison'\n")
    assert module.stat().st_size == len(original)
    point = module.stat()
    py_compile.compile(str(module), doraise=True)
    module.write_bytes(original)
    os.utime(module, ns=(point.st_atime_ns, point.st_mtime_ns))
    result, provenance = launch(batch, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(provenance.read_text())["modules"]["core.a"]["sha256"] == sha(original)


def test_verify_only_does_not_execute_driver_or_claim_replay_success(tmp_path):
    marker = tmp_path / "driver-executed"
    batch = bundle(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    result, provenance = launch(batch, tmp_path, verify_only=True)
    assert result.returncode == 0 and not marker.exists() and not provenance.exists()
    report = json.loads(result.stdout)
    assert report["status"] == "bundle_verified_not_executed" and report["modules"] == {}


def test_missing_frozen_driver_cannot_be_replaced_with_current_driver(tmp_path):
    batch = bundle(tmp_path, "pass\n", {"core/a.py": "A = 1\n"})
    manifest_path = batch / "revised_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["source_hashes"]["scripts/" + RUNNERS[0]]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result, provenance = launch(batch, tmp_path)
    assert result.returncode != 0 and "driver is not recorded" in result.stderr
    assert not provenance.exists()


@pytest.mark.parametrize("projection", [False, True])
def test_control_runner_receives_the_explicit_batch_and_output(tmp_path, projection):
    output = tmp_path / "control-output"
    if projection:
        output.mkdir()
        (output / "official_p0_projection.json").write_bytes(b'{"input":"sealed"}')
    code = ("import argparse\nfrom pathlib import Path\np = argparse.ArgumentParser()\n"
            "p.add_argument('--batch', type=Path)\np.add_argument('--output', type=Path)\na = p.parse_args()\n"
            "assert (a.batch / 'revised_manifest.json').is_file()\na.output.mkdir(exist_ok=True)\n"
            "(a.output / 'result.json').write_text('{}')\n")
    batch = bundle(tmp_path, code, runner=RUNNERS[1])
    result, provenance = launch(batch, tmp_path, runner=RUNNERS[1])
    assert result.returncode == 0, result.stderr
    assert (output / "result.json").is_file() and json.loads(provenance.read_text())["status"] == "completed"
    if projection:
        assert json.loads(provenance.read_text())["projection_sha256"] == sha(b'{"input":"sealed"}')


@pytest.mark.parametrize("relative", ["../escape.py", "/absolute.py", "core\\escaped.py"])
def test_manifest_paths_cannot_escape_the_bundle(tmp_path, relative):
    with pytest.raises(ValueError, match="invalid frozen manifest path"):
        contained(tmp_path.resolve(), relative)


def test_existing_provenance_is_preserved_before_running_driver(tmp_path):
    batch = bundle(tmp_path, "raise AssertionError('must not execute')\n")
    output = tmp_path / "imports.json"
    output.write_text("original", encoding="utf-8")
    result, _ = launch(batch, tmp_path)
    assert result.returncode != 0 and output.read_text() == "original"
