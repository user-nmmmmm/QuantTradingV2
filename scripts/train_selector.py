"""Offline ML selector research entry point. Never submits real orders."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.ml_selection.protocol import load_settings, resolve_path, validate_run


def main(argv=None):
    parser = argparse.ArgumentParser(description="CPU 机器学习选币训练与历史验证")
    parser.add_argument("--config", default=str(ROOT / "config/ml_selection.yaml"))
    parser.add_argument("--stage", choices=["all", "prepare", "supervised", "rl", "evaluate", "walk-forward", "shadow", "resolve-shadow"], default="all")
    parser.add_argument("--run-dir", help="新实验输出目录或已冻结实验目录")
    parser.add_argument("--max-symbols", type=int, help="首次小规模运行的币种上限")
    parser.add_argument("--rl-episodes", type=int, help="首次运行的强化学习回合数")
    parser.add_argument("--market-data-dir", help="前瞻已收盘日线 CSV 目录")
    parser.add_argument("--as-of", help="影子观察信息截点，默认当前 UTC")
    parser.add_argument("--account-state", help="原引擎候选 hook 的完整冻结 RL 状态 JSON")
    args = parser.parse_args(argv)
    from research.ml_selection.pipeline import (artifact_manifest, evaluate, full_run, load_dataset,
        prepare, shadow, supervised_stage, train_policies, freeze_candidate, resolve_shadow, walk_forward)
    from research.ml_selection.protocol import load_inputs, save_json
    from research.ml_selection.models import BernoulliPolicy, load_model
    if args.stage in {"all", "prepare"}:
        settings = load_settings(args.config)
        if args.max_symbols is not None:
            if args.max_symbols < 1:
                parser.error("--max-symbols must be positive")
            settings["max_symbols"] = args.max_symbols
        if args.rl_episodes is not None:
            if args.rl_episodes < 1:
                parser.error("--rl-episodes must be positive")
            settings.setdefault("rl", {})["episodes"] = args.rl_episodes
        name = "ml_selection_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        folder = resolve_path(args.run_dir or f"reports/{name}")
        if args.stage == "all":
            full_run(settings, folder)
        else:
            prepare(settings, folder)
            artifact_manifest(folder)
    else:
        if not args.run_dir:
            parser.error("this stage requires --run-dir")
        if args.max_symbols is not None or args.rl_episodes is not None:
            parser.error("frozen settings cannot be changed; create a new experiment")
        folder = resolve_path(args.run_dir)
        protocol = validate_run(folder)
        if args.stage == "resolve-shadow":
            if not args.market_data_dir:
                parser.error("resolve-shadow requires fresh --market-data-dir")
            outcome = resolve_shadow(folder, protocol, market_data_dir=args.market_data_dir, as_of=args.as_of)
            print(json.dumps({"resolved": len(outcome["resolved"]), "pending": outcome["pending_labels"]}, ensure_ascii=False))
        elif args.stage == "shadow":
            outcome = shadow(folder, protocol, market_data_dir=args.market_data_dir, as_of=args.as_of,
                             account_state=args.account_state)
            print(json.dumps({"status": outcome["status"], "observations": len(outcome["observations"])}, ensure_ascii=False))
        else:
            dataset = load_dataset(folder)
            if args.stage == "supervised":
                frames, _, _, _ = load_inputs(protocol["settings"])
                models, validation = supervised_stage(folder, protocol, frames, dataset)
                if not protocol["settings"].get("rl", {}).get("enabled", True):
                    freeze_candidate(folder, protocol, models, validation)
            elif args.stage == "walk-forward":
                frames, _, _, _ = load_inputs(protocol["settings"])
                walk_forward(folder, protocol, frames, dataset)
            else:
                frames, _, _, _ = load_inputs(protocol["settings"])
                models = {name: load_model(folder / "models" / f"{name}.json")
                          for name in protocol["settings"]["models"]}
                candidate_path = folder / "parent_model.json"
                if not candidate_path.exists():
                    parser.error("run the complete validation matrix before RL/evaluation; use --stage all")
                primary = json.loads(candidate_path.read_text(encoding="utf-8"))["primary_model"]
                models["primary"] = models[primary]
                if args.stage == "rl":
                    policy = train_policies(folder, protocol, frames, dataset, models)
                    validation = json.loads((folder / "validation_comparison.json").read_text(encoding="utf-8"))
                    freeze_candidate(folder, protocol, models, validation, policy)
                else:
                    policy_path = folder / "models/policy_best.json"
                    policy = BernoulliPolicy.load(policy_path) if policy_path.exists() else None
                    evaluate(folder, protocol, frames, dataset, models, policy)
            artifact_manifest(folder)
    print(f"研究输出：{folder}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
