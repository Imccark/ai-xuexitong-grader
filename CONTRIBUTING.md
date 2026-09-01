# Contributing

感谢参与改进本项目。

1. 从最新 `main` 创建功能分支。
2. 使用 Python 3.13 和 `uv sync --extra orientation --group dev` 安装依赖。
3. 运行 `uv run pytest -q -m "not online"`。
4. 涉及界面时运行 `npm ci` 和 `npm test`。
5. 提交前检查 diff、待提交文件和 Git 历史中是否存在密钥或个人信息。

禁止提交真实学生作业、姓名/学号映射、API Key、浏览器登录信息、运行日志、checkpoint、模型响应缓存、私有教师标注配置及 `.codex/skills/`。

在线测试必须显式启用并设置有限预算；普通测试和 CI 不应调用付费服务。
