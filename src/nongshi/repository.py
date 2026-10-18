"""Append-only SQLite storage for local plan versions."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from nongshi.schema import PlanVersion
from nongshi.schema_v2 import PlanVersionV2


class PlanStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS plan_versions (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT REFERENCES plan_versions(id),
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )

    def save(self, version: PlanVersion) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            if (
                version.parent_id
                and connection.execute(
                    "SELECT 1 FROM plan_versions WHERE id = ?", (version.parent_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("找不到上一版本；请先保存原计划")
            try:
                connection.execute(
                    "INSERT INTO plan_versions (id, parent_id, created_at, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        version.id,
                        version.parent_id,
                        version.created_at.isoformat(),
                        version.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("计划版本已存在，不能覆盖历史") from error

    def load(self, version_id: str) -> PlanVersion:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT payload FROM plan_versions WHERE id = ?", (version_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"找不到计划版本 {version_id}")
        return PlanVersion.model_validate_json(row[0])

    def latest(self) -> PlanVersion | None:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT payload FROM plan_versions ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return PlanVersion.model_validate_json(row[0]) if row else None

    def heads(self) -> list[PlanVersion]:
        """Return the newest version of each independent legacy plan history."""
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT parent.payload FROM plan_versions AS parent "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM plan_versions AS child WHERE child.parent_id = parent.id"
                ") ORDER BY parent.rowid DESC"
            ).fetchall()
        return [PlanVersion.model_validate_json(row[0]) for row in rows]

    def history(self, version_id: str) -> list[PlanVersion]:
        versions: list[PlanVersion] = []
        current: str | None = version_id
        while current is not None:
            version = self.load(current)
            versions.append(version)
            current = version.parent_id
        return list(reversed(versions))


class PlanStoreV2:
    """Append-only v2 history sharing the SQLite file with read-only v1 records."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS plan_versions_v2 (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT REFERENCES plan_versions_v2(id),
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )

    def save(self, version: PlanVersionV2) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            if (
                version.parent_id
                and connection.execute(
                    "SELECT 1 FROM plan_versions_v2 WHERE id = ?", (version.parent_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("找不到上一版本；请先保存原计划")
            try:
                connection.execute(
                    "INSERT INTO plan_versions_v2 (id, parent_id, created_at, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        version.id,
                        version.parent_id,
                        version.created_at.isoformat(),
                        version.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("v2计划版本已存在，不能覆盖历史") from error

    def load(self, version_id: str) -> PlanVersionV2:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT payload FROM plan_versions_v2 WHERE id = ?", (version_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"找不到v2计划版本 {version_id}")
        return PlanVersionV2.model_validate_json(row[0])

    def latest(self) -> PlanVersionV2 | None:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT payload FROM plan_versions_v2 ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return PlanVersionV2.model_validate_json(row[0]) if row else None

    def heads(self) -> list[PlanVersionV2]:
        """Return the newest version of each independent plan history."""
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT parent.payload FROM plan_versions_v2 AS parent "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM plan_versions_v2 AS child WHERE child.parent_id = parent.id"
                ") ORDER BY parent.rowid DESC"
            ).fetchall()
        return [PlanVersionV2.model_validate_json(row[0]) for row in rows]

    def history(self, version_id: str) -> list[PlanVersionV2]:
        versions: list[PlanVersionV2] = []
        current: str | None = version_id
        while current is not None:
            version = self.load(current)
            versions.append(version)
            current = version.parent_id
        return list(reversed(versions))
