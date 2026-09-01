# 工具目录

根目录只保留日常使用入口。研发和维护脚本按用途放在以下目录，并统一从项目根目录使用 `python -m` 运行。

## 布局与数据处理

位于 `tools/layout/`：

- `benchmark_local_layout`：本地布局模型基准测试
- `derive_layout_orientation_overrides`：生成页面方向覆盖配置
- `prepare_all_layout_images`：批量准备布局图片
- `prepare_rectified_labeling_images`：准备校正后的标注图片
- `prepare_unique_layout_labeling_manifest`：生成去重标注清单
- `run_teacher_labeling`：运行教师布局标注
- `audit_layout_source_dataset`：检查布局数据源覆盖情况
- `prepare_layout_cloud_dataset`：整理布局训练数据
- `benchmark_annotation_api`：标注接口并发基准测试

示例：

```powershell
uv run python -m tools.layout.benchmark_local_layout --help
```

## 模型评测

位于 `tools/evaluation/`：

- `prepare_codex_judge_packets`：生成匿名裁判包
- `run_model_judge`：运行可选的在线独立模型裁判

示例：

```powershell
uv run python -m tools.evaluation.prepare_codex_judge_packets --help
```

## 高级批改工具

位于 `tools/grading/`：

- `run_candidate_batch`：直接处理已准备好的 Graph state JSONL
- `run_reference_compile`：批量编译标准答案 manifest
- `run_shadow_grading`：生成新旧结果影子对照报告

示例：

```powershell
uv run python -m tools.grading.run_candidate_batch --help
```
