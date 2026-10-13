"""Frozen research-input manifest for the tracked Binance daily bars (infrastructure roadmap D2).

Two different files describe ``data/binance/1d``, on purpose:

* ``_manifest.json`` is the download *cache* manifest. The downloader rewrites it whenever it
  runs, it may list only the symbols fetched last, and its hashes are taken over the bytes as
  written on the downloading machine (CRLF on Windows).
* ``_frozen_inputs.json`` (this module) is the *frozen research input* manifest. Only this
  script writes it, never the downloader. It pins every adopted file by SHA-256 over the
  LF-normalized bytes (what git stores), so the digest is the same on every platform, and it
  records how much is actually known about the file's origin.

Provenance is stated honestly: a hash proves *which bytes* were used, not where they came from or
whether they were available at the time. A file is ``download_manifest`` only when the cache
manifest recorded the same bytes; every other file is ``unverified``, and every file is
``point_in_time: unknown`` until a collector with explicit availability times produced it.

  python scripts/data_inputs.py verify            # CI / dev.py check: fail on missing, replaced or unregistered files
  python scripts/data_inputs.py build             # (re)write the manifest from the git-tracked CSVs (`git add` a new one first); review the diff
  python scripts/data_inputs.py build --check     # exit 1 when the committed manifest is not what `build` would write
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "binance" / "1d"
CACHE_MANIFEST = "_manifest.json"
FROZEN_MANIFEST = "_frozen_inputs.json"
SCHEMA = "frozen-research-inputs/v1"
HASH_POLICY = "sha256 over bytes with CRLF normalized to LF (the content git stores)"
CRLF, LF = bytes([13, 10]), bytes([10])


def normalized_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(CRLF, LF)


def sha256_lf(path: Path) -> str:
    return hashlib.sha256(normalized_bytes(path)).hexdigest()


def describe(path: Path) -> Dict[str, object]:
    """Rows (excluding the header), first and last timestamp and the normalized digest of one CSV."""
    data = normalized_bytes(path)
    reader = csv.reader(data.decode("utf-8").splitlines())
    header = next(reader, None)
    if not header or header[0] != "timestamp":
        raise ValueError(f"{path.name}: unexpected header {header!r}")
    first = last = None
    rows = 0
    for row in reader:
        if not row:
            continue
        rows += 1
        first = row[0] if first is None else first
        last = row[0]
    return {"rows": rows, "first": first, "last": last, "sha256": hashlib.sha256(data).hexdigest()}


def _cache_entries(data_dir: Path) -> Dict[str, dict]:
    """File name -> cache-manifest entry for the symbols the downloader recorded (may be few)."""
    path = data_dir / CACHE_MANIFEST
    if not path.is_file():
        return {}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return {entry["file"]: dict(entry, _generated_at=manifest.get("generated_at"))
            for entry in manifest.get("symbols", {}).values() if entry.get("file")}


def _provenance(path: Path, facts: dict, cache: Dict[str, dict]) -> Dict[str, object]:
    entry = cache.get(path.name)
    if entry is not None:
        raw = path.read_bytes()
        as_written = {hashlib.sha256(raw).hexdigest(), hashlib.sha256(raw.replace(CRLF, LF).replace(LF, CRLF)).hexdigest(),
                      facts["sha256"]}
        if entry.get("sha256") in as_written and entry.get("rows") == facts["rows"]:
            return {"status": "download_manifest", "recorded_sha256": entry["sha256"],
                    "recorded_at": entry["_generated_at"], "source": f"binance {entry.get('market_type', 'spot')} "
                    f"{entry.get('timeframe', '1d')} via {CACHE_MANIFEST}"}
    return {"status": "unverified", "note": "bytes are pinned; no record shows where they came from"}


def build(data_dir: Path = DATA_DIR, names: Optional[List[str]] = None) -> dict:
    """Manifest for ``names`` (default: every CSV in the directory; the CLI passes the git-tracked ones)."""
    cache = _cache_entries(data_dir)
    files = {}
    for path in sorted(data_dir / name for name in (names if names is not None else
                                                    (p.name for p in data_dir.glob("*.csv")))):
        facts = describe(path)
        files[path.name] = {"symbol": path.stem.replace("_", "/", 1), **facts, "point_in_time": "unknown",
                            "provenance": _provenance(path, facts, cache)}
    return {"schema_version": SCHEMA, "dataset": "binance-spot-1d", "hash_policy": HASH_POLICY, "files": files}


def render(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _tracked_csvs(data_dir: Path) -> Optional[List[str]]:
    try:
        rel = data_dir.relative_to(ROOT).as_posix()
        out = subprocess.run(["git", "ls-files", "--", f"{rel}/*.csv"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None
    return sorted(Path(line).name for line in out.splitlines() if line.strip())


def verify(data_dir: Path = DATA_DIR, *, tracked: Optional[List[str]] = None, strict_dir: bool = False) -> List[str]:
    """Problems found, empty when every registered file is present and unchanged and none is unregistered."""
    path = data_dir / FROZEN_MANIFEST
    if not path.is_file():
        return [f"{FROZEN_MANIFEST} is missing"]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA:
        return [f"unsupported schema {manifest.get('schema_version')!r}"]
    registered = manifest.get("files", {})
    errors = []
    for name, entry in sorted(registered.items()):
        csv_path = data_dir / name
        if not csv_path.is_file():
            errors.append(f"{name}: registered input is missing")
            continue
        facts = describe(csv_path)
        for key in ("sha256", "rows", "first", "last"):
            if facts[key] != entry.get(key):
                errors.append(f"{name}: {key} changed ({entry.get(key)} -> {facts[key]})"
                              if key != "sha256" else f"{name}: content changed (sha256 {facts['sha256'][:12]} "
                              f"!= registered {str(entry.get('sha256'))[:12]})")
        if entry.get("provenance", {}).get("status") not in {"download_manifest", "unverified"}:
            errors.append(f"{name}: provenance status must be download_manifest or unverified")
    candidates = sorted(p.name for p in data_dir.glob("*.csv")) if strict_dir else (tracked or [])
    for name in candidates:
        if name not in registered:
            errors.append(f"{name}: input is not registered in {FROZEN_MANIFEST}")
    return errors


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    verify_cmd = sub.add_parser("verify", help="check the files against the committed manifest")
    verify_cmd.add_argument("--strict-dir", action="store_true",
                            help="also fail on untracked CSVs in the directory (default: only tracked files)")
    build_cmd = sub.add_parser("build", help="write the manifest from the files on disk")
    build_cmd.add_argument("--check", action="store_true", help="do not write; fail if the file would change")
    args = parser.parse_args(argv)

    if args.command == "verify":
        errors = verify(tracked=_tracked_csvs(DATA_DIR), strict_dir=args.strict_dir)
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        if not errors:
            manifest = json.loads((DATA_DIR / FROZEN_MANIFEST).read_text(encoding="utf-8"))
            statuses = [f["provenance"]["status"] for f in manifest["files"].values()]
            print(f"Frozen research inputs verified: {len(statuses)} files "
                  f"({statuses.count('download_manifest')} with a download record, {statuses.count('unverified')} unverified)")
        return 1 if errors else 0

    tracked = _tracked_csvs(DATA_DIR)
    text = render(build(DATA_DIR, tracked))
    target = DATA_DIR / FROZEN_MANIFEST
    if args.check:
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            print(f"{FROZEN_MANIFEST} is out of date; run `python scripts/data_inputs.py build` and review the diff",
                  file=sys.stderr)
            return 1
        print(f"{FROZEN_MANIFEST} is up to date")
        return 0
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {target.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
