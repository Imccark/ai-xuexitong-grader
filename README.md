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

先选一种使用方式：

- 控制台优先（推荐新手）：
  1. 启动 UI
  2. 浏览器打开 `http://127.0.0.1:8765`
  3. 在控制台里完成 API Key、建周、前处理、批改、批阅
  
```powershell
python review_app.py --port 8765
```

- 命令行优先（推荐技术用户）：
  1. `create_week`
  2. `run_preprocessing`
  3. `run_batch_grading`
  4. `review_app`

```bash
python3 create_week.py 第二周
python3 run_preprocessing.py --assignment configs/assignments/第二周.json --max-workers 4
python3 run_batch_grading.py --assignment configs/assignments/第二周.json --max-workers 4
python3 review_app.py --assignment configs/assignments/第二周.json --port 8765
```

完整的 Windows/macOS/Linux 分步教程请看：[`三分钟上手（双路径）`](#三分钟上手双路径)。

控制台地址：

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

## 运行依赖

项目当前分成两套运行时：

- Python 运行时
  - `openai`：调用大模型 API
  - `PyMuPDF`：PDF 读写、LaTeX 导出后的 PDF 转 PNG
  - `Pillow`：图片拼接与格式处理
- Node.js 运行时
  - `playwright`：本地截图导出图片（KaTeX 导出链路）
- 系统级可选依赖
  - `lualatex`：仅在你选择 `LaTeX` 图片导出引擎时需要
  - Playwright Chromium 浏览器：仅在你使用 `KaTeX` 图片导出引擎时需要

一句话区分：

- 不用“导出图片”功能，只需要 Python 依赖即可
- 用 `KaTeX` 导出图片，需要 `Node.js + Playwright Chromium`
- 用 `LaTeX` 导出图片，需要 `lualatex`

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
- 图片导出：
  - 可选择默认导出引擎：`LaTeX` / `KaTeX`
  - `LaTeX` 走后端 `lualatex -> PDF -> PNG`
  - `KaTeX` 走后端 `Playwright + KaTeX` 渲染并截图
  - 控制台会显示当前系统下 LaTeX 环境检测结果（Windows/macOS/Linux/WSL）
  - 可在批阅页点击“重新生成图片”强制把当前学生重新入队生成
- subjects.json：
  - 默认表单编辑（新手模式）
  - 可切换高级 JSON 编辑

API Key 配置：

- 在控制台点击“配置 API Key”
- 支持直接输入并保存到 `configs/env/local.env`
- 提供 Linux/PowerShell/CMD 命令一键复制
- 运行脚本时优先读取系统环境变量，未设置时自动回退到本地 `local.env`

## 三分钟上手（双路径）

本项目现在支持两种使用方式：

- A 路线（推荐新手）：以**前端控制台**为主，命令行只负责启动。
- B 路线（推荐技术用户）：以 **bash/PowerShell 命令**为主，控制台用于最终审阅。

先看下面“通用准备”，然后按你偏好的路线走。

### 通用准备（A/B 都要做）

#### 0. 安装 Python

建议安装 **Python 3.10+**。

- Windows：到 https://www.python.org/downloads/windows/ 下载，安装时勾选 `Add python.exe to PATH`
- macOS：到 https://www.python.org/downloads/macos/ 下载（或用 Homebrew）

验证：

```powershell
python --version
```

```bash
python3 --version
```

#### 0.1 安装 Node.js（用于 KaTeX 图片导出）

建议安装 **Node.js 18+**。

- Windows：
  - 到 https://nodejs.org/ 下载 LTS 安装包
  - 安装完成后重新打开 PowerShell
- macOS：
  - 推荐 `brew install node`
  - 或从 https://nodejs.org/ 下载 pkg 安装包
- Linux：
  - 推荐优先使用发行版包管理器或 NodeSource
  - Ubuntu / Debian 可先用系统自带包，或按 NodeSource 官方说明安装较新 LTS

验证：

```powershell
node --version
npm --version
```

```bash
node --version
npm --version
```

#### 1. 先进入项目目录（很重要）

后面所有命令都默认在项目根目录执行（能看到 `README.md`、`requirements.txt`）。

Windows PowerShell：

```powershell
cd D:\你的路径\ai-xuexitong-grader-clean
```

macOS / Linux bash：

```bash
cd /你的路径/ai-xuexitong-grader-clean
```

#### 2. 安装依赖

Windows PowerShell：

```powershell
python -m pip install -r requirements.txt
npm install
npx playwright install chromium
```

macOS / Linux bash：

```bash
python3 -m pip install -r requirements.txt
npm install
npx playwright install chromium
```

说明：

- `pip install -r requirements.txt` 安装 Python 库
- `npm install` 安装 Node 侧依赖
- `npx playwright install chromium` 安装 KaTeX 导出图片需要的 Chromium

如果你确定不会使用 `KaTeX` 图片导出，可以暂时跳过 `npm install` 和 `npx playwright install chromium`。

#### 2.1 可选安装 LaTeX（仅 `LaTeX` 导出引擎需要）

如果你希望使用 `LaTeX` 导出图片，而不是 `KaTeX`，还需要本机安装 `lualatex`。

- Windows：
  - 推荐安装 **MiKTeX** 或 **TeX Live**
  - 安装后确认 `lualatex` 在 PATH 中可直接执行
- macOS：
  - 推荐安装 **MacTeX**
- Linux：
  - 推荐安装 **TeX Live**
- WSL：
  - 需要安装在 WSL 里的 Linux 环境，不是只安装 Windows 侧 TeX

验证：

```powershell
lualatex --version
```

```bash
lualatex --version
```

如果没有安装 LaTeX，也可以直接在控制台把默认导出引擎设为 `KaTeX`。

#### 3. 配置学科（两条路线共用）

先检查并修改：

- `configs/subjects.json`
- `prompts/default_prompt.txt`

至少确认：

- `subject_name`：课程名（如“线性代数”）
- `model`：调用的模型名
- `api_key_env`：API Key 环境变量名（如 `DASHSCOPE_API_KEY`）
- `grading_requirements`：评分重点
- `output_format`：输出格式（决定 `results/*.txt` 与结构化解析）

---

### A 路线：前端控制台优先（推荐新手）

#### A-1. 启动控制台

Windows PowerShell：

```powershell
python review_app.py --port 8765
```

macOS / Linux bash：

```bash
python3 review_app.py --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765
```

#### A-2. 在控制台按顺序操作

1. 点击“配置 API Key”，保存到 `configs/env/local.env`
2. 在 Prompt/subjects 配置区确认学科配置
3. 新增一周（如“第二周”）
4. 把学生 zip 放进 `第二周/raw_submissions/`，并准备 `第二周/answer.tex`
5. 在控制台启动前处理任务和批改任务
6. 切到“批阅”页面做人工复核，必要时手改并保存

说明：脚本会优先读取系统环境变量；未设置时自动回退到本地 `configs/env/local.env`。

---

### B 路线：命令行 / bash 优先（推荐技术用户）

#### B-1. 新建一周

Windows PowerShell：

```powershell
python create_week.py 第二周
```

macOS / Linux bash：

```bash
python3 create_week.py 第二周
```

#### B-2. 放入原始作业与标准答案

把平台导出的、已按学生拆分好的 zip 放进：

```text
第二周/raw_submissions/
```

并填写：

```text
第二周/answer.tex
```

#### B-3. 运行前处理与批改

Windows PowerShell：

```powershell
python run_preprocessing.py --assignment configs/assignments/第二周.json --max-workers 4
python run_batch_grading.py --assignment configs/assignments/第二周.json --max-workers 4
```

macOS / Linux bash：

```bash
python3 run_preprocessing.py --assignment configs/assignments/第二周.json --max-workers 4
python3 run_batch_grading.py --assignment configs/assignments/第二周.json --max-workers 4
```

#### B-4. 启动审阅 UI 做人工复核

Windows PowerShell：

```powershell
python review_app.py --assignment configs/assignments/第二周.json --port 8765
```

macOS / Linux bash：

```bash
python3 review_app.py --assignment configs/assignments/第二周.json --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765
```

补充：

- `--max-workers` 可按机器和 API 限流调整（例如 `3`）
- 需要全量重跑可用 `--regrade`
- 只想从 UI 启动任务，也可先仅执行 `review_app.py`

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
- 控制台支持保存默认图片导出引擎（`LaTeX` / `KaTeX`）
- 批阅页支持“复制图片 / 导出图片 / 重新生成图片”
- `Ctrl+S` / `Cmd+S` 保存学生批注

## 推荐安装方式

如果你只想尽快跑起来，推荐按下面方式安装：

- Windows
  1. 安装 Python 3.10+
  2. 安装 Node.js LTS
  3. 在项目目录执行：

```powershell
python -m pip install -r requirements.txt
npm install
npx playwright install chromium
python review_app.py --port 8765
```

- macOS
  1. `brew install python node`
  2. 在项目目录执行：

```bash
python3 -m pip install -r requirements.txt
npm install
npx playwright install chromium
python3 review_app.py --port 8765
```

- Linux / WSL
  1. 安装 `python3`、`python3-pip`、`nodejs`、`npm`
  2. 在项目目录执行：

```bash
python3 -m pip install -r requirements.txt
npm install
npx playwright install chromium
python3 review_app.py --port 8765
```

如果你想让 `LaTeX` 导出也可用，再额外安装 `lualatex` 即可。

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

图片导出引擎补充说明：

- `KaTeX`：
  - 不依赖系统 LaTeX
  - 由当前浏览器本地渲染后导出 PNG
- `LaTeX`：
  - 依赖系统可执行的 `lualatex`
  - Windows：建议安装 MiKTeX 或 TeX Live，并确保 `lualatex` 在 PATH 中
  - macOS：建议安装 MacTeX，并确保 `lualatex` 可在终端直接执行
  - Linux：建议安装 TeX Live
  - WSL：需要在 WSL 内部安装 TeX Live，不能只装宿主 Windows 侧 LaTeX
- 控制台“图片导出”卡片会直接显示当前环境下 LaTeX 是否可用，以及缺少的命令/宏包提示

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
