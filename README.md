# 作业AI批改辅助项目

这个项目默认用于管理同一门课的多周作业，原生适配学习通的数据，适合高数、线代、大物等基础课的手写作业批改
仓库本身不再自带任何现成周次数据；你需要先创建一周，再开始使用。

前置条件：获取一个具有推理能力、多模态融合的模型的apikey（这里推荐使用阿里云百炼平台上的QWEN3.5系列，阿里云高校用云支持计划有300元免费额度）

日常流程固定为三步：
1. 前处理原始提交
2. 批量评分
3. 人工审阅和修改结果

## 快速上手

### 第一步：配置学科

先检查并修改：

- `configs/subjects.json`
- `prompts/default_prompt.txt`

你至少需要确认这些内容：
- `subject_name`
  - 这门课的名字，例如“线性代数”或“高等数学”，建议用英文命名
- `model`
  - 实际调用的大模型名称，一般保持当前可用模型即可
- `api_key_env`
  - API Key 对应的环境变量名；如果这里写的是 `DASHSCOPE_API_KEY`，你后面就要设置这个环境变量
- `prompt_template`
  - 评分提示词模板文件路径；通常就是 `prompts/default_prompt.txt`
- `grading_requirements`
  - 你希望模型特别注意的评分要求，例如是否重点检查证明题过程
- `output_format`
  - 最终评分结果的固定输出格式；这里决定 `results/*.txt` 长什么样

如果你要换成别的科目，优先改这两个文件，而不是直接改 Python 代码。

### 第二步：设置 API Key

先安装依赖：

```bash
pip install -r requirements.txt
```

进入你的 Python 环境后，设置环境变量：

```bash
export DASHSCOPE_API_KEY="your_api_key"
```

如果你在 `configs/subjects.json` 里把 `api_key_env` 改成了别的名字，这里也要对应修改。

### 第三步：新建一周

例如新建“第一周”：

```bash
python create_week.py 第一周
```

脚本会自动创建：

- `第一周/`
- `第一周/raw_submissions/`
- `第一周/processed_images/`
- `第一周/results/`
- `第一周/temp_workspace/`
- `第一周/answer.tex`
- `configs/assignments/第一周.json`

### 第四步：按顺序执行任务

以“第一周”为例：

1. 把平台导出的、已经按学生拆分好的原始提交压缩包逐个放进 `第一周/raw_submissions/`
   文件形态通常是“一名学生一个 zip”，这里放的是总包解压后的学生级压缩包，不是整班总 zip
2. 填写 `第一周/answer.tex`
3. 运行前处理
4. 运行批量评分
5. 如有需要，打开审阅前端人工修改结果

对应命令：

```bash
python run_preprocessing.py --assignment configs/assignments/第一周.json 
python run_batch_grading.py --assignment configs/assignments/第一周.json --max-workers 4
python review_app.py --assignment configs/assignments/第一周.json --port 8765
```
`--max-workers` 支持手动调整，取决于api的并发数限制与你电脑的性能。

最后在浏览器打开：

```text
http://127.0.0.1:8765
```

## 目录约定

以 `第一周/` 为例：

- `第一周/raw_submissions/`
  - 原始提交压缩包
- `第一周/processed_images/[学生标识]/page_*.png`
  - 前处理后的标准化图片
- `第一周/results/[学生标识].txt`
  - 每个学生单独的评分结果
- `第一周/preprocess_summary.txt`
  - 前处理汇总
- `第一周/summary.txt`
  - 批量评分汇总
- `第一周/answer.tex`
  - 本周标准答案

## 三个主命令分别做什么

`python run_preprocessing.py --assignment configs/assignments/第一周.json --max-workers 4`

- 读取 `raw_submissions/*.zip`
- 自动解压嵌套 zip
- 识别 PDF / JPG / PNG
- 生成 `processed_images/[学生标识]/page_*.png`
- 写入 `preprocess_summary.txt`

`python run_batch_grading.py --assignment configs/assignments/第一周.json --max-workers 4`

- 读取 `processed_images/[学生标识]/page_*.png`
- 批量调用千问 API 评分
- 结果写入 `results/[学生标识].txt`
- 自动跳过已有非空结果
- 写入 `summary.txt`

`python review_app.py --assignment configs/assignments/第一周.json --port 8765`

- 左侧切换学生
- 中间查看作业图片
- 右侧修改批注
- `Ctrl+S` / `Cmd+S` 保存

## 最常用命令

新建一周：

```bash
python create_week.py 第一周
```

只预览、不实际创建：

```bash
python create_week.py 第一周 --dry-run
```

前处理：

```bash
python run_preprocessing.py --assignment configs/assignments/第一周.json --max-workers 4
```

批量评分：

```bash
python run_batch_grading.py --assignment configs/assignments/第一周.json --max-workers 4
```

人工审阅：

```bash
python review_app.py --assignment configs/assignments/第一周.json --port 8765
```

临时手动指定标准答案路径：

```bash
python run_batch_grading.py --assignment configs/assignments/第一周.json --answer-key /path/to/your_answer.tex --max-workers 4
```

重跑失败占位结果：

```bash
python run_batch_grading.py --assignment configs/assignments/第一周.json --retry-failed --max-workers 4
```

这个参数的作用：

- 默认情况下，批量评分脚本会跳过已经存在且非空的 `results/[学生标识].txt`
- 但有些结果文件其实不是正式评分结果，而是失败后写入的占位结果，里面通常会包含“需人工复核”这类文字
- 加上 `--retry-failed` 后，脚本会只针对这类失败占位结果重新评分

适合什么时候用：

- 你修复了 API Key、网络、模型配置、prompt 或上传逻辑之后
- 你希望把之前失败的学生重新批量跑一遍
- 你不想手动删除那些失败结果文件

不适合什么时候用：

- 你已经人工修改过某些学生的结果文件，而且不希望被脚本覆盖
- 你不确定结果文件是不是失败占位结果

简单理解：

- 不加 `--retry-failed`：已有结果一律跳过
- 加了 `--retry-failed`：只有“失败占位结果”会被重跑，正常结果仍然跳过

重新生成已经存在的标准化图片：

```bash
python run_preprocessing.py --assignment configs/assignments/第一周.json --reprocess --max-workers 4
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

