"""Safe typed CLI transport: exact time provenance and real bounded-worker use."""

from copy import deepcopy
import csv
from datetime import datetime, timezone
import inspect
import io
import json
import os
from pathlib import Path
import shutil

import numpy as np
import pandas as pd
import pytest

import analysis.paper_research_typed_inputs as typed
from analysis.paper_research_orchestration import ROOT, run_study
from analysis.research_evidence import source_identity
from core.foundation.data_identity import data_hashes
from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_file
import scripts.run_paper_research_study as cli


def digest(value):
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def inputs():
    index = pd.date_range(
        "2024-01-01T00:00:00.000000001Z", periods=3, freq="ns", name="decision_at"
    )
    frame = pd.DataFrame(
        {
            "close": np.array([1, 2, 3], dtype="float32"),
            "available_at": index + pd.Timedelta(1, "ns"),
            "class": pd.Categorical(["b", "a", "b"], categories=["a", "b", "unused"], ordered=True),
        },
        index=index,
    )
    frame.attrs = {
        "z_context": {"cutoff": index[-1], "version": "revision-1"},
        "a_policy": "strict/local",
    }
    frame.flags.allows_duplicate_labels = False
    series = pd.Series([1, pd.NA, 3], index=index, dtype="Int64", name=("weight", 1))
    series.attrs = {"source": "frozen-model"}
    return {"bars": frame, "weights": series, "json": {"numbers": [1, None, 0.5]}}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


def mutate_payload(snapshot, action):
    manifest_path = Path(snapshot["path"])
    manifest = read_json(manifest_path)
    entry = next(row for row in manifest["inputs"] if row["name"] == "bars")
    path = manifest_path.parent / entry["path"]
    payload = read_json(path)
    action(payload)
    write_json(path, payload)
    entry["wire_sha256"] = sha256_file(path)
    write_json(manifest_path, manifest)
    return manifest_path


def test_safe_roundtrip_preserves_ns_dtype_attrs_axes_and_series_name(tmp_path):
    original = inputs()
    snapshot = typed.write_typed_data_manifest(original, tmp_path / "safe")
    restored = typed.load_typed_data_manifest(
        snapshot["path"],
        expected_manifest_sha256=snapshot["manifest_sha256"],
        expected_data_hashes=snapshot["data_hashes"],
    )
    assert data_hashes(restored) == data_hashes(original)
    assert restored["bars"].index[0].value % 1_000_000_000 == 1
    assert restored["bars"]["available_at"].iloc[0].value % 1_000_000_000 == 2
    assert restored["bars"].index.freqstr == "ns"
    assert restored["bars"].columns.equals(original["bars"].columns)
    assert not restored["bars"].flags.allows_duplicate_labels
    assert restored["weights"].name == ("weight", 1)
    assert restored["weights"].isna().tolist() == [False, True, False]
    assert list(restored["bars"].attrs) == ["z_context", "a_policy"]
    assert all(path.suffix == ".json" for path in (tmp_path / "safe").iterdir())
    with pytest.raises(FileExistsError):
        typed.write_typed_data_manifest(original, tmp_path / "safe")


def test_json_scalar_csv_reconstructs_declared_ns_and_dtype_without_inference(tmp_path):
    data = inputs()
    snapshot = typed.write_typed_data_manifest(data, tmp_path / "csv")
    manifest_path = Path(snapshot["path"])
    manifest = read_json(manifest_path)
    entry = next(row for row in manifest["inputs"] if row["name"] == "bars")
    spec = read_json(manifest_path.parent / entry["path"])
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow([str(i) for i in range(len(spec["data"]))])
    for row in zip(*spec["data"]):
        writer.writerow([json.dumps(item, allow_nan=False) for item in row])
    path = manifest_path.parent / "bars.csv"
    path.write_bytes(stream.getvalue().encode("utf-8"))
    spec.pop("data")
    entry.update(
        format="csv",
        path=path.name,
        wire_sha256=sha256_file(path),
        cell_encoding="json_scalar/v1",
        metadata=spec,
    )
    write_json(manifest_path, manifest)
    restored = typed.load_typed_data_manifest(manifest_path)
    assert data_hashes(restored) == data_hashes(data)


@pytest.mark.parametrize("axis", ["range", "integer", "timedelta", "multi"])
def test_explicit_axis_classes_and_names_are_preserved(tmp_path, axis):
    axes = {
        "range": pd.RangeIndex(2, 8, 2, name="range"),
        "integer": pd.Index([10, 20, 30], dtype="uint32", name="integer"),
        "timedelta": pd.timedelta_range("1ns", periods=3, freq="ns", name="elapsed"),
        "multi": pd.MultiIndex(
            levels=[["a", "unused"], [1, 2, 3]],
            codes=[[0, 0, 0], [0, 1, 2]],
            names=["symbol", "event"],
            sortorder=0,
        ),
    }
    frame = pd.DataFrame({"x": [1.0, 2.0, 3.0]}, index=axes[axis])
    snapshot = typed.write_typed_data_manifest({"x": frame}, tmp_path / axis)
    restored = typed.load_typed_data_manifest(snapshot["path"])
    assert data_hashes({"x": frame}) == data_hashes(restored)


def test_legitimate_declared_missing_values_are_preserved_without_json_nan_token(tmp_path):
    frame = pd.DataFrame(
        {
            "float": [1.0, float("nan")],
            "nullable": pd.Series([pd.NA, 2], dtype="Int64"),
            "time": pd.to_datetime(["2020-01-01", None]),
        }
    )
    snapshot = typed.write_typed_data_manifest({"x": frame}, tmp_path / "missing")
    restored = typed.load_typed_data_manifest(snapshot["path"])
    assert data_hashes({"x": frame}) == data_hashes(restored)
    assert "NaN" not in "".join(p.read_text() for p in (tmp_path / "missing").glob("*.json"))


@pytest.mark.parametrize("mutation", ["physical", "logical", "one_ns", "dtype", "attrs"])
def test_mutation_cannot_alias_the_registered_identity(tmp_path, mutation):
    snapshot = typed.write_typed_data_manifest(inputs(), tmp_path / mutation)
    path = Path(snapshot["path"])
    if mutation == "physical":
        entry = read_json(path)["inputs"][0]
        payload = path.parent / entry["path"]
        payload.write_bytes(payload.read_bytes() + b" ")
    elif mutation == "logical":
        manifest = read_json(path)
        manifest["inputs"][0]["data_sha256"] = "0" * 64
        write_json(path, manifest)
    elif mutation == "one_ns":
        path = mutate_payload(
            snapshot, lambda value: value["index"]["values"][0].update(ns="1704067200000000002")
        )
    elif mutation == "dtype":
        path = mutate_payload(snapshot, lambda value: value["dtypes"][0].update(name="float64"))
    else:
        path = mutate_payload(
            snapshot,
            lambda value: value.update(attrs={"t": "mapping", "items": [["changed", True]]}),
        )
    with pytest.raises(ValueError):
        typed.load_typed_data_manifest(path)


@pytest.mark.parametrize(
    "path_value",
    ["../outside.json", "/outside.json", "C:/outside.json", "a/../b.json", r"a\b.json", "./a.json"],
)
def test_payload_paths_refuse_traversal_absolute_and_platform_aliases(tmp_path, path_value):
    snapshot = typed.write_typed_data_manifest(inputs(), tmp_path / "input")
    path = Path(snapshot["path"])
    manifest = read_json(path)
    manifest["inputs"][0]["path"] = path_value
    write_json(path, manifest)
    with pytest.raises(ValueError, match="path|relative|traversal"):
        typed.load_typed_data_manifest(path)


def test_symlink_payload_alias_is_refused_even_when_hash_matches(tmp_path):
    snapshot = typed.write_typed_data_manifest(inputs(), tmp_path / "input")
    path = Path(snapshot["path"])
    manifest = read_json(path)
    entry = manifest["inputs"][0]
    original = path.parent / entry["path"]
    outside = tmp_path / "outside.json"
    shutil.copyfile(original, outside)
    alias = path.parent / "alias.json"
    try:
        alias.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("local OS does not permit user-created symbolic links")
    entry["path"] = alias.name
    write_json(path, manifest)
    with pytest.raises(ValueError, match="aliases"):
        typed.load_typed_data_manifest(path)


@pytest.mark.parametrize(
    "kind", ["pickle", "opaque", "duplicate_name", "duplicate_columns", "nan", "dtype"]
)
def test_unsafe_or_ambiguous_descriptors_are_rejected(tmp_path, kind):
    snapshot = typed.write_typed_data_manifest(inputs(), tmp_path / kind)
    path = Path(snapshot["path"])
    manifest = read_json(path)
    if kind == "pickle":
        manifest["inputs"][0].update(format="pickle", path="x.pkl")
        (path.parent / "x.pkl").write_bytes(b"not executed")
        manifest["inputs"][0]["wire_sha256"] = sha256_file(path.parent / "x.pkl")
        write_json(path, manifest)
    elif kind == "duplicate_name":
        manifest["inputs"].append(deepcopy(manifest["inputs"][0]))
        write_json(path, manifest)
    elif kind == "opaque":
        path = mutate_payload(
            snapshot, lambda value: value.update(attrs={"t": "python_object", "module": "os"})
        )
    elif kind == "duplicate_columns":
        path = mutate_payload(
            snapshot, lambda value: value["columns"].update(values=["x", "x", "y"])
        )
    elif kind == "dtype":
        path = mutate_payload(snapshot, lambda value: value["dtypes"][0].update(name="V1024"))
    else:
        entry = manifest["inputs"][0]
        payload = path.parent / entry["path"]
        payload.write_text('{"x":NaN}', encoding="utf-8")
        entry["wire_sha256"] = sha256_file(payload)
        write_json(path, manifest)
    with pytest.raises((ValueError, TypeError)):
        typed.load_typed_data_manifest(path)


def test_opaque_api_objects_and_duplicate_columns_refused_before_snapshot_write(tmp_path):
    frame = inputs()["bars"]
    frame.attrs["opaque"] = object()
    with pytest.raises(ValueError, match="unsupported"):
        typed.write_typed_data_manifest({"x": frame}, tmp_path / "opaque")
    assert not (tmp_path / "opaque").exists()
    frame = pd.DataFrame([[1, 2]], columns=["same", "same"])
    with pytest.raises(ValueError, match="duplicate"):
        typed.write_typed_data_manifest({"x": frame}, tmp_path / "duplicate")


def test_cli_input_modes_are_exclusive_and_old_json_is_unchanged(tmp_path, monkeypatch):
    data = {"plain": [1, None, 3]}
    path = tmp_path / "old.json"
    write_json(path, data)
    jobs = tmp_path / "jobs.json"
    write_json(jobs, [])
    captured = []
    monkeypatch.setattr(
        cli,
        "run_study",
        lambda **kwargs: (
            captured.append(kwargs)
            or {
                "engineering": "pass",
                "research": "not_evaluated",
                "coverage": {},
                "fatal_error": None,
            }
        ),
    )
    arguments = [
        "--protocol",
        "protocol.json",
        "--jobs",
        str(jobs),
        "--output",
        str(tmp_path / "out"),
    ]
    assert cli.main(arguments + ["--data", str(path)]) == 0
    assert captured[-1]["data_map"] == data
    snapshot = typed.write_typed_data_manifest(inputs(), tmp_path / "typed")
    assert cli.main(arguments + ["--data-manifest", snapshot["path"]]) == 0
    assert isinstance(captured[-1]["data_map"]["bars"], pd.DataFrame)
    with pytest.raises(SystemExit):
        cli.main(arguments + ["--data", str(path), "--data-manifest", snapshot["path"]])


def make_worker_study(tmp_path):
    import analysis.search_scope as scope_module
    import analysis.research_evidence as evidence_module
    import core.foundation.data_identity as identity_module
    import core.foundation.reproducibility as reproducibility_module

    root = tmp_path / "source"
    modules = [typed, scope_module, evidence_module, identity_module, reproducibility_module]
    for module in modules:
        source = Path(inspect.getfile(module))
        destination = root / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    for name in typed.typed_transport_runtime_sources():
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    worker = root / "scripts/paper_research_worker.py"
    worker.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/paper_research_worker.py", worker)
    (root / "config").mkdir()
    (root / "config/fixture.json").write_text('{"fixed":true}', encoding="utf-8")
    (root / "strategies").mkdir()
    # A real package must win over an installed project package on sys.path.
    (root / "strategies/__init__.py").write_text("", encoding="utf-8")
    (root / "strategies/typed_fixture.py").write_text(
        f"from {identity_module.__name__} import data_hashes\n"
        "import pandas as pd\n\n"
        "def inspect_inputs(bars, weights, label):\n"
        "    assert isinstance(bars,pd.DataFrame) and isinstance(weights,pd.Series)\n"
        "    return {'actual_hashes':data_hashes({'bars':bars,'weights':weights}),"
        "'first_ns':int(bars.index[0].value),'label':label,'research':'not_evaluated'}\n",
        encoding="utf-8",
    )
    data = inputs()
    bundle = typed.write_typed_data_manifest(data, tmp_path / "bundle")
    jobs = [
        {
            "id": "candidate",
            "kind": "python_callable",
            "callable": "strategies.typed_fixture:inspect_inputs",
            "kwargs": {"label": "real-worker"},
            "input_bindings": {"bars": "bars", "weights": "weights"},
        },
        {"id": "benchmark", "kind": "json_summary", "values": [1, 2, 3]},
    ]
    protocol = {
        "schema": "paper-research-protocol/v1",
        "study_id": "typed-worker",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "paper_refs": ["L08"],
        "source_manifest_sha256": digest(source_identity(root)),
        "config_sha256": sha256_file(root / "config/fixture.json"),
        "data_manifest_sha256": digest(data_hashes(data)),
        "data_identity_version": "exact/v2",
        "evaluation_kind": "retrospective",
        "information_policy": {"mode": "retrospective", "knowledge": "local", "unknown": "exclude"},
        "candidate_ids": ["candidate"],
        "benchmark_ids": ["benchmark"],
        "search_scope": "current_local_family",
        "historical_global_search_complete": False,
        "primary_metric": "exact_identity",
        "economic_or_precision_target": "exact typed values",
        "training_selection_rule": "no fitting",
        "evaluation_windows": [
            {
                "id": "fixture",
                "role": "engineering_fixture",
                "start": "2020-01-01",
                "end": "2021-01-01",
            }
        ],
        "cost_policy_id": "fixed",
        "risk_policy_id": "fixed",
        "resampling": {"method": "not_applicable", "reason": "engineering identity"},
        "attempt_budget": 2,
        "resource_budget": {
            "wall_time_seconds": 60,
            "memory_mb": 2048 if os.name == "posix" else 512,
            "max_workers": 1,
        },
        "stop_rules": ["stop on drift"],
        "final_access_policy_id": None,
        "job_table_sha256": digest(jobs),
    }
    return root, data, bundle, protocol, jobs, tmp_path / "run"


def test_cli_typed_inputs_reach_real_worker_and_resume_checks_frozen_safe_bytes(
    tmp_path, monkeypatch
):
    root, data, bundle, protocol, jobs, output = make_worker_study(tmp_path)
    protocol_path, jobs_path = tmp_path / "protocol.json", tmp_path / "jobs.json"
    write_json(protocol_path, protocol)
    write_json(jobs_path, jobs)
    monkeypatch.setattr(cli, "ROOT", root)
    arguments = [
        "--protocol",
        str(protocol_path),
        "--jobs",
        str(jobs_path),
        "--output",
        str(output),
        "--data-manifest",
        bundle["path"],
    ]
    assert cli.main(arguments) == 0
    report = read_json(next(output.glob("report-*.json")))
    result = report["raw_results"]["candidate"]
    assert result["actual_hashes"] == data_hashes({key: data[key] for key in ("bars", "weights")})
    assert result["first_ns"] == data["bars"].index[0].value
    assert result["label"] == "real-worker"
    before = (output / "evidence/attempts.jsonl").read_bytes()
    assert cli.main(arguments + ["--resume"]) == 0
    assert (output / "evidence/attempts.jsonl").read_bytes() == before
    transport = output / "worker_inputs/0000.json"
    transport.write_bytes(transport.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA256"):
        cli.main(arguments + ["--resume"])


@pytest.mark.parametrize("kind", ["unknown_key", "overwrite", "changed_after_freeze", "legacy"])
def test_invalid_binding_cannot_start_an_attempt(tmp_path, kind):
    root, data, _, protocol, jobs, output = make_worker_study(tmp_path)
    if kind == "unknown_key":
        jobs[0]["input_bindings"]["bars"] = "missing"
    elif kind == "overwrite":
        jobs[0]["kwargs"]["bars"] = "shadow"
    elif kind == "legacy":
        protocol["data_identity_version"] = "legacy/v1"
    else:
        jobs[0]["input_bindings"]["bars"] = "weights"
    if kind != "changed_after_freeze":
        protocol["job_table_sha256"] = digest(jobs)
    with pytest.raises(ValueError):
        run_study(protocol=protocol, jobs=jobs, data_map=data, output=output, source_root=root)
    assert not (output / "evidence").exists()


def test_expected_manifest_and_registered_input_set_are_both_enforced(tmp_path):
    snapshot = typed.write_typed_data_manifest(inputs(), tmp_path / "fixed")
    with pytest.raises(ValueError, match="manifest physical"):
        typed.load_typed_data_manifest(snapshot["path"], expected_manifest_sha256="0" * 64)
    with pytest.raises(ValueError, match="registered data map"):
        typed.load_typed_data_manifest(
            snapshot["path"], expected_data_hashes={"invented": "0" * 64}
        )


def test_transport_decoder_runtime_must_be_in_the_declared_source_closure(tmp_path):
    root, data, _, protocol, jobs, output = make_worker_study(tmp_path)
    paths = list(source_identity(root))
    missing = next(name for name in paths if "reproducibility" in name)
    paths.remove(missing)
    protocol["source_manifest_sha256"] = digest(source_identity(root, paths=paths))
    with pytest.raises(ValueError, match="typed decoder runtime"):
        run_study(
            protocol=protocol,
            jobs=jobs,
            data_map=data,
            output=output,
            source_root=root,
            source_paths=paths,
        )
    assert not (output / "evidence").exists()


def test_completed_typed_study_cannot_remove_safe_snapshot_compatibility_marker(tmp_path):
    root, data, _, protocol, jobs, output = make_worker_study(tmp_path)
    report = run_study(protocol=protocol, jobs=jobs, data_map=data, output=output, source_root=root)
    assert report["engineering"] == "pass"
    fixed = output / "fixed_inputs/manifest.json"
    payload = read_json(fixed)
    payload.pop("worker_inputs_manifest_sha256")
    write_json(fixed, payload)
    shutil.rmtree(output / "worker_inputs")
    before = (output / "evidence/attempts.jsonl").read_bytes()
    with pytest.raises(ValueError, match="regular file"):
        run_study(
            protocol=protocol,
            jobs=jobs,
            data_map=data,
            output=output,
            source_root=root,
            resume=True,
        )
    assert (output / "evidence/attempts.jsonl").read_bytes() == before


def test_worker_snapshot_manifest_whitespace_rewrite_is_detected(tmp_path):
    data = inputs()
    snapshot = typed.write_typed_data_manifest(data, tmp_path / "safe")
    manifest = Path(snapshot["path"])
    # Identical decoded JSON still changes the frozen physical transport.
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(ValueError, match="manifest physical"):
        typed.verify_typed_data_snapshot(data, manifest.parent)


def test_string_storage_and_metadata_scalar_types_are_explicit(tmp_path):
    frame = pd.DataFrame(
        {
            "text": pd.Series(["a", None], dtype=pd.StringDtype(storage="python")),
            "flag": pd.Series([True, pd.NA], dtype="boolean"),
        }
    )
    frame.attrs = {"elapsed": pd.Timedelta(1, "ns"), "tuple": ("a", np.int32(3))}
    snapshot = typed.write_typed_data_manifest({"frame": frame}, tmp_path / "string")
    restored = typed.load_typed_data_manifest(snapshot["path"])
    assert restored["frame"]["text"].dtype.storage == "python"
    assert data_hashes(restored) == data_hashes({"frame": frame})


@pytest.mark.parametrize(
    "value", [{1: "integer key", "1": "string key"}, {"nested": [{2: "alias"}]}]
)
def test_nonstring_json_keys_do_not_silently_alias_during_snapshot(tmp_path, value):
    with pytest.raises(ValueError, match="keys must be strings"):
        typed.write_typed_data_manifest({"json": value}, tmp_path / "ambiguous")
    assert not (tmp_path / "ambiguous").exists()
