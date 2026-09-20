"""Auditable Binance archive inputs for the isolated TrendPortfolioV3 study.

Archive existence, announcement publication, and market membership are distinct
facts.  None of the helpers infer a delisting from a series' last observation.
The archive is a retrospectively downloaded version; ``available_at`` for a
candle is its completed market interval, not a claim about archive publication.
"""
from __future__ import annotations

from datetime import datetime, timezone
import csv
import hashlib
import html
import io
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Callable, Mapping
from urllib.parse import urlencode
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd
import requests


BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
ARCHIVE = "https://data.binance.vision/"
CMS = "https://www.binance.com/bapi/composite/v1/public/cms/article/"
DAY = pd.Timedelta(days=1)
SCHEMA = "trend_portfolio_v3_market_data/v1"


def utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_json(path: Path, value) -> None:
    """Atomically update derived progress; raw evidence is written separately."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class EvidenceStore:
    """Content-addressed raw GET responses with resumable successful requests.

    Failures remain in numbered attempt files.  They may be retried on another
    invocation; successful source bytes are never silently replaced.
    """

    def __init__(self, root: Path, timeout=25.0, retries=2, cms_interval=0.8):
        self.root = Path(root)
        self.timeout = timeout
        self.retries = retries
        self._local = threading.local()
        self.cms_interval = cms_interval
        self._cms_lock = threading.Lock()
        self._cms_next = 0.0

    def _pace_cms(self, url):
        if not url.startswith(CMS):
            return
        while True:
            with self._cms_lock:
                wait = self._cms_next - time.monotonic()
                if wait <= 0:
                    self._cms_next = time.monotonic() + self.cms_interval
                    return
            time.sleep(min(wait, 1.0))

    def get(self, url: str) -> tuple[bytes, dict]:
        key = sha256(url.encode())
        folder = self.root / key[:2] / key
        folder.mkdir(parents=True, exist_ok=True)
        pointer = folder / "source.json"
        if pointer.exists():
            record = json.loads(pointer.read_text(encoding="utf-8"))
            payload = (folder / record["body_file"]).read_bytes()
            if record["url"] != url or sha256(payload) != record["body_sha256"]:
                raise ValueError("Cached source integrity mismatch")
            if record["http_status"] == 200 and payload:
                return payload, record
            if record.get("http_status") == 429 and url.startswith(CMS):
                cooldown_end = utc(record["retrieved_at"]) + pd.Timedelta(
                    seconds=float(record.get("retry_after_seconds", 60)))
                remaining = max(0.0, (cooldown_end - utc(datetime.now(timezone.utc))).total_seconds())
                with self._cms_lock:
                    self._cms_next = max(self._cms_next, time.monotonic() + remaining)
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
            session.headers.update({"User-Agent": "TrendPortfolioV3-public-research/1.0",
                                    "Accept": "*/*", "lang": "en"})
        attempt = len(list(folder.glob("attempt_*.json")))
        for retry in range(self.retries + 1):
            self._pace_cms(url)
            attempt += 1
            body = b""
            record = {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                      "method": "GET", "http_status": None, "error": None}
            try:
                response = session.get(url, timeout=self.timeout, allow_redirects=False)
                body = response.content
                record.update(http_status=response.status_code,
                              content_type=response.headers.get("Content-Type"),
                              last_modified=response.headers.get("Last-Modified"),
                              etag=response.headers.get("ETag"))
                if response.status_code == 429 and url.startswith(CMS):
                    retry_after = response.headers.get("Retry-After", "60")
                    try:
                        pause = max(float(retry_after), 60.0)
                    except ValueError:
                        try:
                            pause = max((utc(retry_after) - utc(datetime.now(timezone.utc))).total_seconds(), 60.0)
                        except ValueError:
                            pause = 60.0
                    record["retry_after_seconds"] = pause
                    with self._cms_lock:
                        self._cms_next = max(self._cms_next, time.monotonic() + pause)
                if response.status_code != 200 or not body:
                    record["error"] = f"HTTP {response.status_code}; bytes={len(body)}"
            except requests.RequestException as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
            body_file = f"{sha256(body)}.body"
            body_path = folder / body_file
            if not body_path.exists():
                body_path.write_bytes(body)
            record.update(body_sha256=sha256(body), body_file=body_file,
                          evidence_dir=str(folder), body_bytes=len(body))
            write_json(folder / f"attempt_{attempt:04d}.json", record)
            write_json(pointer, record)
            if not record["error"] or record["http_status"] in {400, 403, 404, 451}:
                break
            if retry < self.retries:
                time.sleep(min(1 + retry, 3))
        return body, record


def list_bucket(get: Callable, prefix: str, *, delimiter=None, start_after=None,
                max_pages=10000) -> tuple[list[str], list[str], list[dict]]:
    """Read every S3 continuation page; fail closed on broken pagination."""
    keys, prefixes, evidence = [], [], []
    token = None
    seen = set()
    for _ in range(max_pages):
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if delimiter:
            params["delimiter"] = delimiter
        if token:
            params["continuation-token"] = token
        elif start_after:
            params["start-after"] = start_after
        body, source = get(BUCKET + "?" + urlencode(params))
        evidence.append(source)
        if source.get("error"):
            raise ValueError(f"S3 listing failed for {prefix}: {source['error']}")
        root = ET.fromstring(body)
        def texts(path):
            return [item.text or "" for item in root.findall(path)]
        keys.extend(texts("{*}Contents/{*}Key"))
        prefixes.extend(texts("{*}CommonPrefixes/{*}Prefix"))
        truncated = (root.findtext("{*}IsTruncated") or "false").lower() == "true"
        if not truncated:
            return sorted(set(keys)), sorted(set(prefixes)), evidence
        token = root.findtext("{*}NextContinuationToken")
        if not token or token in seen:
            raise ValueError("S3 truncated listing has missing or repeated continuation token")
        seen.add(token)
    raise ValueError("S3 page guard exceeded; discovery is incomplete")


def discover_markets(get: Callable) -> tuple[list[str], list[dict]]:
    """Union historical monthly and daily directories, retaining dead markets."""
    found, sources = set(), []
    for kind in ("monthly", "daily"):
        _, prefixes, records = list_bucket(get, f"data/spot/{kind}/klines/", delimiter="/")
        sources.extend(records)
        for prefix in prefixes:
            symbol = prefix.rstrip("/").rsplit("/", 1)[-1]
            if re.fullmatch(r"[A-Z0-9]+USDT", symbol) and symbol != "USDTUSDT":
                found.add(symbol)
    return sorted(found), sources


def verify_checksum(body: bytes, checksum: bytes, filename: str) -> str:
    lines = checksum.decode("utf-8-sig").strip().splitlines()
    if len(lines) != 1:
        raise ValueError("Checksum must identify exactly one archive")
    match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(\S+)", lines[0].strip())
    if not match or match.group(2) != filename:
        raise ValueError("Checksum archive filename mismatch")
    digest = sha256(body)
    if digest != match.group(1).lower():
        raise ValueError("Official archive checksum mismatch")
    return digest


def _archive_timestamp(value):
    number = int(value)
    unit = "us" if number >= 100_000_000_000_000 else "ms"
    stamp = pd.Timestamp(number, unit=unit, tz="UTC")
    if not utc("2000-01-01") <= stamp < utc("2100-01-01"):
        raise ValueError("Unsupported kline timestamp")
    return stamp, unit


def parse_daily_archive(body: bytes, filename: str, as_of) -> tuple[pd.DataFrame, dict]:
    """Retain real quote volume and exact millisecond/microsecond close times."""
    as_of = utc(as_of)
    period_match = re.search(r"-1d-(\d{4}-\d{2}(?:-\d{2})?)\.zip$", filename)
    if not period_match:
        raise ValueError("Archive filename must identify a daily-kline month or day")
    period = period_match.group(1)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) != 1 or members[0].filename != filename.removesuffix(".zip") + ".csv":
            raise ValueError("Unexpected archive CSV member")
        if members[0].file_size > 10_000_000:
            raise ValueError("Daily-kline archive expands beyond limit")
        text = archive.read(members[0]).decode("utf-8-sig")
    rows, issues, excluded, duplicates = {}, [], [], set()
    for line, values in enumerate(csv.reader(io.StringIO(text)), 1):
        if not values or (line == 1 and values[0].lower() in {"open_time", "open time"}):
            continue
        try:
            if len(values) != 12:
                raise ValueError("Expected twelve Binance kline fields")
            opening, unit = _archive_timestamp(values[0])
            closing, closing_unit = _archive_timestamp(values[6])
            if not opening.strftime("%Y-%m-%d").startswith(period):
                raise ValueError("Candle does not belong to archive period")
            quantum = pd.Timedelta(microseconds=1) if unit == "us" else pd.Timedelta(milliseconds=1)
            if opening != opening.floor("D") or unit != closing_unit or closing != opening + DAY - quantum:
                raise ValueError("Kline is not an exact completed UTC day")
            if opening + DAY > as_of:
                excluded.append({"line": line, "reason": "not_available_at_cutoff"})
                continue
            o, high, low, close, volume = [float(item) for item in values[1:6]]
            quote_volume = float(values[7])
            if not all(math.isfinite(v) for v in (o, high, low, close, volume, quote_volume)):
                raise ValueError("Nonfinite price or volume")
            if min(o, high, low, close) <= 0 or min(volume, quote_volume) < 0 or low > min(o, close) or high < max(o, close):
                raise ValueError("Invalid price envelope or negative volume")
            if opening in rows or opening in duplicates:
                duplicates.add(opening)
                rows.pop(opening, None)
                raise ValueError("Duplicate candle: all copies excluded")
            rows[opening] = {"timestamp": opening, "open": o, "high": high, "low": low,
                             "close": close, "volume": volume, "quote_volume": quote_volume,
                             "close_time": closing, "available_at": opening + DAY,
                             "timestamp_unit": unit, "source_archive": filename}
        except (ValueError, OverflowError) as exc:
            issues.append({"line": line, "reason": str(exc), "raw_row": values})
    columns = ["timestamp", "open", "high", "low", "close", "volume", "quote_volume",
               "close_time", "available_at", "timestamp_unit", "source_archive"]
    frame = pd.DataFrame([rows[key] for key in sorted(rows)], columns=columns).set_index("timestamp")
    return frame, {"retained_rows": len(frame), "rejected_rows": issues, "filtered_rows": excluded}


def archive_jobs(get: Callable, symbol: str, start, end) -> tuple[list[str], list[dict]]:
    """Prefer monthly files; use daily files for uncovered observed months/tail."""
    start, end = utc(start), utc(end)
    prefix = f"data/spot/monthly/klines/{symbol}/1d/"
    monthly, _, evidence = list_bucket(get, prefix)
    months = {}
    for key in monthly:
        match = re.search(r"-1d-(\d{4}-\d{2})\.zip$", key)
        if not match:
            continue
        first = utc(match.group(1) + "-01")
        last = first + pd.offsets.MonthBegin(1)
        if last > start and first < end and last <= end:
            months[first.strftime("%Y-%m")] = key
    jobs = list(months.values())
    # Daily listing starts at the earliest missing month after the first known
    # archive. It does not issue thousands of synthetic pre-listing requests.
    first_observed = min((utc(re.search(r"-1d-(\d{4}-\d{2})\.zip$", key).group(1) + "-01")
                          for key in monthly if re.search(r"-1d-(\d{4}-\d{2})\.zip$", key)), default=start)
    daily_start = max(start, first_observed)
    missing_months = [period.strftime("%Y-%m") for period in pd.date_range(
        daily_start.replace(day=1), end - pd.Timedelta(microseconds=1), freq="MS")
        if period.strftime("%Y-%m") not in months]
    if missing_months:
        dprefix = f"data/spot/daily/klines/{symbol}/1d/"
        keys, _, daily_sources = list_bucket(get, dprefix,
                                             start_after=dprefix + f"{symbol}-1d-{missing_months[0]}-00")
        evidence.extend(daily_sources)
        for key in keys:
            match = re.search(r"-1d-(\d{4}-\d{2}-\d{2})\.zip$", key)
            if match and match.group(1)[:7] in missing_months:
                opening = utc(match.group(1))
                if start <= opening and opening + DAY <= end:
                    jobs.append(key)
    return sorted(set(jobs)), evidence


def article_text(node) -> str:
    """Extract readable text while retaining paragraph/list scope boundaries."""
    if isinstance(node, str):
        try:
            return article_text(json.loads(node))
        except (ValueError, TypeError):
            return html.unescape(re.sub(r"<[^>]+>", " ", node))
    if isinstance(node, list):
        return "".join(article_text(child) for child in node)
    if not isinstance(node, dict):
        return ""
    result = str(node.get("text", "")) + article_text(node.get("child", []))
    if node.get("tag") in {"p", "li", "tr", "h1", "h2", "h3"}:
        result += "\n"
    return html.unescape(result)


def list_articles(get: Callable, catalog: int, page_size=50) -> tuple[list[dict], list[dict]]:
    articles, sources, seen = [], [], set()
    for page in range(1, 1001):
        url = CMS + "list/query?" + urlencode({"type": 1, "catalogId": catalog,
                                                "pageNo": page, "pageSize": page_size})
        body, source = get(url)
        sources.append(source)
        if source.get("error"):
            raise ValueError(f"CMS catalog {catalog} page {page}: {source['error']}")
        payload = json.loads(body)
        catalogs = payload.get("data", {}).get("catalogs", [])
        entry = next((item for item in catalogs if int(item["catalogId"]) == catalog), None)
        if payload.get("code") != "000000" or entry is None:
            raise ValueError("Malformed official CMS catalogue")
        batch = entry.get("articles", [])
        fresh = [item for item in batch if item["code"] not in seen]
        if not fresh and len(articles) < int(entry.get("total", 0)):
            raise ValueError("CMS pagination stopped before advertised total")
        articles.extend(fresh)
        seen.update(item["code"] for item in fresh)
        if len(articles) >= int(entry.get("total", 0)):
            return articles, sources
    raise ValueError("CMS pagination guard exceeded")


_DATE_TIME = re.compile(r"(20\d{2}[-/]\d{2}[-/]\d{2})\s+(?:at\s+)?(\d{1,2}:\d{2})(?:\s*(AM|PM))?\s*\(?(?:UTC)\)?", re.I)
_PAIR = re.compile(r"(?<![A-Z0-9])([A-Z0-9]+)\s*/\s*USDT(?![A-Z0-9])")


def extract_article_facts(article: Mapping, source: Mapping) -> dict:
    """Conservative automatic facts; ambiguous scope/date remains unresolved.

    One exact UTC effective time and pair-specific text must share the same
    market-scoped paragraph (or its immediately following list item).  A token
    listing is not automatically a listing of every quote market.
    """
    title = str(article.get("title", ""))
    text = article_text(article.get("body", ""))
    paragraphs = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    published = article.get("publishDate") or article.get("releaseDate")
    available = pd.Timestamp(int(published), unit="ms", tz="UTC").isoformat() if published else None
    result = {"title": title, "article_code": article.get("code"), "events": [],
              "classifications": [], "unresolved": [], "text": text}
    if not available:
        result["unresolved"].append("missing_publication_time")
        return result
    title_lower = title.lower()
    if "futures" in title_lower or "perpetual" in title_lower:
        return result
    spot_removal_anchors = [paragraph for paragraph in paragraphs
                            if re.search(r"delist and cease trading on all trading pairs|cease trading and delist", paragraph, re.I)
                            and not re.search(r"margin|loan|earn|convert|gift|pay", paragraph, re.I)
                            and len(set(_DATE_TIME.findall(paragraph))) == 1]
    for number, paragraph in enumerate(paragraphs):
        context = paragraph
        previous = paragraphs[number - 1] if number else ""
        lower = paragraph.lower()
        if _DATE_TIME.search(paragraph) and not re.search(r"open trading|cease trading|suspend.*borrow|remove|delist", lower):
            context = previous + " " + paragraph
        if (re.search(r"exact trading pairs (?:being )?removed", lower)
                and len(spot_removal_anchors) == 1 and not _DATE_TIME.search(paragraph)):
            context = spot_removal_anchors[0] + " " + paragraph
        scope = context.lower()
        pairs = sorted(set(_PAIR.findall(paragraph)))
        if not pairs:
            continue
        kind = None
        if re.search(r"suspend.{0,60}borrow|stop.{0,30}borrow", scope):
            kind = "borrow_suspended"
        elif "margin" in title_lower or ("margin" in scope and "spot" not in scope):
            if re.search(r"cease trading|remove|delist", scope):
                kind = "margin_delisted"
            elif re.search(r"add|list|open trading", scope):
                kind = "margin_eligible"
        elif re.search(r"cease trading|remove and|delist", scope):
            kind = "spot_delisted"
        elif re.search(r"open trading|commence trading|start trading", scope):
            kind = "spot_listed"
        if kind is None:
            continue
        times = _DATE_TIME.findall(context)
        unique = set(times)
        if len(unique) != 1:
            result["unresolved"].append({"reason": "ambiguous_or_missing_effective_time", "paragraph": paragraph})
            continue
        date, hm, meridian = unique.pop()
        effective = pd.to_datetime(f"{date.replace('/', '-')} {hm} {meridian}".strip(), utc=True)
        for base in pairs:
            result["events"].append({"symbol": base + "/USDT", "kind": kind,
                                      "effective_at": effective.isoformat(), "available_at": available,
                                      "retrieved_at": source.get("retrieved_at"),
                                      "source_url": source.get("url"),
                                      "source_sha256": source.get("body_sha256"),
                                      "source_status": "verified", "source_excerpt": context,
                                      "historical_version_availability": "retrospective_version_only"})
    # Classify only descriptive clauses naming the asset or directly under an
    # explicit "What is Name (TICKER)?" heading. Generic risk warnings and an
    # article's quote-currency description are not classification facts.
    descriptions = []
    for number, paragraph in enumerate(paragraphs):
        match = re.search(r"(?:^|\.\s)([A-Za-z][A-Za-z0-9 .-]{0,60})\s*\(([A-Z0-9]+)\)\s+(?:is|are)\s+(.+)", paragraph)
        if match:
            descriptions.append((match.group(2), match.group(3), paragraph))
        heading = re.fullmatch(r"What (?:is|Is) .{1,70}\(([A-Z0-9]+)\)\s*\??", paragraph)
        if heading and number + 1 < len(paragraphs):
            next_paragraph = paragraphs[number + 1]
            # The heading supplies the identity, the immediately following
            # substantive paragraph supplies the explicit economic description.
            if not re.match(r"risk|reminder|note|please", next_paragraph, re.I):
                descriptions.append((heading.group(1), next_paragraph, paragraph + " " + next_paragraph))
        if "launchpool" in paragraph.lower() or "launchpad" in paragraph.lower():
            launch = re.search(r"(?:Launchpool|Launchpad)\s*[-–:]\s*.{1,80}?\(([A-Z0-9]+)\),\s*(.+)", paragraph, re.I)
            if launch:
                descriptions.append((launch.group(1), launch.group(2), paragraph))
    if "leveraged token" in title_lower:
        for base in sorted(set(_PAIR.findall(text))):
            descriptions.append((base, "a leveraged token", title + " " + base + "/USDT"))
    seen_classifications = set()
    for base, description, excerpt in descriptions:
        description = description.lower()
        first_sentence = re.split(r"\.\s", description, maxsplit=1)[0]
        own_crypto_token = bool(re.search(r"(?:native|governance|utility) token", first_sentence))
        classification = None
        if re.search(r"tokenized (?:stock|securit)|bstocks", description):
            classification = "tokenized_security"
        elif (re.search(r"stable\s?coin|pegged.{0,30}(?:dollar|usd)|fiat.backed", first_sentence)
              and not own_crypto_token):
            classification = "stablecoin"
        elif "leveraged token" in description:
            classification = "leveraged"
        elif re.search(r"fiat currency|national currency", description):
            classification = "fiat"
        elif re.search(r"blockchain|decentralized|cryptocurrency|utility token|governance token|native token|digital asset|web ?3\b|cross.chain", description):
            classification = "crypto"
        if classification and (base, classification) not in seen_classifications:
            seen_classifications.add((base, classification))
            result["classifications"].append({"symbol": base + "/USDT", "classification": classification,
                                               "available_at": available, "source_status": "verified",
                                               "source_url": source.get("url"),
                                               "source_sha256": source.get("body_sha256"),
                                               "source_excerpt": excerpt,
                                               "retrieved_at": source.get("retrieved_at")})
    return result


def build_market_metadata(symbol: str, frame: pd.DataFrame, facts: list[dict]) -> dict:
    events, classes = [], []
    for fact in facts:
        events.extend(item for item in fact.get("events", []) if item["symbol"] == symbol)
        classes.extend(item for item in fact.get("classifications", []) if item["symbol"] == symbol)
    events.sort(key=lambda event: (event["available_at"], event["effective_at"], event["kind"]))
    classes.sort(key=lambda item: item["available_at"])
    listings = [item for item in events if item["kind"] == "spot_listed"]
    listing = min(listings, key=lambda item: item["effective_at"]) if listings else {}
    classification = classes[0] if classes else {}
    return {"symbol": symbol, "venue": "binance", "market_type": "spot",
            "listing_effective_at": listing.get("effective_at"),
            "listing_available_at": listing.get("available_at"),
            "listed_at": listing.get("effective_at"), "available_at": listing.get("available_at"),
            "listing_source": listing.get("source_url"),
            "classification": classification.get("classification", "unknown"),
            "asset_class": classification.get("classification", "unknown"),
            "classification_available_at": classification.get("available_at"),
            "classification_source": classification.get("source_url"),
            "source_status": "verified" if listing and classification else "unknown",
            "listing_status": "verified" if listing else "unknown",
            "classification_status": "verified" if classification else "unknown",
            "observed_first_bar": frame.index.min().isoformat() if len(frame) else None,
            "observed_last_bar": frame.index.max().isoformat() if len(frame) else None,
            "observed_bar_bounds_are_listing_facts": False,
            "events": events, "classification_events": classes,
            "historical_version_availability": "retrospective_version_only"}


class CausalUniverse:
    """Membership adapter that uses fact availability and never future tail bars."""

    def __init__(self, metadata: Mapping[str, dict]):
        self.metadata = metadata.get("symbols", metadata)

    def facts_at(self, symbol: str, timestamp) -> dict:
        point = utc(timestamp)
        item = self.metadata.get(symbol, {})
        events = [event for event in item.get("events", [])
                  if event.get("source_status") == "verified" and utc(event["available_at"]) <= point]
        classes = sorted((event for event in item.get("classification_events", [])
                          if event.get("source_status") == "verified" and utc(event["available_at"]) <= point),
                         key=lambda event: event["available_at"])
        return {"events": events, "classification": classes[-1]["classification"] if classes else "unknown"}

    def active_at(self, symbol: str, timestamp) -> bool:
        point = utc(timestamp)
        state = False
        for event in sorted(self.facts_at(symbol, point)["events"], key=lambda event: event["effective_at"]):
            if utc(event["effective_at"]) <= point and event["kind"] in {"spot_listed", "spot_delisted"}:
                state = event["kind"] == "spot_listed"
        return state

    def scheduled_exit(self, symbol: str, decision_at, next_bar_end) -> bool:
        """Known spot cessation before the next execution interval closes."""
        point, end = utc(decision_at), utc(next_bar_end)
        return any(event["kind"] == "spot_delisted" and point <= utc(event["effective_at"]) <= end
                   for event in self.facts_at(symbol, point)["events"])

    def apply(self, data_map: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        """Retain every actual historical bar; attach only contemporaneous exits."""
        result = {}
        for symbol, frame in data_map.items():
            copied = frame.copy()
            copied["scheduled_exit"] = [self.scheduled_exit(symbol, utc(stamp), utc(stamp) + DAY)
                                          for stamp in copied.index]
            result[symbol] = copied
        return result


def load_data_bundle(root: str | Path) -> tuple[dict[str, pd.DataFrame], dict, dict]:
    root = Path(root)
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    frames = {}
    for symbol, record in sorted(manifest.get("markets", {}).items()):
        if not record.get("csv_path"):
            continue
        path = root / record["csv_path"]
        if sha256(path.read_bytes()) != record["csv_sha256"]:
            raise ValueError(f"Frozen data digest mismatch: {symbol}")
        frame = pd.read_csv(path)
        for column in ("timestamp", "close_time", "available_at"):
            if column in frame:
                frame[column] = pd.to_datetime(frame[column], utc=True, format="mixed")
        frame = frame.set_index("timestamp")
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise ValueError(f"Duplicate or unsorted input: {symbol}")
        frames[symbol] = frame
    return frames, metadata, manifest
