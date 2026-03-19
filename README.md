# AI 学习通作业批改助手

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Xuexitong-orange)
![UI](https://img.shields.io/badge/Review-Local%20Web%20UI-0A7EA4)

AI-powered grader for Xuexitong assignments

一个用于自动批改学习通作业的 AI 助教工具。

本项目使用多模态大模型 + Python 自动化，实现：

- 批量解析学生作业
- 根据教师提供的标准答案进行批改
- 自动生成结构化评语
- 提供本地审阅界面进行人工复核

## Quick Start

首次使用时，先配置 `configs/subjects.json` 和 `prompts/default_prompt.txt`，
再在控制台点击“配置 API Key”保存到本地环境文件 `configs/env/local.env`，然后执行下面三步：

```bash
python create_week.py 第二周
python run_preprocessing.py --assignment configs/assignments/第二周.json 
python run_batch_grading.py --assignment configs/assignments/第二周.json --max-workers 4
```

审阅与人工修改：

```bash
python review_app.py --assignment configs/assignments/第二周.json --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765
```

## 项目目标

AI 批改 + 教师审核

AI 负责生成批改建议，教师保留最终判断。

## 项目特点

- 支持 PDF / 图片作业
- 支持 LaTeX 标准答案
- 使用视觉大模型进行批改
- 批量处理整班作业
- 每个学生生成独立批改报告
- 提供本地审阅 UI
- 支持人工修改与复核工作流程

## 适用场景

- 学习通导出的整班手写作业批改
- 教师已有标准答案，希望先由 AI 给出初步评语
- 需要保留人工复核，而不是完全自动出分
- 想把每周作业流程固定成可重复执行的脚本

## 工作流程

```text
原始作业 zip
        │
        ▼
预处理脚本
(run_preprocessing.py)
        │
        ▼
批量 AI 批改
(run_batch_grading.py)
        │
        ▼
生成学生批改结果
(results/*.txt + results/*.json)
        │
        ▼
本地审阅界面
(review_app.py)
        │
        ▼
人工确认并提交
```

## 界面预览

审阅界面采用三栏布局，适合人工快速复核：

```text
┌──────────────┬──────────────────────────────┬──────────────────────┐
│ 学生列表     │ 作业图片预览                 │ 批注编辑区           │
│              │                              │                      │
│ 1025...张三  │ page_1.png                   │ 题目1：过程基本正确   │
│ 1025...李四  │ page_2.png                   │ 题目2：缺少关键步骤   │
│ 1025...王五  │ page_3.png                   │ 总评：建议复习矩阵运算 │
│ ...          │ 图片区域可独立滚动           │ Ctrl+S / Cmd+S 保存  │
└──────────────┴──────────────────────────────┴──────────────────────┘
```

审阅时可以一边看原始作业图片，一边直接修改 AI 生成的批注，不需要在多个窗口之间来回切换。

## 控制台使用说明

控制台入口：

- 启动 `review_app.py` 后，默认进入“控制台”页
- 左侧可在“控制台 / 批阅”两种视图间切换

左侧周管理：

- 周列表按周目录创建时间排序（Windows/macOS/Linux/WSL 兼容）
- 点击周名称仅表示“选中周”
- 点击“批阅”才会切换到该周的审阅界面
- “新增一周”会创建周目录和对应 assignment 配置

顶部三块状态区：

- 当前作业状态卡：显示当前选中周和时间
- 学生作业文件区：可打开/复制“学生作业文件夹”和 `answer.tex` 路径
- 快捷操作区：
  - 配置 API Key
  - 删除配置（安全）
  - 删除配置+周目录（危险）

任务执行区（与当前选中周联动）：

- 前处理任务：
  - 启动 `run_preprocessing.py --assignment configs/assignments/<当前周>.json`
  - 支持 `max-workers`、`reprocess`
- 批改任务：
  - 启动 `run_batch_grading.py --assignment configs/assignments/<当前周>.json`
  - 支持 `max-workers`、`regrade`

任务防呆与状态：

- 同一周同一任务运行中时，不允许重复启动（前端禁用 + 后端拒绝）
- 任务状态会实时更新为“运行中 / 已完成 / 失败”
- 统计信息会实时输出任务日志（包含每个学生处理进度）
- 同步落盘到 `runtime_logs/*.log`

配置区：

- Prompt 模板：
  - 查看 / 编辑 / 保存 / 恢复默认
- subjects.json：
  - 默认表单编辑（新手模式）
  - 可切换高级 JSON 编辑

API Key 配置：

- 在控制台点击“配置 API Key”
- 支持直接输入并保存到 `configs/env/local.env`
- 提供 Linux/PowerShell/CMD 命令一键复制
- 运行脚本时优先读取系统环境变量，未设置时自动回退到本地 `local.env`

## 三分钟上手

仓库本身不自带任何现成周次数据。正常使用顺序是：

1. 配置学科
2. 设置 API Key
3. 新建一周
4. 运行前处理、批量评分和人工审阅

### 1. 配置学科

先检查并修改：

- `configs/subjects.json`
- `prompts/default_prompt.txt`

你至少需要确认这些内容：

- `subject_name`
  - 这门课的名称，例如“线性代数”或“高等数学”
- `model`
  - 实际调用的大模型名称
- `api_key_env`
  - API Key 对应的环境变量名；如果这里写的是 `DASHSCOPE_API_KEY`，后面就要设置这个环境变量
- `prompt_template`
  - 评分提示词模板文件路径；默认是 `prompts/default_prompt.txt`
- `grading_requirements`
  - 你希望模型特别注意的评分要求
- `output_format`
  - 最终评分结果的固定输出格式；这里决定 `results/*.txt` 长什么样

如果你要换成别的科目，优先修改这两个文件，而不是直接改 Python 代码。

### 2. 安装依赖并设置 API Key

```bash
pip install -r requirements.txt
```

推荐方式：启动 `review_app.py` 后，在控制台点击“配置 API Key”，把 key 保存到 `configs/env/local.env`。

脚本会优先读取系统环境变量；若未设置，则自动读取本地环境文件。

如果你更习惯手动设置环境变量，也可以继续使用：

```bash
export DASHSCOPE_API_KEY="your_api_key"
```

### 3. 新建一周

例如新建“第二周”：

```bash
python create_week.py 第二周
```

脚本会自动创建：

- `第二周/`
- `第二周/raw_submissions/`
- `第二周/processed_images/`
- `第二周/results/`
- `第二周/temp_workspace/`
- `第二周/answer.tex`
- `configs/assignments/第二周.json`

只预览、不实际创建：

```bash
python create_week.py 第二周 --dry-run
```

### 4. 放入原始作业

把平台导出的、已经按学生拆分好的原始提交压缩包逐个放进 `第二周/raw_submissions/`。

这里放入的通常是总包解压后的学生级压缩包，也就是“一名学生一个 zip”，而不是整班总 zip。

示例：

```text
第二周/raw_submissions/
├── 10254700432-张三.zip
├── 10254700433-李四.zip
├── 10254700434-王五.zip
└── 10254700435-赵六.zip
```

然后填写标准答案文件：

```text
第二周/answer.tex
```

### 5. 执行任务

```bash
python run_preprocessing.py --assignment configs/assignments/第二周.json --max-workers 4
python run_batch_grading.py --assignment configs/assignments/第二周.json --max-workers 4
python review_app.py --assignment configs/assignments/第二周.json --port 8765
```

`--max-workers` 可以手动调整，例如改成 `3` 也可以，取决于 API 并发限制和你的电脑性能。

也可以直接在控制台页面启动前处理/批改任务，参数会自动绑定当前选中周。

最后在浏览器打开：

```text
http://127.0.0.1:8765
```

## 目录结构示例

以 `第二周/` 为例：

```text
第二周/
├── answer.tex
├── preprocess_summary.txt
├── summary.txt
├── raw_submissions/
│   ├── 10254700432-张三.zip
│   └── 10254700433-李四.zip
├── processed_images/
│   ├── 10254700432-张三/
│   │   ├── page_1.png
│   │   └── page_2.png
│   └── 10254700433-李四/
│       ├── page_1.png
│       └── page_2.png
└── results/
    ├── 10254700432-张三.txt
    ├── 10254700432-张三.json
    ├── 10254700433-李四.txt
    └── 10254700433-李四.json
```

含义如下：

- `raw_submissions/`
  - 原始学生提交压缩包
- `processed_images/[学生标识]/page_*.png`
  - 前处理后的标准化图片
- `results/[学生标识].txt`
  - 每个学生单独的评分结果
- `results/[学生标识].json`
  - 与 `subjects.json` 的 `output_format` 联动解析后的结构化结果（含按题号拆分）
- `preprocess_summary.txt`
  - 前处理汇总
- `summary.txt`
  - 批量评分汇总
- `answer.tex`
  - 本周标准答案

## 输入输出示例

输入示例：

```text
第二周/raw_submissions/
├── 10254700432-张三.zip
├── 10254700433-李四.zip
├── 10254700434-王五.zip
└── 10254700435-赵六.zip
```

输出示例：

```text
第二周/results/10254700432-张三.txt
```

```text
第1题：
- 得分：8/10
- 评语：思路正确，但最后一步矩阵乘法有符号错误。

第2题：
- 得分：10/10
- 评语：过程完整，结论正确。

总评：
- 本次作业整体较好，建议继续加强行列式化简的细节检查。
```

## 三个主命令分别做什么

`python run_preprocessing.py --assignment configs/assignments/第二周.json --max-workers 4`

- 读取 `raw_submissions/*.zip`
- 自动解压嵌套 zip
- 识别 PDF / JPG / PNG
- 生成 `processed_images/[学生标识]/page_*.png`
- 写入 `preprocess_summary.txt`

`python run_batch_grading.py --assignment configs/assignments/第二周.json --max-workers 4`

- 读取 `processed_images/[学生标识]/page_*.png`
- 批量调用大模型 API 评分
- 结果写入 `results/[学生标识].txt`
- 同步写入 `results/[学生标识].json`（按 `output_format` 结构化解析）
- 默认自动重试失败占位结果（包含“需人工复核”的旧结果）
- 默认跳过已有正常结果
- 写入 `summary.txt`

`python review_app.py --assignment configs/assignments/第二周.json --port 8765`

- 控制台管理周次（新增、删除、打开路径、复制路径）
- 周列表按周目录创建时间排序（Windows/macOS/Linux/WSL 兼容）
- 控制台可配置 API Key（保存到 `configs/env/local.env`）
- 控制台可直接启动前处理/批改任务（绑定当前选中周）
- 统计信息实时显示任务日志与状态
- 审阅台支持左侧切换学生、中间看图、右侧编辑批注
- `Ctrl+S` / `Cmd+S` 保存学生批注

## 最常用命令

新建一周：

```bash
python create_week.py 第二周
```

前处理：

```bash
python run_preprocessing.py --assignment configs/assignments/第二周.json --max-workers 4
```

批量评分：

```bash
python run_batch_grading.py --assignment configs/assignments/第二周.json --max-workers 4
```

人工审阅：

```bash
python review_app.py --assignment configs/assignments/第二周.json --port 8765
```

临时手动指定标准答案路径：

```bash
python run_batch_grading.py --assignment configs/assignments/第二周.json --answer-key /path/to/your_answer.tex --max-workers 4
```

全量重跑所有学生：

```bash
python run_batch_grading.py --assignment configs/assignments/第二周.json --regrade --max-workers 4
```

兼容说明：旧参数 `--retry-failed` 目前仍可用，但语义已对齐为全量重跑（等价于 `--regrade`）。

这个参数的作用：

- 默认情况下：
  - 已有正常结果会跳过
  - 失败占位结果（通常含“需人工复核”）会自动重试
- 加上 `--regrade` 后：
  - 所有学生都会重新批改（无论之前是否成功）

简单理解：

- 不加 `--regrade`：跳过正常结果，自动重试失败占位结果
- 加了 `--regrade`：全部学生重跑

控制台任务运行日志：

- 控制台启动前处理/批改后，日志会实时显示在“统计信息”
- 同时会落盘到仓库根目录 `runtime_logs/*.log`

重新生成已经存在的标准化图片：

```bash
python run_preprocessing.py --assignment configs/assignments/第二周.json --reprocess --max-workers 4
```

这个参数的作用：

- 默认情况下，前处理脚本如果发现某个学生目录里已经有 `page_1.png` 等标准化图片，就会直接跳过
- 加上 `--reprocess` 后，脚本会重新处理这些学生的原始提交，并覆盖原有的标准化图片

简单理解：

- 不加 `--reprocess`：已有图片一律跳过
- 加了 `--reprocess`：已有图片会按当前原始提交重新生成

## 环境要求

- Python 3.10+
- `openai`
- `pymupdf`
- `Pillow`
- 已配置 API Key（推荐通过控制台保存到 `configs/env/local.env`，也支持系统环境变量）

## 隐私与合规说明

本仓库不包含任何学生数据。

使用本项目时请注意：

- 不要上传学生作业数据到公共仓库
- 不要提交 API Key
- 遵守所在学校和平台的使用规则
- 不要违反平台自动化政策

本项目仅用于教学研究与效率工具。

## 未来计划

可能的改进方向：

- 更好的数学公式识别
- 支持更多题型
- 自动生成错题统计
- 自动生成学习报告
- 班级能力分析

## 贡献

欢迎：

- 提交 Pull Request
- 提交 Issue
- 分享改进建议

## License

MIT License

## 作者

Imccark

如果这个项目对你有帮助，欢迎 Star。
