"""SQLite 持久化：建表在应用启动时执行。"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

from .config import settings
from .schemas import (
    PlanIn,
    PlanResponse,
    PlanSummary,
    ServiceIn,
    ServiceResult,
    WaveResult,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id             TEXT PRIMARY KEY,
    name           TEXT,
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('feasible', 'infeasible')),
    total_waves    INTEGER,
    max_concurrent INTEGER NOT NULL,
    max_waves      INTEGER,
    error_json     TEXT
);

CREATE TABLE IF NOT EXISTS plan_services (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id        TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    service_id     TEXT NOT NULL,
    wave           INTEGER,
    failure_domain TEXT,
    position       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_dependencies (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    src     TEXT NOT NULL,
    dst     TEXT NOT NULL,
    position INTEGER NOT NULL
);
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or settings.db_path
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def save_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    created_at: str,
    plan: PlanIn,
    status: str,
    total_waves: Optional[int],
    assignments: Optional[dict[str, int]],
    error_json: Optional[str],
) -> None:
    conn.execute(
        """INSERT INTO plans
           (id, name, created_at, status, total_waves, max_concurrent,
            max_waves, error_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            plan_id,
            plan.name,
            created_at,
            status,
            total_waves,
            plan.max_concurrent,
            plan.max_waves,
            error_json,
        ),
    )
    for pos, svc in enumerate(plan.services):
        conn.execute(
            """INSERT INTO plan_services
               (plan_id, service_id, wave, failure_domain, position)
               VALUES (?, ?, ?, ?, ?)""",
            (
                plan_id,
                svc.id,
                assignments.get(svc.id) if assignments else None,
                svc.failure_domain,
                pos,
            ),
        )
    for pos, (src, dst) in enumerate(plan.dependencies):
        conn.execute(
            """INSERT INTO plan_dependencies (plan_id, src, dst, position)
               VALUES (?, ?, ?, ?)""",
            (plan_id, src, dst, pos),
        )


def _row_to_response(conn: sqlite3.Connection, row: sqlite3.Row) -> PlanResponse:
    svc_rows = conn.execute(
        "SELECT service_id, wave, failure_domain FROM plan_services "
        "WHERE plan_id = ? ORDER BY position",
        (row["id"],),
    ).fetchall()
    dep_rows = conn.execute(
        "SELECT src, dst FROM plan_dependencies WHERE plan_id = ? ORDER BY position",
        (row["id"],),
    ).fetchall()

    services = [
        ServiceIn(id=r["service_id"], failure_domain=r["failure_domain"])
        for r in svc_rows
    ]
    dependencies = [(r["src"], r["dst"]) for r in dep_rows]

    waves: Optional[list[WaveResult]] = None
    assignments: Optional[list[ServiceResult]] = None
    if row["status"] == "feasible":
        assignments = [
            ServiceResult(
                id=r["service_id"],
                wave=r["wave"],
                failure_domain=r["failure_domain"],
            )
            for r in svc_rows
        ]
        by_wave: dict[int, list[str]] = {}
        for r in svc_rows:
            by_wave.setdefault(r["wave"], []).append(r["service_id"])
        waves = [
            WaveResult(
                wave=w,
                services=sorted(by_wave[w]),
                concurrent=len(by_wave[w]),
            )
            for w in sorted(by_wave)
        ]

    error = json.loads(row["error_json"]) if row["error_json"] else None
    return PlanResponse(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        feasible=row["status"] == "feasible",
        status=row["status"],
        total_waves=row["total_waves"],
        max_concurrent=row["max_concurrent"],
        max_waves=row["max_waves"],
        waves=waves,
        assignments=assignments,
        services=services,
        dependencies=dependencies,
        error=error,
    )


def get_plan(conn: sqlite3.Connection, plan_id: str) -> Optional[PlanResponse]:
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        return None
    return _row_to_response(conn, row)


def list_plans(conn: sqlite3.Connection, limit: int, offset: int) -> list[PlanSummary]:
    rows = conn.execute(
        """SELECT p.*, COUNT(s.id) AS service_count
           FROM plans p
           LEFT JOIN plan_services s ON s.plan_id = p.id
           GROUP BY p.id
           ORDER BY p.created_at DESC, p.id
           LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    return [
        PlanSummary(
            id=r["id"],
            name=r["name"],
            created_at=r["created_at"],
            status=r["status"],
            total_waves=r["total_waves"],
            service_count=r["service_count"],
            max_concurrent=r["max_concurrent"],
            max_waves=r["max_waves"],
        )
        for r in rows
    ]


def delete_plan(conn: sqlite3.Connection, plan_id: str) -> bool:
    cur = conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    return cur.rowcount > 0
