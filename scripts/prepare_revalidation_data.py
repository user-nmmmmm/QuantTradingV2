"""Freeze the predeclared 60-symbol Binance basket and validate every input."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
import requests
from scripts.run_expanded_universe_backtest import EXTRA_BASES, fetch_symbol
from scripts.fetch_binance_data import DEFAULT_SYMBOLS

START, END = "2016-01-01", "2026-06-30"
SYMBOLS = DEFAULT_SYMBOLS + [f"{base}/USDT" for base in EXTRA_BASES]
ARCHIVE = "https://data.binance.vision/"
LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"


def archive_history(symbol, folder):
    prefix = f"data/spot/monthly/klines/{symbol.replace('/', '')}/1d/"
    response = requests.get(LISTING, params={"prefix": prefix}, timeout=40)
    response.raise_for_status()
    tree = ET.fromstring(response.content)
    if any(item.text == "true" for item in tree.iter() if item.tag.endswith("IsTruncated")):
        raise ValueError("Archive listing is incomplete")
    keys = sorted(item.text for item in tree.iter() if item.tag.endswith("Key")
                  and item.text.endswith(".zip") and item.text[-11:-4] <= END[:7])
    frames, evidence = [], []
    for key in keys:
        response = requests.get(ARCHIVE + key, timeout=40)
        response.raise_for_status()
        checksum = requests.get(ARCHIVE + key + ".CHECKSUM", timeout=40)
        checksum.raise_for_status()
        digest = hashlib.sha256(response.content).hexdigest()
        if digest != checksum.text.split()[0]:
            raise ValueError("Official archive checksum mismatch")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            frame = pd.read_csv(archive.open(archive.namelist()[0]), header=None).iloc[:, :6]
        frame.columns = ["timestamp", "open", "high", "low", "close", "volume"]
        # Binance spot archives switched from milliseconds to microseconds in 2025.
        raw = pd.to_numeric(frame.pop("timestamp"))
        frame.index = pd.to_datetime(raw, unit="us" if raw.min() > 10**14 else "ms", utc=True).dt.tz_localize(None)
        frames.append(frame.astype(float))
        evidence.append({"url": ARCHIVE + key, "sha256": digest, "official_checksum_verified": True})
    if not frames:
        raise ValueError("No usable official archives")
    frame = pd.concat(frames).sort_index()
    frame = frame.loc[START:END]
    frame.index.name = "timestamp"
    filename = symbol.replace("/", "_") + ".csv"
    frame.to_csv(folder / filename, float_format="%.17g")
    return {"file": filename, "pages": evidence, "source": "Binance official monthly archive"}


def prepare_one(symbol, folder):
    path = folder / (symbol.replace("/", "_") + ".csv")
    sidecar = path.with_suffix(".source.json")
    if path.exists() and sidecar.exists():
        entry = json.loads(sidecar.read_text(encoding="utf-8"))
    else:
        try:
            entry = fetch_symbol(symbol, folder, START, END)
            entry["source"] = "Binance public klines API"
        except Exception as exc:
            print(f"ARCHIVE {symbol}: {type(exc).__name__}", flush=True)
            entry = archive_history(symbol, folder)
        sidecar.write_text(json.dumps(entry, default=str, indent=2), encoding="utf-8")
    frame = pd.read_csv(path, index_col="timestamp", parse_dates=True, float_precision="round_trip")
    frame = frame.loc[START:END]
    if frame.empty or frame.index.has_duplicates or not np.isfinite(frame.to_numpy()).all():
        raise ValueError("Empty, duplicate or nonfinite data")
    if ((frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (frame.volume < 0) | (frame.high < frame[["open", "close", "low"]].max(axis=1))
        | (frame.low > frame[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Invalid OHLCV data requires quarantine")
    missing = pd.date_range(frame.index.min(), frame.index.max(), freq="D").difference(frame.index)
    entry.update(symbol=symbol, first=str(frame.index.min()), last=str(frame.index.max()), rows=len(frame),
                 sha256=hashlib.sha256(path.read_bytes()).hexdigest(), missing_days=[str(day) for day in missing],
                 truncated_before_end=frame.index.max() < pd.Timestamp(END),
                 listing_evidence="first actual Binance bar; formal listing announcement not yet verified",
                 delisting_evidence=None)
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    folder = args.output / "data_inputs"
    folder.mkdir(parents=True, exist_ok=True)
    assert len(SYMBOLS) == len(set(SYMBOLS)) == 60
    (args.output / "universe60.json").write_text(json.dumps(SYMBOLS, indent=2), encoding="utf-8")
    entries, errors = {}, {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(prepare_one, symbol, folder): symbol for symbol in SYMBOLS}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                entries[symbol] = future.result()
                print(f"READY {len(entries)}/60 {symbol}", flush=True)
            except Exception as exc:
                errors[symbol] = f"{type(exc).__name__}: {exc}"
                print(f"BLOCKED {symbol}: {type(exc).__name__}", flush=True)
    manifest = {"start": START, "end_inclusive_utc": END, "symbols": entries, "errors": errors,
                "complete": len(entries) == 60 and not errors, "static_selection_bias": True,
                "lifecycle_evidence_complete": False}
    (args.output / "data_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not manifest["complete"]:
        raise SystemExit("Strict 60/60 gate failed; no official backtest may be declared complete")


if __name__ == "__main__":
    main()
