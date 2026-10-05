"""Build strategy-candidate targets from existing immutable research ledgers."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    from research.ml_selection.candidate_dataset import build_from_episode

    parser = argparse.ArgumentParser(description="只读已有实验账本，生成原策略候选数据；不训练或重放回测")
    parser.add_argument("--run-dir", required=True, help="已有不可变实验目录（需 protocol.json / dataset.csv）")
    parser.add_argument("--episode", required=True, help="实验目录内 episode 相对路径，例如 episodes/test_native")
    parser.add_argument("--output", required=True, help="实验目录外的新输出目录")
    parser.add_argument("--labels-as-of", required=True, help="只允许该时间之前已成熟的目标（UTC）")
    parser.add_argument("--dataset", help="已有因果特征表；默认使用 run-dir/dataset.csv")
    parser.add_argument("--target-type", choices=["actual_exit", "proxy"], default="actual_exit")
    args = parser.parse_args(argv)
    try:
        frame, contract = build_from_episode(args.run_dir, args.episode, args.output,
            labels_as_of=args.labels_as_of, dataset_path=args.dataset, target_type=args.target_type)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    print(f"候选数据已保存：{Path(args.output).resolve()}；候选 {len(frame)}；目标状态 {contract['label_status_counts']}；未进行训练")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
