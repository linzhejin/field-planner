"""Append-only SQLite records for the v3 workbench."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypeVar
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel

from nongshi.schema_v3 import (
    AgentProposalRecordV3,
    AgentProposalV3,
    AgentRunV3,
    EventCommitResultV3,
    FactEventInputV3,
    FactEventV3,
    FieldDraftV3,
    FieldStateV3,
    PlanAdoptionResultV3,
    PlanPreviewV3,
    PlanVersionV3,
    RuleAdoptionRecordV3,
    TaskActionV3,
    UIEditorDraftV1,
    WeatherSnapshotV3,
)
from nongshi.tasks_v4 import (
    FieldIssueV4,
    TaskExecutionGroupSaveResultV4,
    TaskExecutionGroupV4,
    TaskInstanceUpdateResultV4,
    TaskInstanceV4,
    TaskSeriesSaveResultV4,
    TaskSeriesUpdateResultV4,
    TaskSeriesV4,
    expand_task_series_all,
)

T = TypeVar("T", bound=BaseModel)
_connection_timeout: ContextVar[float] = ContextVar("nongshi_sqlite_timeout", default=10.0)


class WorkbenchStoreV3:
    """Persist drafts, confirmed facts, previews and adopted snapshots separately."""

    @contextmanager
    def connection_budget(self, seconds: float) -> Iterator[None]:
        token = _connection_timeout.set(max(0.001, min(1.0, seconds)))
        try:
            yield
        finally:
            _connection_timeout.reset(token)

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_before_upgrade()
        with self._connection() as connection:
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS fields_v3 (
                    id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    draft_version INTEGER NOT NULL,
                    fact_version INTEGER NOT NULL,
                    latest_plan_id TEXT,
                    plan_stale INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fact_events_v3 (
                    event_id TEXT PRIMARY KEY,
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    fact_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS fact_events_v3_field
                    ON fact_events_v3(field_id, fact_version);
                CREATE TABLE IF NOT EXISTS plan_previews_v3 (
                    id TEXT PRIMARY KEY,
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plan_versions_v3 (
                    id TEXT PRIMARY KEY,
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    parent_id TEXT REFERENCES plan_versions_v3(id),
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS plan_versions_v3_field
                    ON plan_versions_v3(field_id, created_at);
                CREATE TABLE IF NOT EXISTS rule_adoptions_v3 (
                    id TEXT PRIMARY KEY,
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    rule_id TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS weather_snapshots_v3 (
                    id TEXT PRIMARY KEY,
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    fetched_at TEXT NOT NULL,
                    coverage_start TEXT NOT NULL,
                    coverage_end TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS weather_snapshots_v3_lookup
                    ON weather_snapshots_v3(field_id, fetched_at);
                CREATE TABLE IF NOT EXISTS agent_runs_v3 (
                    id TEXT PRIMARY KEY,
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    started_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_proposals_v3 (
                    id TEXT PRIMARY KEY,
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    run_id TEXT NOT NULL REFERENCES agent_runs_v3(id),
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS agent_proposals_v3_field
                    ON agent_proposals_v3(field_id, status);
                CREATE TABLE IF NOT EXISTS operations_v3 (
                    operation_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    result_type TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_series_v4 (
                    id TEXT PRIMARY KEY,
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_series_v4_field
                    ON task_series_v4(field_id, created_at);
                CREATE TABLE IF NOT EXISTS task_series_revisions_v4 (
                    revision_id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL REFERENCES task_series_v4(id),
                    task_version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_series_revisions_v4_series
                    ON task_series_revisions_v4(series_id, task_version);
                CREATE TABLE IF NOT EXISTS task_instances_v4 (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL REFERENCES task_series_v4(id),
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    occurrence_number INTEGER NOT NULL,
                    occurrence_date TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(series_id, occurrence_number)
                );
                CREATE INDEX IF NOT EXISTS task_instances_v4_field_date
                    ON task_instances_v4(field_id, occurrence_date);
                CREATE TABLE IF NOT EXISTS task_instance_revisions_v4 (
                    revision_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES task_instances_v4(id),
                    task_version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_instance_revisions_v4_instance
                    ON task_instance_revisions_v4(instance_id, task_version);
                CREATE TABLE IF NOT EXISTS task_execution_groups_v4 (
                    id TEXT PRIMARY KEY,
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_execution_groups_v4_field
                    ON task_execution_groups_v4(field_id, id);
                CREATE TABLE IF NOT EXISTS task_execution_group_revisions_v4 (
                    revision_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL REFERENCES task_execution_groups_v4(id),
                    task_version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_execution_group_revisions_v4_group
                    ON task_execution_group_revisions_v4(group_id, task_version);"""
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS field_issues_v4 (
                    id TEXT PRIMARY KEY,
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    issue_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS field_issues_v4_field_status
                    ON field_issues_v4(field_id, status, issue_key, opened_at);
                CREATE TABLE IF NOT EXISTS field_issue_events_v4 (
                    event_id TEXT PRIMARY KEY,
                    issue_id TEXT NOT NULL REFERENCES field_issues_v4(id),
                    field_id TEXT NOT NULL REFERENCES fields_v3(id),
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS field_issue_events_v4_issue
                    ON field_issue_events_v4(issue_id, created_at);
                CREATE TABLE IF NOT EXISTS ui_editor_drafts_v1 (
                    mode TEXT NOT NULL,
                    field_id TEXT NOT NULL,
                    form_kind TEXT NOT NULL,
                    subject_id TEXT NOT NULL DEFAULT '',
                    base_draft_version INTEGER NOT NULL,
                    base_fact_version INTEGER NOT NULL,
                    base_task_version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (mode, field_id, form_kind, subject_id)
                );
                """
            )

    def _backup_before_upgrade(self) -> None:
        if not self.path.exists():
            return
        backup_path = self.path.with_name(f"{self.path.name}.pre-open-ga.bak")
        if backup_path.exists():
            return
        with sqlite3.connect(self.path, timeout=1.0) as source:
            existing = {
                row[0]
                for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            project_tables = {"plan_versions", "plan_versions_v2", "fields_v3"}
            current_tables = {
                "fields_v3",
                "agent_proposals_v3",
                "task_series_v4",
                "task_instances_v4",
                "task_execution_groups_v4",
                "field_issues_v4",
                "field_issue_events_v4",
            }
            if not existing.intersection(project_tables) or current_tables.issubset(existing):
                return
            with sqlite3.connect(backup_path, timeout=1.0) as target:
                source.backup(target)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path, timeout=_connection_timeout.get(), isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        else:
            if connection.in_transaction:
                connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _json(value: BaseModel | dict[str, object] | list[object]) -> str:
        if isinstance(value, BaseModel):
            return value.model_dump_json()
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _hash(cls, value: object) -> str:
        if isinstance(value, BaseModel):
            raw = value.model_dump_json(exclude_none=False)
        else:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _replay(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        action: str,
        request_hash: str,
        result_type: str,
    ) -> str | None:
        row = connection.execute(
            "SELECT action, request_hash, result_type, result_json "
            "FROM operations_v3 WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        if (row["action"], row["request_hash"], row["result_type"]) != (
            action,
            request_hash,
            result_type,
        ):
            raise ValueError("操作编号已用于不同内容，不能重复使用")
        return str(row["result_json"])

    def _save_operation(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        action: str,
        request_hash: str,
        result: BaseModel,
    ) -> None:
        connection.execute(
            "INSERT INTO operations_v3 "
            "(operation_id, action, request_hash, result_type, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                action,
                request_hash,
                type(result).__name__,
                self._json(result),
                self._now().isoformat(),
            ),
        )

    def _link_issue_tasks(
        self,
        connection: sqlite3.Connection,
        field_id: str,
        issue_id: str,
        task_ids: list[str],
        at: datetime,
    ) -> None:
        row = connection.execute(
            "SELECT payload FROM field_issues_v4 WHERE id=? AND field_id=?",
            (issue_id, field_id),
        ).fetchone()
        if row is None:
            raise ValueError("找不到要关联的问题记录")
        issue = FieldIssueV4.model_validate_json(row["payload"])
        if issue.status != "open":
            raise ValueError("已解决的问题不能再添加新的处理任务")
        added = [task_id for task_id in task_ids if task_id not in issue.related_task_ids]
        if not added:
            return
        issue = issue.model_copy(update={"related_task_ids": [*issue.related_task_ids, *added]})
        connection.execute(
            "UPDATE field_issues_v4 SET payload=? WHERE id=?",
            (self._json(issue), issue.id),
        )
        event = {
            "id": str(uuid4()),
            "type": "task_linked",
            "issue_id": issue.id,
            "task_ids": added,
            "at": at.isoformat(),
        }
        connection.execute(
            "INSERT INTO field_issue_events_v4 "
            "(event_id, issue_id, field_id, created_at, payload) VALUES (?, ?, ?, ?, ?)",
            (event["id"], issue.id, field_id, at.isoformat(), self._json(event)),
        )

    @staticmethod
    def _field_from_row(row: sqlite3.Row) -> FieldStateV3:
        return FieldStateV3.model_validate_json(row["payload"])

    def task_action(
        self, operation_id: str, action: str, request: dict[str, object]
    ) -> TaskActionV3 | None:
        with self._connection() as connection:
            replay = self._replay(
                connection, operation_id, action, self._hash(request), "TaskActionV3"
            )
        return TaskActionV3.model_validate_json(replay) if replay is not None else None

    def prepare_task_action(
        self, operation_id: str, action: str, request: dict[str, object], intent: TaskActionV3
    ) -> TaskActionV3:
        if not operation_id.strip():
            raise ValueError("任务操作需要操作编号")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request_hash = self._hash(request)
            replay = self._replay(connection, operation_id, action, request_hash, "TaskActionV3")
            if replay is not None:
                return TaskActionV3.model_validate_json(replay)
            row = connection.execute(
                "SELECT * FROM fields_v3 WHERE id = ?", (intent.field_id,)
            ).fetchone()
            if row is None:
                raise KeyError("找不到任务所属地块")
            state = self._field_from_row(row)
            if (state.draft_version, state.fact_version) != (
                intent.expected_draft_version,
                intent.expected_fact_version,
            ):
                raise ValueError("版本冲突：任务档案或现场事实已变化，请刷新后重试")
            self._save_operation(connection, operation_id, action, request_hash, intent)
            return intent

    def task_action_preview(self, intent: TaskActionV3) -> PlanPreviewV3 | None:
        """Recover an already persisted response before recomputing against new state."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT action, result_type, result_json FROM operations_v3 WHERE operation_id = ?",
                (f"task-action:{intent.id}:preview",),
            ).fetchone()
        if row is None:
            return None
        if (row["action"], row["result_type"]) != ("preview_plan", "PlanPreviewV3"):
            raise ValueError("操作编号已用于不同内容，不能重复使用")
        preview = PlanPreviewV3.model_validate_json(row["result_json"])
        if (
            preview.field_id != intent.field_id
            or preview.expected_draft_version
            < intent.expected_draft_version + int(intent.draft is not None)
            or preview.expected_fact_version
            < intent.expected_fact_version + int(intent.event is not None)
        ):
            raise ValueError("任务操作与保存预览的依据不匹配，请重新生成预览")
        return self.load_preview(preview.id)

    def save_field_draft(
        self,
        draft: FieldDraftV3,
        *,
        expected_version: int | None,
        operation_id: str,
        proposal_id: str | None = None,
        expected_fact_version: int | None = None,
    ) -> FieldStateV3:
        if not operation_id.strip():
            raise ValueError("保存草稿需要操作编号")
        request_payload = {
            "draft": draft.model_dump(mode="json"),
            "expected_version": expected_version,
        }
        if expected_fact_version is not None:
            request_payload["expected_fact_version"] = expected_fact_version
        request_hash = self._hash(request_payload)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection, operation_id, "save_field_draft", request_hash, "FieldStateV3"
            )
            if replay is not None:
                return FieldStateV3.model_validate_json(replay)
            row = connection.execute(
                "SELECT * FROM fields_v3 WHERE id = ?", (draft.field_id,)
            ).fetchone()
            if expected_version is None:
                if row is not None:
                    raise ValueError("地块已存在；请基于当前版本更新")
                draft_version, fact_version, latest_plan_id, plan_stale, task_version = (
                    1,
                    0,
                    None,
                    False,
                    0,
                )
            else:
                if row is None:
                    raise KeyError(f"找不到地块 {draft.field_id}")
                state = self._field_from_row(row)
                if state.draft_version != expected_version:
                    raise ValueError(
                        f"版本冲突：当前草稿为 v{state.draft_version}，"
                        f"提交依据为 v{expected_version}"
                    )
                if (
                    expected_fact_version is not None
                    and state.fact_version != expected_fact_version
                ):
                    raise ValueError("版本冲突：现场事实已变化，请刷新任务详情后重试")
                draft_version = state.draft_version + 1
                fact_version = state.fact_version
                latest_plan_id = state.latest_plan_id
                plan_stale = bool(state.latest_plan_id)
                task_version = state.task_version
            now = self._now()
            state = FieldStateV3(
                id=draft.field_id,
                mode=draft.mode,
                draft_version=draft_version,
                fact_version=fact_version,
                task_version=task_version,
                draft=draft,
                latest_plan_id=latest_plan_id,
                plan_stale=plan_stale,
                updated_at=now,
            )
            connection.execute(
                "INSERT INTO fields_v3 "
                "(id, mode, draft_version, fact_version, latest_plan_id, plan_stale, "
                "updated_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET mode=excluded.mode, "
                "draft_version=excluded.draft_version, updated_at=excluded.updated_at, "
                "plan_stale=excluded.plan_stale, payload=excluded.payload",
                (
                    state.id,
                    state.mode,
                    state.draft_version,
                    state.fact_version,
                    state.latest_plan_id,
                    int(state.plan_stale),
                    now.isoformat(),
                    self._json(state),
                ),
            )
            self._save_operation(connection, operation_id, "save_field_draft", request_hash, state)
            if proposal_id is not None:
                self._confirm_agent_proposal(connection, proposal_id, operation_id)
            return state

    def get_field(self, field_id: str) -> FieldStateV3:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM fields_v3 WHERE id = ?", (field_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"找不到地块 {field_id}")
        return FieldStateV3.model_validate_json(row["payload"])

    def list_fields(self, *, mode: str | None = None) -> list[FieldStateV3]:
        with self._connection() as connection:
            if mode is None:
                rows = connection.execute(
                    "SELECT payload FROM fields_v3 ORDER BY updated_at, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM fields_v3 WHERE mode = ? ORDER BY updated_at, id",
                    (mode,),
                ).fetchall()
        return [FieldStateV3.model_validate_json(row["payload"]) for row in rows]

    def save_ui_editor_draft(self, draft: UIEditorDraftV1, *, operation_id: str) -> UIEditorDraftV1:
        """Persist form input separately from facts, tasks, and adopted plans."""
        if not operation_id.strip():
            raise ValueError("保存界面草稿需要操作编号")
        saved = draft.model_copy(update={"updated_at": self._now()})
        request_hash = self._hash(draft.model_dump(mode="json", exclude={"updated_at"}))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                operation_id,
                "save_ui_editor_draft",
                request_hash,
                "UIEditorDraftV1",
            )
            if replay is not None:
                return UIEditorDraftV1.model_validate_json(replay)
            field = connection.execute(
                "SELECT payload FROM fields_v3 WHERE id = ?", (draft.field_id,)
            ).fetchone()
            if field is None:
                current_versions = (0, 0, 0)
            else:
                state = self._field_from_row(field)
                current_versions = (
                    state.draft_version,
                    state.fact_version,
                    state.task_version,
                )
            expected_versions = (
                draft.base_draft_version,
                draft.base_fact_version,
                draft.base_task_version,
            )
            if current_versions != expected_versions:
                raise ValueError("地块内容已更新；草稿已保留，请刷新后核对再保存")
            connection.execute(
                """INSERT INTO ui_editor_drafts_v1
                   (mode, field_id, form_kind, subject_id, base_draft_version,
                    base_fact_version, base_task_version, updated_at, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mode, field_id, form_kind, subject_id) DO UPDATE SET
                     base_draft_version=excluded.base_draft_version,
                     base_fact_version=excluded.base_fact_version,
                     base_task_version=excluded.base_task_version,
                     updated_at=excluded.updated_at, payload=excluded.payload""",
                (
                    saved.mode,
                    saved.field_id,
                    saved.form_kind,
                    saved.subject_id,
                    saved.base_draft_version,
                    saved.base_fact_version,
                    saved.base_task_version,
                    saved.updated_at.isoformat(),
                    self._json(saved),
                ),
            )
            self._save_operation(
                connection,
                operation_id,
                "save_ui_editor_draft",
                request_hash,
                saved,
            )
        return saved

    def ui_editor_draft(
        self, mode: str, field_id: str, form_kind: str, subject_id: str = ""
    ) -> UIEditorDraftV1 | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT payload FROM ui_editor_drafts_v1
                   WHERE mode=? AND field_id=? AND form_kind=? AND subject_id=?""",
                (mode, field_id, form_kind, subject_id),
            ).fetchone()
        return UIEditorDraftV1.model_validate_json(row["payload"]) if row else None

    def ui_editor_drafts(self, mode: str, field_id: str) -> list[UIEditorDraftV1]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT payload FROM ui_editor_drafts_v1
                   WHERE mode=? AND field_id=? ORDER BY updated_at DESC""",
                (mode, field_id),
            ).fetchall()
        return [UIEditorDraftV1.model_validate_json(row["payload"]) for row in rows]

    def latest_unsaved_profile_draft_field_id(self, mode: str) -> str | None:
        """Find the latest blank-field identity whose profile draft can be resumed."""
        with self._connection() as connection:
            row = connection.execute(
                """SELECT draft.field_id
                   FROM ui_editor_drafts_v1 AS draft
                   LEFT JOIN fields_v3 AS field
                     ON field.id = draft.field_id AND field.mode = draft.mode
                   WHERE draft.mode = ? AND draft.form_kind = 'profile' AND field.id IS NULL
                   ORDER BY draft.updated_at DESC
                   LIMIT 1""",
                (mode,),
            ).fetchone()
        return str(row["field_id"]) if row else None

    def save_task_series(
        self,
        series: TaskSeriesV4,
        *,
        expected_draft_version: int,
        expected_fact_version: int,
        expected_task_version: int | None = None,
        proposal_id: str | None = None,
        operation_id: str,
    ) -> TaskSeriesSaveResultV4:
        """Save a confirmed recurrence and its stable occurrence rows atomically."""
        if not operation_id.strip():
            raise ValueError("保存周期任务需要操作编号")
        instances = expand_task_series_all(series)
        request_payload = {
            "series": series.model_dump(mode="json"),
            "expected_draft_version": expected_draft_version,
            "expected_fact_version": expected_fact_version,
            "expected_task_version": expected_task_version,
        }
        request_hash = self._hash(request_payload)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                operation_id,
                "save_task_series",
                request_hash,
                "TaskSeriesSaveResultV4",
            )
            if replay is not None:
                return TaskSeriesSaveResultV4.model_validate_json(replay)
            row = connection.execute(
                "SELECT * FROM fields_v3 WHERE id=?", (series.field_id,)
            ).fetchone()
            if row is None:
                raise KeyError("找不到任务所属地块")
            state = self._field_from_row(row)
            if (state.draft_version, state.fact_version) != (
                expected_draft_version,
                expected_fact_version,
            ) or (
                expected_task_version is not None and state.task_version != expected_task_version
            ):
                raise ValueError("版本冲突：地块信息已变化，请刷新后重新保存任务")
            if connection.execute(
                "SELECT 1 FROM task_series_v4 WHERE id=?", (series.id,)
            ).fetchone():
                raise ValueError("周期编号已存在；修改后续任务请使用专门的周期编辑操作")
            if any(instance.field_id != series.field_id for instance in instances):
                raise ValueError("任务实例所属地块与周期不一致")
            created_at = self._now()
            connection.execute(
                "INSERT INTO task_series_v4 "
                "(id, field_id, created_at, payload) VALUES (?, ?, ?, ?)",
                (series.id, series.field_id, created_at.isoformat(), self._json(series)),
            )
            connection.executemany(
                "INSERT INTO task_instances_v4 "
                "(id, series_id, field_id, occurrence_number, occurrence_date, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        instance.id,
                        instance.series_id,
                        instance.field_id,
                        instance.occurrence_number,
                        instance.occurrence_date.isoformat(),
                        self._json(instance),
                    )
                    for instance in instances
                ],
            )
            if series.template.related_issue_id:
                self._link_issue_tasks(
                    connection,
                    series.field_id,
                    series.template.related_issue_id,
                    [instance.id for instance in instances],
                    created_at,
                )
            if proposal_id is not None:
                self._confirm_agent_proposal(connection, proposal_id, operation_id)
            updated_state = state.model_copy(
                update={
                    "task_version": state.task_version + 1,
                    "plan_stale": True,
                    "updated_at": created_at,
                }
            )
            connection.execute(
                "UPDATE fields_v3 SET plan_stale=1, updated_at=?, payload=? WHERE id=?",
                (created_at.isoformat(), self._json(updated_state), state.id),
            )
            result = TaskSeriesSaveResultV4(
                field_id=series.field_id,
                series_id=series.id,
                task_version=updated_state.task_version,
                created_instance_ids=[instance.id for instance in instances],
                created_at=created_at,
            )
            self._save_operation(connection, operation_id, "save_task_series", request_hash, result)
            return result

    def list_task_instances(
        self,
        field_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[TaskInstanceV4]:
        if (start_date is None) != (end_date is None):
            raise ValueError("任务实例查询需要同时提供起止日期")
        with self._connection() as connection:
            if start_date is None:
                rows = connection.execute(
                    "SELECT payload FROM task_instances_v4 "
                    "WHERE field_id=? ORDER BY occurrence_date, id",
                    (field_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM task_instances_v4 "
                    "WHERE field_id=? AND occurrence_date BETWEEN ? AND ? "
                    "ORDER BY occurrence_date, id",
                    (field_id, start_date.isoformat(), end_date.isoformat()),
                ).fetchall()
        return [TaskInstanceV4.model_validate_json(row["payload"]) for row in rows]

    def list_task_execution_groups(self, field_id: str) -> list[TaskExecutionGroupV4]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM task_execution_groups_v4 WHERE field_id=? ORDER BY id",
                (field_id,),
            ).fetchall()
        return [TaskExecutionGroupV4.model_validate_json(row["payload"]) for row in rows]

    def replay_task_execution_group_save(
        self,
        *,
        field_id: str,
        group_id: str,
        member_task_ids: list[str],
        execution_date: date,
        duration_minutes: int,
        expected_task_version: int,
        operation_id: str,
        action: str = "save_task_execution_group",
    ) -> TaskExecutionGroupSaveResultV4 | None:
        request_hash = self._hash(
            {
                "field_id": field_id,
                "group_id": group_id,
                "member_task_ids": member_task_ids,
                "execution_date": execution_date,
                "duration_minutes": duration_minutes,
                "expected_task_version": expected_task_version,
                "action": action,
            }
        )
        with self._connection() as connection:
            replay = self._replay(
                connection,
                operation_id,
                action,
                request_hash,
                "TaskExecutionGroupSaveResultV4",
            )
        return TaskExecutionGroupSaveResultV4.model_validate_json(replay) if replay else None

    def save_task_execution_group(
        self,
        group: TaskExecutionGroupV4,
        *,
        expected_task_version: int,
        operation_id: str,
        action: str = "save_task_execution_group",
    ) -> TaskExecutionGroupSaveResultV4:
        if not operation_id.strip():
            raise ValueError("保存合并出行需要操作编号")
        request_hash = self._hash(
            {
                "field_id": group.field_id,
                "group_id": group.id,
                "member_task_ids": group.member_task_ids,
                "execution_date": group.execution_date,
                "duration_minutes": group.duration_minutes,
                "expected_task_version": expected_task_version,
                "action": action,
            }
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                operation_id,
                action,
                request_hash,
                "TaskExecutionGroupSaveResultV4",
            )
            if replay is not None:
                return TaskExecutionGroupSaveResultV4.model_validate_json(replay)
            field_row = connection.execute(
                "SELECT * FROM fields_v3 WHERE id=?", (group.field_id,)
            ).fetchone()
            if field_row is None:
                raise KeyError("找不到合并出行所属地块")
            state = self._field_from_row(field_row)
            if state.task_version != expected_task_version:
                raise ValueError("版本冲突：任务已经变化，请刷新后重新确认合并出行")
            identity_row = connection.execute(
                "SELECT field_id FROM task_execution_groups_v4 WHERE id=?", (group.id,)
            ).fetchone()
            if identity_row is not None and identity_row["field_id"] != group.field_id:
                raise ValueError("合并出行编号已被其他地块使用")
            previous_row = connection.execute(
                "SELECT payload FROM task_execution_groups_v4 WHERE id=? AND field_id=?",
                (group.id, group.field_id),
            ).fetchone()
            previous = (
                TaskExecutionGroupV4.model_validate_json(previous_row["payload"])
                if previous_row
                else None
            )
            if previous is None and group.version != 1:
                raise ValueError("新合并出行的版本必须从1开始")
            if previous is not None and group.version != previous.version + 1:
                raise ValueError("合并出行版本冲突，请重新读取当前分组")
            existing_rows = connection.execute(
                "SELECT id, payload FROM task_execution_groups_v4 WHERE field_id=? AND id<>?",
                (group.field_id, group.id),
            ).fetchall()
            for row in existing_rows:
                existing = TaskExecutionGroupV4.model_validate_json(row["payload"])
                if existing.status != "cancelled" and group.status != "cancelled":
                    if set(existing.member_task_ids).intersection(group.member_task_ids):
                        raise ValueError("任务已属于另一合并出行组")
            now = self._now()
            next_task_version = state.task_version + 1
            if previous is not None:
                connection.execute(
                    "INSERT INTO task_execution_group_revisions_v4 "
                    "(revision_id, group_id, task_version, updated_at, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        f"{previous.id}:v{next_task_version}",
                        previous.id,
                        next_task_version,
                        now.isoformat(),
                        self._json(previous),
                    ),
                )
            connection.execute(
                "INSERT INTO task_execution_groups_v4 (id, field_id, payload) "
                "VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (group.id, group.field_id, self._json(group)),
            )
            updated_state = state.model_copy(
                update={
                    "task_version": next_task_version,
                    "plan_stale": bool(state.latest_plan_id),
                    "updated_at": now,
                }
            )
            connection.execute(
                "UPDATE fields_v3 SET plan_stale=?, updated_at=?, payload=? WHERE id=?",
                (
                    int(updated_state.plan_stale),
                    now.isoformat(),
                    self._json(updated_state),
                    group.field_id,
                ),
            )
            result = TaskExecutionGroupSaveResultV4(
                field_id=group.field_id,
                group_id=group.id,
                group_version=group.version,
                task_version=next_task_version,
                updated_at=now,
            )
            self._save_operation(
                connection,
                operation_id,
                action,
                request_hash,
                result,
            )
            return result

    def list_task_series(self, field_id: str) -> list[TaskSeriesV4]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM task_series_v4 WHERE field_id=? ORDER BY created_at, id",
                (field_id,),
            ).fetchall()
        return [TaskSeriesV4.model_validate_json(row["payload"]) for row in rows]

    def list_field_issues(self, field_id: str) -> list[FieldIssueV4]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM field_issues_v4 WHERE field_id=? ORDER BY opened_at, id",
                (field_id,),
            ).fetchall()
        return [FieldIssueV4.model_validate_json(row["payload"]) for row in rows]

    def list_field_issue_events(self, field_id: str) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM field_issue_events_v4 WHERE field_id=? ORDER BY rowid",
                (field_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_task_series(self, field_id: str, series_id: str) -> TaskSeriesV4:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM task_series_v4 WHERE id=? AND field_id=?",
                (series_id, field_id),
            ).fetchone()
        if row is None:
            raise KeyError("找不到任务周期")
        return TaskSeriesV4.model_validate_json(row["payload"])

    def update_task_series(
        self,
        series: TaskSeriesV4,
        instances: list[TaskInstanceV4],
        *,
        blocked_instance_ids: set[str],
        expected_draft_version: int,
        expected_fact_version: int,
        expected_task_version: int,
        operation_id: str,
    ) -> TaskSeriesUpdateResultV4:
        """Revise only open occurrences; keep completed or observed snapshots unchanged."""
        if not operation_id.strip():
            raise ValueError("修改任务周期需要操作编号")
        if any(
            instance.series_id != series.id or instance.field_id != series.field_id
            for instance in instances
        ):
            raise ValueError("任务实例与周期编号或地块不一致")
        request_hash = self._hash(
            {
                "series": series.model_dump(mode="json"),
                "instances": [instance.model_dump(mode="json") for instance in instances],
                "blocked_instance_ids": sorted(blocked_instance_ids),
                "expected_draft_version": expected_draft_version,
                "expected_fact_version": expected_fact_version,
                "expected_task_version": expected_task_version,
            }
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                operation_id,
                "update_task_series",
                request_hash,
                "TaskSeriesUpdateResultV4",
            )
            if replay is not None:
                return TaskSeriesUpdateResultV4.model_validate_json(replay)
            field_row = connection.execute(
                "SELECT * FROM fields_v3 WHERE id=?", (series.field_id,)
            ).fetchone()
            if field_row is None:
                raise KeyError("找不到周期所属地块")
            state = self._field_from_row(field_row)
            if (state.draft_version, state.fact_version, state.task_version) != (
                expected_draft_version,
                expected_fact_version,
                expected_task_version,
            ):
                raise ValueError("版本冲突：地块或任务已变化，请刷新后重新修改周期")
            series_row = connection.execute(
                "SELECT payload FROM task_series_v4 WHERE id=? AND field_id=?",
                (series.id, series.field_id),
            ).fetchone()
            if series_row is None:
                raise KeyError("找不到要修改的任务周期")
            old_series = TaskSeriesV4.model_validate_json(series_row["payload"])
            if old_series.id != series.id or old_series.field_id != series.field_id:
                raise ValueError("周期身份不可修改")
            old_rows = connection.execute(
                "SELECT id, payload FROM task_instances_v4 "
                "WHERE series_id=? ORDER BY occurrence_number",
                (series.id,),
            ).fetchall()
            existing = {
                row["id"]: TaskInstanceV4.model_validate_json(row["payload"]) for row in old_rows
            }
            new_by_number = {instance.occurrence_number: instance for instance in instances}
            blocked_instance_ids = set(blocked_instance_ids)
            created_ids: list[str] = []
            updated_ids: list[str] = []
            superseded_ids: list[str] = []
            now = self._now()
            next_version = state.task_version + 1
            connection.execute(
                "INSERT INTO task_series_revisions_v4 "
                "(revision_id, series_id, task_version, updated_at, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    f"{series.id}:v{next_version}",
                    series.id,
                    next_version,
                    now.isoformat(),
                    self._json(old_series),
                ),
            )
            for old in existing.values():
                if old.id in blocked_instance_ids:
                    continue
                replacement = new_by_number.get(old.occurrence_number)
                if replacement is None:
                    replacement = old.model_copy(
                        update={"status": "superseded", "missing_fields": []}
                    )
                    superseded_ids.append(old.id)
                else:
                    updated_ids.append(old.id)
                connection.execute(
                    "INSERT INTO task_instance_revisions_v4 "
                    "(revision_id, instance_id, task_version, updated_at, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        f"{old.id}:v{next_version}",
                        old.id,
                        next_version,
                        now.isoformat(),
                        self._json(old),
                    ),
                )
                connection.execute(
                    "UPDATE task_instances_v4 SET occurrence_date=?, payload=? WHERE id=?",
                    (replacement.occurrence_date.isoformat(), self._json(replacement), old.id),
                )
            existing_numbers = {instance.occurrence_number for instance in existing.values()}
            for instance in instances:
                if instance.occurrence_number in existing_numbers:
                    continue
                connection.execute(
                    "INSERT INTO task_instances_v4 "
                    "(id, series_id, field_id, occurrence_number, occurrence_date, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        instance.id,
                        instance.series_id,
                        instance.field_id,
                        instance.occurrence_number,
                        instance.occurrence_date.isoformat(),
                        self._json(instance),
                    ),
                )
                created_ids.append(instance.id)
            if series.template.related_issue_id:
                self._link_issue_tasks(
                    connection,
                    series.field_id,
                    series.template.related_issue_id,
                    [*updated_ids, *created_ids],
                    now,
                )
            connection.execute(
                "UPDATE task_series_v4 SET payload=? WHERE id=?",
                (self._json(series), series.id),
            )
            updated_state = state.model_copy(
                update={"task_version": next_version, "plan_stale": True, "updated_at": now}
            )
            connection.execute(
                "UPDATE fields_v3 SET plan_stale=1, updated_at=?, payload=? WHERE id=?",
                (now.isoformat(), self._json(updated_state), series.field_id),
            )
            result = TaskSeriesUpdateResultV4(
                field_id=series.field_id,
                series_id=series.id,
                task_version=next_version,
                updated_instance_ids=updated_ids,
                created_instance_ids=created_ids,
                superseded_instance_ids=superseded_ids,
                updated_at=now,
            )
            self._save_operation(
                connection, operation_id, "update_task_series", request_hash, result
            )
            return result

    def get_task_instance(self, field_id: str, task_id: str) -> TaskInstanceV4:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM task_instances_v4 WHERE id=? AND field_id=?",
                (task_id, field_id),
            ).fetchone()
        if row is None:
            raise KeyError("找不到任务实例")
        return TaskInstanceV4.model_validate_json(row["payload"])

    def update_task_instance(
        self,
        instance: TaskInstanceV4,
        *,
        expected_task_version: int,
        operation_id: str,
    ) -> TaskInstanceUpdateResultV4:
        if not operation_id.strip():
            raise ValueError("修改任务需要操作编号")
        request_hash = self._hash(
            {
                "instance": instance.model_dump(mode="json"),
                "expected_task_version": expected_task_version,
            }
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                operation_id,
                "update_task_instance",
                request_hash,
                "TaskInstanceUpdateResultV4",
            )
            if replay is not None:
                return TaskInstanceUpdateResultV4.model_validate_json(replay)
            field_row = connection.execute(
                "SELECT * FROM fields_v3 WHERE id=?", (instance.field_id,)
            ).fetchone()
            if field_row is None:
                raise KeyError("找不到任务所属地块")
            state = self._field_from_row(field_row)
            if state.task_version != expected_task_version:
                raise ValueError("任务版本冲突，请刷新后重新修改")
            old_row = connection.execute(
                "SELECT payload FROM task_instances_v4 WHERE id=? AND field_id=?",
                (instance.id, instance.field_id),
            ).fetchone()
            if old_row is None:
                raise KeyError("找不到任务实例")
            old = TaskInstanceV4.model_validate_json(old_row["payload"])
            if (
                old.series_id != instance.series_id
                or old.occurrence_number != instance.occurrence_number
            ):
                raise ValueError("任务实例身份不可修改")
            now = self._now()
            next_version = state.task_version + 1
            connection.execute(
                "INSERT INTO task_instance_revisions_v4 "
                "(revision_id, instance_id, task_version, updated_at, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    f"{instance.id}:v{next_version}",
                    instance.id,
                    next_version,
                    now.isoformat(),
                    self._json(old),
                ),
            )
            connection.execute(
                "UPDATE task_instances_v4 SET occurrence_date=?, payload=? WHERE id=?",
                (instance.occurrence_date.isoformat(), self._json(instance), instance.id),
            )
            updated_state = state.model_copy(
                update={"task_version": next_version, "plan_stale": True, "updated_at": now}
            )
            connection.execute(
                "UPDATE fields_v3 SET plan_stale=1, updated_at=?, payload=? WHERE id=?",
                (now.isoformat(), self._json(updated_state), instance.field_id),
            )
            result = TaskInstanceUpdateResultV4(
                field_id=instance.field_id,
                task_id=instance.id,
                task_version=next_version,
                instance=instance,
                updated_at=now,
            )
            self._save_operation(
                connection, operation_id, "update_task_instance", request_hash, result
            )
            return result

    def commit_events(
        self,
        field_id: str,
        events: list[FactEventInputV3],
        *,
        expected_fact_version: int,
        operation_id: str,
        proposal_id: str | None = None,
        expected_draft_version: int | None = None,
        expected_task_version: int | None = None,
    ) -> EventCommitResultV3:
        if not events:
            raise ValueError("至少需要一个已确认事实")
        request_payload = {
            "field_id": field_id,
            "events": [event.model_dump(mode="json") for event in events],
            "expected_fact_version": expected_fact_version,
        }
        if expected_draft_version is not None:
            request_payload["expected_draft_version"] = expected_draft_version
        # Task version is an execution-time concurrency guard, not operation identity.
        # Keeping it out of the hash lets a replay use the original event payload after
        # the service has already committed and no longer derives the same current token.
        request_hash = self._hash(request_payload)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                operation_id,
                "commit_events",
                request_hash,
                "EventCommitResultV3",
            )
            if replay is not None:
                result = EventCommitResultV3.model_validate_json(replay)
                return result.model_copy(update={"replayed": True})
            row = connection.execute("SELECT * FROM fields_v3 WHERE id = ?", (field_id,)).fetchone()
            if row is None:
                raise KeyError(f"找不到地块 {field_id}")
            state = self._field_from_row(row)
            if state.fact_version != expected_fact_version:
                raise ValueError(
                    f"版本冲突：现场事实为 v{state.fact_version}，"
                    f"提交依据为 v{expected_fact_version}"
                )
            if expected_draft_version is not None and state.draft_version != expected_draft_version:
                raise ValueError("版本冲突：地块档案已变化，请刷新任务详情后重试")
            if expected_task_version is not None and state.task_version != expected_task_version:
                raise ValueError("任务版本已变化，请刷新任务后重试")
            next_version = state.fact_version + 1
            now = self._now()
            saved_events: list[FactEventV3] = []
            completed_in_batch = {
                event.feedback.task_id
                for event in events
                if event.feedback and event.feedback.kind == "completed" and event.feedback.task_id
            }
            for event in events:
                if event.feedback and event.feedback.plot_id not in {None, field_id}:
                    raise ValueError("反馈中的地块编号与当前地块不一致")
                if event.feedback and event.feedback.kind == "task_result":
                    feedback = event.feedback
                    if feedback.issue_ids:
                        raise ValueError("问题编号由系统生成，不能由提交内容指定")
                    if feedback.resolved_issue_id:
                        if feedback.task_id not in completed_in_batch:
                            raise ValueError("关闭问题需要同时确认复查任务已完成")
                        if any(value != "normal" for value in feedback.result_values.values()):
                            raise ValueError("本次复查仍发现问题，不能关闭问题记录")
                        task_row = connection.execute(
                            "SELECT payload FROM task_instances_v4 WHERE id=? AND field_id=?",
                            (feedback.task_id, field_id),
                        ).fetchone()
                        issue_row = connection.execute(
                            "SELECT payload FROM field_issues_v4 WHERE id=? AND field_id=?",
                            (feedback.resolved_issue_id, field_id),
                        ).fetchone()
                        if task_row is None or issue_row is None:
                            raise ValueError("找不到关联的复查任务或问题记录")
                        task = TaskInstanceV4.model_validate_json(task_row["payload"])
                        issue = FieldIssueV4.model_validate_json(issue_row["payload"])
                        if (
                            task.template.task_type != "follow_up"
                            or task.template.related_issue_id != issue.id
                            or issue.status != "open"
                        ):
                            raise ValueError("只有关联该未解决问题的复查任务可以关闭问题")
                if (
                    event.feedback
                    and event.feedback.task_id
                    and not event.feedback.task_id.startswith(f"{field_id}-")
                    and connection.execute(
                        "SELECT 1 FROM task_instances_v4 WHERE id=? AND field_id=?",
                        (event.feedback.task_id, field_id),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("任务编号不属于当前地块")
                if event.correction_of:
                    target = connection.execute(
                        "SELECT payload FROM fact_events_v3 WHERE event_id = ? AND field_id = ?",
                        (event.correction_of, field_id),
                    ).fetchone()
                    if target is None:
                        raise ValueError("更正引用的原事件不存在")
                saved_feedback = event.feedback
                if saved_feedback and saved_feedback.kind == "task_result":
                    generated_issue_ids: list[str] = []
                    for issue_key, value in saved_feedback.result_values.items():
                        if value != "issue":
                            continue
                        open_row = connection.execute(
                            "SELECT payload FROM field_issues_v4 "
                            "WHERE field_id=? AND issue_key=? AND status='open' "
                            "ORDER BY opened_at DESC LIMIT 1",
                            (field_id, issue_key),
                        ).fetchone()
                        if open_row is not None:
                            issue = FieldIssueV4.model_validate_json(open_row["payload"])
                            issue = issue.model_copy(
                                update={
                                    "source_event_ids": list(
                                        dict.fromkeys([*issue.source_event_ids, event.event_id])
                                    ),
                                    "related_task_ids": list(
                                        dict.fromkeys(
                                            [*issue.related_task_ids, saved_feedback.task_id or ""]
                                        )
                                    ),
                                    "description": "；".join(
                                        part
                                        for part in dict.fromkeys(
                                            [issue.description, saved_feedback.result_notes]
                                        )
                                        if part
                                    ),
                                }
                            )
                            event_type = "observation_added"
                        else:
                            previous_row = connection.execute(
                                "SELECT payload FROM field_issues_v4 "
                                "WHERE field_id=? AND issue_key=? AND status='resolved' "
                                "ORDER BY opened_at DESC LIMIT 1",
                                (field_id, issue_key),
                            ).fetchone()
                            previous_issue = (
                                FieldIssueV4.model_validate_json(previous_row["payload"])
                                if previous_row
                                else None
                            )
                            issue = FieldIssueV4(
                                id=str(uuid5(NAMESPACE_URL, f"{event.event_id}:{issue_key}")),
                                field_id=field_id,
                                issue_key=issue_key,
                                title=issue_key,
                                description=saved_feedback.result_notes,
                                source_task_id=saved_feedback.task_id or "",
                                source_event_ids=[event.event_id],
                                related_task_ids=[saved_feedback.task_id or ""],
                                repeat_of=previous_issue.id if previous_issue else None,
                                opened_at=saved_feedback.at,
                            )
                            connection.execute(
                                "INSERT INTO field_issues_v4 "
                                "(id, field_id, issue_key, status, opened_at, payload) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    issue.id,
                                    field_id,
                                    issue.issue_key,
                                    issue.status,
                                    issue.opened_at.isoformat(),
                                    self._json(issue),
                                ),
                            )
                            event_type = "opened"
                        if open_row is not None:
                            connection.execute(
                                "UPDATE field_issues_v4 SET payload=? WHERE id=?",
                                (self._json(issue), issue.id),
                            )
                        issue_event = {
                            "id": str(uuid4()),
                            "type": event_type,
                            "issue_id": issue.id,
                            "source_event_id": event.event_id,
                            "task_id": saved_feedback.task_id,
                            "at": saved_feedback.at.isoformat(),
                            "description": saved_feedback.result_notes,
                        }
                        connection.execute(
                            "INSERT INTO field_issue_events_v4 "
                            "(event_id, issue_id, field_id, created_at, payload) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                issue_event["id"],
                                issue.id,
                                field_id,
                                saved_feedback.at.isoformat(),
                                self._json(issue_event),
                            ),
                        )
                        generated_issue_ids.append(issue.id)
                    if saved_feedback.resolved_issue_id:
                        row = connection.execute(
                            "SELECT payload FROM field_issues_v4 WHERE id=? AND field_id=?",
                            (saved_feedback.resolved_issue_id, field_id),
                        ).fetchone()
                        if row is None:
                            raise ValueError("找不到待关闭的问题记录")
                        issue = FieldIssueV4.model_validate_json(row["payload"])
                        resolved_at = saved_feedback.at
                        issue = issue.model_copy(
                            update={
                                "status": "resolved",
                                "resolved_at": resolved_at,
                                "related_task_ids": list(
                                    dict.fromkeys(
                                        [*issue.related_task_ids, saved_feedback.task_id or ""]
                                    )
                                ),
                            }
                        )
                        connection.execute(
                            "UPDATE field_issues_v4 SET status='resolved', payload=? WHERE id=?",
                            (self._json(issue), issue.id),
                        )
                        issue_event = {
                            "id": str(uuid4()),
                            "type": "resolved",
                            "issue_id": issue.id,
                            "source_event_id": event.event_id,
                            "task_id": saved_feedback.task_id,
                            "at": resolved_at.isoformat(),
                            "description": "关联复查任务确认问题已解决",
                        }
                        connection.execute(
                            "INSERT INTO field_issue_events_v4 "
                            "(event_id, issue_id, field_id, created_at, payload) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                issue_event["id"],
                                issue.id,
                                field_id,
                                resolved_at.isoformat(),
                                self._json(issue_event),
                            ),
                        )
                    if generated_issue_ids:
                        saved_feedback = saved_feedback.model_copy(
                            update={"issue_ids": list(dict.fromkeys(generated_issue_ids))}
                        )
                saved = FactEventV3(
                    event_id=event.event_id,
                    feedback=saved_feedback,
                    source=event.source,
                    correction_of=event.correction_of,
                    retracts=event.retracts,
                    field_id=field_id,
                    fact_version=next_version,
                    created_at=now,
                )
                try:
                    connection.execute(
                        "INSERT INTO fact_events_v3 "
                        "(event_id, field_id, fact_version, created_at, payload) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            saved.event_id,
                            field_id,
                            saved.fact_version,
                            now.isoformat(),
                            self._json(saved),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise ValueError("事件编号已存在，不能重复记录") from error
                saved_events.append(saved)
            updated = state.model_copy(
                update={
                    "fact_version": next_version,
                    "plan_stale": bool(state.latest_plan_id),
                    "updated_at": now,
                }
            )
            connection.execute(
                "UPDATE fields_v3 SET fact_version=?, plan_stale=?, updated_at=?, "
                "payload=? WHERE id=?",
                (
                    updated.fact_version,
                    int(updated.plan_stale),
                    now.isoformat(),
                    self._json(updated),
                    field_id,
                ),
            )
            result = EventCommitResultV3(
                field_id=field_id,
                fact_version=next_version,
                events=saved_events,
                plan_stale=updated.plan_stale,
            )
            self._save_operation(connection, operation_id, "commit_events", request_hash, result)
            if proposal_id is not None:
                self._confirm_agent_proposal(connection, proposal_id, operation_id)
            return result

    def list_events(self, field_id: str) -> list[FactEventV3]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM fact_events_v3 WHERE field_id = ? ORDER BY rowid",
                (field_id,),
            ).fetchall()
        return [FactEventV3.model_validate_json(row["payload"]) for row in rows]

    def save_preview(self, preview: PlanPreviewV3, *, operation_id: str) -> PlanPreviewV3:
        signature = {
            "field_id": preview.field_id,
            "draft_version": preview.expected_draft_version,
            "fact_version": preview.expected_fact_version,
            "plan_id": preview.expected_plan_id,
        }
        if preview.expected_task_version:
            signature["task_version"] = preview.expected_task_version
        if preview.version.plan.reference_version is not None:
            signature["reference_version"] = preview.version.plan.reference_version
        request_hash = self._hash(signature)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                operation_id,
                "preview_plan",
                request_hash,
                "PlanPreviewV3",
            )
            if replay is not None:
                return PlanPreviewV3.model_validate_json(replay)
            row = connection.execute(
                "SELECT * FROM fields_v3 WHERE id = ?", (preview.field_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"找不到地块 {preview.field_id}")
            state = self._field_from_row(row)
            if (
                state.draft_version,
                state.fact_version,
                state.task_version,
                state.latest_plan_id,
            ) != (
                preview.expected_draft_version,
                preview.expected_fact_version,
                preview.expected_task_version,
                preview.expected_plan_id,
            ):
                raise ValueError("预览依据已变化，请重新计算")
            connection.execute(
                "INSERT INTO plan_previews_v3 (id, field_id, created_at, status, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    preview.id,
                    preview.field_id,
                    preview.created_at.isoformat(),
                    preview.status,
                    self._json(preview),
                ),
            )
            self._save_operation(connection, operation_id, "preview_plan", request_hash, preview)
            return preview

    def load_preview(self, preview_id: str) -> PlanPreviewV3:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM plan_previews_v3 WHERE id = ?", (preview_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"找不到计划预览 {preview_id}")
        return PlanPreviewV3.model_validate_json(row["payload"])

    def latest_actionable_preview(self, field_id: str) -> PlanPreviewV3 | None:
        """Read the newest unconsumed preview whose input versions are current."""
        with self._connection() as connection:
            field = connection.execute(
                "SELECT payload FROM fields_v3 WHERE id = ?", (field_id,)
            ).fetchone()
            if field is None:
                raise KeyError(f"找不到地块 {field_id}")
            state = FieldStateV3.model_validate_json(field["payload"])
            row = connection.execute(
                "SELECT payload FROM plan_previews_v3 WHERE field_id=? ORDER BY rowid DESC LIMIT 1",
                (field_id,),
            ).fetchone()
        if row:
            preview = PlanPreviewV3.model_validate_json(row["payload"])
            if (
                preview.status == "preview"
                and preview.expected_draft_version == state.draft_version
                and preview.expected_fact_version == state.fact_version
                and preview.expected_task_version == state.task_version
                and preview.expected_plan_id == state.latest_plan_id
            ):
                return preview
        return None

    def cancel_preview(self, preview_id: str) -> PlanPreviewV3:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM plan_previews_v3 WHERE id = ?", (preview_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"找不到计划预览 {preview_id}")
            preview = PlanPreviewV3.model_validate_json(row["payload"])
            if preview.status == "adopted":
                raise ValueError("已采用的计划不能取消")
            cancelled = preview.model_copy(update={"status": "cancelled"})
            connection.execute(
                "UPDATE plan_previews_v3 SET status='cancelled', payload=? WHERE id=?",
                (self._json(cancelled), preview_id),
            )
            return cancelled

    def mark_preview_stale(self, preview_id: str) -> PlanPreviewV3:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM plan_previews_v3 WHERE id = ?", (preview_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"找不到计划预览 {preview_id}")
            preview = PlanPreviewV3.model_validate_json(row["payload"])
            if preview.status == "preview":
                preview = preview.model_copy(update={"status": "stale"})
                connection.execute(
                    "UPDATE plan_previews_v3 SET status='stale', payload=? WHERE id=?",
                    (self._json(preview), preview_id),
                )
            return preview

    def adopt_plan(
        self,
        preview_id: str,
        *,
        expected_fact_version: int,
        expected_plan_id: str | None,
        operation_id: str,
    ) -> PlanAdoptionResultV3:
        request_hash = self._hash(
            {
                "preview_id": preview_id,
                "expected_fact_version": expected_fact_version,
                "expected_plan_id": expected_plan_id,
            }
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                operation_id,
                "adopt_plan",
                request_hash,
                "PlanAdoptionResultV3",
            )
            if replay is not None:
                result = PlanAdoptionResultV3.model_validate_json(replay)
                return result.model_copy(update={"replayed": True})
            preview_row = connection.execute(
                "SELECT payload FROM plan_previews_v3 WHERE id = ?", (preview_id,)
            ).fetchone()
            if preview_row is None:
                raise KeyError(f"找不到计划预览 {preview_id}")
            preview = PlanPreviewV3.model_validate_json(preview_row["payload"])
            field_row = connection.execute(
                "SELECT payload FROM fields_v3 WHERE id = ?", (preview.field_id,)
            ).fetchone()
            state = FieldStateV3.model_validate_json(field_row["payload"])
            if preview.status != "preview":
                raise ValueError("该预览已取消或已处理")
            if (
                state.fact_version != expected_fact_version
                or state.fact_version != preview.expected_fact_version
                or state.task_version != preview.expected_task_version
                or preview.version.task_version != state.task_version
                or state.draft_version != preview.expected_draft_version
                or state.latest_plan_id != expected_plan_id
                or preview.expected_plan_id != expected_plan_id
            ):
                raise ValueError("预览已过期，请重新计算")
            current = self._load_current_plan(connection, preview.field_id)
            if current and current.id != expected_plan_id:
                raise ValueError("计划版本冲突，请重新预览")
            has_changes = bool(preview.version.changes) and any(
                change.kind != "unchanged" for change in preview.version.changes
            )
            if current and not has_changes:
                if state.plan_stale:
                    updated_state = state.model_copy(
                        update={"plan_stale": False, "updated_at": self._now()}
                    )
                    connection.execute(
                        "UPDATE fields_v3 SET plan_stale=0, updated_at=?, payload=? WHERE id=?",
                        (
                            updated_state.updated_at.isoformat(),
                            self._json(updated_state),
                            state.id,
                        ),
                    )
                result = PlanAdoptionResultV3(version=current, created_new_version=False)
            else:
                version = preview.version.model_copy(
                    update={"parent_id": current.id if current else None}
                )
                connection.execute(
                    "INSERT INTO plan_versions_v3 (id, field_id, parent_id, created_at, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        version.id,
                        version.field_id,
                        version.parent_id,
                        version.created_at.isoformat(),
                        self._json(version),
                    ),
                )
                updated_state = state.model_copy(
                    update={
                        "latest_plan_id": version.id,
                        "plan_stale": False,
                        "updated_at": self._now(),
                    }
                )
                connection.execute(
                    "UPDATE fields_v3 SET latest_plan_id=?, plan_stale=0, updated_at=?, payload=? "
                    "WHERE id=?",
                    (
                        version.id,
                        updated_state.updated_at.isoformat(),
                        self._json(updated_state),
                        state.id,
                    ),
                )
                result = PlanAdoptionResultV3(version=version, created_new_version=True)
            adopted_preview = preview.model_copy(update={"status": "adopted"})
            connection.execute(
                "UPDATE plan_previews_v3 SET status='adopted', payload=? WHERE id=?",
                (self._json(adopted_preview), preview_id),
            )
            self._save_operation(connection, operation_id, "adopt_plan", request_hash, result)
            return result

    def replay_adoption(
        self,
        preview_id: str,
        *,
        expected_fact_version: int,
        expected_plan_id: str | None,
        operation_id: str,
    ) -> PlanAdoptionResultV3 | None:
        request_hash = self._hash(
            {
                "preview_id": preview_id,
                "expected_fact_version": expected_fact_version,
                "expected_plan_id": expected_plan_id,
            }
        )
        with self._connection() as connection:
            replay = self._replay(
                connection,
                operation_id,
                "adopt_plan",
                request_hash,
                "PlanAdoptionResultV3",
            )
        if replay is None:
            return None
        return PlanAdoptionResultV3.model_validate_json(replay).model_copy(
            update={"replayed": True}
        )

    @staticmethod
    def _load_current_plan(connection: sqlite3.Connection, field_id: str) -> PlanVersionV3 | None:
        row = connection.execute(
            "SELECT versions.payload FROM fields_v3 AS fields "
            "JOIN plan_versions_v3 AS versions ON versions.id = fields.latest_plan_id "
            "WHERE fields.id = ?",
            (field_id,),
        ).fetchone()
        return PlanVersionV3.model_validate_json(row["payload"]) if row else None

    def get_current_plan(self, field_id: str) -> PlanVersionV3 | None:
        with self._connection() as connection:
            return self._load_current_plan(connection, field_id)

    def plan_history(self, field_id: str) -> list[PlanVersionV3]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM plan_versions_v3 WHERE field_id=? ORDER BY rowid",
                (field_id,),
            ).fetchall()
        return [PlanVersionV3.model_validate_json(row["payload"]) for row in rows]

    def save_rule_adoption(self, record: RuleAdoptionRecordV3) -> None:
        with self._connection() as connection, connection:
            connection.execute(
                "INSERT INTO rule_adoptions_v3 (id, field_id, rule_id, reviewed_at, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.field_id,
                    record.rule_id,
                    record.reviewed_at.isoformat(),
                    self._json(record),
                ),
            )

    def list_rule_adoptions(self, field_id: str) -> list[RuleAdoptionRecordV3]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM rule_adoptions_v3 WHERE field_id=? ORDER BY rowid",
                (field_id,),
            ).fetchall()
        return [RuleAdoptionRecordV3.model_validate_json(row["payload"]) for row in rows]

    def save_weather_snapshot(self, snapshot: WeatherSnapshotV3) -> None:
        with self._connection() as connection, connection:
            connection.execute(
                "INSERT INTO weather_snapshots_v3 "
                "(id, field_id, fetched_at, coverage_start, coverage_end, latitude, "
                "longitude, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.id,
                    snapshot.field_id,
                    snapshot.fetched_at.isoformat(),
                    snapshot.coverage_start.isoformat(),
                    snapshot.coverage_end.isoformat(),
                    snapshot.requested_latitude,
                    snapshot.requested_longitude,
                    self._json(snapshot),
                ),
            )

    def cached_weather(
        self,
        field_id: str,
        *,
        latitude: float,
        longitude: float,
        start_date: date,
        end_date: date,
        now: datetime,
    ) -> WeatherSnapshotV3 | None:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM weather_snapshots_v3 WHERE field_id=? "
                "AND latitude=? AND longitude=? AND coverage_start<=? AND coverage_end>=? "
                "ORDER BY fetched_at DESC",
                (field_id, latitude, longitude, start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        for row in rows:
            snapshot = WeatherSnapshotV3.model_validate_json(row["payload"])
            if snapshot.cache_expires_at > now:
                return snapshot
        return None

    def latest_weather(self, field_id: str) -> WeatherSnapshotV3 | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM weather_snapshots_v3 WHERE field_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (field_id,),
            ).fetchone()
        return WeatherSnapshotV3.model_validate_json(row["payload"]) if row else None

    def get_weather_snapshot(self, snapshot_id: str) -> WeatherSnapshotV3:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM weather_snapshots_v3 WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"找不到天气快照 {snapshot_id}")
        return WeatherSnapshotV3.model_validate_json(row["payload"])

    def save_agent_run(
        self, run: AgentRunV3, proposals: list[AgentProposalV3] | None = None
    ) -> None:
        with self._connection() as connection, connection:
            connection.execute(
                "INSERT INTO agent_runs_v3 (id, field_id, started_at, payload) VALUES (?, ?, ?, ?)",
                (run.id, run.field_id, run.started_at.isoformat(), self._json(run)),
            )
            for proposal in proposals or []:
                record = AgentProposalRecordV3(
                    proposal=proposal, run_id=run.id, proposed_at=run.started_at
                )
                connection.execute(
                    "INSERT INTO agent_proposals_v3 (id, field_id, run_id, status, payload) "
                    "VALUES (?, ?, ?, 'pending', ?)",
                    (proposal.id, proposal.field_id, run.id, self._json(record)),
                )

    def agent_proposal(self, proposal_id: str) -> AgentProposalRecordV3:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM agent_proposals_v3 WHERE id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"找不到待确认修改 {proposal_id}")
        return AgentProposalRecordV3.model_validate_json(row["payload"])

    def agent_proposals(self, field_id: str) -> list[AgentProposalRecordV3]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_proposals_v3 WHERE field_id=? ORDER BY rowid DESC",
                (field_id,),
            ).fetchall()
        return [AgentProposalRecordV3.model_validate_json(row["payload"]) for row in rows]

    @staticmethod
    def _confirm_agent_proposal(
        connection: sqlite3.Connection, proposal_id: str, operation_id: str
    ) -> None:
        row = connection.execute(
            "SELECT payload FROM agent_proposals_v3 WHERE id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"找不到待确认修改 {proposal_id}")
        record = AgentProposalRecordV3.model_validate_json(row["payload"])
        if record.status != "pending":
            raise ValueError("修改提案已处理；请刷新状态")
        updated = record.model_copy(
            update={"status": "confirmed", "confirmed_operation_id": operation_id}
        )
        connection.execute(
            "UPDATE agent_proposals_v3 SET status='confirmed', payload=? WHERE id=?",
            (WorkbenchStoreV3._json(updated), proposal_id),
        )

    def update_agent_proposal(self, record: AgentProposalRecordV3) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM agent_proposals_v3 WHERE id=?",
                (record.proposal.id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"找不到待确认修改 {record.proposal.id}")
            old = AgentProposalRecordV3.model_validate_json(row["payload"])
            if old.status != record.status and not (
                old.status == "pending" and record.status == "ignored"
            ):
                raise ValueError("提案状态已变化，请刷新后重试")
            connection.execute(
                "UPDATE agent_proposals_v3 SET status=?, payload=? WHERE id=?",
                (record.status, self._json(record), record.proposal.id),
            )

    def list_agent_runs(self, field_id: str) -> list[AgentRunV3]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_runs_v3 WHERE field_id=? ORDER BY rowid",
                (field_id,),
            ).fetchall()
        return [AgentRunV3.model_validate_json(row["payload"]) for row in rows]
