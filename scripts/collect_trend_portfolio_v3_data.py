"""Resume the official historical Binance USDT spot archive collection.

No exchange credentials, current-symbol universe, artificial candles or inferred
listing dates are used. The manifest explicitly distinguishes archive discovery
from unresolved lifecycle/classification and historical financing evidence.
"""
from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import threading
import traceback

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.trend_portfolio_data import (
    ARCHIVE, CMS, DAY, SCHEMA, EvidenceStore, archive_jobs, build_market_metadata,
    discover_markets, extract_article_facts, list_articles, parse_daily_archive,
    sha256, utc, verify_checksum, write_json,
)

COLLECTOR_SOURCE = Path(__file__).read_bytes()
DATA_MODULE_SOURCE = (ROOT / "core/trend_portfolio_data.py").read_bytes()
COLLECTOR_SHA256 = sha256(COLLECTOR_SOURCE)
PARSER_SHA256 = sha256(DATA_MODULE_SOURCE)


def archive_parser_fingerprint(source):
    """Metadata-only parser edits must not redownload/reparse unchanged candles."""
    names = {"parse_daily_archive", "_archive_timestamp", "verify_checksum", "utc", "sha256"}
    tree = ast.parse(source)
    functions = [ast.dump(node, include_attributes=False) for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    return sha256("\n".join(functions).encode())


ARCHIVE_PARSER_FINGERPRINT = archive_parser_fingerprint(DATA_MODULE_SOURCE)


def collect_lifecycle(store, output: Path, workers=6):
    # Lifecycle access has its own circuit breaker. An unavailable CMS must not
    # hold completed candle collection hostage for hours or repeat a rate ban.
    store = EvidenceStore(store.root, timeout=store.timeout, retries=0, cms_interval=1.0)
    stopped = threading.Event()
    catalog_records, articles, errors = [], {}, []
    for catalog in (48, 161):
        try:
            batch, sources = list_articles(store.get, catalog)
            catalog_records.append({"catalog": catalog, "article_count": len(batch),
                                    "sources": sources, "complete": True})
            for article in batch:
                articles[article["code"]] = article
        except Exception as exc:
            errors.append({"catalog": catalog, "error": f"{type(exc).__name__}: {exc}"})
            catalog_records.append({"catalog": catalog, "complete": False})
    relevant = [item for item in articles.values()
                if not re.search(r"futures|perpetual|options", item["title"], re.I)]
    write_json(output / "lifecycle_catalog.json", {"catalogs": catalog_records,
                                                    "articles": list(articles.values()), "errors": errors})
    print(json.dumps({"phase": "lifecycle", "catalog_articles": len(articles),
                      "relevant_details": len(relevant)}), flush=True)

    def detail(item):
        if stopped.is_set() and not (output / "lifecycle" / (item["code"] + ".json")).exists():
            return {"article_code": item["code"], "error": "deferred_after_cms_rate_limit",
                    "source_status": "not_requested_due_to_rate_limit"}
        body, source = store.get(CMS + "detail/query?articleCode=" + item["code"])
        if source.get("error"):
            if source.get("http_status") == 429:
                stopped.set()
            return {"article_code": item["code"], "error": source["error"], "source": source}
        try:
            payload = json.loads(body)
            data = payload.get("data") or {}
            if payload.get("code") != "000000" or data.get("code") != item["code"]:
                raise ValueError("CMS article identity mismatch")
            fact = extract_article_facts(data, source)
            fact["source"] = source
            write_json(output / "lifecycle" / (item["code"] + ".json"), fact)
            return fact
        except Exception as exc:
            return {"article_code": item["code"], "error": f"{type(exc).__name__}: {exc}", "source": source}

    facts = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fact in pool.map(detail, sorted(relevant, key=lambda item: item["code"])):
            facts.append(fact)
    summary = {"catalogs": catalog_records, "articles_discovered": len(articles),
               "details_requested": len(relevant), "details_failed": sum("error" in fact for fact in facts),
               "parsed_events": sum(len(fact.get("events", [])) for fact in facts),
               "parsed_classifications": sum(len(fact.get("classifications", [])) for fact in facts),
               "errors": errors, "historical_versions_verified": False,
               "interpretation": "Downloaded current announcement versions corroborate published facts; their historical versions are not proven."}
    write_json(output / "lifecycle_facts.json", {"summary": summary, "facts": facts})
    print(json.dumps({"phase": "lifecycle_complete", **{key: summary[key] for key in
                                                       ("details_requested", "details_failed", "parsed_events", "parsed_classifications")}}), flush=True)
    return facts, summary


def collect_market(store, output: Path, symbol: str, start, end, force_reparse=False):
    key = symbol[:-4] + "/USDT"
    result_path = output / "markets" / f"{symbol}.json"
    # Derived market records may be reused only for identical requested bounds
    # and source code, with a fully validated CSV digest and no prior failures.
    parser_digest = PARSER_SHA256
    if result_path.exists() and not force_reparse:
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        path = output / (previous.get("csv_path") or "missing.csv")
        previous_parser = previous.get("archive_parser_fingerprint")
        previous_source = output / "collector_sources" / (previous.get("parser_sha256", "") + ".py")
        if previous_parser is None and previous_source.is_file():
            previous_parser = archive_parser_fingerprint(previous_source.read_bytes())
        retryable_failures = [item for item in previous.get("failures", [])
                              if item.get("stage") in {"archive_discovery", "archive"}]
        if (previous.get("start") == start.isoformat() and previous.get("end_exclusive") == end.isoformat()
                and previous_parser == ARCHIVE_PARSER_FINGERPRINT and not retryable_failures
                and path.is_file() and sha256(path.read_bytes()) == previous.get("csv_sha256")):
            return key, previous
    record = {"symbol": key, "exchange_symbol": symbol, "start": start.isoformat(),
              "end_exclusive": end.isoformat(), "parser_sha256": parser_digest,
              "archive_parser_fingerprint": ARCHIVE_PARSER_FINGERPRINT,
              "archives": [], "failures": [], "csv_path": None}
    try:
        jobs, sources = archive_jobs(store.get, symbol, start, end)
        record.update(directory_sources=sources, archives_discovered=len(jobs), discovery_complete=True)
    except Exception as exc:
        record.update(discovery_complete=False)
        record["failures"].append({"stage": "archive_discovery", "error": f"{type(exc).__name__}: {exc}"})
        write_json(result_path, record)
        return key, record
    frames = []
    for archive_key in jobs:
        filename = archive_key.rsplit("/", 1)[-1]
        item = {"key": archive_key}
        try:
            body, source = store.get(ARCHIVE + archive_key)
            check, check_source = store.get(ARCHIVE + archive_key + ".CHECKSUM")
            item.update(source=source, checksum_source=check_source)
            if source.get("error") or check_source.get("error"):
                raise ValueError(source.get("error") or check_source.get("error"))
            item["official_sha256"] = verify_checksum(body, check, filename)
            frame, quality = parse_daily_archive(body, filename, end)
            frame = frame.loc[(frame.index >= start) & (frame.index < end)]
            item.update(quality)
            if len(frame):
                frames.append(frame)
            if quality["rejected_rows"]:
                record["failures"].append({"stage": "row_validation", "key": archive_key,
                                            "rejected_count": len(quality["rejected_rows"])})
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
            record["failures"].append({"stage": "archive", "key": archive_key, "error": item["error"]})
        record["archives"].append(item)
    if frames:
        frame = pd.concat(frames).sort_index()
        if frame.index.has_duplicates:
            duplicates = frame.index[frame.index.duplicated(keep=False)].unique()
            record["failures"].append({"stage": "cross_archive_duplicate", "dates": [v.isoformat() for v in duplicates]})
            frame = frame.loc[~frame.index.duplicated(keep=False)]
        destination = output / "data" / (symbol[:-4] + "_USDT.csv")
        destination.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(destination, index_label="timestamp")
        expected = pd.date_range(frame.index.min(), frame.index.max(), freq="D")
        missing = expected.difference(frame.index)
        record.update(csv_path=str(destination.relative_to(output)).replace("\\", "/"),
                      csv_sha256=sha256(destination.read_bytes()), rows=len(frame),
                      first_bar=frame.index.min().isoformat(), last_bar=frame.index.max().isoformat(),
                      internal_missing_days=[point.isoformat() for point in missing],
                      stale_tail_days=int((end - DAY - frame.index.max()) / DAY),
                      stale_tail_is_delisting_proof=False)
    else:
        record.update(rows=0, first_bar=None, last_bar=None, internal_missing_days=[])
    write_json(result_path, record)
    return key, record


def build_manifest(output, markets, discovery, lifecycle, args, completed=True):
    summaries = {key: {field: value for field, value in record.items()
                       if field not in {"archives", "directory_sources"}} for key, record in sorted(markets.items())}
    transfer_failures = sum(sum(item.get("stage") in {"archive_discovery", "archive"}
                                for item in record.get("failures", [])) for record in markets.values())
    inventory_complete = (completed and discovery.get("complete") and not discovery.get("diagnostic_subset")
                          and len(markets) == discovery.get("market_count") and not transfer_failures)
    return {"schema": SCHEMA, "research_id": "TrendPortfolioV3", "completed": completed,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "start": args.start, "end_inclusive": args.end,
            "data_availability": "completed_utc_daily_bar; retrospectively downloaded archive version",
            "market_discovery": discovery, "lifecycle": lifecycle, "markets": summaries,
            "coverage": {"historical_archive_markets_discovered": discovery.get("market_count", 0),
                         "markets_processed": len(markets),
                         "markets_with_candles": sum(bool(record.get("rows")) for record in markets.values()),
                         "total_daily_bars": sum(record.get("rows", 0) for record in markets.values()),
                         "archives_verified": sum(sum("official_sha256" in item for item in record.get("archives", [])) for record in markets.values()),
                         "failed_sources_or_rows": sum(len(record.get("failures", [])) for record in markets.values()),
                         "archive_transfer_failures": transfer_failures,
                         "rejected_archive_rows": sum(sum(item.get("rejected_count", 0) for item in record.get("failures", [])) for record in markets.values()),
                         "internal_missing_days": sum(len(record.get("internal_missing_days", [])) for record in markets.values())},
            "archive_inventory_download_complete": bool(inventory_complete),
            "historical_full_universe_verified": False,
            "financing_historical_evidence_coverage": 0.0,
            "blockers": ["historical_lifecycle_and_classification_require_coverage_review",
                         "historical_announcement_versions_unverified",
                         "historical_margin_eligibility_limits_and_rates_incomplete"],
            "collector_sha256": COLLECTOR_SHA256,
            "data_module_sha256": PARSER_SHA256}


def refresh_cached_lifecycle(output: Path):
    """Reparse retained primary source bytes without changing source evidence."""
    aggregate = output / "lifecycle_facts.json"
    if aggregate.exists():
        stored = json.loads(aggregate.read_text(encoding="utf-8"))
        retained = {item.get("article_code"): item for item in stored["facts"]}
        for path in (output / "lifecycle").glob("*.json"):
            fact = json.loads(path.read_text(encoding="utf-8"))
            if fact.get("article_code") not in retained or retained[fact["article_code"]].get("error"):
                retained[fact["article_code"]] = fact
        stored["facts"] = list(retained.values())
    else:
        retained = [json.loads(path.read_text(encoding="utf-8")) for path in (output / "lifecycle").glob("*.json")]
        stored = {"facts": retained, "summary": {"status": "partial_detail_collection",
                  "details_retained": len(retained), "historical_versions_verified": False}}
    facts = []
    for old in stored["facts"]:
        source = old.get("source", {})
        if old.get("error") or not source.get("body_file"):
            facts.append(old)
            continue
        body = (Path(source["evidence_dir"]) / source["body_file"]).read_bytes()
        if sha256(body) != source["body_sha256"]:
            raise ValueError("Lifecycle source digest mismatch")
        article = json.loads(body)["data"]
        fact = extract_article_facts(article, source)
        fact["source"] = source
        facts.append(fact)
        write_json(output / "lifecycle" / (article["code"] + ".json"), fact)
    summary = {**stored["summary"], "parser_sha256": PARSER_SHA256,
               "parsed_events": sum(len(fact.get("events", [])) for fact in facts),
               "parsed_classifications": sum(len(fact.get("classifications", [])) for fact in facts)}
    write_json(output / "lifecycle_facts.json", {"summary": summary, "facts": facts})
    return facts, summary


def refresh_metadata_only(output: Path):
    """Rebuild announcement-derived facts without changing any frozen candle."""
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("completed"):
        raise ValueError("Metadata refresh requires a completed candle collection")
    facts, lifecycle = refresh_cached_lifecycle(output)
    facts += load_supplemental_facts(output)
    metadata = {}
    for symbol, record in manifest["markets"].items():
        if record.get("csv_path"):
            path = output / record["csv_path"]
            if sha256(path.read_bytes()) != record["csv_sha256"]:
                raise ValueError(f"Frozen candle digest mismatch: {symbol}")
            frame = pd.read_csv(path, usecols=["timestamp"])
            frame.index = pd.to_datetime(frame.pop("timestamp"), utc=True, format="mixed")
        else:
            frame = pd.DataFrame()
        metadata[symbol] = build_market_metadata(symbol, frame, facts)
    write_json(output / "metadata.json", {"schema": SCHEMA, "symbols": metadata})
    manifest["lifecycle"] = lifecycle
    manifest["coverage"].update(
        markets_with_verified_listing=sum(item["listing_status"] == "verified" for item in metadata.values()),
        markets_with_verified_classification=sum(item["classification_status"] == "verified" for item in metadata.values()),
        markets_with_verified_both=sum(item["source_status"] == "verified" for item in metadata.values()))
    manifest["metadata_sha256"] = sha256((output / "metadata.json").read_bytes())
    manifest["metadata_parser_sha256"] = PARSER_SHA256
    manifest["metadata_refreshed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(manifest_path, manifest)
    print(json.dumps({"phase": "metadata_refreshed", **manifest["coverage"]}), flush=True)
    return 0


def load_supplemental_facts(output: Path):
    """Retain separately reviewed primary sources with mandatory byte bindings."""
    path = output / "supplemental_facts.json"
    if not path.exists():
        return []
    facts = json.loads(path.read_text(encoding="utf-8"))["facts"]
    for fact in facts:
        for event in fact.get("events", []) + fact.get("classifications", []):
            evidence = (output / event["evidence_file"]).resolve()
            if not evidence.is_relative_to(output.resolve()):
                raise ValueError("Supplemental evidence path escapes bundle")
            if sha256(evidence.read_bytes()) != event["source_sha256"]:
                raise ValueError("Supplemental fact source digest mismatch")
    return facts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/trend_portfolio_v3_20260921/market_data")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2026-09-18", help="Inclusive final complete UTC day")
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--lifecycle-workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-markets", type=int, default=0, help="Explicit diagnostic subset only; never full-universe acceptance")
    parser.add_argument("--skip-lifecycle", action="store_true", help="Reuse existing lifecycle facts; missing facts stay unknown")
    parser.add_argument("--force-reparse", action="store_true", help="Revalidate all archive bytes, using the immutable successful response cache")
    parser.add_argument("--refresh-metadata-only", action="store_true", help="Reparse cached announcement sources while preserving all candle CSVs and their provenance")
    args = parser.parse_args(argv)
    if args.workers < 1 or args.workers > 48 or args.lifecycle_workers < 1 or args.lifecycle_workers > 12:
        parser.error("workers must be 1..48, lifecycle-workers 1..12")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    snapshots = output / "collector_sources"
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / (COLLECTOR_SHA256 + ".py")).write_bytes(COLLECTOR_SOURCE)
    (snapshots / (PARSER_SHA256 + ".py")).write_bytes(DATA_MODULE_SOURCE)
    if args.refresh_metadata_only:
        return refresh_metadata_only(output)
    start, end = utc(args.start), utc(args.end) + DAY
    if start >= end:
        parser.error("start must precede the end")
    store = EvidenceStore(output / "raw", timeout=args.timeout, retries=args.retries)
    markets, discovery, lifecycle = {}, {}, {}
    try:
        symbols, sources = discover_markets(store.get)
        discovery = {"complete": True, "market_count": len(symbols), "symbols": symbols,
                     "sources": sources, "method": "union_paginated_historical_monthly_and_daily_S3_directories",
                     "current_exchange_symbols_used": False}
        write_json(output / "discovery.json", discovery)
    except Exception as exc:
        discovery = {"complete": False, "error": f"{type(exc).__name__}: {exc}"}
        write_json(output / "discovery.json", discovery)
        write_json(output / "metadata.json", {"schema": SCHEMA, "symbols": {}})
        write_json(output / "manifest.json", build_manifest(output, markets, discovery, lifecycle, args, False))
        print(json.dumps(discovery), flush=True)
        return 2
    print(json.dumps({"phase": "discovery", "historical_usdt_markets": len(symbols)}), flush=True)
    selected = symbols[:args.max_markets] if args.max_markets else symbols
    discovery["diagnostic_subset"] = bool(args.max_markets)
    with ThreadPoolExecutor(max_workers=1) as auxiliary:
        lifecycle_future = None if args.skip_lifecycle else auxiliary.submit(
            collect_lifecycle, store, output, args.lifecycle_workers)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(collect_market, store, output, symbol, start, end, args.force_reparse): symbol for symbol in selected}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    key, result = future.result()
                except Exception as exc:
                    key, result = symbol[:-4] + "/USDT", {"rows": 0, "failures": [{"error": repr(exc)}]}
                    traceback.print_exc()
                markets[key] = result
                if len(markets) % 10 == 0 or len(markets) == len(selected):
                    manifest = build_manifest(output, markets, discovery, lifecycle, args, False)
                    write_json(output / "manifest.json", manifest)
                    print(json.dumps({"phase": "archives", "completed": len(markets), "total": len(selected),
                                      "rows": manifest["coverage"]["total_daily_bars"],
                                      "failures": manifest["coverage"]["failed_sources_or_rows"]}), flush=True)
        if lifecycle_future:
            try:
                facts, lifecycle = lifecycle_future.result()
            except Exception as exc:
                facts, lifecycle = [], {"error": f"{type(exc).__name__}: {exc}"}
        elif (output / "lifecycle_facts.json").exists() or (output / "lifecycle").exists():
            facts, lifecycle = refresh_cached_lifecycle(output)
        else:
            facts, lifecycle = [], {"status": "not_collected"}
    metadata = {}
    facts += load_supplemental_facts(output)
    for symbol, record in markets.items():
        if record.get("csv_path"):
            frame = pd.read_csv(output / record["csv_path"], usecols=["timestamp"])
            frame.index = pd.to_datetime(frame.pop("timestamp"), utc=True, format="mixed")
        else:
            frame = pd.DataFrame()
        metadata[symbol] = build_market_metadata(symbol, frame, facts)
    write_json(output / "metadata.json", {"schema": SCHEMA, "symbols": metadata})
    manifest = build_manifest(output, markets, discovery, lifecycle, args)
    manifest["coverage"].update(
        markets_with_verified_listing=sum(item["listing_status"] == "verified" for item in metadata.values()),
        markets_with_verified_classification=sum(item["classification_status"] == "verified" for item in metadata.values()),
        markets_with_verified_both=sum(item["source_status"] == "verified" for item in metadata.values()))
    if args.max_markets:
        manifest["blockers"].append("explicit_diagnostic_market_subset")
    if manifest["coverage"]["failed_sources_or_rows"]:
        manifest["blockers"].append("source_or_validation_failures")
    if manifest["coverage"]["internal_missing_days"]:
        manifest["blockers"].append("internal_candle_gaps")
    manifest["metadata_sha256"] = sha256((output / "metadata.json").read_bytes())
    write_json(output / "manifest.json", manifest)
    print(json.dumps({"phase": "complete", "output": str(output), **manifest["coverage"],
                      "historical_full_universe_verified": False}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
