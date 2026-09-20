"""Supervise reproducible backtests; never change parameters or live admission.

Every invocation owns an exclusive run directory and records failed steps too.
The immutable run receipts are authoritative; index.json/index.csv are atomic,
rebuildable projections. Smoke fixtures never count as real operational days.
Pruning defaults to a plan and preserves referenced, pinned or unowned paths.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OWNER = "quanttrading-automation/v1"
TASKS = ("nightly-data", "weekly-matrix", "weekly-smoke", "monthly-universe", "monthly-optimize", "quarterly-robust")


def utcnow():
    return datetime.now(timezone.utc)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def value_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def aware_time(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("receipt timestamps must be timezone aware")
    return stamp.astimezone(timezone.utc)


def atomic_text(path, text):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")


def is_link(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def managed_root(config):
    raw = Path(config["output_root"])
    raw = raw if raw.is_absolute() else ROOT / raw
    path = raw.resolve()
    boundary = (ROOT / "outputs").resolve()
    if path == boundary or not path.is_relative_to(boundary):
        raise ValueError("automation output must be a subdirectory of this repository's outputs")
    if any(is_link(part) for part in (raw, *raw.parents) if part.exists()):
        raise ValueError("automation output must not traverse a symbolic link or junction")
    return path


def initialize_root(path):
    marker = path / ".automation-owner.json"
    if path.exists() and not marker.exists() and any(path.iterdir()):
        raise ValueError("refusing to take ownership of a nonempty output directory")
    path.mkdir(parents=True, exist_ok=True)
    expected = {"schema_version": OWNER, "repository": str(ROOT.resolve())}
    if marker.exists():
        if read_json(marker) != expected:
            raise ValueError("automation directory belongs to another repository or schema")
    else:
        with marker.open("x", encoding="utf-8") as handle:
            json.dump(expected, handle)
    runs = path / "runs"
    if is_link(runs) or runs.resolve().parent != path.resolve():
        raise ValueError("owned run directory must not redirect outside its root")
    runs.mkdir(exist_ok=True)


@contextmanager
def exclusive_lock(path):
    """Never steal an existing lock, even if its timestamp looks old."""
    lock = path / ".automation.lock"
    token = uuid.uuid4().hex
    try:
        with lock.open("x", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "token": token, "created_at": utcnow().isoformat()}, handle)
    except FileExistsError as exc:
        raise RuntimeError("another automation owns the lock; inspect its process before removing a stale lock") from exc
    try:
        yield
    finally:
        if lock.exists() and read_json(lock).get("token") == token:
            lock.unlink()


def owned_runs(path):
    """Only direct children with matching ownership and receipt identity qualify."""
    rows = []
    for directory in sorted((path / "runs").iterdir()):
        if is_link(directory) or not directory.is_dir():
            continue
        try:
            marker = read_json(directory / ".automation-owner.json")
            row = read_json(directory / "run_log.json")
        except (OSError, ValueError):
            continue
        if (marker == {"schema_version": OWNER, "repository": str(ROOT.resolve()), "run_id": directory.name}
                and row.get("run_id") == directory.name and row.get("schema_version") == OWNER):
            rows.append(row)
    return rows


def rebuild_index(path):
    rows = sorted(owned_runs(path), key=lambda row: (row.get("started_at", ""), row["run_id"]))
    fields = ("run_id", "task", "status", "started_at", "finished_at", "synthetic", "production_evidence", "config_sha256", "source_sha256", "reason")
    projected = [{key: row.get(key) for key in fields} for row in rows]
    # Each file is replaced atomically. JSON is authoritative if a crash occurs
    # between projections; a later invocation regenerates both from receipts.
    save_json(path / "index.json", {"schema_version": OWNER, "generated_at": utcnow().isoformat(), "runs": projected})
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(projected)
    atomic_text(path / "index.csv", buffer.getvalue())
    return rows


def source_identity():
    from scripts.roadmap_baseline import source_manifest
    files = source_manifest(ROOT)
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "files": files}


def seal_run(directory, record):
    names = ["config.json", "config_input.json", "source_identity.json", "cache_identities.json"]
    seal = {"schema_version": "automation-run-seal/v1", "record_sha256": value_sha256(record),
            "artifacts": {name: sha256(directory / name) for name in names if (directory / name).is_file()}}
    save_json(directory / "evidence_seal.json", seal)
    record["evidence_seal_sha256"] = sha256(directory / "evidence_seal.json")


def verified_nightly(path, row, *, now=None):
    """Validate archived capture integrity; this does not prove exchange truth."""
    import pandas as pd
    from core.timeframes import timeframe_delta
    from core.universe import normalize_symbol
    now = now or utcnow()
    if (row.get("task") != "nightly-data" or row.get("status") != "passed"
            or row.get("synthetic") is not False or row.get("production_evidence") is not True):
        raise ValueError("not a successful real nightly refresh")
    directory = path / "runs" / row["run_id"]
    seal_path = directory / "evidence_seal.json"
    if sha256(seal_path) != row.get("evidence_seal_sha256"):
        raise ValueError("receipt seal missing or modified")
    seal = read_json(seal_path)
    unsigned = {key: value for key, value in row.items() if key != "evidence_seal_sha256"}
    if seal.get("schema_version") != "automation-run-seal/v1" or value_sha256(unsigned) != seal.get("record_sha256"):
        raise ValueError("run receipt was changed after sealing")
    required = {"config.json", "config_input.json", "source_identity.json", "cache_identities.json"}
    if not required.issubset(seal.get("artifacts", {})):
        raise ValueError("nightly evidence is incomplete")
    for name in required:
        if sha256(directory / name) != seal["artifacts"][name]:
            raise ValueError("sealed artifact changed: " + name)
    config = read_json(directory / "config_input.json")
    if config != read_json(directory / "config.json") or sha256(directory / "config_input.json") != row.get("config_sha256"):
        raise ValueError("configuration identity mismatch")
    source = read_json(directory / "source_identity.json")
    if value_sha256(source["files"]) != source.get("sha256") or source["sha256"] != row.get("source_sha256"):
        raise ValueError("source manifest identity mismatch")
    started, finished = aware_time(row["started_at"]), aware_time(row["finished_at"])
    if not started <= finished <= now:
        raise ValueError("invalid or future receipt times")
    caches = read_json(directory / "cache_identities.json")
    if set(caches) != set(config["timeframes"]):
        raise ValueError("cache snapshot timeframe coverage incomplete")
    for timeframe, manifest in caches.items():
        if not any(step.get("name") == "fetch_" + timeframe and step.get("status") == "passed"
                   and type(step.get("exit_code")) is int and step["exit_code"] == 0 for step in row.get("steps", [])):
            raise ValueError("successful actual capture step missing")
        captured = aware_time(manifest["generated_at"])
        if not started <= captured <= finished or captured.date() != finished.date():
            raise ValueError("cache capture does not belong to the claimed UTC run day")
        if (manifest.get("schema_version") != "binance-cache/v2" or manifest.get("provider") != "binance"
                or manifest.get("market_type") != "spot" or manifest.get("timeframe") != timeframe or manifest.get("failures")):
            raise ValueError("cache capture source or schema invalid")
        delta = pd.Timedelta(timeframe_delta(timeframe))
        boundary = pd.Timestamp(captured).floor(delta)
        for symbol in config["symbols"]:
            fact = manifest.get("symbols", {}).get(symbol, {})
            coverage = fact.get("coverage", {})
            if (fact.get("symbol") != normalize_symbol(symbol) or fact.get("provider") != "binance"
                    or fact.get("timeframe") != timeframe or fact.get("market_type") != "spot"
                    or coverage.get("status") != "complete" or coverage.get("end_policy") != "closed_bars_only"
                    or type(fact.get("rows")) is not int or fact["rows"] <= 0
                    or type(coverage.get("expected_rows")) is not int or coverage["expected_rows"] <= 0
                    or fact["rows"] < coverage["expected_rows"]
                    or not isinstance(fact.get("sha256"), str) or len(fact["sha256"]) != 64):
                raise ValueError("cache symbol facts are incomplete: " + symbol)
            if (pd.to_datetime(fact["first"], utc=True) > pd.to_datetime(config["start"], utc=True)
                    or pd.to_datetime(fact["requested_start"], utc=True) > pd.to_datetime(config["start"], utc=True)
                    or pd.to_datetime(coverage["effective_end_exclusive"], utc=True) != boundary
                    or pd.to_datetime(fact["last"], utc=True) + delta != boundary
                    or pd.to_datetime(fact["requested_end"], utc=True).date() < captured.date()):
                raise ValueError("cache coverage does not reach this capture's closed-bar boundary")
            expected_rows = int((boundary - pd.to_datetime(fact["requested_start"], utc=True)) / delta)
            if coverage["expected_rows"] != expected_rows:
                raise ValueError("cache observation count does not cover its declared interval")
            int(fact["sha256"], 16)
    return (row["source_sha256"], row["config_sha256"]), caches


def terminate_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=30)


class Runner:
    def __init__(self, directory, record, timeout_seconds):
        self.directory, self.record = directory, record
        self.deadline = time.monotonic() + timeout_seconds

    def step(self, name, arguments):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("overall task budget exhausted")
        started = time.monotonic()
        step = {"name": name, "command": [str(arg) for arg in arguments], "started_at": utcnow().isoformat(),
                "status": "running", "exit_code": None, "log": f"step_{len(self.record['steps']) + 1:02}_{name}.log"}
        self.record["steps"].append(step)
        save_json(self.directory / "run_log.json", self.record)
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        try:
            with (self.directory / step["log"]).open("w", encoding="utf-8") as output:
                process = subprocess.Popen(step["command"], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                           env={**os.environ, "PYTHONUTF8": "1"}, **kwargs)
                try:
                    step["exit_code"] = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    step["status"] = "timeout"
                    terminate_tree(process)
                    raise TimeoutError(f"{name}: child process exceeded task time budget") from None
                except BaseException:
                    terminate_tree(process)
                    raise
            step["status"] = "passed" if step["exit_code"] == 0 else "failed"
            if step["exit_code"] != 0:
                raise RuntimeError(f"{name}: child exited {step['exit_code']}; see {step['log']}")
        except OSError:
            if step["status"] != "timeout":
                step["status"] = "failed"
            raise
        finally:
            step["elapsed_seconds"] = round(time.monotonic() - started, 6)
            step["finished_at"] = utcnow().isoformat()
            save_json(self.directory / "run_log.json", self.record)


def python_command(script, *arguments):
    return [sys.executable, str(ROOT / script), *(str(arg) for arg in arguments)]


def nightly(runner, config):
    from scripts.run_backtest_matrix import verify_cache
    snapshots = {}
    end = utcnow().date().isoformat()
    for timeframe in config["timeframes"]:
        manifest = ROOT / "data" / "binance" / timeframe / "_manifest.json"
        previous = read_json(manifest) if manifest.exists() else {}
        started = utcnow()
        runner.step("fetch_" + timeframe, python_command("scripts/fetch_binance_data.py", "--symbols", *config["symbols"],
                    "--timeframe", timeframe, "--start", config["start"], "--end", end))
        current = verify_cache(config["symbols"], timeframe, generated_after=started, start=config["start"], end=end)
        for symbol in config["symbols"]:
            if current["symbols"][symbol]["rows"] < previous.get("symbols", {}).get(symbol, {}).get("rows", 0):
                raise ValueError("refreshed data lost previously recorded rows: " + symbol)
        snapshots[timeframe] = current
    save_json(runner.directory / "cache_identities.json", snapshots)
    return {"status": "pass", "scope": "verified_closed_bar_cache_refresh", "timeframes": list(snapshots)}


def audit_universe(directory, config):
    """Compare, never rewrite, PIT metadata against explicit independent facts.

    Evidence JSON: schema_version=universe-observation/v1, independent=true,
    source_id, observed_at (aware), records=[{symbol, listed_at, delisted_at,
    source_ref}]. delisted_at is explicit null for an observed active listing;
    missing fields are unknown, never silently converted to still-listed.
    """
    import pandas as pd
    from core.universe import normalize_symbol
    policy = config["universe"]
    universe = (ROOT / policy["file"]).resolve()
    shutil.copyfile(universe, directory / "universe_input.csv")
    result = {"schema_version": "automation-universe-diff/v1", "status": "insufficient_data",
              "scope": "independent_lifecycle_comparison_only_no_universe_mutation",
              "universe_sha256": sha256(universe), "differences": [], "live_admission": False}

    def canonical(rows, *, independent=False):
        output = {}
        for row in rows:
            if not all(name in row for name in ("symbol", "listed_at", "delisted_at")):
                raise ValueError("universe facts require explicit listing and delisting fields")
            symbol = normalize_symbol(row["symbol"])
            if symbol in output:
                raise ValueError("duplicate independent or configured universe symbol: " + symbol)
            listed = pd.to_datetime(row["listed_at"], utc=True)
            raw_end = row["delisted_at"]
            end = None if raw_end is None or raw_end == "" else pd.to_datetime(raw_end, utc=True)
            if pd.isna(listed) or (end is not None and (pd.isna(end) or end <= listed)):
                raise ValueError("invalid lifecycle interval for " + symbol)
            if independent and (not isinstance(row.get("source_ref"), str) or not row["source_ref"].strip()):
                raise ValueError("independent lifecycle observation needs a source_ref")
            output[symbol] = {"listed_at": listed.isoformat(), "delisted_at": end.isoformat() if end is not None else None}
        if not output:
            raise ValueError("universe observations must not be empty")
        return output

    try:
        with universe.open(encoding="utf-8-sig", newline="") as handle:
            configured = canonical(list(csv.DictReader(handle)))
        reference = policy.get("independent_evidence")
        if not reference or not (ROOT / reference).is_file():
            result["reason"] = "independent listing/delisting observations not supplied"
        else:
            evidence_path = (ROOT / reference).resolve()
            evidence = read_json(evidence_path)
            if (evidence.get("schema_version") != "universe-observation/v1" or evidence.get("independent") is not True
                    or not isinstance(evidence.get("source_id"), str) or not evidence["source_id"].strip()):
                raise ValueError("universe evidence must name an independent source and supported schema")
            observed = datetime.fromisoformat(evidence["observed_at"])
            if observed.tzinfo is None or observed > utcnow():
                raise ValueError("universe observation needs a timezone-aware nonfuture timestamp")
            observed = observed.astimezone(timezone.utc)
            independently_observed = canonical(evidence["records"], independent=True)
            shutil.copyfile(evidence_path, directory / "universe_independent_evidence.json")
            result["independent_evidence_sha256"] = sha256(evidence_path)
            result["source_id"] = evidence["source_id"]
            result["observed_at"] = observed.isoformat()
            for symbol in sorted(set(configured) | set(independently_observed)):
                if configured.get(symbol) != independently_observed.get(symbol):
                    result["differences"].append({"symbol": symbol, "configured": configured.get(symbol),
                                                  "independent": independently_observed.get(symbol)})
            if utcnow() - observed > timedelta(days=policy["maximum_observation_age_days"]):
                result["reason"] = "independent universe observations are stale"
            elif set(configured) - set(independently_observed):
                result["reason"] = "independent observation coverage is incomplete"
            else:
                result["status"] = "fail" if result["differences"] else "pass"
                result["reason"] = "lifecycle differences require human review" if result["differences"] else None
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result.update(status="fail", reason=str(exc))
    save_json(directory / "universe_diff.json", result)
    return result


def execute_task(task, runner, config, *, synthetic=False, skip_fetch=False):
    from scripts.evaluate_matrix import evaluate_matrix, evaluate_report
    if task == "nightly-data":
        if synthetic:
            raise ValueError("nightly-data requires real provider facts; synthetic mode is not supported")
        return nightly(runner, config)
    if task == "monthly-universe":
        if synthetic:
            raise ValueError("monthly-universe requires independent lifecycle observations")
        return audit_universe(runner.directory, config)
    if task == "weekly-smoke":
        report = runner.directory / "report"
        runner.step("smoke", python_command("main.py", "--source", "synthetic", "--symbols", "BTC-USDT", "ETH-USDT",
                    "--start", "2020-01-01", "--end", "2020-06-28", "--timeframe", "1d", "--seed", "42",
                    "--capital", "100000", "--report-profile", "full", "--disable-routing-log", "--output-dir", report))
        verdict = evaluate_report(report, maximum_drawdown=config["maximum_drawdown"])
        save_json(runner.directory / "verdict.json", verdict)
        runner.step("replay", python_command("main.py", "--replay-manifest", report / "run_manifest.json"))
        return verdict
    if task == "weekly-matrix":
        if synthetic:
            raise ValueError("use weekly-smoke for isolated synthetic runs")
        if skip_fetch:
            from scripts.run_backtest_matrix import verify_cache
            latest = []
            for row in owned_runs(runner.directory.parent.parent):
                try:
                    identity, caches = verified_nightly(runner.directory.parent.parent, row)
                    if (identity == (runner.record["source_sha256"], runner.record["config_sha256"])
                            and aware_time(row["finished_at"]) >= utcnow() - timedelta(hours=24)):
                        latest.append((row, caches))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            if not latest:
                raise ValueError("--skip-fetch requires sealed same-source/config nightly evidence within 24 hours")
            chosen, expected_caches = max(latest, key=lambda item: item[0]["finished_at"])
            for timeframe in config["timeframes"]:
                actual = verify_cache(config["symbols"], timeframe, generated_after=utcnow() - timedelta(hours=24),
                                      start=config["start"], end=utcnow().date().isoformat())
                if value_sha256(actual) != value_sha256(expected_caches[timeframe]):
                    raise ValueError("current cache identity differs from the sealed nightly snapshot")
            runner.record["referenced_runs"] = [chosen["run_id"]]
            save_json(runner.directory / "cache_identities.json", expected_caches)
        else:
            nightly(runner, config)
        matrix = runner.directory / "matrix"
        runner.step("matrix", python_command("scripts/run_backtest_matrix.py", "--symbols", *config["symbols"],
                    "--timeframes", *config["timeframes"], "--windows", *config["windows"], "--start", config["start"],
                    "--capital", config["capital"], "--seed", config["seed"], "--skip-fetch", "--report-profile", "full", "--output-dir", matrix))
        verdict = evaluate_matrix(matrix, maximum_drawdown=config["maximum_drawdown"])
        save_json(runner.directory / "verdict.json", verdict)
        for number, row in enumerate(verdict["cells"]):
            if row["status"] != "pass":
                raise ValueError("matrix verdict rejected: " + str(row.get("reason")))
            report = Path(row["report_dir"]).resolve()
            if not report.is_relative_to(matrix.resolve()):
                raise ValueError("matrix replay path escaped owned run")
            runner.step(f"replay_{number:04}", python_command("main.py", "--replay-manifest", report / "run_manifest.json"))
        return verdict
    research = config["research"]
    protocol = (ROOT / research["protocol"]).resolve()
    if not protocol.is_file():
        raise ValueError("frozen research protocol is unavailable: " + str(protocol))
    shutil.copyfile(protocol, runner.directory / "research_protocol.json")
    research_end = research["end"]
    if synthetic:
        research_end = min(research_end, (datetime.strptime(research["start"], "%Y-%m-%d") + timedelta(days=364)).date().isoformat())
    arguments = ["--task", task, "--protocol", protocol, "--data-dir", (ROOT / research["data_dir"]).resolve(),
                 "--start", research["start"], "--end", research_end,
                 "--symbols", *config["symbols"],
                 "--output", runner.directory / "research"]
    if synthetic:
        arguments.append("--synthetic")
    runner.step("research", python_command("scripts/run_research_automation.py", *arguments))
    report = read_json(runner.directory / "research" / "research_report.json")
    return {"status": "pass" if report.get("engineering_status") == "pass" else "fail",
            "scope": "research_workflow_completed_not_strategy_admission",
            "research_status": report.get("research_status"), "protocol_sha256": sha256(protocol),
            "report_sha256": sha256(runner.directory / "research" / "research_report.json")}


def run_task(task, config, config_path, path, *, synthetic=False, skip_fetch=False, pinned=False):
    run_id = utcnow().strftime("%Y%m%dT%H%M%S_%fZ") + "_" + task + "_" + uuid.uuid4().hex[:8]
    directory = path / "runs" / run_id
    directory.mkdir(exist_ok=False)
    save_json(directory / ".automation-owner.json", {"schema_version": OWNER, "repository": str(ROOT.resolve()), "run_id": run_id})
    record = {"schema_version": OWNER, "run_id": run_id, "task": task, "status": "running", "steps": [],
              "started_at": utcnow().isoformat(), "finished_at": None, "synthetic": synthetic or task == "weekly-smoke",
              "production_evidence": False, "pinned": pinned, "live_admission": False, "reason": None,
              "config_sha256": sha256(config_path)}
    save_json(directory / "config.json", config)
    shutil.copyfile(config_path, directory / "config_input.json")
    save_json(directory / "run_log.json", record)
    identity = None
    try:
        identity = source_identity()
        save_json(directory / "source_identity.json", identity)
        record["source_sha256"] = identity["sha256"]
        runner = Runner(directory, record, config["timeout_seconds"])
        verdict = execute_task(task, runner, config, synthetic=synthetic, skip_fetch=skip_fetch)
        record["verdict"] = verdict
        if verdict["status"] not in {"pass", "insufficient_data"}:
            raise ValueError("engineering verdict failed: " + str(verdict.get("reason")))
        if sha256(config_path) != record["config_sha256"] or source_identity() != identity:
            raise ValueError("source or configuration changed during the run; evidence is not sealed")
        record["status"] = "passed" if verdict["status"] == "pass" else "insufficient_data"
        record["reason"] = verdict.get("reason")
        record["production_evidence"] = record["status"] == "passed" and not record["synthetic"]
    except (Exception, KeyboardInterrupt) as exc:
        record["status"] = "timeout" if isinstance(exc, TimeoutError) else "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        record["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        record["finished_at"] = utcnow().isoformat()
        seal_run(directory, record)
        save_json(directory / "run_log.json", record)
        if record["status"] != "passed":
            notice = (f"# Automation failure\n\nRun: {run_id}\nTask: {task}\nStatus: {record['status']}\n"
                      f"Reason: {record['reason']}\n\nReceipt: {directory / 'run_log.json'}\n"
                      "Local notice only; no external delivery or operator acknowledgement is claimed.\n")
            atomic_text(directory / "ALERT.md", notice)
            atomic_text(path / "ALERT.md", notice)
        rebuild_index(path)
    return record


def operational_status(path, *, now=None):
    now = now or utcnow()
    rows = owned_runs(path)
    daily = {}
    for row in rows:
        if row.get("task") != "nightly-data" or row.get("synthetic") is not False or not row.get("finished_at"):
            continue
        ended = datetime.fromisoformat(row["finished_at"]).astimezone(timezone.utc)
        if ended > now:
            continue
        day = ended.date()
        if day not in daily or ended > daily[day][0]:
            daily[day] = (ended, row)
    current = now.date() if now.date() in daily else now.date() - timedelta(days=1)
    count = 0
    fixed_identity, stopped_reason = None, None
    while current in daily:
        try:
            identity, _ = verified_nightly(path, daily[current][1], now=now)
            if fixed_identity is not None and identity != fixed_identity:
                stopped_reason = "source_or_configuration_changed"
                break
            fixed_identity = identity
            count += 1
            current -= timedelta(days=1)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            stopped_reason = "unverified_daily_evidence: " + str(exc)
            break
    return {"schema_version": OWNER, "generated_at": now.isoformat(), "run_count": len(rows),
            "consecutive_real_daily_refresh_days": count, "required_days": 14,
            "daily_data_observation": "pass" if count >= 14 else "pending_evidence",
            "fixed_identity": fixed_identity, "streak_stopped_reason": stopped_reason,
            "scope": "sealed latest daily refresh per UTC date with one source/config identity; synthetic and future excluded",
            "limitation": "local hash integrity cannot substitute for independently authenticated exchange evidence",
            "automation_acceptance": "requires_weekly_runs_and_real_alert_acknowledgement_evidence",
            "live_admission": False, "latest_runs": sorted(rows, key=lambda row: row["started_at"])[-10:]}


def referenced_runs(run_ids, owned_directories=()):
    """Conservatively preserve any run mentioned in active or sealed documents."""
    protected = set()
    files = list(ROOT.glob("*.md"))
    for directory in (ROOT / "docs", ROOT / "reports", ROOT / "config"):
        if directory.exists():
            files.extend(path for path in directory.rglob("*") if path.suffix.lower() in {".json", ".md", ".csv", ".txt"} and path.is_file())
    owned_files = []
    for directory in owned_directories:
        owned_files.extend((path, directory.name) for path in directory.rglob("*")
                           if path.is_file() and not is_link(path) and path.suffix.lower() in {".json", ".md", ".log", ".txt"})
    for path, owner_id in [*((path, None) for path in files), *owned_files]:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            # Overlap preserves a run identity split across chunk boundaries.
            tail = ""
            for chunk in iter(lambda: handle.read(1024 * 1024), ""):
                text = tail + chunk
                protected.update(run_id for run_id in run_ids - protected if run_id != owner_id and run_id in text)
                tail = text[-256:]
    return protected


def prune(path, config, *, apply=False, now=None):
    now = now or utcnow()
    rows = owned_runs(path)
    protected = referenced_runs({row["run_id"] for row in rows}, [path / "runs" / row["run_id"] for row in rows]) | set(config["pinned_runs"])
    # Preserve receipts consumed by another owned run, including historical
    # nightly inputs to a matrix even when no document links them yet.
    protected.update(run_id for row in rows for run_id in row.get("referenced_runs", []))
    candidates, kept = [], []
    cutoff = now - timedelta(days=config["retain_days"])
    for row in rows:
        run_id = row["run_id"]
        directory = path / "runs" / run_id
        reason = None
        if row.get("pinned") or (directory / "PINNED").exists() or run_id in protected:
            reason = "pinned_or_referenced"
        elif not row.get("finished_at") or row.get("status") == "running":
            reason = "not_completed"
        elif datetime.fromisoformat(row["finished_at"]) >= cutoff:
            reason = "retained_age"
        elif is_link(directory) or any(is_link(item) for item in directory.rglob("*")):
            reason = "contains_link_or_junction"
        if reason:
            kept.append({"run_id": run_id, "reason": reason})
        else:
            candidates.append(run_id)
    if apply:
        for run_id in candidates:
            target = (path / "runs" / run_id).resolve()
            if target.parent != (path / "runs").resolve() or target == path.resolve():
                raise ValueError("prune target escaped owned run root")
            shutil.rmtree(target)
        rebuild_index(path)
    result = {"schema_version": OWNER, "generated_at": now.isoformat(), "dry_run": not apply,
              "candidates": candidates, "kept": kept, "unowned_paths": "always_preserved"}
    save_json(path / ("prune_applied.json" if apply else "prune_plan.json"), result)
    return result


def load_config(path):
    config = read_json(path)
    if config.get("schema_version") != "backtest-automation-config/v1":
        raise ValueError("unknown automation configuration schema")
    for field in ("timeout_seconds", "capital", "maximum_drawdown", "retain_days"):
        value = config.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(field + " must be finite and positive")
    if config["maximum_drawdown"] > 1 or config["retain_days"] < 14:
        raise ValueError("drawdown is a ratio <=1; retention must preserve at least 14 days")
    if (not isinstance(config.get("seed"), int) or isinstance(config["seed"], bool)
            or not isinstance(config.get("pinned_runs"), list)):
        raise ValueError("integer seed and pinned_runs list required")
    for field in ("symbols", "timeframes", "windows"):
        values = config.get(field)
        if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value or value.startswith("-") for value in values):
            raise ValueError(field + " must contain nonempty arguments")
        if len(set(values)) != len(values):
            raise ValueError(field + " must contain unique values")
    if any(value not in {"1d", "4h", "1h"} for value in config["timeframes"]):
        raise ValueError("automation supports 1d, 4h and 1h timeframes")
    datetime.strptime(config["start"], "%Y-%m-%d")
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=(*TASKS, "status", "prune"))
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "automation.json")
    parser.add_argument("--synthetic", action="store_true", help="research fixture only; smoke is always synthetic")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--pin", action="store_true", help="protect this run from retention cleanup")
    parser.add_argument("--apply", action="store_true", help="apply an owned-run prune plan; default is dry-run")
    args = parser.parse_args(argv)
    if args.apply and args.task != "prune":
        parser.error("--apply is valid only for prune")
    if args.skip_fetch and args.task != "weekly-matrix":
        parser.error("--skip-fetch is valid only for weekly-matrix")
    try:
        config = load_config(args.config)
        path = managed_root(config)
        initialize_root(path)
        if args.task == "status":
            result = operational_status(path)
        else:
            with exclusive_lock(path):
                if args.task == "prune":
                    result = prune(path, config, apply=args.apply)
                else:
                    result = run_task(args.task, config, args.config, path, synthetic=args.synthetic,
                                      skip_fetch=args.skip_fetch, pinned=args.pin)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0 if args.task in {"status", "prune"} or result["status"] == "passed" else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc), "live_admission": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
