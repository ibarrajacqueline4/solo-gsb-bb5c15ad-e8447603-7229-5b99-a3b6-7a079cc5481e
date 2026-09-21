"""请求 / 响应的 Pydantic 模型。"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, model_validator

NonEmptyId = Annotated[str, Field(min_length=1, max_length=200)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]


class ServiceIn(BaseModel):
    id: NonEmptyId
    # 故障域标识：同域服务不能在同一波发布；为空表示无故障域约束
    failure_domain: Optional[str] = None


class PlanIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=300)
    services: list[ServiceIn] = Field(min_length=1)
    # 有向依赖边 from -> to：from 必须排在 to 之前发布
    dependencies: list[tuple[NonEmptyId, NonEmptyId]] = Field(default_factory=list)
    # 每波并发上限
    max_concurrent: PositiveInt
    # 可选的波次上限：给定后若不可行将返回结构化原因
    max_waves: Optional[PositiveInt] = None

    @model_validator(mode="after")
    def _validate(self) -> "PlanIn":
        seen: set[str] = set()
        duplicates: list[str] = []
        for svc in self.services:
            if svc.id in seen:
                duplicates.append(svc.id)
            seen.add(svc.id)
        if duplicates:
            raise ValueError(f"duplicate service ids: {sorted(set(duplicates))}")

        unknown: list[str] = []
        for src, dst in self.dependencies:
            if src not in seen:
                unknown.append(src)
            if dst not in seen:
                unknown.append(dst)
        if unknown:
            raise ValueError(
                f"dependencies reference unknown service ids: {sorted(set(unknown))}"
            )
        return self


# ---------- 结构化错误原因 ----------


class CycleDetail(BaseModel):
    cycles: list[list[str]] = Field(
        description="构成环的服务 id 序列（首尾不同，边隐含闭合）"
    )


class ChainDetail(BaseModel):
    service_ids: list[str] = Field(description="触发该下限的关键依赖链")
    required: int = Field(description="该链需要的最少波数")


class FailureDomainDetail(BaseModel):
    failure_domain: str
    service_ids: list[str]
    required: int = Field(description="该故障域至少需要的波数")


class CapacityDetail(BaseModel):
    required: int = Field(description="按每波并发上限至少需要的波数")


class MaxWavesConstraintDetail(BaseModel):
    max_waves: int


class ConstraintViolation(BaseModel):
    type: Literal[
        "dependency",
        "wave_capacity",
        "failure_domain",
        "max_waves_limit",
        "service_assignment",
    ]
    description: str
    service_ids: Optional[list[str]] = None
    from_service: Optional[str] = None
    to_service: Optional[str] = None
    wave: Optional[int] = None
    max_concurrent: Optional[int] = None
    failure_domain: Optional[str] = None
    max_waves: Optional[int] = None


class PlanningError(BaseModel):
    code: Literal[
        "DEPENDENCY_CYCLE",
        "MAX_WAVES_CHAIN",
        "MAX_WAVES_CAPACITY",
        "MAX_WAVES_FAILURE_DOMAIN",
        "CONSTRAINTS_INFEASIBLE",
        "SOLVER_TIMEOUT",
    ]
    message: str
    detail: dict
    # 仅 CONSTRAINTS_INFEASIBLE 时返回：定位到服务与约束的最小冲突集
    conflicting_constraints: Optional[list[ConstraintViolation]] = None


# ---------- 方案输出 ----------


class ServiceResult(BaseModel):
    id: str
    wave: int
    failure_domain: Optional[str] = None


class WaveResult(BaseModel):
    wave: int
    services: list[str]
    concurrent: int


class PlanResponse(BaseModel):
    id: str
    name: Optional[str] = None
    created_at: str
    feasible: bool
    status: Literal["feasible", "infeasible"]
    total_waves: Optional[int] = None
    max_concurrent: int
    max_waves: Optional[int] = None
    waves: Optional[list[WaveResult]] = None
    assignments: Optional[list[ServiceResult]] = None
    services: list[ServiceIn]
    dependencies: list[tuple[str, str]]
    error: Optional[PlanningError] = None


class PlanSummary(BaseModel):
    id: str
    name: Optional[str] = None
    created_at: str
    status: Literal["feasible", "infeasible"]
    total_waves: Optional[int] = None
    service_count: int
    max_concurrent: int
    max_waves: Optional[int] = None
