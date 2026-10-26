# 任务操作与历史恢复服务验收（2026-10-23）

<!-- SNAPSHOT_NOTICE:START -->
> 历史快照：2026-10-23 主工作区的产品对齐检查，与另一工作树的 v12 完整评测不是同一版本。当前进度见 [STATUS.md](../STATUS.md)。
<!-- SNAPSHOT_NOTICE:END -->

本报告只覆盖任务编辑、取消和历史恢复服务，以及隔离 SQLite 自动验证；页面交互和真实农业适用性由整体验收另行说明。

## 已实现接口

- `WorkbenchServiceV3.edit_task(field_id, task_id, *, preferred_date, duration_minutes, priority, expected_draft_version, expected_fact_version, operation_id)`：保存用户确认的任务设置，返回新预览。指定日期写为同一天的最早日期和目标日期，时长必须大于零，优先级为 1–3；只允许当前七天内且不早于今天的日期。当前事实、未知作业条件、资源容量和时间下限继续由统一排程器校验。
- `WorkbenchServiceV3.cancel_task(field_id, task_id, *, reason, expected_draft_version, expected_fact_version, operation_id)`：必须填写非空原因；写入不可变的取消事件并返回新预览。任务保留为 `cancelled`，原版本和取消原因继续可查，普通事实回放及档案更新不会撤销取消。
- `WorkbenchServiceV3.preview_restore_plan(field_id, version_id, *, operation_id=None)`：只选取当前地块历史。使用当前档案和当前有效事实重算，旧版本未完成时段仅作偏好；当前完成锁、原时段、取消状态、容量、未知条件及当前时钟下限继续有效。返回预览，保留当前档案和已采用版本；需要用户另行采用。

任务编辑和取消同时校验档案、事实版本；数据库事务内继续检查另一版本以防检查后发生并发变化。版本 guard 纳入操作哈希，避免更换 guard 后意外重放旧操作。恢复预览记录所选历史版本，恢复来源也纳入预览操作哈希。

已记录完成事件即锁定任务，即使完成预览尚未采用也不能普通编辑或取消；取消事件尚未采用时也不能直接记录完成。显式完成更正继续使用原有更正事件链，原事件保持不变。

Markdown 导出显示中文的“已取消”和版本变更文案。

## 验证

- 首次运行新生命周期测试得到 13 个预期失败：缺少三项服务接口；补齐实现后 13 项通过。
- 针对数据库操作 guard 未纳入幂等身份，回归测试先得到 `DID NOT RAISE ValueError`；修复后纳入整体验证。
- 针对恢复来源未记录及操作身份未区分历史版本，回归测试先得到来源为空的失败；修复后纳入整体验证。
- 首轮：`uv run --no-sync pytest -q tests/test_task_lifecycle.py tests/test_product_alignment_backend.py tests/test_planning_v2.py tests/test_workbench_v3.py --basetemp C:/Users/HUAWEI/AppData/Local/Temp/nongshi-task-lifecycle-verified-20261023` → **86 passed in 22.88s**。
- 重试修复后：相同四个测试文件，`--basetemp C:/Users/HUAWEI/AppData/Local/Temp/nongshi-task-retry-final-20261023` → **97 passed in 28.71s**。所有测试使用外部唯一 `--basetemp`，不读取或修改用户运行数据库。
- 派生编号碰撞修复后最终：相同四个测试文件，`--basetemp C:/Users/HUAWEI/AppData/Local/Temp/nongshi-task-collision-final-20261023` → **101 passed in 30.14s**。生命周期文件共 36 项测试。
- 对本任务涉及的六个 Python 文件运行 `ruff check` → **All checks passed**；`ruff format --check` → **6 files already formatted**。
- `git diff --check` 返回 0；仅提示其他已有修改文件将在后续 Git 操作时转换 LF/CRLF，无空白错误。

回归覆盖：指定日期不能绕过现场不可作业或未知事实；非法日期、时长、优先级及过期版本不写入；完成锁；取消原因、重载和普通事实回放；当前取消保留；历史恢复保留当前档案、当前完成、当前时长和容量；恢复不自动采用；可行旧时段偏好；跨地块版本拒绝；数据库操作 guard 与恢复来源的幂等身份。

## 独立 review 发现的重试问题与修复

Review 复现：首次修改或取消成功后，以相同操作编号和相同原参数重试，会在前置版本检查处被拒绝；若第一次计算预览失败，同一操作也无法恢复。新增测试先出现 **7 failed, 1 passed**，包含取消并发请求生成不同 UUID 的失败。

现在把已校验的任务操作意图登记在现有 `operations_v3` 表，固定原始档案修改或取消事件的 UUID 和时间。相同编号及参数先加载原意图，再通过现有数据库幂等操作重放保存，已生成预览则恢复原响应；尚无预览则继续计算。不同内容或不同操作类型复用编号会拒绝。首次登记、保存仍在事务内校验档案与事实版本，并发相同请求只保存一次。

补充验证：两类任务操作均覆盖重载后重试、改变参数拒绝、预览计算失败后恢复、已保存预览但响应丢失后恢复、两个并发请求；取消还验证提交前中断后事件时间不变。重试不产生重复档案、事实或采用版本。无需新增表或数据库迁移。

第二次 review 发现派生操作编号碰撞：外部手动预览使用 `task-action-preview`，后续任务操作使用 `task-action` 时，可能把旧预览当作本次响应。新增四项测试先全部失败，同时覆盖 `-draft`、`-fact` 占用导致任务修改失败。

现为每项冻结意图保存独立 UUID，内部保存和预览编号使用 `task-action:<意图 UUID>:<步骤>`，不再从用户操作编号拼接后缀。恢复时还检查预览地块及档案、事实版本不得早于该意图的保存结果。碰撞与重试相关子集 **14 passed, 22 deselected in 8.19s**。

## 边界

- 本改动不提交、推送、部署，不添加依赖，不进行破坏性数据库迁移。
- 编辑/取消确认会先保存档案或事实，再计算预览；计算失败时已确认数据仍保留，可调用 `preview_plan` 重算。
- 农业规则继续处于开发验证阶段；模拟通过不构成田间验证或农业专业验收。
