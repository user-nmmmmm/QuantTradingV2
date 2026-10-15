"""Frozen research-input manifest for the tracked Binance daily bars (infrastructure roadmap D2).

Two different files describe ``data/binance/1d``, on purpose:

* ``_manifest.json`` is the download *cache* manifest. The downloader rewrites it whenever it
  runs and it may list only the symbols fetched last. Legacy hashes used the downloading
  machine's raw newline spelling; new records declare an LF-normalized hash policy.
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
  python scripts/data_inputs.py corroborate       # (network) compare every file with Binance's public daily klines and
                                                  # record the result as independent corroboration

Corroboration is weaker than a download record and is kept separate from ``provenance.status``: it says the
values still agree with what Binance publishes today, not that the file was ever downloaded from there. It is bound
to the file's digest (``for_sha256``), so it cannot outlive a change to the file.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from core.foundation.content_hash import cache_hash_matches, sha256_lf_bytes

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "binance" / "1d"
CACHE_MANIFEST = "_manifest.json"
FROZEN_MANIFEST = "_frozen_inputs.json"
SCHEMA = "frozen-research-inputs/v1"
KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
CORROBORATION_STATUSES = {"match", "match_except_final_bar", "mismatch", "unavailable"}
COMPARISON_POLICY = "binance-daily-comparison/v2"
HASH_POLICY = "sha256 over bytes with CRLF normalized to LF (the content git stores)"
CRLF, LF = bytes([13, 10]), bytes([10])


def normalized_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(CRLF, LF)


def sha256_lf(path: Path) -> str:
    return sha256_lf_bytes(path.read_bytes())


def describe(path: Path) -> Dict[str, object]:
    """Rows (excluding the header), first and last timestamp and the normalized digest of one CSV."""
    return _describe_bytes(normalized_bytes(path), path.name)


def _describe_bytes(data: bytes, name: str) -> Dict[str, object]:
    reader = csv.reader(data.decode("utf-8").splitlines())
    header = next(reader, None)
    if not header or header[0] != "timestamp":
        raise ValueError(f"{name}: unexpected header {header!r}")
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
        if sha256_lf_bytes(raw) != facts["sha256"]:
            raise ValueError(f"{path.name}: content changed while building input identity")
        if cache_hash_matches(raw, entry) and entry.get("rows") == facts["rows"]:
            return {"status": "download_manifest", "recorded_sha256": entry["sha256"],
                    "recorded_at": entry["_generated_at"], "source": f"binance {entry.get('market_type', 'spot')} "
                    f"{entry.get('timeframe', '1d')} via {CACHE_MANIFEST}"}
    return {"status": "unverified", "note": "bytes are pinned; no record shows where they came from"}


def build(data_dir: Path = DATA_DIR, names: Optional[List[str]] = None, previous: Optional[dict] = None) -> dict:
    """Manifest for ``names`` (default: every CSV in the directory; the CLI passes the git-tracked ones)."""
    cache = _cache_entries(data_dir)
    files = {}
    for path in sorted(data_dir / name for name in (names if names is not None else
                                                    (p.name for p in data_dir.glob("*.csv")))):
        facts = describe(path)
        files[path.name] = {"symbol": path.stem.replace("_", "/", 1), **facts, "point_in_time": "unknown",
                            "provenance": _provenance(path, facts, cache)}
        kept = ((previous or {}).get("files", {}).get(path.name) or {}).get("corroboration")
        if kept and kept.get("for_sha256") == facts["sha256"]:
            files[path.name]["corroboration"] = kept
    return {"schema_version": SCHEMA, "dataset": "binance-spot-1d", "hash_policy": HASH_POLICY, "files": files}


def read_bars(path: Path) -> Dict[str, Tuple[Decimal, ...]]:
    """date -> (open, high, low, close, volume) as decimals, so '51.43' and '51.43000000' compare equal."""
    return _bars_from_bytes(normalized_bytes(path), path.name)


def _bars_from_bytes(data: bytes, name: str) -> Dict[str, Tuple[Decimal, ...]]:
    reader = csv.reader(data.decode("utf-8").splitlines())
    if next(reader, None) != ["timestamp", "open", "high", "low", "close", "volume"]:
        raise ValueError(f"{name}: expected timestamp and OHLCV columns")
    bars = {}
    previous = None
    for row in reader:
        if not row:
            continue
        try:
            if len(row) != 6 or datetime.strptime(row[0], "%Y-%m-%d").strftime("%Y-%m-%d") != row[0]:
                raise ValueError("expected a daily date and five values")
            if row[0] in bars:
                raise ValueError("duplicate date")
            if previous is not None and row[0] <= previous:
                raise ValueError("dates must be strictly increasing")
            bars[row[0]] = tuple(Decimal(value) for value in row[1:])
            previous = row[0]
        except (ValueError, InvalidOperation) as error:
            raise ValueError(f"{name}: invalid daily row {row[0]!r}: {error}") from error
    if not bars:
        raise ValueError(f"{name}: empty local CSV cannot be corroborated")
    return bars


def _valid_bar(bar: Tuple[Decimal, ...]) -> bool:
    return (len(bar) == 5 and all(isinstance(value, Decimal) and value.is_finite() for value in bar)
            and min(bar[:4]) > 0 and bar[4] >= 0
            and bar[2] <= min(bar[0], bar[3]) <= max(bar[0], bar[3]) <= bar[1])


def _compatible_partial(local: Tuple[Decimal, ...], remote: Tuple[Decimal, ...]) -> bool:
    """Necessary OHLCV invariants, not proof of historical capture time."""
    return (_valid_bar(local) and _valid_bar(remote) and local[0] == remote[0]
            and local[1] <= remote[1] and local[2] >= remote[2] and local[4] <= remote[4])


def compare_bars(local: Dict[str, Tuple[Decimal, ...]], remote: Dict[str, Tuple[Decimal, ...]]) -> dict:
    """Row-by-row comparison restricted to the local date range; at most 5 examples of each kind are listed."""
    if not local:
        raise ValueError("empty local bars cannot be corroborated")
    first, last = min(local), max(local)
    remote = {day: bar for day, bar in remote.items() if first <= day <= last}
    fields = ("open", "high", "low", "close", "volume")
    invalid = sorted(day for day, bar in local.items() if not _valid_bar(bar))
    invalid_remote = sorted(day for day, bar in remote.items() if not _valid_bar(bar))
    valid_common = (local.keys() & remote.keys()) - set(invalid) - set(invalid_remote)
    differences = [{"date": day, "field": fields[i], "local": str(local[day][i]), "remote": str(remote[day][i])}
                   for day in sorted(valid_common) for i in range(5) if local[day][i] != remote[day][i]]
    only_local, only_remote = sorted(local.keys() - remote.keys()), sorted(remote.keys() - local.keys())
    matching = sum(1 for day in valid_common if local[day] == remote[day])
    agree = not differences and not only_local and not only_remote and not invalid and not invalid_remote
    final_bar_only = (bool(differences) and {item["date"] for item in differences} == {last}
                      and not only_local and not only_remote and not invalid and not invalid_remote
                      and _compatible_partial(local[last], remote[last]))
    status = "match" if agree else "match_except_final_bar" if final_bar_only else "mismatch"
    return {"status": status, "rows_local": len(local), "rows_remote": len(remote),
            "rows_matching": matching, "difference_count": len(differences),
            "invalid_local_count": len(invalid), "invalid_remote_count": len(invalid_remote),
            "only_local_count": len(only_local), "only_remote_count": len(only_remote),
            "examples": {"differences": differences[:5], "only_local": only_local[:5], "only_remote": only_remote[:5],
                         "invalid_local": invalid[:5], "invalid_remote": invalid_remote[:5]}}


def fetch_binance_daily(symbol: str, first: str, last: str, *, opener=urllib.request.urlopen,
                        pause: float = 0.05) -> Optional[Dict[str, Tuple[Decimal, ...]]]:
    """Daily klines of ``symbol`` (like ``AAVEUSDT``) for [first, last] from Binance's public market-data endpoint.
    ``None`` only for Binance -1121 (invalid symbol); a successful empty response is {}."""
    day_ms = 86_400_000
    start = int(datetime.strptime(first, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    end = int(datetime.strptime(last, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    if start > end:
        raise ValueError("first date must not be after last date")
    bars: Dict[str, Tuple[Decimal, ...]] = {}
    while start <= end:
        query = urllib.parse.urlencode({"symbol": symbol, "interval": "1d", "startTime": start, "endTime": end, "limit": 1000})
        for attempt in range(3):
            try:
                with opener(f"{KLINES_URL}?{query}", timeout=30) as response:
                    rows = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 400:
                    try:
                        payload = json.loads(exc.read().decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        payload = {}
                    if isinstance(payload, dict) and payload.get("code") == -1121 and not bars:
                        return None
                    raise
                if attempt == 2:
                    raise
            except urllib.error.URLError:
                if attempt == 2:
                    raise
            time.sleep(1 + attempt)
        if not isinstance(rows, list):
            raise ValueError("Binance klines response must be a list")
        if not rows:
            break
        previous = start - 1
        for row in rows:
            if (not isinstance(row, list) or len(row) < 6 or type(row[0]) is not int
                    or not previous < row[0] <= end or row[0] < start or row[0] % day_ms):
                raise ValueError("invalid or non-advancing Binance daily kline page")
            day = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            try:
                bar = tuple(Decimal(str(value)) for value in row[1:6])
            except InvalidOperation as error:
                raise ValueError("invalid Binance OHLCV values") from error
            if not _valid_bar(bar):
                raise ValueError("invalid Binance OHLCV values")
            bars[day] = bar
            previous = row[0]
        start = rows[-1][0] + day_ms
        time.sleep(pause)
    return bars


def _input_snapshot(path: Path, entry: dict) -> tuple:
    data = normalized_bytes(path)
    facts = _describe_bytes(data, path.name)
    changed = [key for key in ("sha256", "rows", "first", "last") if facts[key] != entry.get(key)]
    if changed:
        raise ValueError(f"{path.name}: does not match frozen input ({', '.join(changed)}); review and rebuild first")
    return facts, _bars_from_bytes(data, path.name)


def corroborate(data_dir: Path = DATA_DIR, names: Optional[List[str]] = None, *,
                fetch: Callable[..., Optional[dict]] = fetch_binance_daily,
                now: Optional[Callable[[], datetime]] = None) -> dict:
    """The manifest with a fresh ``corroboration`` block on every entry (or only on ``names``)."""
    path = data_dir / FROZEN_MANIFEST
    recorded = path.read_bytes()
    manifest = json.loads(recorded)
    if manifest.get("schema_version") != SCHEMA:
        raise ValueError("unsupported frozen input manifest")
    files = manifest["files"]
    unknown = sorted(set(names or []) - files.keys())
    if unknown:
        raise ValueError(f"unknown corroboration filenames: {', '.join(unknown)}")
    selected = sorted(set(names) if names else files)
    if any("/" in name or "\\" in name or Path(name).name != name or not name.endswith(".csv") for name in selected):
        raise ValueError("corroboration inputs must be registered CSV basenames")
    # Pin and parse the same immutable bytes; validate every selected input before fetching.
    snapshots = {name: _input_snapshot(data_dir / name, files[name]) for name in selected}
    stamp = (now or (lambda: datetime.now(timezone.utc)))().strftime("%Y-%m-%dT%H:%M:%SZ")
    for name in selected:
        entry = files[name]
        facts, local = snapshots[name]
        remote = fetch(entry["symbol"].replace("/", ""), facts["first"], facts["last"])
        block = ({"status": "unavailable", "note": "the public endpoint does not know this symbol"} if remote is None
                 else compare_bars(local, remote))
        entry["corroboration"] = {**block, "source": "binance spot 1d klines (data-api.binance.vision)",
                                  "checked_at": stamp, "for_sha256": facts["sha256"],
                                  "comparison_policy": COMPARISON_POLICY}
    for name in selected:
        if sha256_lf(data_dir / name) != snapshots[name][0]["sha256"]:
            raise ValueError(f"{name}: content changed during corroboration")
    if path.read_bytes() != recorded:
        raise ValueError("frozen input manifest changed during corroboration")
    return manifest


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
        evidence = entry.get("corroboration")
        if evidence is not None:
            if evidence.get("status") not in CORROBORATION_STATUSES:
                errors.append(f"{name}: corroboration status must be one of {sorted(CORROBORATION_STATUSES)}")
            if evidence.get("for_sha256") != entry.get("sha256"):
                errors.append(f"{name}: corroboration belongs to a different version of the file")
            if evidence.get("comparison_policy") not in {None, COMPARISON_POLICY}:
                errors.append(f"{name}: unsupported corroboration comparison policy")
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
    corroborate_cmd = sub.add_parser("corroborate", help="(network) compare the files with Binance's public daily klines")
    corroborate_cmd.add_argument("names", nargs="*", help="limit to these file names (default: all registered)")
    args = parser.parse_args(argv)

    try:
        return _run(args)
    except (ValueError, OSError, urllib.error.URLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


def _run(args) -> int:

    if args.command == "verify":
        errors = verify(tracked=_tracked_csvs(DATA_DIR), strict_dir=args.strict_dir)
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        if not errors:
            manifest = json.loads((DATA_DIR / FROZEN_MANIFEST).read_text(encoding="utf-8"))
            statuses = [f["provenance"]["status"] for f in manifest["files"].values()]
            blocks = [f["corroboration"] for f in manifest["files"].values() if "corroboration" in f]
            evidence = [f.get("corroboration", {}).get("status") for f in manifest["files"].values()]
            legacy = sum("comparison_policy" not in block for block in blocks)
            print(f"Frozen research inputs verified: {len(statuses)} files "
                  f"({statuses.count('download_manifest')} with a download record, {statuses.count('unverified')} unverified; "
                  f"independent corroboration: {evidence.count('match')} match, {evidence.count('match_except_final_bar')} match except the "
                  f"final bar, {evidence.count('mismatch')} mismatch, "
                  f"{evidence.count('unavailable')} unavailable, {evidence.count(None)} not checked; "
                  f"{legacy} legacy comparison records not re-evaluated)")
        return 1 if errors else 0

    target = DATA_DIR / FROZEN_MANIFEST
    if args.command == "corroborate":
        updated = corroborate(DATA_DIR, args.names, fetch=fetch_binance_daily)
        target.write_text(render(updated), encoding="utf-8", newline="\n")
        for name, entry in sorted(updated["files"].items()):
            block = entry.get("corroboration", {})
            print(f"{name:<18} {block.get('status', '-'):<12} matching {block.get('rows_matching', '-')}/{entry['rows']}")
        return 0
    tracked = _tracked_csvs(DATA_DIR)
    previous = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else None
    text = render(build(DATA_DIR, tracked, previous))
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
