"""Fail-fast environment and dependency self-check."""
import argparse, importlib, importlib.metadata, platform, re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
def pins(path):
    found = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^ ;]+)(?:\s*;.*)?", raw.strip())
        if match: found.append(match.groups())
    return found
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict-lock", action="store_true")
    args = parser.parse_args()
    errors = [] if sys.version_info >= (3, 11) else ["Python 3.11+ is required"]
    required = pins(ROOT / ("requirements.lock.txt" if args.strict_lock else "requirements.txt"))
    for distribution, expected in required:
        try:
            if not args.strict_lock:
                importlib.import_module({"PyYAML": "yaml"}.get(distribution, distribution.replace("-", "_")))
            actual = importlib.metadata.version(distribution)
        except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
            errors.append(f"{distribution} unavailable: {type(exc).__name__}")
            continue
        if actual != expected: errors.append(f"{distribution}=={expected} required; found {actual}")
    print(f"Python: {platform.python_version()} ({sys.executable})")
    print(f"Platform: {platform.platform()}")
    for error in errors: print(f"ERROR: {error}", file=sys.stderr)
    if not errors: print(f"Environment OK: {len(required)} dependencies verified")
    return bool(errors)
if __name__ == "__main__": raise SystemExit(main())
