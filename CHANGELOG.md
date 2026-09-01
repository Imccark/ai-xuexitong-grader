# Changelog

本项目遵循语义化版本号。

## [2.0.0] - 2026-09-02

### Added

- LangGraph 多智能体证据优先批改工作流。
- SQLite checkpoint、内容寻址缓存、在线预算和批次熔断。
- grader、verifier、符号风险审计与有界自动纠错。
- 本地页面方向、图像质量、布局与 OCR 处理链路。
- 只读 Agent 结果和证据审阅界面。
- 匿名独立模型裁判包、校验器和指标计算工具。
- 离线 Python 测试、Playwright 界面测试和 GitHub Actions。

### Changed

- 默认正式结果源由旧版结果切换为 `candidate`。
- 在线调用要求显式 `--online`、样本范围和 token/调用预算。
- 项目以 Python 3.13+ 的源码应用形式发布。

### Security

- 默认禁止自动提交成绩。
- 私有配置、学生数据、运行产物、密钥文件和本地 Codex 技能不再纳入公开版本。

### Migration

- 旧版 `results/` 保留为只读回滚材料，不会被 Agent 流程覆盖。
- 回滚步骤见 `docs/langgraph-multi-agent-grading-migration-and-rollback.md`。
