# Phase 5 研究与样本外验证

本目录固化训练、验证与最终 Holdout 协议，并保存 Walk-forward、Purged CV、参数平台、因子消融、集中度、成本、多场景、试验台账和最终准入证据。

完成研究流程不等于策略获准上线。`final_holdout_report.json` 是一次性最终测试；若门槛失败，必须建立新假设和新协议版本，不得返回该 Holdout 调参。

## 2026-09-20 准入解释修正（FIX-08）

本目录报告使用 Phase0 已见历史档案，不能证明当前实现取得独立 holdout 准入。旧报告原文保留，当前准入解释以本修正为准。`scripts/run_phase5_analysis.py` 默认拒绝旧准入路径；只有显式 `--retrospective --output 新目录` 才生成回顾性诊断，并记录有效 phase5 配置及输入摘要。当前代码研究使用 `scripts/run_strategy_review.py` 的独立冻结批次，命令见 `--help`。历史前瞻协议的最早成熟日仍为 2027-05-08；本轮修改影响候选身份，须按原变更规则登记，不能直接沿用旧代码 hash。
