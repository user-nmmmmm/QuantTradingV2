"""Minimal worker bootstrap. Limits are installed before task imports/work."""

from __future__ import annotations

import importlib
from importlib.machinery import ModuleSpec
from importlib.util import spec_from_file_location
import inspect
import json
import os
from pathlib import Path
import sys


class _RegisteredProjectFinder:
    """Resolve only this job's project prefixes from its registered source tree."""

    def __init__(self, root, prefixes):
        self.root = root
        self.prefixes = frozenset(prefixes)

    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split(".")
        if (
            parts[0] not in self.prefixes
            or parts[0] in sys.stdlib_module_names
            or any(not part.isidentifier() for part in parts)
        ):
            return None
        candidate = self.root.joinpath(*parts).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("registered project import escapes source root")
        if candidate.is_dir():
            initializer = candidate / "__init__.py"
            if initializer.is_file():
                if not initializer.resolve().is_relative_to(self.root):
                    raise ValueError("registered package initializer escapes source root")
                return spec_from_file_location(
                    fullname, initializer, submodule_search_locations=[str(candidate)]
                )
            spec = ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(candidate)]
            return spec
        source = candidate.with_suffix(".py")
        if source.is_file():
            if not source.resolve().is_relative_to(self.root):
                raise ValueError("registered module escapes source root")
            return spec_from_file_location(fullname, source)
        return None


def _project_import_prefixes(task):
    prefixes = set()
    if task["kind"] == "python_callable":
        module_name = task["callable"].split(":", 1)[0]
        parts = module_name.split(".")
        if any(not part.isidentifier() for part in parts):
            raise ValueError("stable importable module name required")
        if parts[0] in sys.stdlib_module_names:
            raise ValueError(
                "actual imported module origin differs from registered source: "
                "stdlib callable prefix is forbidden"
            )
        prefixes.add(parts[0])
    if task.get("input_bindings"):
        # These are the fixed typed decoder and exact identity dependencies;
        # never infer import prefixes by scanning the supplied directory.
        prefixes.update({"analysis", "core"})
    return prefixes


def _dispatch_in_registered_source(task, root, request):
    # main installs OS limits before reaching this project-code import boundary.
    finder = _RegisteredProjectFinder(root, _project_import_prefixes(task))
    sys.meta_path.insert(0, finder)
    try:
        return _dispatch(task, root, request)
    finally:
        sys.meta_path.remove(finder)


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
    result = _dispatch_in_registered_source(request["job"], root, request)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True)
    Path(request["result_path"]).write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    main()
