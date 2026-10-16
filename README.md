# 农时窗口

油菜苗期农事规划助手。首版面向湖北已进入苗期的三块示范地，围绕巡田苗情检查、沟渠检查维护，提供有依据的七天安排与反馈重排。

当前是开发环境起点：已提供隔离环境、配置入口、本地自检和配置测试。天气工具、农事规则、排程、模型接入及产品网页仍待实现。

## 环境配置

需要 Python 3.12 和 uv。当前电脑已准备好；新电脑安装 uv 后执行：

```powershell
uv python install 3.12
uv sync --locked
```

在本目录执行命令即可，不必激活虚拟环境。uv 使用根目录的 `.venv`，依赖版本以 `uv.lock` 为准；不要向系统 Python、Anaconda 基础环境或 GIS 环境安装这些依赖。

## 使用方法

```powershell
uv run python -m nongshi.check_env
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

依次进行环境自检、配置测试、静态检查和格式检查。自检会导入依赖并在临时数据库验证 SQLite 写入与重新打开后的读取，不调用外部接口；`model_fields_filled` 只代表字段填写情况，不能证明模型连接成功。

验证 Streamlit 安装可用：

```powershell
uv run python -m streamlit hello
```

浏览器打开 `http://127.0.0.1:8501`，停止时按 Ctrl+C。这是 Streamlit 自带示例，不是本项目的农事规划界面。

首次选定模型服务商后，在没有 `.env` 时复制：

```powershell
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

在本地编辑 `.env` 的服务商、模型名称、接口地址和密钥。环境变量优先于 `.env`；暂未选定时保持空白即可。当前尚无模型客户端。不要把真实密钥发进群聊或提交到 Git。

## 项目结构

```text
AIC/
├── AIC claude code/       项目说明、任务清单、官方附件和模板
├── src/nongshi/           配置读取和环境自检；后续业务代码放这里
├── tests/                pytest 测试
├── .env.example          不含密钥的配置样例
├── .streamlit/           本地监听和遥测配置
├── pyproject.toml        依赖、打包和测试配置
└── uv.lock               可复现依赖版本
```

阅读顺序：先看资料目录中的 `农时窗口_项目说明_20261016.docx`，再用 `农时窗口_任务清单_优化版_20261016.docx` 跟踪 T 编号。官方附件和原始模板保留原样。

## 技术栈

- Python 3.12、Streamlit：本地应用环境与后续网页。
- HTTPX：后续天气与模型 HTTP 请求。
- Pydantic / pydantic-settings：结构校验与本地配置。
- SQLite：Python 标准库自带，无需另装数据库服务器。
- pytest、Ruff：测试、检查与格式化。

暂不引入训练框架、向量数据库或多智能体框架。实际农事阈值须由可追溯规则支持。

## 下一步

1. 与资料负责人确认规则卡片字段、原文和适用条件；同时整理三块标注为模拟的地块样例。
2. 约定地块、任务、反馈和计划版本的数据结构，并准备手工七天计划与重排参考结果。
3. 实现天气工具与失败提示，跑通输入、查证、排程、输出的最小流程。
4. 选定模型服务商后接入工具调用；用已有参考算例检查，不把顺利出文字当作正确排程。

## 协作与依据

提交说明写清任务编号，例如 `feat: T1.3 add timestamped weather data`。运行数据放 `data/local/`，密钥、环境和本地数据库已被忽略。测试临时目录使用 `.pytest_cache/tmp`，避免依赖其他项目遗留的系统临时目录权限。

环境命令参考 [uv 项目管理](https://docs.astral.sh/uv/guides/projects/) 与 [Streamlit 安装指南](https://docs.streamlit.io/get-started/installation/command-line)；配置读取参考 [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)。
