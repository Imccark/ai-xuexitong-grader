# 本地切题 Agent 架构与上线门禁

## 目标

使用本地 PP-DocLayoutV3 替代高频在线页面定位，同时保留现有多模态
`PageObserver` 和 `QuestionLocator` 作为低置信兜底。布局模型只负责区域，
不负责数学转写、判分或总评。

## 生产状态流

```text
normalized page
  -> local PP-DocLayoutV3
  -> default text/formula regions
  -> left-strip PP-OCRv5 question-id anchors
  -> deterministic vertical merge
  -> deterministic layout gate
       accepted -> existing page router
       rejected/unavailable -> online PageObserver
  -> page-level literal transcription
  -> evidence gate
  -> per-question grader / symbol auditor / verifier
  -> deterministic aggregator
```

本地布局输出和门禁理由写入每名学生的：

- `agent_artifacts/<student_hash>/input_manifest.json`
- `agent_artifacts/<student_hash>/page_evidence.json`
- `agent_artifacts/<student_hash>/run_audit.json`

## 安全门禁

本地页面只有同时满足以下条件才跳过在线观察器：

- 默认模型至少检测到一个文本或公式内容区域；
- 至少存在一个唯一且高置信的题号锚点；
- 相邻题号之间的文本、公式区域能够完整合并；
- 第一题号之前不存在无法归属的内容区域；
- 区域和题号置信度分别达到配置阈值；
- 同级的不同题目不存在异常高重叠；
- 没有未归属的 `student_answer`、`cross_page_continuation` 或 `unknown`；
- bbox 合法且面积不低于最低比例。

身份和页眉区域不会进入题目路由。模型缺失、OCR 缺失、输出 schema
不兼容或门禁失败均只触发回退，不会被解释为学生未作答。

## 配置

`configs/agent_pipeline.json` 的 `local_layout` 段控制：

- 模型名、模型目录和推理引擎；
- 是否允许自动下载（生产必须为 `false`）；
- 区域、题号和几何阈值；
- OCR 模型目录；
- 训练类别到项目类别的映射。

本地运行时采用懒加载并缓存初始化失败，避免每页反复加载模型。默认
模型的通用 `text`、`inline_formula`、`display_formula` 等类别不会直接
进入题目路由，只有经过题号锚点合并且通过门禁的整题候选才会被采用。
权重未部署或题号不明确时会安全回退，正式结果不会被空检测覆盖。

## 训练与发布

当前布局标注任务完成前，不读取正在写入的结果目录训练。只有数据包
满足 `upload_ready=true`、隐藏集冻结、隔离页为零且 tar hash 验证通过，
才启动云端训练。

发布物必须包含：

- 模型权重与 SHA-256；
- 数据集版本与哈希；
- 类别表；
- PaddleX/PaddleOCR/导出引擎版本；
- 隐藏集布局报告；
- 本机推理基准；
- 回滚所需的上一模型版本。

## 第一版验收指标

以下为初始门槛，最终阈值以冻结隐藏集校准为准：

| 指标 | 门槛 |
|---|---:|
| 整题区域召回率 | >= 98% |
| 小题区域召回率 | >= 97% |
| 跨页 continuation 召回率 | >= 95% |
| 唯一题号映射准确率 | >= 98% |
| 高置信路径误放行率 | <= 0.5% |
| 相邻题串入率 | <= 2% |
| 在线定位兜底率 | <= 20% |

模型和 OCR 权重就位后，可用纯本地基准命令验证运行时；该命令不会调用
在线 Provider，报告只保存图片 SHA-256，不保存源图片路径：

```powershell
.\.venv\Scripts\python.exe benchmark_local_layout.py `
  --images 第四周\processed_images `
  --answer-manifest evaluation\answer_manifests\第四周\manifest.json `
  --max-pages 200
```

模型升级先运行 shadow，对照在线观察器但不改变正式结果；随后小比例
canary，最后才切成本地主路径。任一关键指标回退时，将 `local_layout.enabled`
设为 `false` 即可恢复现有在线定位，不需要迁移候选结果。
