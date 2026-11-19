"""Run the post-V1 research protocol in a new immutable experiment directory."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    from research.ml_selection.protocol import load_settings, resolve_path
    from research.ml_selection.next_round import run_next
    parser = argparse.ArgumentParser(description="执行机器学习选币合并后六阶段研究")
    parser.add_argument("--config", default=str(ROOT / "config/ml_selection_next.yaml"))
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--previous-run", default=str(ROOT / "reports/ml_selection_full_20261004_v3"))
    parser.add_argument("--pilot-only", action="store_true")
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument("--forward-data-dir", help="取得真实公共行情的不可变采集目录")
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    if args.max_symbols is not None:
        if args.max_symbols < 1:
            parser.error("--max-symbols must be positive")
        settings["max_symbols"] = args.max_symbols
    outcome = run_next(settings, resolve_path(args.run_dir), previous_run=resolve_path(args.previous_run),
                       pilot_only=args.pilot_only, forward_data_dir=args.forward_data_dir)
    print(f"完成研究输出：{resolve_path(args.run_dir)}；状态：{outcome.get('status', 'historical_research_completed')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
