from __future__ import annotations

import hashlib
import io
import json
from urllib.parse import parse_qs, urlparse
import zipfile

import pandas as pd
import pytest

from core.trend_portfolio_data import (
    CMS, CausalUniverse, EvidenceStore, archive_jobs, build_market_metadata, discover_markets,
    extract_article_facts, list_articles, list_bucket, load_data_bundle,
    parse_daily_archive, sha256, utc, verify_checksum,
)


def xml_page(*, prefixes=(), keys=(), token=None):
    return ("<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
            + "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes)
            + "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
            + f"<IsTruncated>{str(token is not None).lower()}</IsTruncated>"
            + (f"<NextContinuationToken>{token}</NextContinuationToken>" if token else "")
            + "</ListBucketResult>").encode()


def response(body):
    return body, {"http_status": 200, "error": None}


def zipped(filename, rows):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(filename.removesuffix(".zip") + ".csv", "\n".join(rows))
    return out.getvalue()


def row(day, unit="ms", quote_volume=987654321, close_delta=0):
    stamp = utc(day)
    divisor = 1000 if unit == "us" else 1_000_000
    opening = stamp.value // divisor
    closing = (stamp + pd.Timedelta(days=1)).value // divisor - 1 + close_delta
    return f"{opening},100,120,90,110,123,{closing},{quote_volume},42,80,9000,0"


def test_directory_discovery_paginates_retains_retired_daily_only_and_not_current_symbols():
    calls = []
    def get(url):
        calls.append(url)
        params = parse_qs(urlparse(url).query)
        prefix = params["prefix"][0]
        if "monthly" in prefix and "continuation-token" not in params:
            return response(xml_page(prefixes=[prefix + "BTCUSDT/", prefix + "BTCEUR/"], token="next"))
        if "monthly" in prefix:
            assert params["continuation-token"] == ["next"]
            return response(xml_page(prefixes=[prefix + "DEADUSDT/"]))
        return response(xml_page(prefixes=[prefix + "NEWUSDT/", prefix + "BTCUSDT/"]))
    markets, evidence = discover_markets(get)
    assert markets == ["BTCUSDT", "DEADUSDT", "NEWUSDT"]
    assert len(calls) == len(evidence) == 3


def test_repeated_pagination_token_is_not_success():
    with pytest.raises(ValueError, match="repeated"):
        list_bucket(lambda url: response(xml_page(token="same")), "prefix/")


def test_archive_checksum_is_bound_to_bytes_and_filename():
    body = b"bytes"
    digest = hashlib.sha256(body).hexdigest()
    assert verify_checksum(body, f"{digest}  BTC.zip".encode(), "BTC.zip") == digest
    with pytest.raises(ValueError, match="filename"):
        verify_checksum(body, f"{digest}  OTHER.zip".encode(), "BTC.zip")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_checksum(body + b"!", f"{digest}  BTC.zip".encode(), "BTC.zip")


@pytest.mark.parametrize("unit,day", [("ms", "2024-12-31"), ("us", "2025-01-01")])
def test_quote_volume_and_timestamp_precision_are_preserved(unit, day):
    name = "BTCUSDT-1d-" + day + ".zip"
    frame, report = parse_daily_archive(zipped(name, [row(day, unit)]), name, "2025-01-03")
    assert frame.iloc[0]["quote_volume"] == 987654321
    assert frame.iloc[0]["quote_volume"] != frame.iloc[0]["close"] * frame.iloc[0]["volume"]
    assert frame.iloc[0]["timestamp_unit"] == unit
    precision = pd.Timedelta(microseconds=1) if unit == "us" else pd.Timedelta(milliseconds=1)
    assert frame.iloc[0]["close_time"] == utc(day) + pd.Timedelta(days=1) - precision
    assert frame.iloc[0]["available_at"] == utc(day) + pd.Timedelta(days=1)
    assert report["rejected_rows"] == []


def test_incomplete_and_duplicate_candles_cannot_become_signals():
    name = "AAAUSDT-1d-2025-01.zip"
    frame, report = parse_daily_archive(zipped(name, [row("2025-01-01", "us"),
        row("2025-01-01", "us"), row("2025-01-02", "us", close_delta=-1),
        row("2025-01-03", "us")]), name, "2025-01-03T12:00Z")
    assert frame.empty
    assert len(report["rejected_rows"]) == 2
    assert len(report["filtered_rows"]) == 1


def test_monthly_precedence_and_daily_tail_no_prelisting_fabrication():
    def get(url):
        params = parse_qs(urlparse(url).query)
        prefix = params["prefix"][0]
        if "monthly" in prefix:
            return response(xml_page(keys=[prefix + "AAAUSDT-1d-2026-08.zip",
                                           prefix + "AAAUSDT-1d-2026-08.zip.CHECKSUM"]))
        assert "AAAUSDT-1d-2026-09-00" in params["start-after"][0]
        return response(xml_page(keys=[prefix + "AAAUSDT-1d-2026-09-01.zip",
                                       prefix + "AAAUSDT-1d-2026-09-19.zip"]))
    jobs, _ = archive_jobs(get, "AAAUSDT", "2019-01-01", "2026-09-19")
    assert len(jobs) == 2
    assert jobs[0].endswith("2026-09-01.zip")
    assert jobs[1].endswith("2026-08.zip")


def article(title, lines, published="2020-01-01"):
    return {"code": "article", "title": title, "publishDate": utc(published).value // 1_000_000,
            "body": {"node": "root", "child": [
                {"tag": "p", "child": [{"text": line}]} for line in lines]}}


SOURCE = {"url": "https://www.binance.com/article", "retrieved_at": "2026-09-21T00:00Z",
          "body_sha256": "abc"}


def test_explicit_pair_listing_and_classification_have_distinct_availability():
    facts = extract_article_facts(article("Binance Will List Alpha (AAA)", [
        "Binance will open trading for AAA/USDT at 2020-01-02 08:00 (UTC).",
        "Alpha (AAA) is a decentralized blockchain protocol."]), SOURCE)
    assert facts["events"][0]["kind"] == "spot_listed"
    assert facts["events"][0]["effective_at"] == "2020-01-02T08:00:00+00:00"
    assert facts["events"][0]["available_at"] == "2020-01-01T00:00:00+00:00"
    assert facts["classifications"][0]["classification"] == "crypto"
    metadata = build_market_metadata("AAA/USDT", pd.DataFrame(index=pd.date_range("2019-01-01", periods=2, tz="UTC")), [facts])
    assert metadata["listing_effective_at"] != metadata["observed_first_bar"]
    assert metadata["source_status"] == "verified"


def test_margin_events_never_delist_the_spot_market():
    facts = extract_article_facts(article("Binance Margin Will Delist AAA", [
        "Binance will remove and cease trading on AAA/USDT at 2020-01-02 08:00 (UTC)."]), SOURCE)
    assert facts["events"][0]["kind"] == "margin_delisted"
    assert build_market_metadata("AAA/USDT", pd.DataFrame(), [facts])["listed_at"] is None


def test_explicit_spot_pairs_bind_to_spot_cessation_not_withdrawal_or_margin_dates():
    facts = extract_article_facts(article("Binance Will Delist AAA", [
        "We have decided to delist and cease trading on all trading pairs at 2020-01-03 03:00 (UTC):",
        "Alpha (AAA)", "Please note:", "The exact trading pairs being removed are: AAA/BTC, AAA/USDT.",
        "Withdrawals cease at 2020-02-03 03:00 (UTC).",
        "Binance Margin will remove AAA/USDT at 2020-01-02 03:00 (UTC)."]), SOURCE)
    events = {event["kind"]: event for event in facts["events"]}
    assert events["spot_delisted"]["effective_at"] == "2020-01-03T03:00:00+00:00"
    assert events["margin_delisted"]["effective_at"] == "2020-01-02T03:00:00+00:00"


def test_ambiguous_dates_and_non_usdt_markets_do_not_generate_listing_facts():
    facts = extract_article_facts(article("Binance Will List Alpha", [
        "Binance will open trading for AAA/BTC at 2020-01-02 08:00 (UTC).",
        "Binance will open trading for AAA/USDT at 2020-01-02 08:00 (UTC) or 2020-01-03 09:00 (UTC)."]), SOURCE)
    assert not facts["events"]
    assert facts["unresolved"]


def test_causal_membership_and_future_fact_invariance_no_inferred_last_bar_exit():
    listing = {"kind": "spot_listed", "effective_at": "2019-01-01", "available_at": "2018-12-31", "source_status": "verified"}
    future = {"kind": "spot_delisted", "effective_at": "2020-01-04", "available_at": "2020-01-03", "source_status": "verified"}
    frames = {"AAA/USDT": pd.DataFrame({"close": [1, 2]}, index=pd.date_range("2020-01-01", periods=2, tz="UTC"))}
    original = CausalUniverse({"AAA/USDT": {"events": [listing]}})
    extended = CausalUniverse({"AAA/USDT": {"events": [listing, future]}})
    pd.testing.assert_frame_equal(original.apply(frames)["AAA/USDT"], extended.apply(frames)["AAA/USDT"])
    assert not extended.apply(frames)["AAA/USDT"]["scheduled_exit"].any()
    assert extended.active_at("AAA/USDT", "2020-01-02")
    assert not extended.active_at("AAA/USDT", "2020-01-04")
    assert extended.scheduled_exit("AAA/USDT", "2020-01-03", "2020-01-04")


def test_unknown_classification_is_explicit_not_crypto_default():
    item = build_market_metadata("AAA/USDT", pd.DataFrame(index=pd.date_range("2019-01-01", periods=3, tz="UTC")), [])
    assert item["classification"] == "unknown"
    assert item["listing_effective_at"] is None
    assert item["source_status"] == "unknown"


def test_named_description_heading_is_classification_evidence_not_generic_risk_warning():
    facts = extract_article_facts(article("Binance Will List Alpha (AAA)", [
        "What is Alpha (AAA)?", "Alpha is a blockchain protocol. AAA is its native token.",
        "Risk warning: Cryptocurrency trading is risky."]), SOURCE)
    assert facts["classifications"][0]["symbol"] == "AAA/USDT"
    assert facts["classifications"][0]["classification"] == "crypto"
    facts = extract_article_facts(article("Binance Adds BBB/USDT", [
        "Risk warning: Cryptocurrency trading is risky."]), SOURCE)
    assert facts["classifications"] == []


def test_governance_token_of_stablecoin_protocol_is_not_itself_classified_stablecoin():
    facts = extract_article_facts(article("Binance Will List Alpha (AAA)", [
        "Alpha (AAA) is the governance token of a protocol that issues a dollar stablecoin."]), SOURCE)
    assert facts["classifications"][0]["classification"] == "crypto"
    stable = extract_article_facts(article("Binance Will List Beta (BBB)", [
        "Beta (BBB) is a dollar-backed stablecoin. It has a separate governance token."]), SOURCE)
    assert stable["classifications"][0]["classification"] == "stablecoin"


def test_daily_row_from_wrong_month_rejected_even_with_authentic_archive_name():
    name = "AAAUSDT-1d-2025-01.zip"
    frame, report = parse_daily_archive(zipped(name, [row("2025-02-01", "us")]), name, "2025-03-01")
    assert frame.empty
    assert "archive period" in report["rejected_rows"][0]["reason"]


def test_cms_catalogue_pagination_checks_advertised_total():
    calls = []
    def get(url):
        calls.append(url)
        number = int(parse_qs(urlparse(url).query)["pageNo"][0])
        return response(json.dumps({"code": "000000", "data": {"catalogs": [{
            "catalogId": 48, "total": 2, "articles": [{"code": str(number)}]}]}}).encode())
    articles, sources = list_articles(get, 48, page_size=1)
    assert [item["code"] for item in articles] == ["1", "2"]
    assert len(sources) == 2


def test_bundle_loader_checks_frozen_digest(tmp_path):
    (tmp_path / "metadata.json").write_text(json.dumps({"symbols": {}}), encoding="utf-8")
    path = tmp_path / "AAA_USDT.csv"
    path.write_text("timestamp,close,quote_volume\n2020-01-01T00:00:00Z,1,123\n", encoding="utf-8")
    manifest = {"markets": {"AAA/USDT": {"csv_path": path.name, "csv_sha256": sha256(path.read_bytes())}}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    frames, _, _ = load_data_bundle(tmp_path)
    assert frames["AAA/USDT"].iloc[0]["quote_volume"] == 123
    path.write_text(path.read_text() + "2020-01-02T00:00:00Z,2,100\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_data_bundle(tmp_path)


def test_evidence_retry_after_is_shared_and_successful_bytes_resume(tmp_path, monkeypatch):
    import core.trend_portfolio_data as module
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls = []
    class Reply:
        def __init__(self, status, body, headers):
            self.status_code, self.content, self.headers = status, body, headers
    class Session:
        headers = {}
        def get(self, url, **kwargs):
            calls.append(clock[0])
            return Reply(429, b"", {"Retry-After": "90"}) if len(calls) == 1 else Reply(200, b"official bytes", {})
    monkeypatch.setattr(module.requests, "Session", Session)
    store = EvidenceStore(tmp_path, retries=1)
    body, source = store.get(CMS + "detail/query?articleCode=abc")
    assert body == b"official bytes"
    assert calls[1] - calls[0] >= 90
    assert source["body_sha256"] == sha256(body)
    resumed, same = store.get(CMS + "detail/query?articleCode=abc")
    assert resumed == body and same == source
    assert len(calls) == 2
    assert len(list(tmp_path.glob("*/*/attempt_*.json"))) == 2


def test_metadata_refinement_does_not_invalidate_identical_candle_parser():
    from scripts.collect_trend_portfolio_v3_data import archive_parser_fingerprint
    original = b"def utc(x): return x\ndef classify(x): return 'unknown'\n"
    metadata_edit = b"def utc(x): return x\ndef classify(x): return 'crypto'\n"
    candle_edit = b"def utc(x): return x + 1\ndef classify(x): return 'unknown'\n"
    assert archive_parser_fingerprint(original) == archive_parser_fingerprint(metadata_edit)
    assert archive_parser_fingerprint(original) != archive_parser_fingerprint(candle_edit)


def test_empty_historical_market_cache_resumes_without_none_path_error(tmp_path):
    from scripts.collect_trend_portfolio_v3_data import collect_market
    (tmp_path / "markets").mkdir()
    (tmp_path / "markets/AAAUSDT.json").write_text(json.dumps({"csv_path": None, "rows": 0}), encoding="utf-8")
    class Store:
        def get(self, url):
            return response(xml_page())
    symbol, record = collect_market(Store(), tmp_path, "AAAUSDT", utc("2026-09-01"), utc("2026-09-19"))
    assert symbol == "AAA/USDT"
    assert record["rows"] == 0
    assert record["csv_path"] is None
    assert record["failures"] == []


def test_cms_rate_limit_defers_remaining_details_instead_of_blocking_candle_completion(tmp_path, monkeypatch):
    import scripts.collect_trend_portfolio_v3_data as script
    articles = [{"code": str(i), "title": "Binance Will List X"} for i in range(3)]
    monkeypatch.setattr(script, "list_articles", lambda get, catalog: (articles, []))
    calls = []
    class Store:
        root = tmp_path / "raw"
        timeout = 1
        def get(self, url):
            calls.append(url)
            return b"", {"error": "HTTP 429", "http_status": 429}
    monkeypatch.setattr(script, "EvidenceStore", lambda *args, **kwargs: Store())
    facts, summary = script.collect_lifecycle(Store(), tmp_path, workers=1)
    assert len(calls) == 1
    assert summary["details_failed"] == 3
    assert sum(f.get("source_status") == "not_requested_due_to_rate_limit" for f in facts) == 2
