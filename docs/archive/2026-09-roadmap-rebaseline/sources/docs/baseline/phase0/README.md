# Phase 0 基线归档说明

本目录对应路线图任务 T-0.1 至 T-0.5，是 Phase 0（基线管理 / 治理）的全部产出。所有内容基于 [baseline_lock.md](./baseline_lock.md) 中锁定的 commit `ff14fb8cce57310f1e0828349a0357225ce33956` 生成，作为后续 Phase 1+ 修复工作的对照基线，**不得修改**。

| 文件/目录 | 对应任务 | 说明 |
| --- | --- | --- |
| [baseline_lock.md](./baseline_lock.md) | T-0.1 | 锁定的 Git SHA、分支、工作区状态 |
| [archived_reports/](./archived_reports/) + [reports_manifest.json](./reports_manifest.json) | T-0.2 | 最近 5 组回测的 report.txt / trades.csv / equity.csv / benchmark.csv / data_quality_report.json 及图表，文件已设为只读（chmod 444），并附 SHA-256 哈希清单 |
| [config_snapshot/](./config_snapshot/) | T-0.3 | `config/config.py`、`config/params.yaml` 的只读快照及哈希清单 |
| [freeze_notice.md](./freeze_notice.md) | T-0.5 | 研究基线冻结声明，禁止当前结果用于实盘或扩容 |

问题台账（严重度、责任人、复现步骤）见 Roadmap 工作簿「问题清单」sheet（T-0.4）。

## 归档的 5 组回测（按时间排序，最近5次运行）

1. `20260824_161753_720d_3Syms_Ret101.8pct`
2. `20260824_161807_720d_3Syms_Ret117.7pct`
3. `20260824_163700_3498d_10Syms_Ret17.0pct`
4. `20260824_163836_3498d_10Syms_Ret-15.8pct`
5. `20260824_170434_3498d_10Syms_Ret18.2pct`

## 复核方法

```bash
# 校验归档文件哈希与清单一致
python3 -c "
import hashlib, json
m = json.load(open('docs/baseline/phase0/reports_manifest.json', encoding='utf-8'))
for d, files in m['sets'].items():
    for f, meta in files.items():
        h = hashlib.sha256(open(f'docs/baseline/phase0/archived_reports/{d}/{f}', 'rb').read()).hexdigest()
        assert h == meta['sha256'], (d, f)
print('所有哈希校验通过')
"
```
