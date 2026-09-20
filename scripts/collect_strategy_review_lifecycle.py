"""Archive bounded primary lifecycle evidence and current public instrument facts.

Current instrument metadata is explicitly not a historical lifecycle registry.
Downloaded announcement versions are retrospective corroboration, not proof
that the same version was available at a historical decision timestamp.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.collect_strategy_review_public import SYMBOLS, digest, immutable_write, json_bytes


def archive_get(root, name, url):
    metadata_path = root / f"{name}.source.json"
    body_path = root / f"{name}.body"
    if metadata_path.exists():
        record = json.loads(metadata_path.read_text(encoding="utf-8"))
        body = body_path.read_bytes()
        if record["url"] != url or digest(body) != record["body_sha256"]:
            raise ValueError("Source archive identity mismatch")
        return body, record
    record = {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
              "method": "GET", "http_status": None, "error": None}
    body = b""
    try:
        response = requests.get(url, timeout=20, allow_redirects=False)
        body = response.content
        record["http_status"] = response.status_code
        record["content_type"] = response.headers.get("Content-Type")
        if response.status_code != 200 or not body:
            record["error"] = f"HTTP {response.status_code}; body_bytes={len(body)}"
    except requests.RequestException as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    record.update(body_sha256=digest(body), body_path=body_path.name)
    immutable_write(body_path, body)
    immutable_write(metadata_path, json_bytes(record))
    return body, record


def article_text(node):
    if isinstance(node, dict):
        return str(node.get("text", "")) + " " + " ".join(article_text(child) for child in node.get("child", []))
    return ""


def get_binance_announcement(root, name, url):
    code = url.rsplit("/", 1)[-1]
    api_url = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query?" + urlencode({"articleCode": code})
    raw, source = archive_get(root, name, api_url)
    if source["error"]:
        return {}, "", source
    payload = json.loads(raw)
    if payload.get("code") != "000000" or payload.get("data", {}).get("code") != code:
        return {}, "", {**source, "error": "CMS article code mismatch or API failure"}
    data = payload["data"]
    text = re.sub(r"\s+", " ", article_text(json.loads(data["body"]))).strip()
    immutable_write(root / f"{name}.extracted.txt", text.encode("utf-8"))
    return data, text, source


def validate_existing(root, symbol, facts):
    data, text, source = get_binance_announcement(root, f"existing_{symbol.split('/')[0]}", facts["url"])
    checks = {}
    if data:
        published = pd.Timestamp(data["publishDate"], unit="ms", tz="UTC")
        checks["announced_at"] = published.floor("min") == pd.Timestamp(facts["announced_at"]).floor("min")
        for field in ("margin_cutoff", "borrow_suspension"):
            formatted = pd.Timestamp(facts[field]).strftime("%Y-%m-%d %H:%M")
            checks[field] = formatted in text
    return {
        "symbol": symbol, "venue": "binance", "configured_facts": facts,
        "source": source, "title": data.get("title"),
        "announcement_published_at": (
            pd.Timestamp(data["publishDate"], unit="ms", tz="UTC").isoformat() if data else None
        ),
        "checks": checks, "status": "corroborated" if checks and all(checks.values()) else "unknown_or_mismatch",
        "evidence_grade": "primary_official_cms" if checks and all(checks.values()) else "unknown",
        "validation": "Publication minute and both configured effective times found in the corresponding official announcement; scopes manually checked as margin and borrowing.",
        "historical_version_availability": "unknown",
    }


def current_instruments(root):
    results = []
    for venue in ("binance", "okx"):
        for symbol in SYMBOLS:
            instrument = symbol.replace("/", "" if venue == "binance" else "-")
            url = ("https://api.binance.com/api/v3/exchangeInfo?" + urlencode({"symbol": instrument})
                   if venue == "binance" else "https://www.okx.com/api/v5/public/instruments?" + urlencode({"instType": "SPOT", "instId": instrument}))
            body, source = archive_get(root, f"current_{venue}_{symbol.replace('/', '')}", url)
            facts = None
            if source["error"] is None:
                payload = json.loads(body)
                items = payload.get("symbols" if venue == "binance" else "data", [])
                facts = next((item for item in items if item.get("symbol" if venue == "binance" else "instId") == instrument), None)
            results.append({
                "venue": venue, "symbol": symbol, "source": source,
                "status": "current_instrument_verified" if facts else "unknown",
                "current_state": facts.get("status" if venue == "binance" else "state") if facts else None,
                "current_instrument": facts,
                "historical_lifecycle_coverage": "unknown_non_exhaustive",
                "interpretation": "Current metadata only; does not prove historical listing dates or absence of suspensions.",
            })
    return results


def selected_events(root):
    specifications = [
        {"venue": "binance", "symbol": "ETH/USDT", "name": "binance_eth_merge",
         "url": "https://www.binance.com/en/support/announcement/detail/9a4805dffb8741a78f26075762a22a9c",
         "announced_at": "2022-09-13T14:41:00Z", "effective_at": "2022-09-15T00:30:00Z",
         "effective_precision": "estimated_time_subject_to_TTD", "event": "ERC20 deposit and withdrawal suspension",
         "spot_trading_halt_proven": False},
        {"venue": "binance", "symbol": "SOL/USDT", "name": "binance_sol_withdrawals",
         "url": "https://www.binance.com/en/support/announcement/detail/4fd0cd11c66642e9be99830070c6b038",
         "announced_at": "2024-03-06T09:46:00Z", "effective_at": "2024-03-04",
         "effective_precision": "date_only_intermittent", "event": "Intermittent Solana network withdrawal suspension",
         "spot_trading_halt_proven": False},
    ]
    results = []
    for specification in specifications:
        data, text, source = get_binance_announcement(root, specification["name"], specification["url"])
        expected = pd.Timestamp(specification["announced_at"]).floor("min")
        date = specification["effective_at"][:10]
        valid = bool(data and pd.Timestamp(data["publishDate"], unit="ms", tz="UTC").floor("min") == expected and date in text)
        results.append({**specification, "source": source,
                        "evidence_grade": "primary_official_cms" if valid else "unknown",
                        "status": "corroborated" if valid else "unknown_or_mismatch"})
    # Preserve the attempted primary source even if its former URL no longer resolves.
    _, source = archive_get(root, "okx_eth_merge", "https://www.okx.com/en-us/help/eth-merge-service-update")
    results.append({"venue": "okx", "symbol": "ETH/USDT", "source": source,
                    "status": "unknown_local_archive_unavailable", "evidence_grade": "unknown",
                    "event": "ETH Merge service notice; historical source URL investigated",
                    "spot_trading_halt_proven": False})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, default=ROOT / "reports/strategy_review_20260919")
    args = parser.parse_args()
    root = args.batch / "public_data/lifecycle_sources"
    inputs = (ROOT / "config/universe60_lifecycle.json").read_bytes()
    configured = json.loads(inputs)
    existing = [validate_existing(root, symbol, facts) for symbol, facts in configured.items()]
    instruments = current_instruments(root)
    events = selected_events(root)
    fees = []
    for venue, url in (("binance", "https://www.binance.com/en/fee/trading"), ("okx", "https://www.okx.com/fees")):
        _, source = archive_get(root, f"{venue}_fee_document", url)
        fees.append({"venue": venue, "source": source,
                     "historical_fee_series_verified": False,
                     "modeled_maker_rate": 0.001, "modeled_taker_rate": 0.001,
                     "classification": "frozen_common_research_assumption_not_reconstructed_historical_fees"})
    report = {
        "schema": "strategy_review_lifecycle_sources/v1",
        "config_sha256": digest(inputs), "collector_sha256": digest(Path(__file__).read_bytes()),
        "protocol_sha256": digest((args.batch / "review_protocol.json").read_bytes()),
        "existing_events": existing, "current_instruments": instruments,
        "selected_events_non_exhaustive": events, "fee_documentation": fees,
        "survivorship_and_selection_bias_resolved": False,
        "historical_full_universe_rebuilt": False,
        "unverified_events_policy": "unknown; absence of located evidence is not evidence of no events",
    }
    immutable_write(root / "manifest.json", json_bytes(report))
    print(json.dumps({"existing_events_corroborated": sum(e["status"] == "corroborated" for e in existing),
                      "current_instruments_verified": sum(e["status"] == "current_instrument_verified" for e in instruments),
                      "selected_events_corroborated": sum(e["status"] == "corroborated" for e in events),
                      "manifest": str(root / "manifest.json")}), flush=True)


if __name__ == "__main__":
    main()
