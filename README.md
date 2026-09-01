# AI 学习通作业批改助手

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Xuexitong-orange)
![Version](https://img.shields.io/badge/Version-2.0.0-0A7EA4)

AI-powered grader for Xuexitong assignments

一个用于批量处理和批改学习通作业的 AI 助教工具。

本项目使用多模态大模型 + Python 自动化，实现：

- 批量整理学生提交的 PDF、Word 和图片作业
- 自动校正页面方向和拍照透视
- 根据教师提供的标准答案逐题批改
- 自动复查容易看错的符号、步骤和结论
- 生成结构化批改结果和评语
- 在本地网页中查看批改结论与原图证据

## Quick Start

推荐从 [Releases](https://github.com/Imccark/ai-xuexitong-grader/releases/tag/v2.0.0) 下载 `ai-xuexitong-grader-v2.0.0-windows.zip`，解压后在目录中打开 PowerShell。

安装 Python 依赖：

```powershell
uv sync --extra orientation --no-dev
```

启动本地控制台：

```powershell
uv run python review_app.py --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765
```

第一次使用时，在控制台依次完成：

1. 配置 API Key
2. 创建新一周作业
3. 放入标准答案和学生作业
4. 运行图片预处理
5. 启动 AI 批改
6. 查看 Agent 结果

## 项目目标

让整班作业的整理、预处理、批改和结果查看形成一套可重复执行的流程，减少逐份打开文件和手工整理评语的时间。

v2.0 默认使用多 Agent 批改流程。系统会先识别题目和学生答案，再进行批改与复查，最后生成统一格式的结果。

## 项目特点

- 支持 PDF、DOCX、PNG、JPG 等常见作业格式
- 支持 LaTeX 标准答案
- 支持整班作业批量预处理和批改
- 自动恢复图片方向并校正拍照透视
- 对负号、小数点、等号、分数线和涂改痕迹进行重点复查
- 支持任务中断后继续运行
- 提供本地网页控制台和结果查看界面
- 可导出带批注的作业图片
- 默认不会自动向学习通提交成绩

## 运行依赖

基础运行需要：

- Python 3.13
- uv
- 可访问的多模态模型 API

可选依赖：

- Node.js 20+：使用 KaTeX 导出批注图片时需要
- Playwright Chromium：使用 KaTeX 导出时需要
- LuaLaTeX：选择 LaTeX 导出方式时需要
- PaddleOCR 模型：启用本地布局识别和 OCR 时需要

如果只进行预处理、AI 批改和网页查看，安装 Python 依赖即可。

需要 KaTeX 图片导出时再运行：

```powershell
npm ci
npx playwright install chromium
```

## 适用场景

- 学习通导出的整班手写作业
- 教师已有标准答案，希望自动生成逐题批改结果
- 作业中包含公式、矩阵、证明题或计算过程
- 希望统一保存每周作业和批改记录
- 希望在本地查看原图、增强图和 AI 判断依据

## 工作流程

```text
学习通导出的作业
        │
        ▼
创建作业周并放入标准答案
        │
        ▼
本地图片预处理
        │
        ▼
AI 逐题批改与自动复查
        │
        ▼
生成批改结果
        │
        ▼
本地网页查看和导出
```

## 三分钟上手

### 第一步：准备环境

安装 Python 3.13 和 uv，然后在项目目录运行：

```powershell
uv sync --extra orientation --no-dev
```

### 第二步：启动控制台

```powershell
uv run python review_app.py --port 8765
```

打开 `http://127.0.0.1:8765`。

控制台会显示作业周、API Key、预处理、批改和结果查看入口。推荐第一次使用时直接通过控制台完成所有操作。

### 第三步：配置 API Key

在控制台点击“配置 API Key”，填写百炼兼容接口的 Key。

也可以复制示例文件：

```powershell
Copy-Item -LiteralPath "configs/env/local.env.example" -Destination "configs/env/local.env"
```

然后编辑：

```dotenv
DASHSCOPE_API_KEY=
```

`configs/env/local.env` 只保存在本机，不要发送给他人，也不要提交到 GitHub。

### 第四步：创建作业周

在控制台输入作业周名称并点击创建，或者运行：

```powershell
uv run python create_week.py "新作业周"
```

创建后会得到：

```text
新作业周/
├── answer.tex
├── raw_submissions/
├── processed_images/
└── results/
```

将教师标准答案保存为 `answer.tex`，把从学习通导出的学生作业放入 `raw_submissions/`。

### 第五步：预处理作业

控制台点击“运行预处理”，或者运行：

```powershell
uv run python run_preprocessing.py `
  --assignment "configs/assignments/新作业周.json" `
  --max-workers 4
```

预处理会：

- 解压学生提交文件
- 将 PDF 和 DOCX 转成图片
- 修正 EXIF 方向
- 检测纸张边缘并进行透视校正
- 判断页面是否需要旋转
- 把整理后的图片写入 `processed_images/`

这一步在本地运行，不调用大模型 API。

### 第六步：运行 AI 批改

推荐直接点击控制台中的“启动批改”。控制台会按照当前学生数量自动设置本次运行范围和预算。

命令行方式：

```powershell
uv run python run_batch_grading.py `
  --assignment "configs/assignments/新作业周.json" `
  --engine candidate `
  --online `
  --max-students 10 `
  --max-calls 60 `
  --max-input-tokens 250000 `
  --max-output-tokens 50000
```

请根据实际学生数量修改 `--max-students`。调用次数和 token 参数是单名学生的上限，用于避免任务异常时持续消耗额度。

### 第七步：查看结果

批改完成后，在控制台点击“Agent 结果”。

结果页面可以查看：

- 学生整体批改状态
- 每道题的正确、部分错误或错误结论
- AI 转写的学生答案
- 批改理由和风险提示
- 对应的作业页和证据位置
- 原图、标准化图片和增强图片

结果页为只读展示，避免误操作修改已经生成的 Agent 结果。

## 命令行使用

### 创建作业周

```powershell
uv run python create_week.py "新作业周"
```

### 预处理

```powershell
uv run python run_preprocessing.py --assignment "configs/assignments/新作业周.json" --max-workers 4
```

### 批量批改

```powershell
uv run python run_batch_grading.py --assignment "configs/assignments/新作业周.json" --engine candidate --online --max-students 10 --max-calls 60 --max-input-tokens 250000 --max-output-tokens 50000
```

### 启动结果页面

```powershell
uv run python review_app.py --assignment "configs/assignments/新作业周.json" --port 8765
```

## 目录结构示例

```text
ai-xuexitong-grader/
├── configs/
│   ├── agent_pipeline.json
│   ├── subjects.json
│   ├── assignments/
│   └── env/
├── grading_graph/
├── models/
├── prompts/
├── review_ui/
├── create_week.py
├── run_preprocessing.py
├── run_batch_grading.py
└── review_app.py
```

每个作业周的数据单独保存在对应目录中：

```text
新作业周/
├── answer.tex
├── raw_submissions/
│   ├── 学生作业_A.zip
│   └── 学生作业_B.pdf
├── processed_images/
├── agent_artifacts/
└── results/
```

## 常见问题

### 找不到 API Key

确认控制台中配置的环境变量名称与 `configs/subjects.json` 中的 `api_key_env` 一致。默认使用 `DASHSCOPE_API_KEY`。

### 预处理后图片方向不正确

项目自带页面方向模型。如果少量页面仍然旋转错误，可先检查原文件的 EXIF 信息和拍摄角度，再重新运行预处理。

### 本地布局模型不可用

运行包不会附带体积较大的 PaddleOCR 布局和 OCR 权重。缺少这些权重时，批改流程会使用配置中的备用页面观察方式。

### 批改命令提示缺少预算

命令行在线批改必须同时提供 `--online`、`--max-students`、`--max-calls`、`--max-input-tokens` 和 `--max-output-tokens`。通过控制台启动时会自动补齐。

### 无法导出批注图片

如果选择 KaTeX 导出，请确认已经运行：

```powershell
npm ci
npx playwright install chromium
```

如果选择 LaTeX 导出，请确认系统可以使用 `lualatex`。

## 使用提醒

- 学生作业可能包含姓名、学号和手写内容，请妥善保管本地作业目录。
- 使用在线模型批改时，作业图片会发送到你所配置的模型服务。
- 不要把 API Key 写进公开文件、截图或 Git 提交。
- AI 批改结果可能存在误判，正式使用前建议抽查结果。
- 项目默认不会自动向学习通提交成绩。

## License

[MIT](LICENSE)

## 作者

[Imccark](https://github.com/Imccark)
