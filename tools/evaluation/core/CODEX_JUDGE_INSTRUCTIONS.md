# Codex / ChatGPT Work 多模态裁判协议

本协议让当前 Codex 或 ChatGPT Work 任务直接担任千问候选结果的独立裁判，不需要 `OPENAI_API_KEY`，也不调用 `tools.evaluation.run_model_judge` 的在线 Provider。

## 硬性顺序

每个 packet 必须严格按以下顺序处理，不能一次性同时读取两个 context：

1. 只读取 `blind_context.json`，使用图像查看能力逐页检查其中的原图；增强图只能辅助观察，不能作为唯一证据。
2. 在尚未读取 `candidate_context.json` 前，先写下独立 verdict、置信度、证据页、负号风险和简短依据。
3. 再读取 `candidate_context.json`，对抗性检查千问候选和独立盲判是否共同漏掉负号、小数点、等号、分数线、上下标、涂改、题号错配或证明逻辑跳步。
4. 最后重新查看关键原图，输出裁决。不能按多数投票；证据冲突时必须 `model_disputed`。

## 可评分门槛

只有同时满足以下条件才输出 `model_confirmed`：

- `decisive=true`；
- `evidence_sufficient=true`；
- `judge_confidence>=0.80`；
- `partial` 或 `incorrect` 至少有一个有效证据页。

其余均输出 `model_disputed`，`expected_verdict` 必须为 `null`，`scoreable=false`。争议项不进入准确率分母。

## 输出

逐题结果追加到 `evaluation/model_judgments.jsonl`，字段必须符合 `evaluation.validate_model_judgments`。至少包含：

- `annotation_source=independent_multimodal_model_judge`
- `judge_runtime=codex_chatgpt_work`
- `assignment_id`、`student_hash`、`question_id`
- `candidate_verdict`、`candidate_supported`
- `expected_verdict`、`judge_confidence`、`scoreable`
- `evidence_refs`、`reason_codes`、`judge_summary`
- `passes.independent`、`passes.critic`、`passes.adjudicator`

不得把姓名、学号、API Key 或原始绝对路径写入裁判结果。每完成一题立即落盘，以便任务中断后续跑。

## 完成后的验证

```powershell
uv run python -m tools.evaluation.core.validate_model_judgments `
  --judgments evaluation/model_judgments.jsonl `
  --output evaluation/reports/model_judge_gate_report.json

uv run python -m tools.evaluation.core.compute_metrics `
  --candidate-root . `
  --model-judgments evaluation/model_judgments.jsonl `
  --reference-source model `
  --output evaluation/reports/model_judge_metrics.json
```
