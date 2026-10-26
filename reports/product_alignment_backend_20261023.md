# 产品对齐：后端可靠性修复与验证（2026-10-23）

<!-- SNAPSHOT_NOTICE:START -->
> 历史快照：2026-10-23 主工作区的产品对齐检查，与另一工作树的 v12 完整评测不是同一版本。当前进度见 [STATUS.md](../STATUS.md)。
<!-- SNAPSHOT_NOTICE:END -->

## 范围与状态

完成本轮 Task 1 的后端实现：实时时钟下限、已采用完成事件的显式更正和撤回、按绝对日期对齐的草稿保存、可恢复预览的只读查询以及过期预览拦截。没有提交、推送、发布、生产依赖变更或用户数据库迁移。页面交互与浏览器验收由主任务负责。

## 修复与接口

- `build_plan_v2(..., not_before: datetime | None = None)`：新增显式排程下限；服务在初次构建及事实重算时均传入注入的 `now()`，精确到秒的下限向上取整到分钟。已完成项继续保留历史原时段。V2 直接调用默认仍支持确定性的离线历史回放；实时 V3 服务始终显式传时钟。
- 服务默认 `today()` 从同一 `now()` 转为北京时间计算，避免注入时钟与默认系统日期混用。已有 D02 两条入口均显式注入 `today`、`now`，无需修改运行器。
- 完成状态由完整事件历史的有效事件投影重建。历史版本里没有对应事件的旧完成记录仍受锁定保护；普通档案修改不能解除完成。只有显式的更正/撤回链可以替换受事件管理的完成事实。重算使用历史计划的深复制，不改写旧版本。
- 更正只能引用当前有效的链末事件，且保持同一任务/事实标识；可继续更正撤回事件来恢复事实。原完成事件、更正事件、撤回事件均保留。普通重复完成及同批重复完成均拒绝，失败批次不写事实。
- 更正完成时间即使不改变原安排，也会生成明确的 `completed` 变化，避免采用逻辑误判为“没有变化”而继续保留旧完成时间。
- `align_draft_dates(draft, *, start_date=None) -> FieldDraftV3`：公开模块函数；服务保存时也自动执行。按日历日期保留七日范围交集，新增加而未显式提供的日期其现场、天气、人员工时和时段为空。`profile.workability_by_date` 仍以实际日期为键，仅保留新范围内的值。原本空的逐日列表继续为空；计算时仍按未知条件构建七天。旧范围外的逐日反馈保留在事件历史中，不再错误应用到新日期范围。
- `store.latest_actionable_preview(field_id) -> PlanPreviewV3 | None`：只读查询最新一条预览，核对预览状态、档案版本、事实版本及当前计划。最新预览取消或采用后，不回退到更早预览。
- `service.latest_actionable_preview(field_id) -> PlanPreviewV3 | None`：在存储检查之上核对天气快照有效期与未完成任务是否已落入过去。读取不创建预览、不采用计划、不改写状态。
- `adopt_plan` 同样拦截已经过去的未完成时段；仍保留操作编号幂等回放，使成功采用后的同一请求重试不创建重复版本。

## 失败到通过的证据

1. 新增 `tests/test_product_alignment_backend.py` 后首次运行：**5 failed, 1 passed**。具体复现为：10:05:01 的服务时钟仍给出 09:00 新任务；已采用完成记录的更正被当作重复完成拒绝；允许再次更正已被替换的原事件；缺少预览恢复 API；改起始日保留旧日列表触发校验失败。
2. 初步实现后：**6 passed**。
3. 增加采用结果校验、取消后不复活旧预览、经过时间不再采用后：**3 failed, 9 passed**。新增发现是更正时间虽然出现在预览里，采用却仍保留旧时间；取消较新预览会回退旧预览；经过时间后的预览仍可恢复。修复后继续扩展完成锁与日期测试。
4. 显式给出等价 `plot_id` 的完成更正首次测试：**1 failed, 14 passed**；对缺省和明确的本地块编号统一后通过。
5. 原有服务/Agent/验收测试中的固定历史日期补充显式 `now` 时钟，防止运行机器日期变化影响历史回放；未弱化业务断言。维护任务完成后重算的重复任务编号问题在相关现有用例中被发现并修复。
6. D02 回放测试首次扩展运行：**3 failed, 40 passed**。发现原合成测试脚本在 10 月 20 日 18:12 采用计划后，却回退到同日 10:00 提交已完成；这依赖原来把新任务排进过去的缺陷。仅将该测试完成时刻改为实际新安排结束的 10 月 21 日 09:30。另一新需求用例原在 10:01 计算、10:02 采用 10:01 的新任务，调整显式计算时刻为 10:01:01，按分钟上取整后在 10:02 采用。未修改正式案例、历史评分或 D02 运行器。

## 最终验证命令和结果

```powershell
uv run --no-sync pytest -q tests/test_product_alignment_backend.py tests/test_workbench_v3.py tests/test_agent_service.py tests/test_b04_b06_acceptance.py tests/test_planning_v2.py --basetemp="$env:TEMP/nongshi-core-tests"
# 94 passed in 16.88s

uv run --no-sync pytest -q tests/test_d02_agent_execution.py tests/test_d02_fixed_weather_contract.py tests/test_d02_runner.py tests/test_d02_runner_scripted.py tests/test_reference_replay.py --basetemp="$env:TEMP/nongshi-core-d02-tests"
# 43 passed in 6.72s

uv run --no-sync ruff check src/nongshi/service_v3.py src/nongshi/planning_v2.py src/nongshi/repository_v3.py tests/test_product_alignment_backend.py tests/test_agent_service.py tests/test_b04_b06_acceptance.py tests/test_workbench_v3.py tests/test_d02_runner_scripted.py

uv run --no-sync ruff format --check src/nongshi/service_v3.py src/nongshi/planning_v2.py src/nongshi/repository_v3.py tests/test_product_alignment_backend.py tests/test_agent_service.py tests/test_b04_b06_acceptance.py tests/test_workbench_v3.py tests/test_d02_runner_scripted.py
```

使用独立系统临时目录，避免并行的页面测试清理共享 `.pytest_tmp`。测试全部使用隔离 SQLite 数据库。

Ruff 检查结果为 `All checks passed!`；格式检查结果为 `8 files already formatted`。

## 变更文件与边界

实现：`src/nongshi/service_v3.py`、`src/nongshi/planning_v2.py`、`src/nongshi/repository_v3.py`。

测试：新增 `tests/test_product_alignment_backend.py`（15 项），调整 `tests/test_workbench_v3.py`、`tests/test_agent_service.py`、`tests/test_b04_b06_acceptance.py` 的历史时钟以及 `tests/test_d02_runner_scripted.py` 的不可能时间线。

这是本地后端业务回归证据，不能替代浏览器交互验证、真人使用体验、农业适用性复核或在线模型/天气可用性证明。若用户查看预览期间其新任务起始时刻已过，采用会明确要求重新计算；页面需提供这个重算入口。

## 独立 review 的 P2 修复与复核

独立审查发现并复现：完成 A → 撤回 A → 采用恢复未完成的版本 → 另记完成 B（09:45）并采用 → 恢复旧撤回链 A（09:30），旧逻辑会无条件解除该任务的完成去重锁，导致两个独立有效完成链并存，旧时间覆盖新时间。

新增 `test_reviving_old_retraction_cannot_override_another_active_completion_chain`，首次运行明确失败：预期拒绝重复完成但没有抛出异常。修复仅允许更正目标事件本身承载有效完成记录时解除对应完成锁；没有完成事实的撤回标记不能解除另一链已经记录的完成。回归还核对拒绝后事件数、事实版本、计划历史和当前 09:45 完成时间均不变。

复核命令采用独立 `--basetemp="$env:TEMP/nongshi-core-review-tests"`，运行本报告上述五个核心服务/计划测试文件，结果为 **95 passed in 17.85s**（新增后端回归累计 16 项）。`ruff check` 为 `All checks passed!`，`ruff format --check` 为 `2 files already formatted`。未提交或发布。
