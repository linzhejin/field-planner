"""Append-only SQLite records for the v3 workbench."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from nongshi.schema_v3 import (
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
    WeatherSnapshotV3,
)

T = TypeVar("T", bound=BaseModel)


class WorkbenchStoreV3:
    """Persist drafts, confirmed facts, previews and adopted snapshots separately."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS operations_v3 (
                    operation_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    result_type TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );"""
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
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

    @staticmethod
    def _field_from_row(row: sqlite3.Row) -> FieldStateV3:
        return FieldStateV3.model_validate_json(row["payload"])

    def save_field_draft(
        self,
        draft: FieldDraftV3,
        *,
        expected_version: int | None,
        operation_id: str,
    ) -> FieldStateV3:
        if not operation_id.strip():
            raise ValueError("保存草稿需要操作编号")
        request_hash = self._hash(
            {"draft": draft.model_dump(mode="json"), "expected_version": expected_version}
        )
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
                draft_version, fact_version, latest_plan_id, plan_stale = 1, 0, None, False
            else:
                if row is None:
                    raise KeyError(f"找不到地块 {draft.field_id}")
                state = self._field_from_row(row)
                if state.draft_version != expected_version:
                    raise ValueError(
                        f"版本冲突：当前草稿为 v{state.draft_version}，"
                        f"提交依据为 v{expected_version}"
                    )
                draft_version = state.draft_version + 1
                fact_version = state.fact_version
                latest_plan_id = state.latest_plan_id
                plan_stale = bool(state.latest_plan_id)
            now = self._now()
            state = FieldStateV3(
                id=draft.field_id,
                mode=draft.mode,
                draft_version=draft_version,
                fact_version=fact_version,
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

    def commit_events(
        self,
        field_id: str,
        events: list[FactEventInputV3],
        *,
        expected_fact_version: int,
        operation_id: str,
    ) -> EventCommitResultV3:
        if not events:
            raise ValueError("至少需要一个已确认事实")
        request_hash = self._hash(
            {
                "field_id": field_id,
                "events": [event.model_dump(mode="json") for event in events],
                "expected_fact_version": expected_fact_version,
            }
        )
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
            next_version = state.fact_version + 1
            now = self._now()
            saved_events: list[FactEventV3] = []
            for event in events:
                if event.feedback and event.feedback.plot_id not in {None, field_id}:
                    raise ValueError("反馈中的地块编号与当前地块不一致")
                if (
                    event.feedback
                    and event.feedback.task_id
                    and not event.feedback.task_id.startswith(f"{field_id}-")
                ):
                    raise ValueError("任务编号不属于当前地块")
                if event.correction_of:
                    target = connection.execute(
                        "SELECT payload FROM fact_events_v3 WHERE event_id = ? AND field_id = ?",
                        (event.correction_of, field_id),
                    ).fetchone()
                    if target is None:
                        raise ValueError("更正引用的原事件不存在")
                saved = FactEventV3(
                    **event.model_dump(),
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
            if (state.draft_version, state.fact_version, state.latest_plan_id) != (
                preview.expected_draft_version,
                preview.expected_fact_version,
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

    def save_agent_run(self, run: AgentRunV3) -> None:
        with self._connection() as connection, connection:
            connection.execute(
                "INSERT INTO agent_runs_v3 (id, field_id, started_at, payload) VALUES (?, ?, ?, ?)",
                (run.id, run.field_id, run.started_at.isoformat(), self._json(run)),
            )

    def list_agent_runs(self, field_id: str) -> list[AgentRunV3]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_runs_v3 WHERE field_id=? ORDER BY rowid",
                (field_id,),
            ).fetchall()
        return [AgentRunV3.model_validate_json(row["payload"]) for row in rows]
