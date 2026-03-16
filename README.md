# AI 学习通作业批改助手

AI-powered grader for Xuexitong assignments

一个用于自动批改学习通作业的 AI 助教工具。

本项目使用多模态大模型 + Python 自动化，实现：

- 批量解析学生作业（PDF / 图片）
- 根据教师提供的标准答案进行批改
- 自动生成结构化评语
- 提供本地审阅界面进行人工复核

项目目标：

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

## 三分钟上手

仓库本身不自带任何现成周次数据。正常使用顺序是：先配置学科，再设置 API Key，再新建一周，最后开始处理作业。

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

先安装依赖：

```bash
pip install -r requirements.txt
```

进入你的 Python 环境后，设置环境变量：

```bash
export DASHSCOPE_API_KEY="your_api_key"
```

如果你在 `configs/subjects.json` 里把 `api_key_env` 改成了别的名字，这里也要对应修改。

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

### 4. 按顺序执行任务

以“第二周”为例：

1. 把平台导出的、已经按学生拆分好的原始提交压缩包逐个放进 `第二周/raw_submissions/`
2. 填写 `第二周/answer.tex`
3. 运行前处理
4. 运行批量评分
5. 如有需要，打开审阅前端人工修改结果

这里放入的通常是总包解压后的学生级压缩包，也就是“一名学生一个 zip”，而不是整班总 zip。

对应命令：

```bash
python run_preprocessing.py --assignment configs/assignments/第二周.json 
python run_batch_grading.py --assignment configs/assignments/第二周.json --max-workers 4
python review_app.py --assignment configs/assignments/第二周.json --port 8765
```

`--max-workers` 可以手动调整，例如改成 `3` 也可以，取决于 API 并发限制和你的电脑性能。

最后在浏览器打开：

```text
http://127.0.0.1:8765
```

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
(results/*.txt)
        │
        ▼
本地审阅界面
(review_app.py)
        │
        ▼
人工确认并提交
```

## 目录约定

以 `第二周/` 为例：

- `第二周/raw_submissions/`
  - 原始提交压缩包
- `第二周/processed_images/[学生标识]/page_*.png`
  - 前处理后的标准化图片
- `第二周/results/[学生标识].txt`
  - 每个学生单独的评分结果
- `第二周/preprocess_summary.txt`
  - 前处理汇总
- `第二周/summary.txt`
  - 批量评分汇总
- `第二周/answer.tex`
  - 本周标准答案

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
- 自动跳过已有非空结果
- 写入 `summary.txt`

`python review_app.py --assignment configs/assignments/第二周.json --port 8765`

- 左侧切换学生
- 中间查看作业图片
- 右侧修改批注
- `Ctrl+S` / `Cmd+S` 保存

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

重跑失败占位结果：

```bash
python run_batch_grading.py --assignment configs/assignments/第二周.json --retry-failed --max-workers 4
```

这个参数的作用：

- 默认情况下，批量评分脚本会跳过已经存在且非空的 `results/[学生标识].txt`
- 但有些结果文件其实不是正式评分结果，而是失败后写入的占位结果，里面通常会包含“需人工复核”这类文字
- 加上 `--retry-failed` 后，脚本会只针对这类失败占位结果重新评分

适合什么时候用：

- 你修复了 API Key、网络、模型配置、prompt 或上传逻辑之后
- 你希望把之前失败的学生重新批量跑一遍
- 你不想手动删除那些失败结果文件

简单理解：

- 不加 `--retry-failed`：已有结果一律跳过
- 加了 `--retry-failed`：只有失败占位结果会被重跑，正常结果仍然跳过

重新生成已经存在的标准化图片：

```bash
python run_preprocessing.py --assignment configs/assignments/第二周.json --reprocess --max-workers 4
```

这个参数的作用：

- 默认情况下，前处理脚本如果发现某个学生目录里已经有 `page_1.png` 等标准化图片，就会直接跳过
- 加上 `--reprocess` 后，脚本会重新处理这些学生的原始提交，并覆盖原有的标准化图片

适合什么时候用：

- 你修改了前处理逻辑，想让旧结果按新逻辑重新生成
- 你发现某些学生的图片顺序、页数或清晰度有问题
- 你替换了 `raw_submissions/` 中的原始压缩包，想重新产出 `processed_images/`

需要注意：

- 这个操作会覆盖该周已有的标准化图片
- 它不会自动删除 `results/*.txt`，所以如果你重新生成了图片，通常还要再决定是否重跑评分

简单理解：

- 不加 `--reprocess`：已有图片一律跳过
- 加了 `--reprocess`：已有图片会按当前原始提交重新生成

## 环境要求

- Python 3.10+
- `openai`
- `pymupdf`
- `Pillow`
- 已设置环境变量 `DASHSCOPE_API_KEY`

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
