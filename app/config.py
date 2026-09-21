"""运行配置，全部来自环境变量，均有默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    # SQLite 数据库文件路径
    db_path: str = os.getenv("PLANNER_DB", "/data/planner.db")
    # CP-SAT 单次求解时间上限（秒）：用于可行性证明与冲突定位
    solver_time_limit_seconds: float = float(
        os.getenv("SOLVER_TIME_LIMIT_SECONDS", "30")
    )
    # 无波次上限时，在贪心构造解之上做短时优化的预算（秒）；
    # 超时则直接交付确定的贪心方案
    solver_optimize_seconds: float = float(
        os.getenv("SOLVER_OPTIMIZE_SECONDS", "5")
    )
    # CP-SAT 工作线程数；1 保证结果完全确定
    solver_num_workers: int = _int_env("SOLVER_NUM_WORKERS", 1)
    # 求解器随机种子
    solver_random_seed: int = _int_env("SOLVER_RANDOM_SEED", 42)


settings = Settings()
