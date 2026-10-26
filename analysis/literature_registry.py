"""Paper literature registry for the paper research programme (PR25-00).

Turns the Scholar-derived Excel navigation workbook into a hashed, read-only
registry and maps the eight primary sources (L01-L08) onto it. Presence in the
workbook is *computed* from titles, never assumed. Nothing here claims a paper
was read beyond the stated reading level or that a method was reproduced.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.foundation.reproducibility import sha256_bytes

SCHEMA = "paper-literature-registry/v1"
SHEET = "论文总表"
HEADER_ROW = 4
COLUMNS = {"编号": "excel_id", "大分类": "major", "小分类": "minor", "论文题目": "title", "作者": "authors",
           "年份": "year", "被引次数": "citations", "阅读参考": "reading_tier", "论文链接": "link", "PDF链接": "pdf_link"}
EXCEL_READING_LEVEL = "metadata_and_search_snippet_only"

# L01-L08 as listed in paper_research_roadmap_20261025.md section 3. DOI/version
# are unknown until PR25-00 evidence is collected, so they stay null.
PRIMARY_SOURCES = (
    ("L01", "The Probability of Backtest Overfitting", "https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf"),
    ("L02", "The Deflated Sharpe Ratio", "https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf"),
    ("L03", "A Reality Check for Data Snooping", "https://onlinelibrary.wiley.com/doi/10.1111/1468-0262.00152"),
    ("L04", "Optimal Execution of Portfolio Transactions", "https://doi.org/10.21314/JOR.2001.041"),
    ("L05", "Honey, I Shrunk the Sample Covariance Matrix", "https://ledoit.net/honey.pdf"),
    ("L06", "Common Risk Factors in Cryptocurrency", "https://www.nber.org/papers/w25882"),
    ("L07", "Multi-Period Trading via Convex Optimization", "https://web.stanford.edu/~boyd/papers/cvx_portfolio.html"),
    ("L08", "The Dataflow Model", "https://research.google/pubs/the-dataflow-model-a-practical-approach-to-balancing-correctness-latency-and-cost-in-massive-scale-unbounded-out-of-order-data-processing/"),
)
# Title text the workbook row must contain for a source to count as present.
# The workbook title for DSR is longer than the paper's short title.
EXPECTED_EXCEL_IDS = {"L02": 12, "L04": 252}

# (task, excel ids, role, limitation) - section 2.2 of the integration plan.
SUPPLEMENTS = (
    ("PR25-02/06", (13, 14, 15), "protocol fields and overfitting-correction background", "#15 needs full text before portability is judged"),
    ("PR25-07", (20, 21, 22), "design reference for null calibration and power", "2026 preprints with ~0 citations; design reference only"),
    ("PR25-02/18", (16, 17), "report fields and failure-item checklist", "2026 preprints; design reference only"),
    ("PR25-03/04", (72,), "motivation for strict mode and mature labels", "text-model setting; analogy only"),
    ("PR25-06/13", (18,), "scale of Sharpe inflation in crypto factor backtests", "0 citations; read full text before quoting numbers"),
    ("PR25-09", (267, 266, 243), "cost-model form and calibration cross-check", "equity setting; crypto transfer needs stated differences"),
    ("PR25-10/14", (249, 170, 246, 248), "no-trade band and cost-aware rebalancing baseline", "#248 is recent; conclusions need re-checking"),
    ("PR25-11/12", (157, 166, 165, 171, 172), "reserve covariance / risk-allocation candidates", "not in the first-round three arms; any arm needs its own registration"),
    ("PR25-16", (211, 94, 204, 207, 218), "expected behaviour of the trend strategy and ML selection layer", "equity/futures settings"),
    ("PR25-16", (41, 42, 44, 51, 244), "background and failure modes for the frozen RL comparison", "no new RL training"),
    ("PR25-05", (1, 3, 11), "background for membership and size definitions", "does not replace historical membership data"),
)
RESERVE_CANDIDATES = (157, 166, 165, 171, 172)
EXCLUDED_DIRECTIONS = (
    ("limit order book deep learning", (24, 40)), ("market making / Avellaneda-Stoikov", (232, 264)),
    ("LLM, news sentiment, satellite data", (53, 82)), ("graph neural networks", (83, 92)),
    ("options and deep hedging", (118, 127)), ("volatility / GARCH family", (104, 117)),
    ("online, universal portfolio and Kelly", (130, 156)), ("pairs trading and statistical arbitrage", (175, 186)),
    ("tail and systemic risk", (222, 231)),
)


def normalize_title(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _clean(value: Any) -> Any:
    return None if value is None or str(value).strip() == "" else str(value).strip()


def read_workbook_rows(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the master sheet; returns (rows, source) with a byte hash of the file."""
    import openpyxl

    path = Path(path)
    raw = path.read_bytes()
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if SHEET not in wb.sheetnames:
            raise ValueError(f"sheet {SHEET!r} not found")
        sheet = wb[SHEET]
        header = None
        rows = []
        for index, values in enumerate(sheet.iter_rows(values_only=True), 1):
            if index == HEADER_ROW:
                header = [str(v).strip() if v is not None else "" for v in values]
                missing = [name for name in COLUMNS if name not in header]
                if missing:
                    raise ValueError(f"missing columns: {missing}")
                continue
            if header is None or index < HEADER_ROW or not values or values[0] is None:
                continue
            row = {COLUMNS[name]: _clean(values[header.index(name)]) for name in COLUMNS}
            rows.append(row)
        note = wb["使用说明"]["A2"].value if "使用说明" in wb.sheetnames else None
    finally:
        wb.close()
    retrieved = re.search(r"检索日期[:：]\s*(\d{4}-\d{2}-\d{2})", str(note or ""))
    source = {"file_name": path.name, "sha256": sha256_bytes(raw), "sheet": SHEET, "row_count": len(rows),
              "retrieved_on": retrieved.group(1) if retrieved else None,
              "nature": "Scholar search snapshot; not a systematic review; categories are navigation labels"}
    return rows, source


def _typed(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["excel_id"] = int(out["excel_id"])
    for key in ("year", "citations"):
        out[key] = int(out[key]) if out.get(key) is not None else None
    out["has_pdf_link"] = out.pop("pdf_link") is not None
    out["reading_level"] = EXCEL_READING_LEVEL
    return out


def build_registry(rows: Sequence[Mapping[str, Any]], source: Mapping[str, Any], *, supplements=SUPPLEMENTS,
                   reserve_candidates=RESERVE_CANDIDATES) -> dict[str, Any]:
    papers = sorted((_typed(row) for row in rows), key=lambda p: p["excel_id"])
    by_title = [(normalize_title(p["title"]), p["excel_id"]) for p in papers]
    primary = []
    for source_id, title, url in PRIMARY_SOURCES:
        needle = normalize_title(title)
        matches = [excel_id for text, excel_id in by_title if needle in text]
        primary.append({"id": source_id, "title": title, "url": url, "doi_or_version": None,
                        "in_excel": bool(matches), "excel_ids": matches,
                        "reading_level": "source_located_full_text_not_obtained",
                        "assumptions_and_formulas_recorded": False})
    registry = {
        "schema": SCHEMA, "source_workbook": dict(source), "papers": papers, "primary_sources": primary,
        "supplements": [{"task": task, "excel_ids": list(ids), "role": role, "limitation": limit}
                        for task, ids, role, limit in supplements],
        "reserve_candidates": list(reserve_candidates),
        "excluded_directions": [{"direction": name, "excel_id_range": list(span)} for name, span in EXCLUDED_DIRECTIONS],
        "claims": {"full_excel_mapping_of_primary_sources": False,
                   "note": "primary sources keep their own origins; presence in the workbook is computed from titles"},
    }
    problems = validate_registry(registry)
    if problems:
        raise ValueError("; ".join(problems))
    return registry


def validate_registry(registry: Mapping[str, Any]) -> list[str]:
    """Structural and cross-reference checks; returns a list of problems (empty if valid)."""
    problems: list[str] = []
    if registry.get("schema") != SCHEMA:
        problems.append("unexpected schema")
    papers = registry.get("papers") or []
    ids = [p.get("excel_id") for p in papers]
    if len(set(ids)) != len(ids):
        problems.append("duplicate excel_id")
    if registry.get("source_workbook", {}).get("row_count") != len(papers):
        problems.append("row_count does not match papers")
    known = set(ids)
    titles = {p["excel_id"]: p for p in papers}
    for entry in registry.get("supplements", []):
        missing = [i for i in entry["excel_ids"] if i not in known]
        if missing:
            problems.append(f"supplement {entry['task']} references unknown ids {missing}")
    for excel_id in registry.get("reserve_candidates", []):
        if excel_id not in known:
            problems.append(f"reserve candidate {excel_id} unknown")
    primary = {p["id"]: p for p in registry.get("primary_sources", [])}
    if set(primary) != {item[0] for item in PRIMARY_SOURCES}:
        problems.append("primary source ids differ from L01-L08")
    for source_id, entry in primary.items():
        if entry["in_excel"] != bool(entry["excel_ids"]):
            problems.append(f"{source_id}: in_excel disagrees with excel_ids")
        for excel_id in entry["excel_ids"]:
            if excel_id not in known or normalize_title(entry["title"]) not in normalize_title(titles[excel_id]["title"]):
                problems.append(f"{source_id}: excel id {excel_id} does not match the title")
    return problems


def presence_differs_from_expected(registry: Mapping[str, Any]) -> list[str]:
    """Compare computed workbook presence with the recorded 2026-10-25 expectation (L02, L04 only)."""
    problems = []
    for entry in registry["primary_sources"]:
        expected = EXPECTED_EXCEL_IDS.get(entry["id"])
        if expected is None and entry["in_excel"] or expected is not None and entry["excel_ids"] != [expected]:
            problems.append(f"{entry['id']}: found {entry['excel_ids']}, expected {expected}")
    return problems
