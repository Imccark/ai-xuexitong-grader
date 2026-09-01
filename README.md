# AI 学习通作业批改助手 v2.0

![Python](https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Workflow](https://img.shields.io/badge/Workflow-LangGraph-1C3C3C)

面向学习通导出作业的本地批改工具。v2.0 使用 LangGraph 编排多智能体证据链，对 PDF、图片和 DOCX 作业进行预处理、逐题批改、风险复核与确定性汇总，并在本地只读界面展示结果和证据。

> 本项目不会自动向学习通提交成绩。默认配置中的 `auto_submit` 为 `false`；任何远程提交都应由使用者单独确认。

## v2.0 主要变化

- LangGraph 证据优先批改流程，支持本地 SQLite checkpoint 和断点恢复。
- grader、verifier、符号审计和确定性 aggregator 形成有界纠错循环。
- 页面方向检测、透视校正、图像质量评估和可选的本地布局/OCR。
- API 调用必须显式启用在线模式，并设置学生数、调用次数和 token 预算。
- Agent 结果与证据只读展示，保留旧结果作为回滚材料。
- 学生数据、运行产物、密钥文件和本地自动化技能默认不进入 Git。

完整变更见 [CHANGELOG.md](CHANGELOG.md)，v1 迁移与回滚见 [迁移手册](docs/langgraph-multi-agent-grading-migration-and-rollback.md)。

## 系统要求

- Python 3.13.x
- [uv](https://docs.astral.sh/uv/)
- Node.js 20+（仅界面端到端测试或 KaTeX 图片导出需要）
- Windows 10/11 为主要测试平台

本仓库以源码应用方式发布，不提供可独立安装的 Python wheel。

## 安装

克隆仓库后，在项目根目录运行：

```powershell
uv sync --extra orientation --group dev
npm ci
```

仓库内置约 7 MB 的页面方向 ONNX 模型。PaddleOCR 布局和 OCR 权重不随仓库发布，需要时请放入 `models/document_layout/` 和 `models/ocr/` 对应目录。

## 配置密钥

复制示例配置：

```powershell
Copy-Item -LiteralPath "configs/env/local.env.example" -Destination "configs/env/local.env"
```

然后只在本机的 `configs/env/local.env` 中填写：

```dotenv
DASHSCOPE_API_KEY=
OPENAI_API_KEY=
```

- `DASHSCOPE_API_KEY`：候选批改使用的百炼兼容接口密钥。
- `OPENAI_API_KEY`：仅在显式启用可选 Responses API 裁判时需要。
- `configs/env/local.env` 已被 Git 忽略；不要把真实密钥写进源码、命令行、日志或截图。

## 基本流程

### 1. 创建作业周

```powershell
uv run python create_week.py "作业周名称"
```

将标准答案和从学习通导出的原始作业放入新建目录。作业图片、学生文件和运行结果默认被 Git 忽略。

### 2. 预处理

```powershell
uv run python run_preprocessing.py --assignment "configs/assignments/作业周名称.json" --max-workers 4
```

预处理在本地执行格式转换、页面拉平和方向校正，不会调用外部模型 API。

### 3. LangGraph 批改

在线调用必须同时显式提供范围和预算：

```powershell
uv run python run_batch_grading.py `
  --assignment "configs/assignments/作业周名称.json" `
  --engine candidate `
  --online `
  --max-students 10 `
  --max-calls 60 `
  --max-input-tokens 250000 `
  --max-output-tokens 50000
```

省略 `--online` 或必要预算时，candidate 引擎会拒绝发起远程调用。结果保存在作业目录的 `agent_artifacts/` 中，不覆盖旧版 `results/`。

### 4. 查看结果

```powershell
uv run python review_app.py --assignment "configs/assignments/作业周名称.json" --port 8765
```

浏览器打开 `http://127.0.0.1:8765`。界面展示 Agent 结论、风险状态、证据图片和边界框，不提供人工改判或自动提交入口。

## LangGraph 流程

```text
作业页 → 页面观察/布局 → 题目路由 → 转写
     → grader 候选判断 → verifier 对抗复核
     → 必要时局部符号审计/有界纠错
     → 确定性 aggregator → candidate_result.json
```

关键配置位于 `configs/agent_pipeline.json`：

- `feature_flag=candidate`：v2 默认使用 Agent 结果源。
- `shadow.auto_submit=false`：禁止自动提交成绩。
- `budgets`：每名学生的调用和 token 上限。
- `retry`：仅对明确的瞬时错误进行有限重试。
- `local_layout`：控制本地布局模型、OCR 和失败回退策略。

## 测试

离线测试不会调用真实模型服务：

```powershell
uv run pytest -q -m "not online"
```

界面测试：

```powershell
npx playwright install chromium
npm test
```

在线测试带有 `online` 标记，必须显式授权并提供预算，不能在普通 CI 中运行。

## 数据与隐私

- `.codex/skills/`、`configs/teacher_labeling.json`、`configs/env/local.env` 不纳入公开仓库。
- 原始作业、学生图片、姓名/学号映射、批改结果、checkpoint、缓存、运行日志和训练数据均已加入忽略规则。
- 远程多模态批改会把完成任务所需的作业图像发送给所配置的模型服务。使用前应确认学校政策、学生授权和服务商的数据处理条款。
- 日志、Graph state、artifact 和前端接口不得保存或返回 API Key。
- 发布前请运行仓库与 Git 历史的密钥扫描；发现疑似真实密钥时应先轮换，再处理历史记录。

更多安全报告方式见 [SECURITY.md](SECURITY.md)。

## 项目结构

```text
grading_graph/       LangGraph 状态、节点、预算和适配器
evaluation/          匿名评测包、裁判校验和指标工具
review_ui/           本地只读审阅界面
configs/             公开运行配置；私有配置被忽略
models/              可公开分发的本地推理模型与模型说明
tests/               Python 离线测试
e2e/                 Playwright 界面测试
```

## 发布与贡献

提交代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。公开发布不得包含学生数据、真实 API Key、浏览器登录状态、私有教师标注配置或本地自动化技能。

## License

[MIT](LICENSE)
