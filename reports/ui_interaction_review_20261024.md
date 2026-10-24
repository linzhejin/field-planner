# 工作台交互复查（2026-10-24）

本次只检查本地交互，不把模拟浏览器检查称为真实农户体验或农业有效性验证。

## 开源参考与取舍

- [Streamlit LLM 示例](https://github.com/streamlit/llm-examples/blob/main/Chatbot.py)把对话记录和输入放在同一交互区。本项目保留现有助手区，空白建档时先呈现唯一必要操作，不让无内容的结果区分散注意。
- [OpenAI Agents 的人工确认流程](https://github.com/openai/openai-agents-python/blob/main/docs/human_in_the_loop.md)和 [ADK 的 Streamlit 待审批示例](https://github.com/jackwotherspoon/adk-human-in-the-loop)都明确显示待处理决定。借鉴的是可见的“待确认”状态：计划结果区现在提示待确认修改数量，并说明计划尚未变化；具体字段仍在助手区核对。
- [CopilotKit](https://github.com/CopilotKit/CopilotKit)强调聊天、共享状态和确认流程联动。本项目继续让表单、助手和结果读取同一业务服务，没有为界面另建事实状态，也没有引入新框架。

## 复现与修正

桌面 1440px 首次进入时，地块名称输入框实测宽约 76px，因为未建档表单已被放进三栏工作台。现在未建档时使用独立、限宽 820px 的建档区，实测输入框约 351px；保存后才显示三栏或窄屏页签。截图见 [桌面](ui_onboarding_desktop_20261024.png)和[窄屏](ui_onboarding_mobile_20261024.png)。

只有一块已保存地时，点“新增地块”后原选择器消失，无法返回原地。直接增加切换入口又暴露出未保存表单内容会悄悄丢失；最终改为明确的“放弃未保存内容并返回”，需要保留时先保存这块地，再使用常规地块切换。浏览器和状态测试覆盖返回原地及不新增空记录。

## 验证范围

临时 SQLite 数据库、无生产密钥的真实浏览器检查覆盖桌面首屏、390px 窄屏首屏、保存、再新增和明确放弃后返回原地；两种宽度均无页面横向溢出。完整 pytest：454 项通过；Ruff 检查与格式检查通过；环境检查通过。模型、天气接口及真实农情没有在这次 UI 复查中重新验收。
