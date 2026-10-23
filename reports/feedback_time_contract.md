# Actual completion time contract

`FeedbackV2.at` for `completed` means the actual completion instant explicitly
supplied by the user. `AgentRunV3.started_at` is message receipt and
`FactEventV3.created_at` is persistence. For example completion 09:40, message
10:00 and save 10:02 are three distinct timestamps. A scheduled end is never
completion evidence. Existing persisted data remains readable unchanged.

`ground_completed_time(proposed_at, received_at, source_text)` returns frozen
`CompletionTimeGrounding(accepted, actual_at, reason)`. Both datetimes must be
aware. The caller must first verify the fragment comes from the actual user's
message and refers to the proposed task; a model-authored claim is not evidence.
Only accepted results expose `actual_at`. Rejection reasons are
`timezone_required`, `future_actual_completion_time`,
`missing_actual_completion_time`, `ambiguous_actual_completion_time`, and
`actual_completion_time_mismatch`. Missing completion time must not prevent an
independent, sufficiently specified resource update in the controller.

Supported: timezone-aware ISO timestamps, numeric Chinese YYYY年M月D日 dates,
HH:MM or numeric 上午/下午/晚上/凌晨 clocks, and explicit 刚刚/现在完成 wording.
A bare numeric clock uses the message receipt's Asia/Shanghai date; explicit
昨天 and full dates override it. Generic 今天已完成 and 上午完成 have insufficient
precision. Unsupported Chinese number words should prompt for
`YYYY-MM-DD HH:MM` (local time) or an ISO timestamp with timezone. Equivalent
aware timestamps compare as instants. Future completion is rejected.

`audit_completed_time_visibility(case)` returns problems without modifying the
case. New formal `internal-auto-v4` seals reject completed confirmation events
unless user chats visible at the referenced proposal step ground the instant.
Each source uses its own message receipt, not confirmation/save time. Earlier
user chats are included; later messages, assistant text, hidden confirmation
payloads and schedule output cannot supply evidence. Resource clauses do not
establish completion time. This is a conservative lexical time visibility audit,
not proof of real-world completion. Audit clauses must name the exact task ID
or an unambiguous supported task type mapped through the declared
`stage_execution_policy.task_ids`; unknown task mappings fail conservatively.
Supported inspection aliases include 巡查/巡田/巡视/检查; maintenance includes
维护/清沟/清理沟/沟渠清理/疏通沟. Earlier unrelated tasks cannot supply a clock.
The standalone helper still requires the caller to verify task identity.
Negative, interrogative and uncertain/conditional assertions are rejected.
Non-ISO clocks with seconds are unsupported and rejected rather than truncated;
use aware ISO for second precision.
Legacy development entrypoints remain available. Old seals are not rewritten;
scoring gates are unchanged.

Before running all groups on the four known affected private cases, append an
**author-specified exact actual completion date/time** to each relevant user
message before its proposal step. A pre-run versioned erratum may expose an author-specified simulated event
timestamp already present in the source script, recording its provenance and
reason. This input repair requires no additional teammate approval. The case
author must check each target event
and its local date/time; do not derive the value from a scheduler or hidden
confirmation answer. Keep the original four cases and seal files, record the
revisions/errata externally, and seal a new version. This repository does not
copy private answers into runtime prompts or silently revise private cases.

Public simulated tests cover precise vs vague input, resource-time isolation,
timezone equivalence, receipt vs completion vs persistence, future/mismatched
times, later-message exclusion and formal-seal rejection. They establish parser
and audit behavior, not agronomic or real-world completion correctness.
