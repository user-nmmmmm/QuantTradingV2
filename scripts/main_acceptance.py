"""Run offline main-branch acceptance, then create/verify a portable evidence ZIP.

No uploads, exchange calls, git commits, or deletion. A failed check is retained
as evidence and prevents a passing acceptance result.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip()


def validate_clean_source(root: Path, output: Path) -> dict:
    """Reject all unregistered inputs before importing/running any tests.

    Only non-executable evidence inside this exact invocation's output may
    be untracked. A conftest/module in that directory is still an input.
    """
    root, output = root.resolve(), output.resolve()
    def paths(*args):
        return subprocess.check_output(["git", args[0], "-z", *args[1:]], cwd=root).decode("utf-8").split("\0")
    changed = [p for p in paths("diff", "--name-only", "HEAD", "--") if p]
    untracked = [p for p in paths("ls-files", "--others", "--exclude-standard") if p]
    # Ignore rules are not authorization to execute unhashed local business
    # code. Enumerate only controlled roots, so credentials/runtime outputs
    # and dependency environments are never collected into evidence.
    controlled = ("analysis", "backtest", "composition", "config", "core", "dashboard", "data",
                  "live_trading", "research", "router", "scripts", "strategies", "tests")
    ignored = paths("ls-files", "--others", "--ignored", "--exclude-standard", "--", *controlled,
                    ":(top,glob)*.py", ":(top,glob)*.toml", ":(top,glob)*.yaml", ":(top,glob)*.json")
    input_suffixes = {".py", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
    untracked += [name for name in ignored if name and Path(name).suffix.lower() in input_suffixes
                  and "__pycache__" not in Path(name).parts]
    allowed_evidence = {".json", ".csv", ".log", ".md", ".png", ".pdf", ".zip"}
    unexpected = []
    for name in untracked:
        path = (root / name).resolve()
        if not path.is_relative_to(output) or path.suffix.lower() not in allowed_evidence:
            unexpected.append(name)
    if changed or unexpected:
        raise ValueError("Acceptance inputs differ from commit: " + ", ".join(changed + unexpected))
    return {"policy": "clean-source/v2", "untracked_output_files": sorted(untracked)}


def run_checks(output: Path) -> int:
    identity = validate_clean_source(ROOT, output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "logs").mkdir()
    base = git("rev-parse", "HEAD")
    commands = [
        ("environment", ["scripts/check_environment.py"]),
        ("lock", ["scripts/verify_lock.py"]),
        ("lint", ["-m", "ruff", "check", "."]),
        ("types", ["-m", "mypy", "core/domain.py", "core/runtime.py", "live_trading/execution_adapter.py"]),
        ("coverage", ["scripts/run_portable_tests.py", "-q", "--cov=core", "--cov=backtest", "--cov=live_trading", "--cov-fail-under=55"]),
        ("sandbox_discovery", ["scripts/run_portable_tests.py", "-q", "tests/test_exchange_sandbox_e2e.py"]),
    ]
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "QUANT_SANDBOX_E2E": "0"}
    results = []

    def execute(name, arguments):
        command = [sys.executable, *arguments]
        print(f"START {name}", flush=True)
        started = datetime.now(timezone.utc).isoformat()
        with (output / "logs" / f"{name}.log").open("wb") as handle:
            result = subprocess.run(command, cwd=ROOT, env=environment, stdout=handle, stderr=subprocess.STDOUT, check=False)
        results.append({"name": name, "command": command, "exit_code": result.returncode,
                        "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
                        "log": f"logs/{name}.log"})
        write_json(output / "checks.json", {"commit": base, "checks": results})
        print(f"DONE {name}: exit={result.returncode}", flush=True)
        return result.returncode

    for name, arguments in commands:
        execute(name, arguments)
    for index in range(1, 4):
        destination = output / f"current_{index}"
        reference = output / "current_1" if index > 1 else ROOT / "outputs/health_diagnosis/after_stop_lifecycle_fix"
        if execute(f"current_{index}", ["scripts/run_p0_recovery_backtest.py", "--output", str(destination),
                                       "--verify-reference", str(reference)]):
            break
    execute("isolated", ["scripts/run_p0_recovery_backtest.py", "--isolate-portfolio-breaker", "--output", str(output / "isolated"),
                         "--verify-reference", str(ROOT / "outputs/health_diagnosis/after_fix_isolated_breaker")])
    passed = all(row["exit_code"] == 0 for row in results) and len(results) == 10
    write_json(output / "acceptance.json", {
        "schema": "main-acceptance/v1", "commit": base, "tree": git("rev-parse", "HEAD^{tree}"),
        "local_passed": passed, "checks": results, "python": sys.version,
        "input_policy": identity,
        "sandbox_execution": "not_executed_credentials_disabled", "holdout": "not_opened",
        "scope": "engineering acceptance, not strategy admission or live deployment",
    })
    print(f"ACCEPTANCE local_passed={passed} OUTPUT={output}", flush=True)
    return 0 if passed else 1


def verify_bundle(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive member")
        for info in archive.infolist():
            # ZipInfo.filename normalizes backslashes on Windows. Validate
            # the original central-directory name before normalization.
            name = info.orig_filename
            point = PurePosixPath(name)
            if point.is_absolute() or ".." in point.parts or "\\" in name or ":" in name:
                raise ValueError("Unsafe archive member")
        manifest = json.loads(archive.read("bundle_manifest.json"))
        expected = manifest["files"]
        if set(names) != set(expected) | {"bundle_manifest.json"}:
            raise ValueError("Archive member set differs from manifest")
        for name, record in expected.items():
            content = archive.read(name)
            if len(content) != record["bytes"] or sha(content) != record["sha256"]:
                raise ValueError(f"Archive hash mismatch: {name}")
    return {"passed": True, "members_verified": len(expected), "zip_sha256": sha(path.read_bytes()), "bytes": path.stat().st_size}


def package(output: Path, destination: Path) -> int:
    acceptance = json.loads((output / "acceptance.json").read_text(encoding="utf-8"))
    commit = acceptance["commit"]
    remote_ci = json.loads((output / "remote_ci.json").read_text(encoding="utf-8"))
    remote_passed = (remote_ci["head_sha"] == commit and remote_ci["event"] == "push"
                     and remote_ci["branch"] == "main" and remote_ci["conclusion"] == "success")
    tool_checks = []
    for name, arguments in (
        ("archive_tests", ["scripts/run_portable_tests.py", "-q", "tests/test_main_acceptance_archive.py"]),
        ("archive_lint", ["-m", "ruff", "check", "scripts/main_acceptance.py", "tests/test_main_acceptance_archive.py"]),
    ):
        with (output / "logs" / f"{name}.log").open("wb") as handle:
            result = subprocess.run([sys.executable, *arguments], cwd=ROOT,
                                    stdout=handle, stderr=subprocess.STDOUT, check=False)
        tool_checks.append({"name": name, "exit_code": result.returncode})
        if result.returncode:
            raise ValueError(f"Archive tooling failed: {name}")
    write_json(output / "archive_tool_validation.json", {"checks": tool_checks})
    source_manifest = ROOT / "reports/20260902_211518_3239d_30Syms_Ret137.0pct/run_manifest.json"
    inputs = json.loads(source_manifest.read_text(encoding="utf-8"))
    members = {}
    members["source.zip"] = subprocess.check_output(["git", "archive", "--format=zip", commit], cwd=ROOT)
    members["inputs/run_manifest.json"] = source_manifest.read_bytes()
    for row in inputs["data_snapshots"].values():
        relative = PurePosixPath(row["path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in row["path"] or ":" in row["path"]:
            raise ValueError("Unsafe input snapshot path")
        data = (source_manifest.parent / "data_inputs" / row["path"]).read_bytes()
        if sha(data) != row["sha256"]:
            raise ValueError(f"Input snapshot hash mismatch: {row['path']}")
        members[f"inputs/data_inputs/{row['path']}"] = data
    # Only this acceptance directory; never collect workspace-wide reports or DBs.
    allowed = {".json", ".csv", ".log", ".md"}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.suffix in allowed and not path.is_symlink():
            members[f"evidence/{path.relative_to(output).as_posix()}"] = path.read_bytes()
    members["tools/main_acceptance.py"] = Path(__file__).read_bytes()
    members["tools/test_main_acceptance_archive.py"] = (ROOT / "tests/test_main_acceptance_archive.py").read_bytes()
    instructions = (
        "# Portable engineering acceptance evidence\n\n"
        f"Commit: {commit}\nLocal checks passed: {acceptance['local_passed']}\n\n"
        "1. Verify the outer ZIP hash against its separate index, then run:\n"
        "   python tools/main_acceptance.py verify PATH_TO_OUTER_ZIP\n"
        "2. Extract source.zip into source/. Install its requirements-dev.txt in an isolated Python 3.11+ environment.\n"
        "3. From source/, run the paired experiment against the bundled inputs:\n"
        "   python scripts/run_p0_recovery_backtest.py --source-manifest ../inputs/run_manifest.json "
        "--output ../reproduced --verify-reference ../evidence/current_1\n"
        "4. For the isolated arm add --isolate-portfolio-breaker and compare ../evidence/isolated.\n\n"
        "The source manifest describes a HISTORICAL run. Authoritative current configs/results are in evidence/.\n"
        "This bundle does not include the Python runtime or offline dependency wheels.\n"
        "Sandbox credentials are disabled. Holdout/real-money admission are NOT validated.\n"
        "No publication license for market data is granted by this local bundle. Do not upload without review.\n"
    )
    members["README.md"] = instructions.encode("utf-8")
    manifest = {"schema": "acceptance-bundle/v1", "commit": commit, "local_passed": acceptance["local_passed"],
                "files": {name: {"sha256": sha(data), "bytes": len(data)} for name, data in sorted(members.items())}}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(members.items()):
            archive.writestr(name, data)
        archive.writestr("bundle_manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    verification = verify_bundle(destination)
    index = {"schema": "acceptance-archive-index/v1", "commit": commit, "local_passed": acceptance["local_passed"],
             "remote_ci_passed": remote_passed, "remote_ci_url": remote_ci["url"],
             "engineering_acceptance_passed": bool(acceptance["local_passed"] and remote_passed),
             "publication": "local_only_not_uploaded", "archive_name": destination.name,
             "verification": verification, "manifest": manifest}
    write_json(destination.with_suffix(".index.json"), index)
    print(json.dumps({key: value for key, value in index.items() if key != "manifest"}, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run")
    run.add_argument("--output", required=True, type=Path)
    bundle = commands.add_parser("package")
    bundle.add_argument("--output", required=True, type=Path)
    bundle.add_argument("--zip", required=True, type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("zip", type=Path)
    args = parser.parse_args()
    if args.action == "run":
        return run_checks(args.output.resolve())
    if args.action == "package":
        return package(args.output.resolve(), args.zip.resolve())
    print(json.dumps(verify_bundle(args.zip), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
