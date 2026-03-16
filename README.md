# AI 学习通作业批改助手

**AI-powered grader for Xuexitong assignments**

一个用于 **自动批改学习通作业** 的 AI 助教工具。

该项目利用 **多模态大模型 + Python 自动化** 实现：

* 批量解析学生作业（PDF / 图片）
* 根据教师提供的标准答案进行批改
* 自动生成结构化评语
* 提供本地审阅界面进行人工复核

目标是把老师从 **机械批改** 中解放出来，同时保留 **最终人工确认** 的教学控制权。

---

# 项目特点

* 支持 **PDF / 图片作业**
* 支持 **LaTeX 标准答案**
* 使用 **视觉大模型进行批改**
* 批量处理整班作业
* 每个学生生成 **独立批改报告**
* 提供 **本地审阅 UI**
* 支持 **人工修改与复核**

设计理念：

> **AI 批改 + 人工审核**

AI 提供建议，教师保留最终判断。

---

# 工作流程

项目日常使用流程非常简单：

```
原始作业 zip
        │
        ▼
Claude Code / 人工整理作业
        │
        ▼
Python 批量调用大模型批改
        │
        ▼
生成每个学生的批改结果
        │
        ▼
本地审阅界面检查与修改
        │
        ▼
人工提交到教学平台
```

---

# 快速开始

## 1 安装依赖

建议使用 Python 3.10+

```bash
pip install -r requirements.txt
```

---

## 2 配置 API Key

项目使用 **DashScope（千问）API**。

设置环境变量：

```bash
export DASHSCOPE_API_KEY="your_api_key"
```

Windows：

```bash
set DASHSCOPE_API_KEY=your_api_key
```

---

## 3 初始化作业周目录

```bash
python create_week.py 第一周
```

将生成如下结构：

```
第一周/
├── raw_submissions
├── processed_images
├── results
├── summary
└── answer.tex
```

---

## 4 放入学生作业

将学习通导出的 **按学生分组的 zip 文件** 放入：

```
第一周/raw_submissions/
```

例如：

```
第一周/raw_submissions/
├── 10254700432-张三.zip
├── 10254700433-李四.zip
```

---

## 5 整理作业图片

将学生作业转换为统一图片格式：

```bash
python scripts/prepare_submissions.py 第一周
```

输出结构：

```
第一周/processed_images/
├── 10254700432-张三
│   ├── page_1.png
│   ├── page_2.png
│   └── page_3.png
```

---

## 6 批量 AI 批改

```bash
python run_batch_grading.py --week 第一周
```

每个学生会生成一个批改文件：

```
第一周/results/
├── 10254700432-张三.txt
├── 10254700433-李四.txt
```

示例输出：

```
========================================
姓名/学号：张三
整体情况：部分错误

错误细节：
1. 第 3 题矩阵计算错误
2. 第 5 题未完成

证明题审查：
推导逻辑基本正确，但缺少关键步骤说明。

改进建议：
建议复习行列式展开和矩阵秩的相关知识。
========================================
```

---

## 7 本地审阅与修改

启动审阅界面：

```bash
python review_app.py
```

浏览器打开：

```
http://localhost:8501
```

教师可以：

* 查看 AI 批改结果
* 修改评语
* 调整评分
* 确认最终结果

---

# 项目结构

```
ai-xuexitong-grader
│
├── configs
│   └── subjects.json
│
├── prompts
│   └── grading_prompt.txt
│
├── review_ui
│   └── 审阅界面代码
│
├── scripts
│   └── 数据整理脚本
│
├── grade_evaluator.py
├── pdf_helper.py
├── run_batch_grading.py
├── review_app.py
│
├── create_week.py
│
└── README.md
```

---

# 设计原则

项目遵循三个核心原则：

### 1 AI 只负责建议

AI 负责生成批改意见，但不自动提交。

---

### 2 教师保留最终决策

教师可以：

* 修改评语
* 调整评分
* 选择是否采纳 AI 建议

---

### 3 自动化只做重复劳动

AI 用于处理：

* 图片解析
* 批量评分
* 生成评语

而不是替代教师判断。

---

# 隐私与合规说明

本仓库 **不包含任何学生数据**。

使用本项目时请注意：

* 不要上传学生作业数据到公共仓库
* 不要提交 API Key
* 遵守所在学校和平台的使用规则
* 不要违反平台自动化政策

本项目仅用于 **教学研究与个人效率工具**。

---

# 未来计划

可能的改进方向：

* 更好的数学公式识别
* 支持更多题型
* 自动生成错题统计
* 自动生成学习报告
* 可视化班级能力分析

---

# 贡献

欢迎提交：

* Pull Request
* Issue
* 改进建议

---

# License

MIT License

---

# 作者

项目作者：

Imccark

如果这个项目对你有帮助，欢迎 ⭐ Star。

---
