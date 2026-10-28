"""Safe, versioned local transport for exact research inputs.

Only JSON and CSV containing explicit JSON scalar cells are read. The format
never imports classes, evaluates dtype expressions or unpickles caller files.
Physical file digests and exact/v2 logical identities are both mandatory.
"""

from __future__ import annotations

import csv
from io import StringIO
import json
from pathlib import Path, PurePosixPath
import re

import numpy as np
import pandas as pd

from analysis.search_scope import _load_json, _strict_json
from core.foundation.data_identity import EXACT_DATA_IDENTITY, data_hashes
from core.foundation.reproducibility import canonical_json, sha256_bytes


SCHEMA = "paper-research-typed-inputs/v1"
PANDAS_SCHEMA = "paper-research-pandas/v1"
_HASH = re.compile(r"[0-9a-f]{64}")
_SIMPLE_DTYPES = {
    "bool",
    "int8",
    "int16",
    "int32",
    "int64",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "float32",
    "float64",
    "object",
    "string",
    "boolean",
    "Int8",
    "Int16",
    "Int32",
    "Int64",
    "UInt8",
    "UInt16",
    "UInt32",
    "UInt64",
    "Float32",
    "Float64",
    "timedelta64[ns]",
}
_TIME_DTYPE = re.compile(r"datetime64\[ns(?:, ([A-Za-z0-9_+./:-]+))?\]")


def _json_bytes(value):
    return (
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")


def _hash(value, name):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"{name}: lowercase SHA256 required")
    return value


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}: integer required")
    return value


def _keys(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or set(value) - set(required) - set(optional)
        or set(required) - set(value)
    ):
        raise ValueError("unsupported or missing typed descriptor fields")


def _file_without_alias(path):
    path = Path(path).absolute()
    if path.resolve() != path or not path.is_file():
        raise ValueError("local input must be a regular file without symbolic aliases")
    return path


def _payload_path(root, value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("local payload requires a relative POSIX path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("payload path traversal is forbidden")
    path = _file_without_alias(root.joinpath(*pure.parts))
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError("payload escapes the manifest directory")
    return path


def _meta(value):
    """Encode a small explicit scalar/container vocabulary, never arbitrary repr."""
    if value is pd.NA:
        return {"t": "NA"}
    if value is pd.NaT:
        return {"t": "NaT"}
    if isinstance(value, pd.Timestamp):
        return {
            "t": "timestamp",
            "ns": str(value.value),
            "tz": None if value.tz is None else str(value.tz),
        }
    if isinstance(value, pd.Timedelta):
        return {"t": "timedelta", "ns": str(value.value)}
    if isinstance(value, np.generic):
        if value.dtype.kind not in "biufMm" or value.dtype.hasobject:
            raise ValueError("unsupported numpy metadata scalar")
        if value.dtype.kind == "f" and np.isinf(value):
            raise ValueError("infinite typed values are unsupported")
        return {"t": "numpy", "dtype": value.dtype.str, "hex": value.tobytes().hex()}
    if isinstance(value, dict):
        return {"t": "mapping", "items": [[_meta(key), _meta(item)] for key, item in value.items()]}
    if isinstance(value, (list, tuple)):
        return {
            "t": "tuple" if isinstance(value, tuple) else "list",
            "items": [_meta(item) for item in value],
        }
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and np.isfinite(value):
        return value
    if isinstance(value, float) and np.isnan(value):
        return {"t": "python_nan"}
    raise ValueError(f"unsupported typed input value: {type(value).__name__}")


def _unmeta_numpy(value):
    _keys(value, {"t", "dtype", "hex"})
    dtype = np.dtype(value["dtype"])
    if (
        dtype.hasobject
        or dtype.subdtype
        or dtype.fields
        or dtype.kind not in "biufMm"
        or dtype.itemsize > 8
    ):
        raise ValueError("unsafe numpy scalar dtype")
    raw = bytes.fromhex(value["hex"])
    if len(raw) != dtype.itemsize:
        raise ValueError("numpy scalar byte length differs from dtype")
    scalar = np.frombuffer(raw, dtype=dtype, count=1)[0]
    if dtype.kind == "f" and np.isinf(scalar):
        raise ValueError("infinite typed values are unsupported")
    return scalar


def _unmeta_container(value):
    _keys(value, {"t", "items"})
    if not isinstance(value["items"], list):
        raise ValueError("typed metadata items must be a list")
    if value["t"] == "mapping":
        result = {}
        for pair in value["items"]:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("metadata mapping pair required")
            key, item = map(_unmeta, pair)
            if key in result:
                raise ValueError("duplicate metadata key")
            result[key] = item
        return result
    items = [_unmeta(item) for item in value["items"]]
    return tuple(items) if value["t"] == "tuple" else items


def _unmeta(value):
    if not isinstance(value, dict):
        return _strict_json(value)
    kind = value.get("t")
    if kind in {"mapping", "list", "tuple"}:
        return _unmeta_container(value)
    if kind == "numpy":
        return _unmeta_numpy(value)
    if kind == "timestamp":
        _keys(value, {"t", "ns", "tz"})
        timestamp = pd.Timestamp(_ns(value["ns"]), unit="ns")
        return (
            timestamp
            if value["tz"] is None
            else timestamp.tz_localize("UTC").tz_convert(value["tz"])
        )
    if kind == "timedelta":
        _keys(value, {"t", "ns"})
        return pd.Timedelta(_ns(value["ns"]), unit="ns")
    if kind in {"NA", "NaT", "python_nan"}:
        _keys(value, {"t"})
        return {"NA": pd.NA, "NaT": pd.NaT, "python_nan": float("nan")}[kind]
    raise ValueError("unsupported typed metadata tag; opaque objects are forbidden")


def _ns(value):
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]+", value):
        raise ValueError("nanosecond values must be integer strings")
    result = int(value)
    if not -(2**63) < result < 2**63:
        raise ValueError("nanosecond value is outside valid int64 range")
    return result


def _dtype(dtype):
    if isinstance(dtype, pd.StringDtype) and dtype.storage != "python":
        raise ValueError("only explicit python string storage is supported")
    if isinstance(dtype, pd.CategoricalDtype):
        return {
            "kind": "category",
            "categories": [_meta(item) for item in dtype.categories],
            "ordered": dtype.ordered,
        }
    name = str(dtype)
    if name not in _SIMPLE_DTYPES and not _TIME_DTYPE.fullmatch(name):
        raise ValueError(f"unsupported typed dtype: {name}")
    return {"kind": "dtype", "name": name}


def _undtype(value):
    if not isinstance(value, dict):
        raise ValueError("explicit dtype descriptor required")
    if value.get("kind") == "category":
        _keys(value, {"kind", "categories", "ordered"})
        if not isinstance(value["ordered"], bool) or not isinstance(value["categories"], list):
            raise ValueError("invalid categorical dtype")
        return pd.CategoricalDtype(
            [_unmeta(item) for item in value["categories"]], ordered=value["ordered"]
        )
    _keys(value, {"kind", "name"})
    name = value["name"]
    if (
        value["kind"] != "dtype"
        or not isinstance(name, str)
        or (name not in _SIMPLE_DTYPES and not _TIME_DTYPE.fullmatch(name))
    ):
        raise ValueError("unsupported typed dtype")
    return pd.StringDtype(storage="python") if name == "string" else pd.api.types.pandas_dtype(name)


def _axis(index):
    names = [_meta(name) for name in index.names]
    if isinstance(index, pd.MultiIndex):
        return {
            "kind": "multi",
            "levels": [_axis(level) for level in index.levels],
            "codes": [code.tolist() for code in index.codes],
            "names": names,
            "sortorder": index.sortorder,
        }
    if isinstance(index, pd.RangeIndex):
        return {
            "kind": "range",
            "start": index.start,
            "stop": index.stop,
            "step": index.step,
            "names": names,
        }
    result = {
        "kind": "index",
        "dtype": _dtype(index.dtype),
        "values": [_meta(item) for item in index],
        "names": names,
    }
    if isinstance(index, (pd.DatetimeIndex, pd.TimedeltaIndex)):
        result["frequency"] = index.freqstr
    return result


def _unaxis(value):
    if not isinstance(value, dict) or not isinstance(value.get("names"), list):
        raise ValueError("explicit axis descriptor required")
    names = [_unmeta(name) for name in value["names"]]
    if value.get("kind") == "multi":
        _keys(value, {"kind", "levels", "codes", "names", "sortorder"})
        return pd.MultiIndex(
            levels=[_unaxis(level) for level in value["levels"]],
            codes=value["codes"],
            names=names,
            sortorder=value["sortorder"],
            verify_integrity=True,
        )
    if len(names) != 1:
        raise ValueError("single index requires exactly one axis name")
    if value.get("kind") == "range":
        _keys(value, {"kind", "start", "stop", "step", "names"})
        for key in ("start", "stop", "step"):
            _integer(value[key], f"range.{key}")
        if value["step"] == 0:
            raise ValueError("RangeIndex step must be nonzero")
        return pd.RangeIndex(value["start"], value["stop"], value["step"], name=names[0])
    _keys(value, {"kind", "dtype", "values", "names"}, {"frequency"})
    if value["kind"] != "index" or not isinstance(value["values"], list):
        raise ValueError("unsupported axis kind")
    index = pd.Index(
        pd.array([_unmeta(item) for item in value["values"]], dtype=_undtype(value["dtype"])),
        name=names[0],
    )
    if "frequency" in value:
        if not isinstance(index, (pd.DatetimeIndex, pd.TimedeltaIndex)):
            raise ValueError("frequency is only supported on temporal axes")
        index.freq = value["frequency"]
    return index


def _pandas_spec(value):
    if value.columns.has_duplicates if isinstance(value, pd.DataFrame) else False:
        raise ValueError("duplicate dataframe column names are forbidden")
    result = {
        "schema": PANDAS_SCHEMA,
        "kind": "dataframe" if isinstance(value, pd.DataFrame) else "series",
        "index": _axis(value.index),
        "attrs": _meta(value.attrs),
        "allows_duplicate_labels": value.flags.allows_duplicate_labels,
    }
    if isinstance(value, pd.DataFrame):
        result.update(
            columns=_axis(value.columns),
            dtypes=[_dtype(dtype) for dtype in value.dtypes],
            data=[
                [_meta(item) for item in value.iloc[:, i].array] for i in range(len(value.columns))
            ],
        )
    else:
        result.update(
            name=_meta(value.name),
            dtype=_dtype(value.dtype),
            data=[_meta(item) for item in value.array],
        )
    return result


def _restore_pandas(spec):
    common = {"schema", "kind", "index", "attrs", "allows_duplicate_labels", "data"}
    kind = spec.get("kind") if isinstance(spec, dict) else None
    _keys(spec, common | ({"columns", "dtypes"} if kind == "dataframe" else {"name", "dtype"}))
    if spec["schema"] != PANDAS_SCHEMA or kind not in {"dataframe", "series"}:
        raise ValueError("unsupported pandas payload schema")
    if not isinstance(spec["allows_duplicate_labels"], bool) or not isinstance(spec["data"], list):
        raise ValueError("invalid pandas labels/data descriptor")
    index = _unaxis(spec["index"])
    if kind == "series":
        value = pd.Series(
            pd.array([_unmeta(item) for item in spec["data"]], dtype=_undtype(spec["dtype"])),
            index=index,
            name=_unmeta(spec["name"]),
        )
    else:
        value = _restore_frame(spec, index)
    attrs = _unmeta(spec["attrs"])
    if not isinstance(attrs, dict):
        raise ValueError("pandas attrs must be an explicit mapping")
    value.attrs = attrs
    value.flags.allows_duplicate_labels = spec["allows_duplicate_labels"]
    return value


def _restore_frame(spec, index):
    columns = _unaxis(spec["columns"])
    if columns.has_duplicates:
        raise ValueError("duplicate dataframe column names are forbidden")
    if (
        not isinstance(spec["dtypes"], list)
        or len(columns) != len(spec["dtypes"])
        or len(columns) != len(spec["data"])
    ):
        raise ValueError("column count differs from declared data or dtypes")
    vectors = {}
    for position, (dtype, cells) in enumerate(zip(spec["dtypes"], spec["data"])):
        if not isinstance(cells, list) or len(cells) != len(index):
            raise ValueError("column length differs from declared index")
        vectors[position] = pd.array([_unmeta(item) for item in cells], dtype=_undtype(dtype))
    frame = pd.DataFrame(vectors, index=index)
    frame.columns = columns
    return frame


def _csv_spec(raw, metadata):
    if not isinstance(metadata, dict) or "data" in metadata:
        raise ValueError("CSV requires pandas metadata without inline data")
    kind = metadata.get("kind")
    count = len(_unaxis(metadata["columns"])) if kind == "dataframe" else 1
    rows = list(csv.reader(StringIO(raw.decode("utf-8"), newline="")))
    if not rows or rows[0] != [str(i) for i in range(count)]:
        raise ValueError("CSV positional header must match declared columns exactly")
    if any(len(row) != count for row in rows[1:]):
        raise ValueError("CSV row width differs from declared columns")
    columns = [[_load_json(row[position]) for row in rows[1:]] for position in range(count)]
    return {**metadata, "data": columns if kind == "dataframe" else columns[0]}


def _read_entry(root, entry):
    _keys(
        entry,
        {"name", "kind", "format", "path", "wire_sha256", "data_sha256"},
        {"metadata", "cell_encoding"},
    )
    if not isinstance(entry["name"], str) or not entry["name"].strip():
        raise ValueError("nonempty input name required")
    expected = _hash(entry["wire_sha256"], "wire_sha256")
    _hash(entry["data_sha256"], "data_sha256")
    path = _payload_path(root, entry["path"])
    raw = path.read_bytes()
    if sha256_bytes(raw) != expected:
        raise ValueError("typed payload physical SHA256 mismatch")
    if entry["format"] == "json" and path.suffix.lower() == ".json":
        if "metadata" in entry or "cell_encoding" in entry:
            raise ValueError("JSON payload metadata must be inside the signed payload")
        value = _load_json(raw.decode("utf-8"))
        value = value if entry["kind"] == "json" else _restore_pandas(value)
    elif entry["format"] == "csv" and path.suffix.lower() == ".csv":
        if entry.get("cell_encoding") != "json_scalar/v1" or entry["kind"] not in {
            "dataframe",
            "series",
        }:
            raise ValueError("CSV requires explicit json_scalar/v1 cell encoding")
        value = _restore_pandas(_csv_spec(raw, entry.get("metadata")))
    else:
        raise ValueError("only explicit local JSON or CSV payloads are supported")
    actual_kind = (
        "dataframe"
        if isinstance(value, pd.DataFrame)
        else "series"
        if isinstance(value, pd.Series)
        else "json"
    )
    if entry["kind"] != actual_kind:
        raise ValueError("typed entry kind differs from payload")
    return value


def load_typed_data_manifest(path, *, expected_manifest_sha256=None, expected_data_hashes=None):
    """Load local safe inputs and verify every physical and exact logical digest."""
    path = _file_without_alias(path)
    if path.suffix.lower() != ".json":
        raise ValueError("typed input manifest must be JSON")
    raw = path.read_bytes()
    if expected_manifest_sha256 is not None and sha256_bytes(raw) != _hash(
        expected_manifest_sha256, "manifest SHA256"
    ):
        raise ValueError("typed input manifest physical SHA256 mismatch")
    manifest = _load_json(raw.decode("utf-8"))
    _keys(manifest, {"schema", "data_identity_version", "data_manifest_sha256", "inputs"})
    if manifest["schema"] != SCHEMA or manifest["data_identity_version"] != EXACT_DATA_IDENTITY:
        raise ValueError("unsupported typed input manifest schema or identity version")
    if not isinstance(manifest["inputs"], list) or not manifest["inputs"]:
        raise ValueError("nonempty input list required")
    result = {}
    for entry in manifest["inputs"]:
        value = _read_entry(path.parent, entry)
        if entry["name"] in result:
            raise ValueError("duplicate typed input name")
        result[entry["name"]] = value
    hashes = data_hashes(result, version=EXACT_DATA_IDENTITY)
    declared = {entry["name"]: entry["data_sha256"] for entry in manifest["inputs"]}
    if hashes != declared or _digest(hashes) != _hash(
        manifest["data_manifest_sha256"], "data manifest SHA256"
    ):
        raise ValueError("typed input exact/v2 identity mismatch")
    if expected_data_hashes is not None and hashes != expected_data_hashes:
        raise ValueError("typed input identities differ from the registered data map")
    return result


def _digest(value):
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def typed_data_transport(data_map):
    """Return deterministic safe payload bytes and their manifest, without I/O."""
    hashes = data_hashes(data_map, version=EXACT_DATA_IDENTITY)
    if not hashes:
        raise ValueError("typed transport requires nonempty data_map")
    payloads, entries = {}, []
    for number, name in enumerate(sorted(hashes)):
        value = data_map[name]
        kind = (
            "dataframe"
            if isinstance(value, pd.DataFrame)
            else "series"
            if isinstance(value, pd.Series)
            else "json"
        )
        raw = _json_bytes(_pandas_spec(value) if kind != "json" else _json_payload(value))
        filename = f"{number:04d}.json"
        payloads[filename] = raw
        entries.append(
            {
                "name": name,
                "kind": kind,
                "format": "json",
                "path": filename,
                "wire_sha256": sha256_bytes(raw),
                "data_sha256": hashes[name],
            }
        )
    manifest = {
        "schema": SCHEMA,
        "data_identity_version": EXACT_DATA_IDENTITY,
        "data_manifest_sha256": _digest(hashes),
        "inputs": entries,
    }
    return payloads, _json_bytes(manifest), hashes


def write_typed_data_manifest(data_map, output):
    """Freeze a new deterministic safe snapshot; an existing directory is refused."""
    payloads, manifest, hashes = typed_data_transport(data_map)
    output = Path(output).absolute()
    if output.resolve() != output:
        raise ValueError("snapshot directory must not traverse symbolic aliases")
    output.mkdir(parents=True, exist_ok=False)
    for name, raw in payloads.items():
        (output / name).write_bytes(raw)
    path = output / "manifest.json"
    path.write_bytes(manifest)
    load_typed_data_manifest(
        path, expected_manifest_sha256=sha256_bytes(manifest), expected_data_hashes=hashes
    )
    return {
        "path": str(path.resolve()),
        "manifest_sha256": sha256_bytes(manifest),
        "data_hashes": hashes,
    }


def verify_typed_data_snapshot(data_map, output):
    """Recompute the deterministic transport from the registered current inputs."""
    _, manifest, hashes = typed_data_transport(data_map)
    path = Path(output) / "manifest.json"
    load_typed_data_manifest(
        path, expected_manifest_sha256=sha256_bytes(manifest), expected_data_hashes=hashes
    )
    return {
        "path": str(path.resolve()),
        "manifest_sha256": sha256_bytes(manifest),
        "data_hashes": hashes,
    }


def validate_job_input_bindings(jobs, data_map, *, data_identity_version=EXACT_DATA_IDENTITY):
    """Bindings are part of the already preregistered job table, not file paths."""
    for job in jobs:
        bindings = job.get("input_bindings", {})
        if not isinstance(bindings, dict):
            raise ValueError("input_bindings must map argument names to registered data keys")
        if bindings and (
            job.get("kind") != "python_callable" or data_identity_version != EXACT_DATA_IDENTITY
        ):
            raise ValueError("typed input bindings require python_callable and exact/v2 identity")
        kwargs = job.get("kwargs", {})
        if not isinstance(kwargs, dict):
            raise ValueError("job kwargs must be an object")
        if set(bindings) & set(kwargs):
            raise ValueError("input_bindings cannot overwrite explicit kwargs")
        for argument, data_name in bindings.items():
            if not isinstance(argument, str) or not argument.isidentifier():
                raise ValueError("input binding argument must be a Python parameter name")
            if not isinstance(data_name, str) or data_name not in data_map:
                raise ValueError("input binding refers to an undeclared data key")


def bind_job_inputs(job, data_map):
    """Inject only verified registered inputs into explicitly declared arguments."""
    validate_job_input_bindings([job], data_map)
    return {
        **job.get("kwargs", {}),
        **{argument: data_map[name] for argument, name in job.get("input_bindings", {}).items()},
    }


def typed_transport_runtime_sources(*, root=None):
    """Identify decoder code and its imported local identity/JSON dependencies."""
    import importlib
    import sys

    base = Path(root or Path(__file__).resolve().parents[1]).resolve()
    module_names = {
        __name__,
        _load_json.__module__,
        data_hashes.__module__,
        canonical_json.__module__,
        "analysis.research_evidence",
        "core.foundation.source_bytes",
    }
    files = set()
    for name in module_names:
        module = sys.modules.get(name) or importlib.import_module(name)
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(base):
            raise ValueError("typed transport runtime module origin differs from registered root")
        files.add(path)
        for parent in path.parents:
            if parent == base:
                break
            initializer = parent / "__init__.py"
            if initializer.is_file():
                files.add(initializer)
    return {
        path.relative_to(base).as_posix(): sha256_bytes(path.read_bytes()) for path in sorted(files)
    }


def registered_typed_worker_inputs(data_map, output, source_hashes):
    """Bind safe transport and decoder runtime to the study's registered bytes."""
    transport = verify_typed_data_snapshot(data_map, output)
    runtime = typed_transport_runtime_sources()
    if any(source_hashes.get(name) != expected for name, expected in runtime.items()):
        raise ValueError("typed decoder runtime drifted from the registered source closure")
    return {**transport, "runtime_sources": runtime}


def _json_payload(value):
    """Keep JSON object keys explicit instead of silently stringifying aliases."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings; aliases are forbidden")
        for item in value.values():
            _json_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _json_payload(item)
    return _strict_json(value)
