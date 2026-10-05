"""调度网页离线回测与研究任务，计算在独立子进程中执行。

普通回测和注册的研究任务共用一个执行槽。内存中的任务与日志有数量上限，
持久实验档案独立保存；服务重启会把未完成记录标为 interrupted，而不会续跑。
"""

from __future__ import annotations

import copy
import csv
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import RLock, Thread
from typing import Any

from dashboard.visual_data import list_markets
from dashboard.market_analysis import validate_data_selection
from dashboard.experiment_store import ExperimentStore
from dashboard.strategy_presets import (strategy_catalog, validate_strategy, load_base_config,
    configuration_for_strategy, config_diff, write_config_snapshot)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}
LIMITS = {
    "max_symbols": 4, "min_days": 30, "max_days": 1096,
    "min_capital": 100, "max_capital": 1_000_000_000,
    "max_slippage_bps": 100, "max_jobs": 30,
}
_FIELDS = {"source", "symbols", "start", "end", "capital", "slippage_bps", "seed"}
SYNTHETIC_SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"]
ORIGINAL_PRESET = "original_100k"
ORIGINAL_PERIOD = ("2020-01-01", "2026-09-18")


class JobConflict(RuntimeError):
    """A job cannot be admitted while the service or another job is busy."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    if not minimum <= value <= maximum or not math.isfinite(value):
        raise ValueError(f"{field} must be between {minimum:g} and {maximum:g}")
    return float(value)


class BacktestJobs:
    """One active subprocess, bounded history/logs, and explicit lifecycle cleanup."""

    def __init__(self, data_dir: Path, reports_dir: Path, *, enabled: bool = True,
                 timeout: float = 1200) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.reports_dir = Path(reports_dir).resolve()
        self.enabled = enabled
        self.timeout = timeout
        self.csrf_token = secrets.token_urlsafe(32)
        self._lock = RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active: str | None = None
        self._process: subprocess.Popen | None = None
        self._worker_thread: Thread | None = None
        self._closed = False
        self._range_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
        self._policy_cache: tuple[tuple[int, int], int] | None = None
        self.store = ExperimentStore(self.reports_dir / ".dashboard" / "experiments.sqlite3")
        self.store.interrupt_active()
        for job in self.store.recent_jobs(LIMITS["max_jobs"]):
            job["logs"] = deque(job.get("logs", []), maxlen=160)
            self._jobs[job["id"]] = job
        self._task_types: dict[str, tuple[Any, str]] = {}

    @staticmethod
    def strategy_catalog() -> dict[str, Any]:
        return strategy_catalog()

    def preview_strategy(self, strategy: Any) -> dict[str, Any]:
        selection = validate_strategy(strategy)
        base, digest = load_base_config(PROJECT_ROOT / "config" / "params.yaml")
        proposed = configuration_for_strategy(base, selection, experiment_id="web-preview")
        return {"strategy": selection, "config_diff": config_diff(base, proposed),
                "base_config_sha256": digest}

    def save_preset(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) - {"id", "name", "description", "strategy"} or not {"name", "strategy"}.issubset(payload):
            raise ValueError("Expected preset name, strategy and optional id/description")
        selection = validate_strategy(payload["strategy"])
        return self.store.save_preset(identifier=payload["id"] if "id" in payload else "preset_" + secrets.token_hex(8),
            name=payload["name"], description=payload.get("description", ""), strategy=selection)

    def list_presets(self) -> list[dict[str, Any]]:
        return self.store.list_presets()

    def delete_preset(self, identifier: str) -> None:
        self.store.delete_preset(identifier)

    def history(self, **filters) -> dict[str, Any]:
        result = self.store.search(**filters)
        for item in result["items"]:
            self._selector_parameter(item)
        return result

    @staticmethod
    def _selector_parameter(job: dict[str, Any]) -> None:
        """Old ordinary backtests used the original strategy without a selector."""
        if job.get("kind", "backtest") == "backtest":
            parameters = job.get("parameters")
            if not isinstance(parameters, dict):
                parameters = job["parameters"] = {}
            parameters["use_selector"] = parameters.get("use_selector") is True

    def register_task_type(self, kind: str, command_builder: Any, success_file: str = "result.json") -> None:
        if kind in {"backtest", ""} or not isinstance(kind, str) or not kind.isidentifier() or len(kind) > 30:
            raise ValueError("Invalid task kind")
        if not callable(command_builder) or not isinstance(success_file, str) or Path(success_file).name != success_file or success_file in {".", ".."}:
            raise ValueError("Invalid internal task adapter")
        with self._lock:
            if kind in self._task_types:
                raise ValueError("Task kind already registered")
            self._task_types[kind] = (command_builder, success_file)

    def _required_symbols(self) -> int:
        """Project the configured health minimum without loading the trading engine."""
        import yaml

        path = PROJECT_ROOT / "config" / "params.yaml"
        info = path.stat()
        key = (info.st_mtime_ns, info.st_size)
        with self._lock:
            if self._policy_cache and self._policy_cache[0] == key:
                return self._policy_cache[1]
        try:
            configuration = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError("Invalid backtest YAML configuration") from exc
        if not isinstance(configuration, dict):
            raise ValueError("Invalid backtest configuration")
        baseline = configuration.get("strategy_health") or {}
        research = configuration.get("research") or {}
        routing = configuration.get("routing") or {}
        if not all(isinstance(section, dict) for section in (baseline, research, routing)):
            raise ValueError("Invalid strategy health configuration")
        overrides = research.get("strategy_health_overrides") or {}
        if not isinstance(overrides, dict) or any(not isinstance(name, str) for name in routing.values()):
            raise ValueError("Invalid per-strategy health configuration")
        required = 1
        for name in set(routing.values()) - {"Cash", ""}:
            override = overrides.get(name, {})
            if not isinstance(override, dict):
                raise ValueError("Invalid per-strategy health configuration")
            policy = {**baseline, **override}
            if policy.get("enabled", True):
                count = policy.get("probation_min_distinct_symbols", 1)
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise ValueError("Invalid strategy health symbol requirement")
                required = max(required, count)
        with self._lock:
            self._policy_cache = (key, required)
        return required

    def _cache_range(self, symbol: str) -> dict[str, Any]:
        path = self.data_dir / (symbol.replace("/", "_") + ".csv")
        if path.is_symlink() or not path.is_file():
            return {}
        info = path.stat()
        key = (info.st_mtime_ns, info.st_size)
        with self._lock:
            cached = self._range_cache.get(symbol)
            if cached and cached[0] == key:
                return dict(cached[1])
        if info.st_size > 32 * 1024 * 1024:
            raise ValueError("Daily cache exceeds the web backtest size limit")
        first, last, rows = None, None, 0
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not {"timestamp", "open", "high", "low", "close", "volume"}.issubset(reader.fieldnames or []):
                raise ValueError("Invalid local OHLCV cache schema")
            for index, row in enumerate(reader):
                if index >= 100_000:
                    raise ValueError("Daily cache exceeds the web backtest row limit")
                try:
                    moment = datetime.fromisoformat(row.get("timestamp", "")).date()
                except (ValueError, TypeError):
                    continue
                first = min(first, moment) if first else moment
                last = max(last, moment) if last else moment
                rows += 1
        result = {"start": first.isoformat(), "end": last.isoformat(), "rows": rows} if first else {}
        with self._lock:
            self._range_cache[symbol] = (key, result)
        return dict(result)

    def options(self) -> dict[str, Any]:
        symbols = list_markets(self.data_dir)
        required = self._required_symbols()
        use_local = len(symbols) >= required
        chosen = (symbols if use_local else SYNTHETIC_SYMBOLS)[:max(3, required)]
        ranges = [self._cache_range(symbol) for symbol in chosen] if use_local else []
        cache_range = {"start": max(item["start"] for item in ranges),
                       "end": min(item["end"] for item in ranges)} if ranges and all(ranges) else {}
        end = date.fromisoformat(cache_range["end"]) if cache_range else datetime.now(timezone.utc).date() - timedelta(days=1)
        start = end - timedelta(days=365)
        if cache_range:
            start = max(start, date.fromisoformat(cache_range["start"]))
        return {
            "enabled": self.enabled and not self._closed and required <= LIMITS["max_symbols"],
            "csrf_token": self.csrf_token,
            "sources": [{"id": "local", "label": "本地历史缓存"},
                        {"id": "synthetic", "label": "合成情景（非真实行情）"}],
            "symbols": symbols,
            "synthetic_symbols": list(SYNTHETIC_SYMBOLS),
            "required_symbols": required,
            "defaults": {"source": "local" if use_local else "synthetic", "symbols": chosen,
                         "start": start.isoformat(), "end": end.isoformat(),
                         "capital": 10000, "slippage_bps": 5, "seed": 42, "use_selector": False},
            "limits": {**LIMITS, "timeout_seconds": self.timeout},
            "cache_range": cache_range,
            "strategy": "当前 config/params.yaml 的策略路由与风控配置",
            "timeframe": "1d",
            "durable_history": True,
        }

    def _validate(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and "preset" in payload:
            if set(payload) != {"preset", "use_selector"} or payload["preset"] != ORIGINAL_PRESET:
                raise ValueError("Expected original_100k preset and use_selector only")
            if type(payload["use_selector"]) is not bool:
                raise ValueError("use_selector must be a boolean")
            data, smart = self._original_registration()
            return {"preset": ORIGINAL_PRESET, "use_selector": payload["use_selector"],
                    "source": "local", "symbols": list(data["symbols"]),
                    "start": ORIGINAL_PERIOD[0], "end": ORIGINAL_PERIOD[1], "capital": 100000.0,
                    "slippage_bps": smart["arms"]["smart"]["parameters"]["execution"]["slippage_bps"],
                    "seed": 42, "strategy": validate_strategy(None)}
        if not isinstance(payload, dict) or not _FIELDS.issubset(payload) or set(payload) - _FIELDS - {"strategy", "use_selector"}:
            raise ValueError("Expected source, symbols, start, end, capital, slippage_bps, seed and optional strategy/use_selector")
        use_selector = payload.get("use_selector", False)
        if type(use_selector) is not bool:
            raise ValueError("use_selector must be a boolean")
        selection = validate_strategy(payload.get("strategy"))
        source = payload["source"]
        if source not in ("local", "synthetic"):
            raise ValueError("source must be local or synthetic")
        symbols = payload["symbols"]
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= LIMITS["max_symbols"]:
            raise ValueError("Select between 1 and 4 symbols")
        allowed = set(list_markets(self.data_dir)) if source == "local" else set(SYNTHETIC_SYMBOLS)
        if any(not isinstance(symbol, str) or symbol not in allowed for symbol in symbols):
            raise ValueError("Unknown or invalid symbol")
        if len(set(symbols)) != len(symbols):
            raise ValueError("Duplicate symbols are not allowed")
        required = self._required_symbols()
        if len(symbols) < required:
            raise ValueError(f"Current strategy health policy requires at least {required} distinct symbols")
        if source == "local" and not set(symbols).issubset(list_markets(self.data_dir)):
            raise ValueError("Selected symbols have no local cache")
        try:
            start, end = (date.fromisoformat(payload[field]) for field in ("start", "end"))
        except (ValueError, TypeError):
            raise ValueError("start and end must be YYYY-MM-DD dates") from None
        if start.isoformat() != payload["start"] or end.isoformat() != payload["end"]:
            raise ValueError("start and end must be YYYY-MM-DD dates")
        if not LIMITS["min_days"] <= (end - start).days <= LIMITS["max_days"]:
            raise ValueError("Date range must span between 30 and 1096 days")
        if source == "local":
            for symbol in symbols:
                available = self._cache_range(symbol)
                if not available or available["start"] > start.isoformat() or available["end"] < end.isoformat():
                    raise ValueError(f"{symbol} local cache does not cover the selected dates")
            validate_data_selection(self.data_dir, symbols, start.isoformat(), end.isoformat())
        capital = _number(payload["capital"], "capital", LIMITS["min_capital"], LIMITS["max_capital"])
        slippage = _number(payload["slippage_bps"], "slippage_bps", 0, LIMITS["max_slippage_bps"])
        seed = payload["seed"]
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
            raise ValueError("seed must be an integer between 0 and 4294967295")
        result = {"source": source, "symbols": list(symbols), "start": start.isoformat(),
                  "end": end.isoformat(), "capital": capital, "slippage_bps": slippage, "seed": seed,
                  "use_selector": use_selector}
        if "strategy" in payload:
            result["strategy"] = selection
        return result

    @staticmethod
    def _original_registration() -> tuple[dict[str, Any], dict[str, Any]]:
        """Read only the fixed preset metadata; the runner verifies every input hash."""
        try:
            metadata = []
            for relative in ("reports/multicoin_100k_20261004/registration.json",
                             "reports/smart_capital_100k_20261004/registration.json"):
                with (PROJECT_ROOT / relative).open("rb") as handle:
                    raw = handle.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("Original registration metadata exceeds size limit")
                metadata.append(json.loads(raw))
            data, smart = metadata
            symbols = data["symbols"]
            arm = smart["arms"]["smart"]
            if (not isinstance(symbols, list) or len(symbols) != 60
                    or any(not isinstance(symbol, str) for symbol in symbols)
                    or len(set(symbols)) != 60
                    or set(symbols) != set(data["engine_frame_hashes"])
                    or data["engine_frame_hashes"] != smart["engine_frame_hashes"]
                    or data["input_files"] != smart["input_files"]
                    or (data["start"], data["end"]) != ORIGINAL_PERIOD
                    or (smart["start"], smart["end"]) != ORIGINAL_PERIOD
                    or data["initial_capital"] != 100000
                    or smart["capital"] != 100000
                    or arm["engine_options"]["initial_capital"] != 100000
                    or not isinstance(arm["parameters"], dict)
                    or arm["engine_options"]["capital_allocation"]["enabled"] is not True):
                raise ValueError("Original preset registration differs from its fixed account")
            _number(arm["parameters"]["execution"]["slippage_bps"], "registered slippage_bps", 0, 100)
            return data, smart
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError("Original 100k backtest preset metadata is unavailable or invalid") from exc

    def _public(self, job: dict[str, Any]) -> dict[str, Any]:
        result = {key: copy.deepcopy(value) for key, value in job.items() if not key.startswith("_") and key != "logs"}
        self._selector_parameter(result)
        result["logs"] = list(job["logs"])
        if "_created_clock" in job:
            result["elapsed_seconds"] = round((job.get("_finished_clock") or time.monotonic()) - job["_created_clock"], 1)
        else:
            result.setdefault("elapsed_seconds", 0)
        return result

    def _persist(self, job: dict[str, Any], *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - job.get("_persisted_clock", 0) >= 1.0:
            self.store.save_job(self._public(job))
            job["_persisted_clock"] = now

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public(job) for job in reversed(list(self._jobs.values()))]

    def submit(self, payload: Any) -> dict[str, Any]:
        parameters = self._validate(payload)
        return self._submit("backtest", parameters)

    def submit_task(self, kind: str, validated_parameters: dict[str, Any]) -> dict[str, Any]:
        """Internal adapter API: the registering service validates its own schema."""
        if kind not in self._task_types:
            raise ValueError("Unknown research task kind")
        if not isinstance(validated_parameters, dict):
            raise ValueError("Task parameters must be an object")
        encoded = json.dumps(validated_parameters, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 32 * 1024:
            raise ValueError("Task parameters exceed size limit")
        validate_strategy(validated_parameters.get("strategy"))
        return self._submit(kind, copy.deepcopy(validated_parameters))

    def _submit(self, kind: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """在同一把锁内预留执行槽、冻结配置并登记任务，再启动工作线程。"""
        with self._lock:
            if not self.enabled or self._closed:
                raise JobConflict("Web backtests are disabled")
            if self._active is not None:
                raise JobConflict("A backtest is already running; wait or cancel it first")
            prefix = "web_" if kind == "backtest" else "research_"
            run_id = prefix + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(4)
            base, base_digest = load_base_config(PROJECT_ROOT / "config" / "params.yaml")
            if parameters.get("preset") == ORIGINAL_PRESET:
                _, smart = self._original_registration()
                configured = copy.deepcopy(smart["arms"]["smart"]["parameters"])
            else:
                configured = configuration_for_strategy(base, parameters.get("strategy"), experiment_id=run_id)
            if kind == "backtest":
                from core.runtime import validate_selector_execution_path
                validate_selector_execution_path(
                    selector_enabled=parameters.get("use_selector") is True,
                    portfolio_targets_enabled=(configured.get("portfolio_targets") or {}).get("enabled", False),
                )
            # 快照绑定本次提交；后续表单或基础配置的编辑不应改变已登记任务。
            # 这里只冻结配置，普通 compact 报告不包含完整代码和行情快照。
            snapshot = self.reports_dir / ".dashboard" / "configs" / f"{run_id}.yaml"
            snapshot_digest = write_config_snapshot(snapshot, configured)
            selector_fields = {}
            if kind == "backtest" and parameters.get("use_selector") is True:
                # Capture weights when the request is admitted, before publishing
                # a queued task. Serving must never resolve the moving alias later.
                selector_fields = self._freeze_selector(run_id)
            job = {"id": run_id, "kind": kind, "run_id": None, "status": "queued", "parameters": parameters,
                   "created_at": _now(), "started_at": None, "finished_at": None,
                   "exit_code": None, "error": None, "cancel_requested": False,
                   "strategy": validate_strategy(parameters.get("strategy")),
                   "config_sha256": snapshot_digest, "base_config_sha256": base_digest,
                   "config_diff": config_diff(base, configured), "config_file": "config.snapshot.yaml",
                   "result_file": "equity.csv" if kind == "backtest" else self._task_types[kind][1],
                   "_config_path": snapshot,
                   **selector_fields,
                   "logs": deque(maxlen=160), "_created_clock": time.monotonic()}
            self._persist(job, force=True)
            self._jobs[run_id] = job
            while len(self._jobs) > LIMITS["max_jobs"]:
                self._jobs.pop(next(iter(self._jobs)))
            self._active = run_id
            worker = Thread(target=self._run, args=(run_id,), daemon=True, name="dashboard-backtest")
            self._worker_thread = worker
            worker.start()
            return self._public(job)

    def _freeze_selector(self, run_id: str) -> dict[str, Any]:
        """Validate the submitted package and its copied contents before admission."""
        from backtest.coin_selector import read_bundle, snapshot_bundle
        from core.reproducibility import sha256_file

        try:
            path, package = read_bundle()
            identity = {"bundle_path": str(path), "bundle_sha256": sha256_file(path)}
            directory = self.reports_dir / ".dashboard" / "selectors" / run_id
            manifest = snapshot_bundle(identity, directory)
            _, frozen = read_bundle(manifest)
            if sha256_file(manifest) != identity["bundle_sha256"] or frozen != package:
                raise ValueError("Current selector package changed during submission")
            return {"_selector_bundle_path": manifest,
                    "selector_bundle_sha256": identity["bundle_sha256"],
                    "selector_model_id": frozen["candidate"]["model_id"]}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError("Unable to freeze the current ML coin selector package; task was not created") from exc

    def _command(self, run_id: str, parameters: dict[str, Any]) -> list[str]:
        job = self._jobs[run_id]
        if job["kind"] != "backtest":
            command = self._task_types[job["kind"]][0](run_id, copy.deepcopy(parameters), self.reports_dir / run_id, job["_config_path"])
            if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
                raise ValueError("Internal research command builder returned invalid argv")
            return command
        mode = "on" if parameters.get("use_selector") is True else "off"
        if parameters.get("preset") == ORIGINAL_PRESET:
            command = [sys.executable, "-u", str(PROJECT_ROOT / "scripts" / "run_selector_backtest.py"),
                       "--coin-selector", mode, "--output-dir", str(self.reports_dir / run_id)]
        else:
            command = [sys.executable, "-u", "-m", "dashboard.backtest_worker",
                       "--config", str(job["_config_path"]), "--config-sha256", job["config_sha256"],
                       "--source", parameters["source"], "--symbols", *parameters["symbols"],
                       "--start", parameters["start"], "--end", parameters["end"],
                       "--capital", str(parameters["capital"]),
                       "--slippage", str(parameters["slippage_bps"] / 10000),
                       "--seed", str(parameters["seed"]), "--timeframe", "1d",
                       "--coin-selector", mode,
                       "--disable-routing-log", "--report-profile", "compact",
                       "--output-dir", str(self.reports_dir / run_id)]
        if parameters.get("use_selector") is True:
            manifest = job.get("_selector_bundle_path")
            if manifest is None:
                raise ValueError("Enabled backtest has no frozen selector package")
            command.extend(["--selector-bundle", str(manifest)])
        if parameters["source"] == "local" and parameters.get("preset") != ORIGINAL_PRESET:
            command.extend(["--data-dir", str(self.data_dir)])
        return command

    def _read_logs(self, run_id: str, stream: Any) -> None:
        try:
            while True:
                line = stream.readline(2048)
                if not line:
                    break
                with self._lock:
                    self._jobs[run_id]["logs"].append(line.rstrip("\r\n"))
                    self._persist(self._jobs[run_id])
        except (OSError, ValueError):
            return

    def _run(self, run_id: str) -> None:
        process = None
        reader = None
        status, error, exit_code = "failed", None, None
        try:
            with self._lock:
                job = self._jobs[run_id]
                if self._closed or job["cancel_requested"]:
                    status = "cancelled"
                    return
                self.reports_dir.mkdir(parents=True, exist_ok=True)
                environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", MPLBACKEND="Agg")
                process = subprocess.Popen(self._command(run_id, job["parameters"]), cwd=PROJECT_ROOT,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", shell=False, env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self._process = process
                job.update(status="running", started_at=_now())
                self._persist(job, force=True)
                reader = Thread(target=self._read_logs, args=(run_id, process.stdout), daemon=True)
                reader.start()
            try:
                exit_code = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait(timeout=10)
                status, error = "timed_out", "Backtest exceeded the execution time limit"
            else:
                with self._lock:
                    cancelled = job["cancel_requested"]
                if cancelled:
                    status = "cancelled"
                elif exit_code != 0:
                    error = f"Backtest exited with code {exit_code}; inspect the log for details"
                elif not (self.reports_dir / run_id / job["result_file"]).is_file():
                    error = "Backtest completed without an equity report" if job["kind"] == "backtest" else "Research task completed without its result artifact"
                else:
                    status = "succeeded"
        except Exception as exc:
            # An isolated worker failure must always release the single-job slot.
            error = f"Unable to run backtest: {type(exc).__name__}: {str(exc)[:300]}"
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        finally:
            if reader is not None:
                reader.join(timeout=3)
            if process is not None and process.stdout is not None:
                process.stdout.close()
            with self._lock:
                job = self._jobs[run_id]
                if job["cancel_requested"]:
                    status, error = "cancelled", None
                job.update(status=status, error=error, exit_code=exit_code, finished_at=_now(),
                           _finished_clock=time.monotonic(), run_id=run_id if status == "succeeded" else None)
                directory = self.reports_dir / run_id
                if directory.is_dir():
                    try:
                        shutil.copyfile(job["_config_path"], directory / "config.snapshot.yaml")
                        # 报告目录可能在运行中已存在；原子发布完成标记，供读取端
                        # 判断状态，避免仅凭 equity.csv 就把半成品当成成功报告。
                        marker = directory / "dashboard_job.json"
                        temporary = directory / "dashboard_job.json.tmp"
                        temporary.write_text(json.dumps({"status": status, "parameters": job["parameters"],
                            "created_at": job["created_at"], "finished_at": job["finished_at"],
                            "error": error, "kind": job["kind"], "strategy": job["strategy"],
                            "config_sha256": job["config_sha256"], "base_config_sha256": job["base_config_sha256"],
                            **{key: job[key] for key in ("selector_bundle_sha256", "selector_model_id") if key in job},
                            "config_diff": job["config_diff"]}, ensure_ascii=False), encoding="utf-8")
                        temporary.replace(marker)
                    except OSError:
                        job.update(status="failed", run_id=None, error="Unable to save completed job metadata")
                try:
                    self._persist(job, force=True)
                finally:
                    self._process = None
                    self._active = None

    def cancel(self, run_id: Any) -> dict[str, Any]:
        if not isinstance(run_id, str):
            raise ValueError("id must be a job identifier")
        with self._lock:
            job = self._jobs.get(run_id)
            if job is None:
                raise FileNotFoundError("Backtest job not found")
            if job["status"] not in TERMINAL:
                job["cancel_requested"] = True
                if self._process is not None:
                    try:
                        self._process.kill()
                    except ProcessLookupError:
                        pass
                self._persist(job, force=True)
            return self._public(job)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._active:
                self.cancel(self._active)
            worker = self._worker_thread
        if worker is not None:
            worker.join(timeout=15)
