# T-0.1 基线锁定记录（代码版本 / 分支 / 工作区状态）

- **锁定时间**：2026-08-25
- **仓库**：QuantTradingV1（远程 `origin` = https://github.com/user-nmmmmm/QuantTradingV2.git，旧远程 `old-origin` = https://github.com/user-nmmmmm/QuantTrading--.git）
- **分支（branch）**：`feature/factors-extended-indicators`
- **Git SHA（commit）**：`ff14fb8cce57310f1e0828349a0357225ce33956`
- **上游跟踪**：`origin/feature/factors-extended-indicators`（本地与远程一致，无 ahead/behind）
- **工作区状态（dirty status）**：`git status --porcelain` 输出为空 → **clean**（无未提交改动、无未追踪文件）
- **最近提交**：`ff14fb8 Wire extended factors into strategy entry confirmations`（2026-08-24T16:32:46+08:00）

## 复核方法

任何人可通过以下命令复核本记录：

```bash
git rev-parse HEAD                  # 应输出 ff14fb8cce57310f1e0828349a0357225ce33956
git rev-parse --abbrev-ref HEAD     # 应输出 feature/factors-extended-indicators
git status --porcelain              # 应无输出（clean）
```

## 验收对照

- ✅ Git SHA 可查：`ff14fb8cce57310f1e0828349a0357225ce33956`
- ✅ Branch 可查：`feature/factors-extended-indicators`
- ✅ Dirty status 可查：clean（无改动）

本记录作为 Phase 0 基线冻结（Gate G0）的代码版本依据，后续 T-0.2（回测归档）、T-0.3（配置快照）均基于此 commit 生成。
