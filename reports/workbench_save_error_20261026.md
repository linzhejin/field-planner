# 地块档案保存异常复核

日期：2026-10-26（北京时间）

范围：主工作区 `main` 的地块档案保存；另对用户当前打开的 `127.0.0.1:8504` 页面做只读控制台检查。

## 结论

`main` 的档案保存代码此前会把英文 `ValueError` 原文显示给用户，也没有捕获 SQLite 和其他运行时异常；这些异常会成为 Streamlit 红色异常框。已增加数据库写入时间上限、分类捕获和中文恢复提示。失败时保留填写内容，解除故障后可直接重试。

用户描述的那次红色异常没有原文或堆栈，无法将历史问题归因到某一个异常类型。本次在修复前用英文 `ValueError`、SQLite 锁和通用运行时异常复现了同类英文错误/未处理异常；修复后这三类均显示中文提示。

## 修复内容

- SQLite 保存最多等待 1 秒，锁冲突时说明本地数据暂时无法写入。
- 仅向用户展示中文业务校验信息；英文 `ValueError` 和其他异常改为通用中文提示，并在服务日志保留堆栈供排查。
- 保存失败不清除档案输入；允许再次提交。

实现位置：[workbench.py](../src/nongshi/workbench.py)；回归测试：[test_workbench_save_errors.py](../tests/test_workbench_save_errors.py)。

## 验证证据

- 红灯验证：修复前新回归用例为 `2 failed`，分别复现 SQLite `database is locked` 未捕获和 `RuntimeError` 英文异常框。
- 完整测试：修复后最终复验 `uv run --no-sync pytest -q --tb=short` 为 `387 passed in 79.98s`。
- 最终格式后聚焦测试：`uv run --no-sync pytest -q tests/test_workbench_save_errors.py --tb=short` 为 `3 passed in 2.62s`。
- 静态检查：`ruff check .` 通过；`ruff format --check .` 显示 `139 files already formatted`；`git diff --check` 退出码为 0。Git 仅提示工作区现存文件的 LF/CRLF 转换告警。
- 实页复验：主工作区独立实例 `127.0.0.1:8517/?mode=real` 使用临时 SQLite 数据库。正常建档成功；人为锁库后显示“本地数据暂时无法写入”，输入仍在；解除锁后重试成功，地块回到工作台且显示“地块档案已保存”。浏览器错误/警告日志为空，视口和文档宽度均为 1280px。提交等待期间首帧仍显示原表单及“有未保存修改”；没有捕获白屏或遮罩反复闪现，错误提示在数据库等待结束后出现。
- 用户原 `8504` 标签页只读检查：当前控制台错误/警告为空；未点击、刷新或写入。没有历史控制台记录，故无法重建先前异常。

实页截图：[保存成功](workbench_save_success_main_20261026.png)、[错误提示出现前保留输入的表单](workbench_save_error_main_20261026.png)、[锁库时可见的中文错误提示](workbench_save_error_visible_main_20261026.png)。后两张分别记录提交失败的输入状态和错误反馈。

## 边界

这是本地隔离数据库和模拟故障下的交互验证，不证明真实农情、外部模型或天气服务可用。此前的桌面/窄屏布局与按钮交互证据见 [`reference-workbench-sync` 工作树中的交互排查记录](<C:/Users/HUAWEI/.codex/worktrees/reference-workbench-sync/AIC/reports/workbench_interaction_audit_20261026.md>)；本次没有重测外部服务或田间流程。

## 追加复验：同一表单失败后重试

本轮在主工作区 `127.0.0.1:8513/?mode=real` 的隔离 SQLite 中再次复验，未操作用户的 `8504` 页面：

- 真实页面填写地块名称并保存，提示“地块档案已保存”；刷新后档案仍存在。
- 编辑已保存档案时持有 SQLite 写锁并点击保存，页面显示“本地数据暂时无法写入”；英文 `database is locked` 未展示，输入仍在、编辑对话框保持打开，页面无 Streamlit 异常块。
- 释放锁后在同一个浏览器会话和表单点击重试，保存成功、对话框关闭；浏览器控制台错误为 0。
- 最新完整回归：`uv run --no-sync pytest -q --tb=short` 为 400 passed；Ruff 与格式检查通过。更早的 387 项记录保留为当时快照，不代表最新测试总数。

本次实测证明 SQLite 锁定场景可恢复，并覆盖了未预期英文 `ValueError`/`RuntimeError` 的本地回归用例；由于用户无法提供原始英文异常文本或堆栈，仍不能确认历史那次报错的具体根因。

## 2026-10-26 合并后复验

- 合并后的完整pytest为593项通过，其中包括本报告涉及的SQLite锁定、英文`ValueError`、通用运行时异常和同表单重试回归。
- 用户当前的 `8504` 页面只做只读检查，浏览器控制台错误为空；本轮没有在该页面写入档案。保存失败和恢复成功的实际写入仍由本报告前述隔离数据库实例验证。
- 代码合并提交 `2bedcc866e16166865978d74406b2a680ee141c6` 已同步到远端；冻结v12证据仍对应其单独封存的代码提交，不能混用。
