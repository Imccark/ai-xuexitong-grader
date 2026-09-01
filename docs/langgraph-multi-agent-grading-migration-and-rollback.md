# LangGraph 批改迁移与回滚手册

本手册对应 `docs/langgraph-multi-agent-grading-upgrade-plan.md` 的当前实现。所有命令从仓库根目录执行，并使用项目约定的共享 Python 环境。2026-08-28 起默认配置为 `candidate`：LangGraph Agent 是正式结果源，旧 `results/` 文件保留为只读回滚材料，不会在切换时被覆盖。

## 不可破坏的边界

- `app/configs/agent_pipeline.json` 的 `feature_flag` 和 `shadow.formal_result_source` 当前均为 `candidate`；回滚时必须同时改回 `legacy`。
- candidate、checkpoint 和 cache 分目录保存；Agent 运行不会覆盖现有 `results/`，前端与导出链路直接读取当前 `candidate_result.json`。
- API key 只能从环境变量读取，不能写进命令行、Graph state、checkpoint、artifact、日志或报告。
- 本地页面已删除人工 finalize/改判/Gold 标注入口；`auto_submit=false`，学习通远程提交仍需单独、明确授权。
- 所有迁移先在 staging 副本执行并校验，禁止用 `git reset --hard`、`git checkout --` 或删除未知未提交文件来“清理”现场。

## 迁移前检查

```powershell
git status --short
uv lock --check
uv run python -m pip check
uv run pytest -q -m "not online"
uv run python -m tools.evaluation.core.validate_model_judgments `
  --judgments evaluation/model_judgments.jsonl `
  --output evaluation/reports/model_judge_gate_report.json
```

只有测试通过、历史结果 hash 未变化、模型裁判门禁通过并且用户明确要求扩量时，才可扩大在线样本。裁判门禁失败时，保留现场并停止扩量。

## 历史结果与 schema

历史 TXT/JSON 通过 `grading_graph.adapters.legacy_result` 做只读兼容投影；不会自动改写旧正式结果。Graph state 支持从 `0.9` 增量迁移到 `1.0`，重复执行具有幂等性，未来未知版本会 fail-closed。

旧人工 Gold 草稿生成器已移除。当前只有通过三阶段模型裁判门禁的 `model_confirmed` 记录可进入评测分母；`model_disputed` 始终保持不可计分。

## 重新前处理的 staging 流程

`app.run_preprocessing --reprocess` 会为每名学生先生成 staging 页面，逐页校验成功后才原子替换目标目录；旧目录先移动到 `preprocess_backups`。失败时旧目录保持不变。

```powershell
uv run python -m app.run_preprocessing --assignment app/configs/assignments/第二周.json --reprocess --max-workers 4
```

迁移后至少核对：提交数、成功/失败数、页数、旧结果 hash 和备份目录。不要直接覆盖 `results/` 或原始提交；发现页数、hash 或人工决策异常时立即停止。

## Agent 正式运行

命令行运行仍必须显式允许在线调用，并同时给出学生数、调用数和输入/输出 token 上限；省略 `--online` 时程序拒绝调用。控制台按钮本身视为一次显式启动操作，会按当前学生目录数和 `app/configs/agent_pipeline.json` 的预算自动补齐这些参数。

```powershell
uv run python -m app.run_batch_grading --assignment app/configs/assignments/第二周.json `
  --engine candidate --online --max-students 10 --max-calls 20 `
  --max-input-tokens 50000 --max-output-tokens 10000
```

provider 连续三名学生失败时停止当前批次。回滚时将 `feature_flag` 与 `shadow.formal_result_source` 同时改为 `legacy` 并重启本地服务；candidate artifact 保留用于审计，不需要反向迁移，旧 `results/` 会重新成为页面和导出数据源。

## 前处理回滚

先确认目标目录、备份目录和 hash，不能把当前目录直接删除。将当前目录移到明确命名的 quarantine 目录后，再把选定的备份目录原子移回目标目录；操作后重新运行 hash、页数和读取回归。若目标文件被占用或备份不唯一，停止并人工处理，不强制覆盖。

示意流程（路径必须由操作者按当前周次和学生目录核实）：

```powershell
$week = (Resolve-Path "第二周").Path
$student = "<已核实的学生目录名>"
$target = Join-Path $week "processed_images\$student"
$backupRoot = Join-Path $week "preprocess_backups"
$quarantine = Join-Path $week "preprocess_quarantine\$student-$(Get-Date -Format yyyyMMddHHmmss)"

New-Item -ItemType Directory -Path (Split-Path -Parent $quarantine) -Force | Out-Null
Move-Item -LiteralPath $target -Destination $quarantine
Move-Item -LiteralPath "<已核实的备份目录>" -Destination $target
```

完成后执行 `evaluation/preprocessing_dry_run.json` 对应的只读检查，并确认历史正式结果和教师决策未被修改。quarantine 目录在完成审计前不得删除。

## 立即停止条件

出现 key 泄漏、正式结果或教师决策不可恢复覆盖、未 finalized 可提交、重复提交、无证据扣分、预算超限、隐藏集被调参或测试被弱化时，停止当前批次，保留 checkpoint、artifact、hash 和报告，禁止继续扩大样本。
