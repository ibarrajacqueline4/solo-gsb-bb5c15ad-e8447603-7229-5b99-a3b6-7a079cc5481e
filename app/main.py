"""FastAPI 入口：发布波次规划服务。"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from sqlite3 import Connection
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from . import db
from .scheduler import solve_plan
from .schemas import PlanIn, PlanResponse, PlanSummary


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="发布波次规划 API",
    description=(
        "提交服务依赖图、每波并发上限与故障域约束，"
        "由 OR-Tools CP-SAT 生成波次数最少的发布方案；"
        "成环或约束不可行时返回可定位到服务与约束的结构化原因。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn() -> Connection:
    conn = db.connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


DbConn = Annotated[Connection, Depends(get_conn)]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/plans",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["plans"],
    summary="创建发布波次方案",
)
def create_plan(plan: PlanIn, conn: DbConn) -> PlanResponse | JSONResponse:
    plan_id = str(uuid.uuid4())
    created_at = _utc_now()
    result = solve_plan(plan)

    if result.feasible:
        error_json = None
        record_status = "feasible"
        total_waves = result.total_waves
        assignment_rows = {
            s.id: result.assignments[s.id] for s in plan.services
        }
    else:
        error_json = result.error.model_dump_json()
        record_status = "infeasible"
        total_waves = None
        assignment_rows = None

    db.save_plan(
        conn,
        plan_id,
        created_at,
        plan,
        record_status,
        total_waves,
        assignment_rows,
        error_json,
    )

    response = db.get_plan(conn, plan_id)
    if result.feasible:
        return response

    # 不可行/超时：记录已入库，返回 422（超时为 503）并携带结构化原因
    http_status = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if result.error.code == "SOLVER_TIMEOUT"
        else status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    return JSONResponse(
        status_code=http_status,
        content=response.model_dump(mode="json"),
    )


@app.get(
    "/api/plans",
    response_model=list[PlanSummary],
    tags=["plans"],
    summary="列出方案",
)
def list_plans(
    conn: DbConn,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlanSummary]:
    return db.list_plans(conn, limit, offset)


@app.get(
    "/api/plans/{plan_id}",
    response_model=PlanResponse,
    tags=["plans"],
    summary="查询单个方案",
)
def get_plan(plan_id: str, conn: DbConn) -> PlanResponse:
    response = db.get_plan(conn, plan_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"plan {plan_id!r} not found")
    return response


@app.delete(
    "/api/plans/{plan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["plans"],
    summary="删除方案",
)
def delete_plan(plan_id: str, conn: DbConn) -> None:
    if not db.delete_plan(conn, plan_id):
        raise HTTPException(status_code=404, detail=f"plan {plan_id!r} not found")
