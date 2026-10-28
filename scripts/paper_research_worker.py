"""Minimal worker bootstrap. Limits are installed before task imports/work."""

from __future__ import annotations

import importlib
import inspect
import json
import os
from pathlib import Path
import sys


def _callable_function(task, root):
    module_name, qualified = task["callable"].split(":", 1)
    module = importlib.import_module(module_name)
    expected = (root / (module_name.replace(".", "/") + ".py")).resolve()
    if Path(getattr(module, "__file__", "")).resolve() != expected:
        raise ValueError("actual imported module origin differs from registered source")
    function = module
    for name in qualified.split("."):
        if not name.isidentifier():
            raise ValueError("stable importable callable name required")
        function = getattr(function, name)
    if (
        not inspect.isfunction(function)
        or function.__module__ != module_name
        or function.__qualname__ != qualified
        or function.__closure__
        or Path(inspect.getsourcefile(function) or "").resolve() != expected
        or Path(function.__code__.co_filename).resolve() != expected
    ):
        raise ValueError("callable executable origin differs from registered function")
    return function


def _bound_kwargs(task, root, request):
    kwargs = task.get("kwargs", {})
    if not task.get("input_bindings"):
        return kwargs
    import analysis.paper_research_typed_inputs as typed_decoder

    if (
        Path(typed_decoder.__file__).resolve()
        != (root / "analysis/paper_research_typed_inputs.py").resolve()
    ):
        raise ValueError("actual typed decoder origin differs from registered source")
    transport = request.get("typed_inputs")
    if not isinstance(transport, dict) or set(transport) != {
        "path",
        "manifest_sha256",
        "data_hashes",
        "runtime_sources",
    }:
        raise ValueError("verified control-plane typed inputs required")
    if typed_decoder.typed_transport_runtime_sources(root=root) != transport["runtime_sources"]:
        raise ValueError("typed decoder runtime bytes differ from registered sources")
    data_map = typed_decoder.load_typed_data_manifest(
        transport["path"],
        expected_manifest_sha256=transport["manifest_sha256"],
        expected_data_hashes=transport["data_hashes"],
    )
    return typed_decoder.bind_job_inputs(task, data_map)


def _dispatch(task, root, request):
    kind = task["kind"]
    if kind == "account_fixture":
        from scripts.capture_paper_research_baseline import replay_fixed_account

        result = replay_fixed_account()
        if result["engineering"] != "pass":
            raise ValueError("real Broker fixture failed reconciliation")
        return result
    if kind == "json_summary":
        from decimal import Decimal

        values = [Decimal(str(value)) for value in task["values"]]
        return {
            "observations": len(values),
            "sum": float(sum(values)),
            "mean": float(sum(values) / len(values)) if values else None,
            "research": "not_evaluated",
        }
    if kind == "python_callable":
        function = _callable_function(task, root)
        return function(**_bound_kwargs(task, root, request))
    raise ValueError("unknown job adapter")


def main():
    request = json.loads(sys.stdin.buffer.readline())
    if os.name == "posix":
        import resource

        cap = int(request["memory_mb"] * 1024 * 1024)
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    root = Path(request["source_root"]).resolve()
    sys.path.insert(0, str(root))
    result = _dispatch(request["job"], root, request)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True)
    Path(request["result_path"]).write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    main()
