"""Apply known listing-boundary repairs and build the frozen PIT universe.

The local SUI/USDT cache once contained an unrelated pre-listing series from
2022.  Binance spot trading for SUI/USDT starts on 2023-05-03, so those rows
must never enter a backtest or be used as indicator warm-up history.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd


LISTING_OVERRIDES = {"SUI/USDT": pd.Timestamp("2023-05-03")}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repair(data_dir: Path, universe_path: Path) -> None:
    manifest_path = data_dir / "_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for symbol, listed_at in LISTING_OVERRIDES.items():
        record = manifest["symbols"][symbol]
        csv_path = data_dir / record["file"]
        frame = pd.read_csv(csv_path, parse_dates=["timestamp"])
        frame = frame.loc[frame["timestamp"] >= listed_at].copy()
        if frame.empty or frame["timestamp"].min() != listed_at:
            raise RuntimeError(
                f"{symbol} has no first tradable bar at {listed_at.date()}"
            )
        frame.to_csv(csv_path, index=False, lineterminator="\n")
        record.update({
            "rows": int(len(frame)),
            "first": frame["timestamp"].min().isoformat(),
            "last": frame["timestamp"].max().isoformat(),
            "sha256": _sha256(csv_path),
        })

    manifest["data_corrections"] = {
        "SUI/USDT": {
            "removed_before": "2023-05-03",
            "reason": "exclude non-Binance/pre-listing series",
        }
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    requested_end = pd.Timestamp(manifest["requested_end"])
    rows = []
    for symbol, record in manifest["symbols"].items():
        last = pd.Timestamp(record["last"])
        rows.append({
            # main.py keeps the user's symbol spelling as the data-map key.
            "symbol": symbol.replace("/", "-"),
            "listed_at": pd.Timestamp(record["first"]).date().isoformat(),
            "delisted_at": (
                (last + pd.Timedelta(days=1)).date().isoformat()
                if last.normalize() < requested_end.normalize() else ""
            ),
            "source": "binance_ohlcv_cache_manifest",
        })
    pd.DataFrame(rows).sort_values("symbol").to_csv(
        universe_path, index=False, lineterminator="\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data/binance/1d")
    parser.add_argument(
        "--universe", default="config/universe_binance_spot_1d.csv"
    )
    args = parser.parse_args()
    repair(Path(args.data_dir), Path(args.universe))


if __name__ == "__main__":
    main()
