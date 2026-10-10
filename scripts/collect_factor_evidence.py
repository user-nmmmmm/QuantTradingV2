"""Bounded anonymous first-party daily factor inputs; no historical PIT assertion."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parents[1]
from analysis.historical_factor_inputs import (
    DEFAULT_ASSETS, METRICS, coinmetrics_records, freeze_factor_inputs, build_factor_proxies, lifecycle_evidence_records, public_metric_groups,
)
from analysis.factor_registry import historical_input_proxy_definitions, freeze_factor_registry
from core.data_versions import DataVersionStore

BASE = "https://community-api.coinmetrics.io/v4/"
DOCS = {
    "market_cap_methodology": "https://raw.githubusercontent.com/coinmetrics/docs-website/master/asset-metrics/market/capmrktcurusd.md",
    "current_supply_methodology": "https://raw.githubusercontent.com/coinmetrics/docs-website/master/asset-metrics/supply/splycur.md",
    "daily_price_methodology": "https://raw.githubusercontent.com/coinmetrics/docs-website/master/asset-metrics/market/priceusd.md",
    "community_api_methodology": "https://raw.githubusercontent.com/coinmetrics/docs-website/master/api.md",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def immutable_json(path, value):
    raw = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("immutable evidence identity changed")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(raw)


class AnonymousAuth(requests.auth.AuthBase):
    def __call__(self, request):
        request.headers.pop("Authorization", None)
        return request


class PublicArchive:
    """Bounded GETs; exact response bytes, safe metadata and no credential lookup."""
    def __init__(self, root, *, session=None, timeout=8, max_requests=20):
        self.root, self.timeout, self.max_requests = Path(root), timeout, max_requests
        self.owned = session is None
        self.session = session or requests.Session()
        self.session.trust_env = True
        self.session.auth = AnonymousAuth()
        self.calls, self.started = 0, time.monotonic()

    def fetch(self, name, url):
        if not (url.startswith(BASE) or url in DOCS.values()):
            raise ValueError("unregistered public source")
        body_path, meta_path = self.root / (name + ".body"), self.root / (name + ".source.json")
        if body_path.exists() or meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            raw = body_path.read_bytes()
            if meta["url"] != url or sha(raw) != meta["body_sha256"]:
                raise ValueError("raw source identity mismatch")
            return raw, meta
        meta = {"url": url, "method": "GET", "observed_at": None, "available_at": None,
                "published_at": None, "revision_at": None, "status": None, "error_type": None}
        raw = b""
        for attempt in range(2):
            if self.calls >= self.max_requests or time.monotonic() - self.started > 180:
                raise ValueError("registered request/time budget exhausted")
            self.calls += 1
            try:
                with self.session.get(url, timeout=(self.timeout, self.timeout), allow_redirects=False, stream=True) as response:
                    meta["status"] = response.status_code
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=65536):
                        size += len(chunk)
                        if size > 20_000_000 or time.monotonic() - self.started > 180:
                            raise ValueError("bounded public response limit exceeded")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    meta["observed_at"] = now()
                    meta["available_at"] = now()
                    meta["response_headers"] = {key: response.headers[key] for key in ("Content-Type", "Date", "ETag") if key in response.headers}
                    if response.status_code == 429 and attempt == 0:
                        wait = float(response.headers.get("Retry-After", "1"))
                        if 0 <= wait <= 5:
                            time.sleep(wait)
                            continue
                    if response.status_code != 200:
                        meta["error_type"] = "http_error"
                    break
            except requests.RequestException as exc:
                meta.update(error_type=type(exc).__name__, observed_at=now(), available_at=now())
                break
        meta.update(body_sha256=sha(raw), body_bytes=len(raw), historical_pit_verified=False)
        self.root.mkdir(parents=True, exist_ok=True)
        with body_path.open("xb") as handle:
            handle.write(raw)
        immutable_json(meta_path, meta)
        if self.owned:
            time.sleep(0.7)
        return raw, meta

    def close(self):
        if self.owned:
            self.session.close()


def collect(output, *, start="2020-01-01T00:00:00Z", end_exclusive="2026-09-20T00:00:00Z",
            assets=DEFAULT_ASSETS, archive=None, resume=False, source_root=ROOT):
    output, source_root = Path(output), Path(source_root)
    assets = tuple(sorted(assets))
    if assets != tuple(sorted(DEFAULT_ASSETS)):
        raise ValueError("this registered study uses only the six existing review assets")
    source_paths = ("analysis/historical_factor_inputs.py", "analysis/factor_registry.py", "scripts/collect_factor_evidence.py")
    protocol = {"schema": "historical-factor-collection/v1", "assets": list(assets), "start": start,
        "end_exclusive": end_exclusive, "metrics": list(METRICS), "frequency": "1d", "max_requests": 20,
        "retrospective_only": True, "no_current_universe_backfill": True, "orders_submitted": 0,
        "source_hashes": {p: sha((source_root / p).read_bytes()) for p in source_paths},
        "cohort_reason": "same six pre-existing strategy-review assets, not sorted by currently observed cap"}
    protocol["entitlement_policy"] = "request only explicit community=true daily metric combinations; retain unsupported fields as unknown"
    if resume:
        old = json.loads((output / "registration.json").read_text(encoding="utf-8"))
        if old["protocol"] != protocol:
            raise ValueError("resume requires unchanged registered source/protocol")
    else:
        output.mkdir(parents=True, exist_ok=False)
        immutable_json(output / "registration.json", {"registered_at": now(), "protocol": protocol})
    owned = archive is None
    archive = archive or PublicArchive(output / "sources")
    records, sources, issues = [], [], []
    result = {"schema": "historical-factor-evidence/v1", "status": "started", "retrospective_only": True,
        "historical_pit_verified": False, "full_universe_rebuilt": False, "orders_submitted": 0}
    try:
        requests_to_make = [(name, url) for name, url in DOCS.items()]
        requests_to_make += [("metric_reference", BASE + "reference-data/asset-metrics?" + urlencode({"metrics": ",".join(METRICS), "page_size": 100})),
            ("asset_reference", BASE + "reference-data/assets?" + urlencode({"assets": ",".join(assets), "page_size": 100})),
            ("current_provider_coverage", BASE + "catalog-v2/asset-metrics?" + urlencode({"assets": ",".join(assets), "metrics": ",".join(METRICS), "page_size": 100}))]
        catalog = {}
        for name, url in requests_to_make:
            body, meta = archive.fetch(name, url)
            sources.append({"name": name, **meta})
            if meta["error_type"]:
                issues.append({"source": name, "reason": meta["error_type"], "http_status": meta["status"]})
            elif name == "current_provider_coverage":
                catalog = json.loads(body)
        groups = public_metric_groups(catalog, assets=assets)
        result["public_metric_groups"] = groups
        for group_no, group in enumerate(groups):
            params = {"assets": ",".join(group["assets"]), "metrics": ",".join(group["metrics"]), "frequency": "1d",
                      "start_time": start, "end_time": end_exclusive, "end_inclusive": "false", "page_size": 10000}
            tokens = set()
            for page in range(4):
                url = BASE + "timeseries/asset-metrics?" + urlencode(params)
                name = f"metrics_group_{group_no:02d}_page_{page:02d}"
                body, meta = archive.fetch(name, url)
                sources.append({"name": name, **meta})
                if meta["error_type"]:
                    issues.append({"source": name, "reason": meta["error_type"], "http_status": meta["status"]})
                    break
                payload = json.loads(body)
                records.extend(coinmetrics_records(payload, source_url=url, raw_sha256=meta["body_sha256"],
                    observed_at=meta["observed_at"], available_at=meta["available_at"], assets=group["assets"], start=start, end_exclusive=end_exclusive))
                token = payload.get("next_page_token")
                if not token:
                    break
                if token in tokens or page == 3:
                    raise ValueError("unexpected pagination beyond registered bounds")
                tokens.add(token)
                params["next_page_token"] = token
        if len({record["record_id"] for record in records}) != len(records):
            raise ValueError("duplicate input revision across pages")
        input_snapshot = freeze_factor_inputs(output / "data_versions", records, source_refs=sources,
                                             code_refs=protocol["source_hashes"])
        result["input_snapshot_id"] = input_snapshot["snapshot_id"]
        frame, factor_summary = build_factor_proxies(records, assets=assets, start=start, end_exclusive=end_exclusive)
        csv = frame.to_csv(index=False).encode("utf-8")
        path = output / "factor_proxies.csv"
        if path.exists():
            if path.read_bytes() != csv:
                raise ValueError("factor output changed")
        else:
            with path.open("xb") as handle:
                handle.write(csv)
        factor_summary["file"], factor_summary["sha256"] = path.name, sha(csv)
        result["factor_production"] = factor_summary
        cap_cohort = sorted({asset for group in groups if set(METRICS) <= set(group["metrics"]) for asset in group["assets"]})
        if len(cap_cohort) >= 2 and cap_cohort != list(assets):
            subset, subset_summary = build_factor_proxies(records, assets=cap_cohort, start=start, end_exclusive=end_exclusive)
            subset_path = output / "public_subset_factor_proxies.csv"
            subset_bytes = subset.to_csv(index=False).encode("utf-8")
            if subset_path.exists():
                if subset_path.read_bytes() != subset_bytes:
                    raise ValueError("subset factor output changed")
            else:
                with subset_path.open("xb") as handle:
                    handle.write(subset_bytes)
            subset_summary.update(file=subset_path.name, sha256=sha(subset_bytes),
                selection_rule="current provider metric entitlement only; not historical eligible universe")
            result["public_subset_factor_production"] = subset_summary
        definitions = historical_input_proxy_definitions(cap_cohort if len(cap_cohort) >= 2 else assets)
        frozen = freeze_factor_registry(output / "data_versions", definitions,
            observed_at=json.loads((output / "registration.json").read_text())["registered_at"], code_refs=protocol["source_hashes"])
        result["factor_definition_snapshot_id"] = frozen["snapshot_id"]
        legacy_path = source_root / "reports/strategy_review_20260919/public_data/lifecycle_sources/manifest.json"
        if legacy_path.exists():
            raw = legacy_path.read_bytes()
            legacy = json.loads(raw)
            registration_at = json.loads((output / "registration.json").read_text())["registered_at"]
            lifecycle = lifecycle_evidence_records(legacy, raw_sha256=sha(raw), observed_at=registration_at, source_reference=str(legacy_path))
            snapshot = DataVersionStore(output / "data_versions").create_snapshot("legacy-scoped-lifecycle-evidence", lifecycle,
                source_refs={"manifest_path": str(legacy_path), "sha256": sha(raw)},
                metadata={"full_universe_rebuilt": False, "historical_membership_status": "unknown"})
            result["lifecycle"] = {"snapshot_id": snapshot["snapshot_id"], "records": len(lifecycle),
                "full_universe_rebuilt": False, "current_metadata_is_historical": False,
                "margin_or_withdrawal_events_are_spot_delistings": False}
        counts = {asset: {"rows": sum(r["data"]["asset"] == asset for r in records),
            "complete_input_rows": sum(r["data"]["asset"] == asset and not r["data"]["quality_issues"] and
                all(r["data"][name] is not None for name in METRICS.values()) for r in records)} for asset in assets}
        result.update(status="limited_evidence_complete" if records and not issues else "limited_or_unavailable",
            input_rows=len(records), coverage=counts, sources=sources, issues=issues,
            full_factor_status="unavailable", gaps=["Full historical listing/delisting and suspended-asset universe absent",
                "Historical revision/publication availability unknown; today's capture is retrospective",
                "Provider current-supply market cap differs from circulating/free-float market cap"],
            alternative_source_limits={"coingecko_demo": {"documentation": "https://docs.coingecko.com/demo/reference/coins-id-market-chart-range",
                "restriction": "API key required; historical access limited to 365 days; not requested"}})
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__, input_rows=len(records), sources=sources, issues=issues)
    finally:
        if owned:
            archive.close()
    result["network_requests"] = archive.calls
    immutable_json(output / "manifest.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    result = collect(args.output, resume=args.resume)
    print(json.dumps({key: result.get(key) for key in ("status", "input_rows", "coverage", "factor_production", "issues", "error_type", "network_requests")}))
    return result


if __name__ == "__main__":
    main()
