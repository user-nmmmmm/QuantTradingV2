"""Run a manifest-bound historical driver in a fresh interpreter with frozen project imports.

Use --verify-only to check the bundle without executing its driver. A missing driver/module is an
error, never permission to use the current checkout. Historical drivers and evidence stay unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys

RUNNERS = ("run_strategy_review_meta.py", "replay_strategy_review_controls.py")
PROJECT_ROOTS = {
    "analysis", "backtest", "cli", "composition", "config", "core", "dashboard", "live_trading",
    "main", "ops", "research", "resolve_live_order", "router", "run_live", "scripts", "signal_meta",
    "strategies", "tests",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def contained(root, relative):
    name = PurePosixPath(relative)
    if name.is_absolute() or ".." in name.parts or "\\" in relative:
        raise ValueError(f"invalid frozen manifest path: {relative}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"frozen path escapes its bundle: {relative}")
    return path


def verified_bytes(root, relative, hashes):
    if relative not in hashes:
        raise ImportError(f"module or driver is not recorded in the frozen manifest: {relative}")
    data = contained(root, relative).read_bytes()
    if digest(data) != hashes[relative]:
        raise ValueError(f"frozen content hash mismatch: {relative}")
    return data


def verify_bundle(batch, runner):
    manifest_bytes = (batch / "revised_manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema") != "strategy_review_revised/v1" or not manifest.get("source_hashes"):
        raise ValueError("unsupported or empty revised source manifest")
    source = (batch / "revised_source").resolve()
    inputs = (batch / "frozen_inputs").resolve()
    if not source.is_relative_to(batch) or not inputs.is_relative_to(batch):
        raise ValueError("frozen source or inputs escape the historical batch")
    for relative in manifest["source_hashes"]:
        verified_bytes(source, relative, manifest["source_hashes"])
    for relative in manifest["input_hashes"]:
        verified_bytes(inputs, relative, manifest["input_hashes"])
    relative_runner = "scripts/" + runner
    runner_bytes = verified_bytes(source, relative_runner, manifest["source_hashes"])
    return source, manifest["source_hashes"], {
        "schema": "frozen-replay-imports/v1", "manifest_sha256": digest(manifest_bytes),
        "runner": relative_runner, "runner_sha256": digest(runner_bytes), "modules": {},
    }


class FrozenLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, filename, guard):
        super().__init__(fullname, filename)
        self.guard = guard

    def get_code(self, fullname):
        # Compile the verified source bytes, never a possibly stale/unrecorded .pyc.
        relative = Path(self.path).resolve().relative_to(self.guard.source).as_posix()
        data = verified_bytes(self.guard.source, relative, self.guard.hashes)
        self.guard.modules[fullname] = {"path": relative, "sha256": digest(data)}
        return compile(data, self.path, "exec", dont_inherit=True)


class FrozenImports(importlib.abc.MetaPathFinder):
    def __init__(self, source, hashes, modules):
        self.source, self.hashes, self.modules = source, hashes, modules
        self.roots = PROJECT_ROOTS | {PurePosixPath(p).parts[0].removesuffix(".py")
                                      for p in hashes if p.endswith(".py")}

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] not in self.roots:
            return None
        search = [str(self.source)] if path is None else list(path)
        if any(not Path(folder).resolve().is_relative_to(self.source) for folder in search):
            raise ImportError(f"non-frozen package search path: {fullname}")
        spec = importlib.machinery.PathFinder.find_spec(fullname, search, target)
        if spec is None:
            raise ModuleNotFoundError(f"required frozen module is unavailable: {fullname}")
        if spec.origin is None:
            locations = list(spec.submodule_search_locations or [])
            if not locations or any(not Path(folder).resolve().is_relative_to(self.source) for folder in locations):
                raise ImportError(f"non-frozen namespace package: {fullname}")
            for folder in locations:
                prefix = Path(folder).resolve().relative_to(self.source).as_posix() + "/"
                if not any(relative.startswith(prefix) for relative in self.hashes):
                    raise ImportError(f"namespace is not recorded in the frozen manifest: {fullname}")
            spec.submodule_search_locations = locations
            return spec
        origin = Path(spec.origin).resolve()
        if not origin.is_relative_to(self.source) or origin.suffix != ".py":
            raise ImportError(f"non-frozen source module: {fullname}")
        relative = origin.relative_to(self.source).as_posix()
        verified_bytes(self.source, relative, self.hashes)
        loader = FrozenLoader(fullname, str(origin), self)
        return importlib.util.spec_from_file_location(fullname, origin, loader=loader)


def run_worker(args, source, hashes, report):
    guard = FrozenImports(source, hashes, report["modules"])
    existing = [name for name in sys.modules if name.split(".", 1)[0] in guard.roots]
    if existing:
        raise ImportError("project modules were loaded before frozen isolation: " + ", ".join(sorted(existing)))
    sys.meta_path.insert(0, guard)
    runner = contained(source, report["runner"])
    sys.argv = [str(runner), "--batch", str(args.batch)]
    if args.output:
        sys.argv += ["--output", str(args.output)]
    report["status"] = "failed"
    try:
        data = verified_bytes(source, report["runner"], hashes)
        try:
            exec(compile(data, str(runner), "exec", dont_inherit=True),
                 {"__name__": "__main__", "__file__": str(runner), "__package__": None})
        except SystemExit as error:
            if error.code not in (None, 0):
                raise
        if "projection_sha256" in report:
            projection = args.output / "official_p0_projection.json"
            if digest(projection.read_bytes()) != report["projection_sha256"]:
                raise ValueError("control projection changed during replay")
        report["status"] = "completed"
    finally:
        with args.provenance.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--runner", choices=RUNNERS, required=True)
    parser.add_argument("--output", type=Path, help="control output outside the batch; may contain only its projection input")
    parser.add_argument("--provenance", type=Path, help="new import provenance JSON outside the historical batch")
    parser.add_argument("--verify-only", action="store_true", help="verify the bundle without importing/executing it")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.batch = args.batch.resolve()
    try:
        source, hashes, report = verify_bundle(args.batch, args.runner)
        for name in ("output", "provenance"):
            path = getattr(args, name)
            if path is not None:
                path = path.parent.resolve() / path.name
                setattr(args, name, path)
                if path.is_relative_to(args.batch) or path.is_symlink():
                    raise ValueError(f"{name} must be outside the historical batch and not a symlink")
                if name == "provenance" and path.exists():
                    raise ValueError("provenance must be a new path outside the historical batch")
                if name == "output" and path.exists():
                    if not path.is_dir() or any(p.name != "official_p0_projection.json" for p in path.iterdir()):
                        raise ValueError("control output already contains results or unrelated files")
                    projection = path / "official_p0_projection.json"
                    if projection.exists():
                        report["projection_sha256"] = digest(projection.read_bytes())
        if args.verify_only:
            report["status"] = "bundle_verified_not_executed"
            print(json.dumps(report, sort_keys=True))
            return 0
        if args.provenance is None:
            raise ValueError("execution requires --provenance outside the historical batch")
        if args.runner == RUNNERS[1] and args.output is None:
            raise ValueError("control replay requires --output outside the historical batch")
        if args.runner == RUNNERS[0] and args.output is not None:
            raise ValueError("the historical meta driver accepts --batch only; omit --output")
        if not args.provenance.parent.is_dir():
            raise ValueError("the provenance parent directory must already exist")
        if args._worker:
            return run_worker(args, source, hashes, report)
        command = [sys.executable, "-I", str(Path(__file__).resolve()), "--_worker",
                   "--batch", str(args.batch), "--runner", args.runner, "--provenance", str(args.provenance)]
        if args.output:
            command += ["--output", str(args.output)]
        return subprocess.run(command, cwd=source, check=False).returncode
    except (ValueError, OSError, ImportError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
