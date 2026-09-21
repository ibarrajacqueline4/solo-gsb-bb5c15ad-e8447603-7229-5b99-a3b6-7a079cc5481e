"""发布波次规划核心：环检测、结构性预检、贪心构造与 CP-SAT 求解。

求解流程：
  1. Tarjan 强连通分量检测依赖环（含自环），定位到成环服务序列。
  2. 给定 max_waves 时先做三项结构性预检（最长依赖链 / 纯容量 / 单故障域大小），
     快速返回带服务与约束定位的可读原因。
  3. 确定性贪心 list-scheduling 给出近优可行方案与上界。
  4. 无 max_waves：在 [理论下界, 贪心上界] 内用 CP-SAT 二分证明最少可行波次，
     证明超时则交付确定的贪心方案；再在最少波次下做字典序早排优化。
     给定 max_waves：带“假设门控”的模型一次求解，不可行时用 CP-SAT unsat core
     经删除法求出最小冲突约束集（MUS），定位到具体服务、依赖边、波次与故障域。

CP-SAT 约束（W 波模型，wave[s] ∈ 域）：
  * 依赖边 (u -> v)：wave[u] + 1 <= wave[v]
  * 每波并发：对每个波次 w，sum(x[s,w]) <= max_concurrent
  * 故障域：对每个故障域 f 与波次 w，sum(x[s,w], s∈f) <= 1
  * x[s,w] 为通道布尔量，wave[s] 由 x 唯一定义，保证输出顺序确定
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Optional

from ortools.sat.python import cp_model as cp

from .config import settings
from .schemas import (
    CapacityDetail,
    ChainDetail,
    ConstraintViolation,
    CycleDetail,
    FailureDomainDetail,
    MaxWavesConstraintDetail,
    PlanningError,
    PlanIn,
    WaveResult,
)


# ---------------------------------------------------------------------------
# 图工具
# ---------------------------------------------------------------------------


def find_cycles(
    ids: list[str], edges: set[tuple[str, str]]
) -> list[list[str]]:
    """Tarjan 强连通分量，返回所有真实环（含自环），每个环给出一条路径。"""
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = [0]
    sccs: list[list[str]] = []

    adj: dict[str, list[str]] = {s: [] for s in ids}
    for u, v in edges:
        adj[u].append(v)
    for s in ids:
        adj[s].sort()

    def strongconnect(v: str) -> None:
        index[v] = counter[0]
        lowlink[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj[v]:
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    import sys

    sys.setrecursionlimit(max(10000, len(ids) * 4 + 1000))
    for s in ids:
        if s not in index:
            strongconnect(s)

    cycles: list[list[str]] = []
    for comp in sccs:
        members = set(comp)
        if len(comp) == 1:
            node = comp[0]
            if (node, node) not in edges:
                continue
            cycles.append([node])
            continue
        path = _extract_cycle_path(comp[0], adj, members)
        # 旋转到最小 id 开头，消除遍历起点导致的表示差异
        k = min(range(len(path)), key=lambda i: path[i])
        path = path[k:] + path[:k]
        cycles.append(path)

    cycles.sort(key=lambda c: c[0])
    return cycles


def _extract_cycle_path(
    start: str, adj: dict[str, list[str]], members: set[str]
) -> list[str]:
    """在某个强连通分量内部做 DFS，找到一条回到 start 的简单环路径。

    维护从 start 出发的当前路径；遇到指向路径上某节点的回边即得到一个环。
    """
    # 每个元素为 (节点, 下一个待探索邻边下标)
    stack: list[tuple[str, int]] = [(start, 0)]
    on_path: dict[str, int] = {start: 0}
    while stack:
        u, i = stack[-1]
        if i >= len(adj[u]):
            stack.pop()
            del on_path[u]
            continue
        w = adj[u][i]
        stack[-1] = (u, i + 1)
        if w not in members:
            continue
        if w in on_path:
            # 回边：路径 on_path[w:] 构成环（边 u->w 隐含闭合）
            return [node for node, _ in stack[on_path[w] :]]
        on_path[w] = len(stack)
        stack.append((w, 0))
    return [start]  # 理论不可达：强连通分量内必有环


def longest_chain(
    ids: list[str], edges: set[tuple[str, str]]
) -> tuple[int, list[str]]:
    """DAG 上的最长依赖链（节点数）及具体链；供 max_waves 预检使用。"""
    indeg: dict[str, int] = {s: 0 for s in ids}
    adj: dict[str, list[str]] = {s: [] for s in ids}
    for u, v in edges:
        adj[u].append(v)
        indeg[v] += 1

    # 输入顺序 + 排序保证拓扑过程确定
    ready = sorted([s for s in ids if indeg[s] == 0])
    depth: dict[str, int] = {s: 1 for s in ids}
    pred: dict[str, Optional[str]] = {s: None for s in ids}
    while ready:
        u = ready.pop(0)
        for v in sorted(adj[u]):
            if depth[u] + 1 > depth[v]:
                depth[v] = depth[u] + 1
                pred[v] = u
            indeg[v] -= 1
            if indeg[v] == 0:
                ready.append(v)
                ready.sort()

    # 取深度最大且 id 最小的节点，保证回溯出的链确定
    best_depth = max(depth.values())
    end = min(s for s in ids if depth[s] == best_depth)
    chain: list[str] = []
    cur: Optional[str] = end
    while cur is not None:
        chain.append(cur)
        cur = pred[cur]
    chain.reverse()
    return best_depth, chain


# ---------------------------------------------------------------------------
# 结构化预检（max_waves 给定时的必要条件；快速、可读）
# ---------------------------------------------------------------------------


@dataclass
class PrecheckFailure:
    error: PlanningError


def _precheck(
    ids: list[str],
    edges: set[tuple[str, str]],
    domains: dict[str, str],
    max_concurrent: int,
    max_waves: int,
) -> Optional[PrecheckFailure]:
    # 1. 最长依赖链
    chain_len, chain = longest_chain(ids, edges)
    if chain_len > max_waves:
        return PrecheckFailure(
            PlanningError(
                code="MAX_WAVES_CHAIN",
                message=(
                    f"最长依赖链包含 {chain_len} 个服务，超过波次上限 {max_waves}；"
                    "同一依赖链上的服务必须分波发布"
                ),
                detail={
                    "chain": ChainDetail(
                        service_ids=chain, required=chain_len
                    ).model_dump(),
                    "max_waves_limit": MaxWavesConstraintDetail(
                        max_waves=max_waves
                    ).model_dump(),
                },
            )
        )

    # 2. 纯并发容量
    cap_need = -(-len(ids) // max_concurrent)
    if cap_need > max_waves:
        return PrecheckFailure(
            PlanningError(
                code="MAX_WAVES_CAPACITY",
                message=(
                    f"{len(ids)} 个服务、每波最多 {max_concurrent} 个，"
                    f"至少需要 {cap_need} 波，超过波次上限 {max_waves}"
                ),
                detail={
                    "capacity": CapacityDetail(required=cap_need).model_dump(),
                    "max_waves_limit": MaxWavesConstraintDetail(
                        max_waves=max_waves
                    ).model_dump(),
                },
            )
        )

    # 3. 单个故障域大小
    members: dict[str, list[str]] = {}
    for s in ids:
        f = domains.get(s)
        if f:
            members.setdefault(f, []).append(s)
    for f in sorted(members):
        ss = sorted(members[f])
        if len(ss) > max_waves:
            return PrecheckFailure(
                PlanningError(
                    code="MAX_WAVES_FAILURE_DOMAIN",
                    message=(
                        f"故障域 {f!r} 包含 {len(ss)} 个服务，同域不能同波，"
                        f"至少需要 {len(ss)} 波，超过波次上限 {max_waves}"
                    ),
                    detail={
                        "failure_domain": FailureDomainDetail(
                            failure_domain=f,
                            service_ids=ss,
                            required=len(ss),
                        ).model_dump(),
                        "max_waves_limit": MaxWavesConstraintDetail(
                            max_waves=max_waves
                        ).model_dump(),
                    },
                )
            )
    return None


# ---------------------------------------------------------------------------
# CP-SAT 求解
# ---------------------------------------------------------------------------



@dataclass
class SolveResult:
    feasible: bool
    total_waves: Optional[int] = None
    assignments: dict[str, int] = field(default_factory=dict)
    error: Optional[PlanningError] = None


@dataclass
class _Model:
    model: cp.CpModel
    wave: dict[str, cp.IntVar]
    assumptions: list[cp.IntVar]
    assum_meta: list[dict]
    clean_proto: object = None


def _earliest_waves(ids: list[str], edges: set[tuple[str, str]]) -> dict[str, int]:
    """DAG 前向最长链：每个服务的最早可能波次，用于裁剪变量域。"""
    adj: dict[str, list[str]] = {s: [] for s in ids}
    indeg: dict[str, int] = {s: 0 for s in ids}
    for u, v in edges:
        adj[u].append(v)
        indeg[v] += 1
    earliest = {s: 1 for s in ids}
    ready = sorted(s for s in ids if indeg[s] == 0)
    while ready:
        u = ready.pop(0)
        for v in sorted(adj[u]):
            if earliest[u] + 1 > earliest[v]:
                earliest[v] = earliest[u] + 1
            indeg[v] -= 1
            if indeg[v] == 0:
                ready.append(v)
                ready.sort()
    return earliest


def _build_model(
    ids: list[str],
    edges: set[tuple[str, str]],
    domains: dict[str, str],
    k: int,
    W: int,
    earliest: dict[str, int],
    with_objective: bool,
    with_assumptions: bool,
    loc_horizon: Optional[int] = None,
) -> _Model:
    """构建 W 波的紧凑 CP-SAT 模型。

    * 普通模型（with_assumptions=False）：wave 域裁剪到 [earliest[s], W]，
      全部约束为硬约束，用于可行性证明与确定性优化，规模小速度快。
    * 定位模型（with_assumptions=True）：wave 域放宽到 [1, loc_horizon]
      且所有业务约束门控为假设。必须存在“逃生波次” (W+1..loc_horizon)，
      这样放松波次上限/依赖门控后模型确实可解，这些约束才能进入最小冲突集。
    """
    if with_assumptions:
        horizon = loc_horizon or W
        lo_wave = {s: 1 for s in ids}
    else:
        horizon = W
        lo_wave = earliest

    model = cp.CpModel()
    wave = {s: model.NewIntVar(lo_wave[s], horizon, f"wave_{s}") for s in ids}

    x: dict[tuple[str, int], cp.IntVar] = {}
    slots: dict[str, list[int]] = {}
    for s in ids:
        ws = [w for w in range(lo_wave[s], horizon + 1)]
        slots[s] = ws
        vs = []
        for w in ws:
            b = model.NewBoolVar(f"x_{s}__w{w}")
            x[s, w] = b
            vs.append(b)
            model.Add(wave[s] == w).OnlyEnforceIf(b)
        if with_assumptions:
            model.Add(sum(vs) <= 1)  # “至少一个”由门控假设补上
        else:
            model.AddExactlyOne(vs)

    assumptions: list[cp.IntVar] = []
    assum_meta: list[dict] = []

    def add_assumption(lit: cp.IntVar, meta: dict) -> None:
        assumptions.append(lit)
        assum_meta.append(meta)

    # 服务必须排入某个波
    if with_assumptions:
        for s in ids:
            lit = model.NewBoolVar(f"assign_{s}")
            model.AddBoolOr([x[s, w] for w in slots[s]]).OnlyEnforceIf(lit)
            add_assumption(
                lit,
                {
                    "type": "service_assignment",
                    "service_ids": [s],
                    "description": f"服务 {s} 必须被排入某个波次",
                },
            )

    # 依赖边
    for u, v in sorted(edges):
        if with_assumptions:
            lit = model.NewBoolVar(f"dep_{u}__{v}")
            model.Add(wave[u] + 1 <= wave[v]).OnlyEnforceIf(lit)
        else:
            model.Add(wave[u] + 1 <= wave[v])
            continue
        add_assumption(
            lit,
            {
                "type": "dependency",
                "from_service": u,
                "to_service": v,
                "service_ids": [u, v],
                "description": f"依赖约束：{u} 必须早于 {v} 至少一个波次发布",
            },
        )

    # 每波并发（定位模型只对 1..W 建门控，W+1..horizon 为逃生波次）
    cap_hi = W if with_assumptions else horizon
    for w in range(1, cap_hi + 1):
        present = [s for s in ids if lo_wave[s] <= w <= horizon]
        if with_assumptions:
            lit = model.NewBoolVar(f"cap_w{w}")
            model.Add(sum(x[s, w] for s in present) <= k).OnlyEnforceIf(lit)
            add_assumption(
                lit,
                {
                    "type": "wave_capacity",
                    "wave": w,
                    "max_concurrent": k,
                    "service_ids": ids,
                    "description": f"第 {w} 波并发不超过 {k} 个服务",
                },
            )
        else:
            model.Add(sum(x[s, w] for s in present) <= k)

    # 故障域：同域同波至多一个（同样只约束 1..W）
    fd_members: dict[str, list[str]] = {}
    for s, f in domains.items():
        fd_members.setdefault(f, []).append(s)
    for f in sorted(fd_members):
        ss = sorted(fd_members[f])
        for w in range(1, cap_hi + 1):
            here = [s for s in ss if lo_wave[s] <= w <= horizon]
            if len(here) <= 1:
                continue
            if with_assumptions:
                lit = model.NewBoolVar(f"fd_{f}_w{w}")
                model.Add(sum(x[s, w] for s in here) <= 1).OnlyEnforceIf(lit)
            else:
                model.Add(sum(x[s, w] for s in here) <= 1)
                continue
            add_assumption(
                lit,
                {
                    "type": "failure_domain",
                    "failure_domain": f,
                    "wave": w,
                    "service_ids": ss,
                    "description": (
                        f"故障域 {f!r} 的服务（{', '.join(ss)}）"
                        f"不能在第 {w} 波同时发布"
                    ),
                },
            )

    clean_proto = None
    if with_assumptions:
        # 波次上限是该 W 模型的定义域本身；仍显式登记一条门控约束，
        # 让“不能超过 W 波”作为一条可定位的业务约束出现在冲突集中。
        cut = model.NewBoolVar("max_waves_limit")
        for s in ids:
            model.Add(wave[s] <= W).OnlyEnforceIf(cut)
        add_assumption(
            cut,
            {
                "type": "max_waves_limit",
                "max_waves": W,
                "service_ids": list(ids),
                "description": f"全部服务必须在第 {W} 波（含）之前发布",
            },
        )
        # 加入全量假设之前留档，供 MUS 子集检验克隆，避免假设累积
        clean_proto = copy.deepcopy(model.Proto())
        model.AddAssumptions(assumptions)

    if with_objective:
        # 字典序早排：按输入顺序的服务加权极小化波次，
        # 权重严格递减且不溢出，使最优解在相同输入下唯一确定
        n = len(ids)
        weight = {s: n + 1 - i for i, s in enumerate(ids)}
        model.Minimize(sum(weight[s] * wave[s] for s in ids))

    return _Model(model, wave, assumptions, assum_meta, clean_proto)


def _new_solver(time_limit: float) -> cp.CpSolver:
    solver = cp.CpSolver()
    solver.parameters.num_workers = settings.solver_num_workers
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.random_seed = settings.solver_random_seed
    solver.parameters.log_search_progress = False
    return solver


def _solve_feasibility(
    ids: list[str],
    edges: set[tuple[str, str]],
    domains: dict[str, str],
    k: int,
    W: int,
    earliest: dict[str, int],
    time_limit: float,
    with_assumptions: bool = False,
    loc_horizon: Optional[int] = None,
) -> tuple[str, Optional[cp.CpSolver], Optional[_Model]]:
    built = _build_model(
        ids,
        edges,
        domains,
        k,
        W,
        earliest,
        with_objective=False,
        with_assumptions=with_assumptions,
        loc_horizon=loc_horizon,
    )
    solver = _new_solver(time_limit)
    status = solver.Solve(built.model)
    return solver.StatusName(status), solver, built


def _check_infeasible(active: list[int], built: _Model, clean_proto,
                      time_limit: float) -> Optional[bool]:
    """在假设模型的干净快照上仅启用 active 子集，判定是否仍不可行。"""
    model = cp.CpModel(copy.deepcopy(clean_proto))
    model.AddAssumptions([built.assumptions[i] for i in active])
    solver = _new_solver(time_limit)
    status = solver.Solve(model)
    if status == cp.INFEASIBLE:
        return True
    if status in (cp.OPTIMAL, cp.FEASIBLE):
        return False
    return None


def _minimize_core(
    built: _Model, clean_proto, seed_core: list[int]
) -> list[int]:
    """删除法求不可约冲突集（MUS）：逐条移除，移除后仍不可行则永久删除。"""
    # 先尝试噪声更大的“服务必须排入”假设，使冲突集聚焦业务约束
    ordered = sorted(
        seed_core,
        key=lambda i: 0 if built.assum_meta[i]["type"] == "service_assignment" else 1,
    )
    active = list(seed_core)
    check_limit = min(settings.solver_time_limit_seconds, 5.0)
    for i in ordered:
        trial = [j for j in active if j != i]
        result = _check_infeasible(trial, built, clean_proto, check_limit)
        if result is True:
            active.remove(i)
        # False=该约束必要，保留；None=超时，保守保留
    return active


def _infeasible_result(built: _Model, solver: cp.CpSolver) -> SolveResult:
    """从不可行求解中提取并最小化冲突约束集。"""
    raw_core = solver.SufficientAssumptionsForInfeasibility()
    index_to_assumption = {
        lit.Index(): i for i, lit in enumerate(built.assumptions)
    }
    seed = [
        index_to_assumption[int(idx)]
        for idx in raw_core
        if int(idx) in index_to_assumption
    ]
    minimal = _minimize_core(built, built.clean_proto, seed)

    violations: list[ConstraintViolation] = []
    seen_meta: set[str] = set()
    for i in minimal:
        meta = built.assum_meta[i]
        key = repr(meta)
        if key in seen_meta:
            continue
        seen_meta.add(key)
        violations.append(ConstraintViolation(**meta))
    return SolveResult(
        feasible=False,
        error=PlanningError(
            code="CONSTRAINTS_INFEASIBLE",
            message=(
                "约束之间相互冲突，无法排出可行的波次方案；"
                "conflicting_constraints 给出最小冲突约束集"
                "（其中任意一条放宽后该冲突即解除），可定位到具体服务与约束"
            ),
            detail={"constraint_count": len(violations)},
            conflicting_constraints=violations,
        ),
    )


def _greedy_schedule(
    ids: list[str],
    edges: set[tuple[str, str]],
    k: int,
    domains: dict[str, str],
) -> tuple[dict[str, int], int]:
    """确定性 list scheduling：每波在容量与故障域约束下尽早安排就绪服务。

    给出最优波次数的构造上界；与理论下界相等时即为最优，无需调用 CP-SAT。
    """
    succ: dict[str, list[str]] = {s: [] for s in ids}
    indeg: dict[str, int] = {s: 0 for s in ids}
    for u, v in edges:
        succ[u].append(v)
        indeg[v] += 1

    wave_of: dict[str, int] = {}
    remaining = set(ids)
    w = 0
    while remaining:
        w += 1
        ready = sorted(s for s in remaining if indeg[s] == 0)
        chosen: list[str] = []
        fds_used: set[str] = set()
        for s in ready:
            if len(chosen) >= k:
                break
            f = domains.get(s)
            if f is not None and f in fds_used:
                continue
            chosen.append(s)
            if f is not None:
                fds_used.add(f)
        for s in chosen:
            wave_of[s] = w
            remaining.discard(s)
            for t in succ[s]:
                indeg[t] -= 1
    return wave_of, w


def solve_plan(plan: PlanIn) -> SolveResult:
    ids = [s.id for s in plan.services]
    domains = {s.id: s.failure_domain for s in plan.services if s.failure_domain}
    edges = set(plan.dependencies)
    k = plan.max_concurrent
    max_waves = plan.max_waves
    n = len(ids)
    tl = settings.solver_time_limit_seconds

    # 1. 环检测（与波次上限无关的硬错误）
    cycles = find_cycles(ids, edges)
    if cycles:
        return SolveResult(
            feasible=False,
            error=PlanningError(
                code="DEPENDENCY_CYCLE",
                message="服务依赖图存在环，环上的服务无法排出先后顺序",
                detail={"cycle": CycleDetail(cycles=cycles).model_dump()},
            ),
        )

    earliest = _earliest_waves(ids, edges)
    depth = max(earliest.values())

    fd_members: dict[str, list[str]] = {}
    for s, f in domains.items():
        fd_members.setdefault(f, []).append(s)

    # 贪心构造：既作无界时的上界/兜底，也为定位模型提供“逃生波次”上界
    greedy_assignment, greedy_W = _greedy_schedule(ids, edges, k, domains)

    # 2. 给定 max_waves：结构性预检 + 一次性带假设求解（支持冲突定位）
    if max_waves is not None:
        pre = _precheck(ids, edges, domains, k, max_waves)
        if pre is not None:
            return SolveResult(feasible=False, error=pre.error)

        # 预检通过却仍不可行，根因必是约束交互；扩展上界取贪心上界
        loc_horizon = max(max_waves + 1, greedy_W)
        status_name, solver, built = _solve_feasibility(
            ids, edges, domains, k, max_waves, earliest, tl,
            with_assumptions=True, loc_horizon=loc_horizon,
        )
        if status_name in ("OPTIMAL", "FEASIBLE"):
            pass  # 落到下方确定性求解
        elif status_name == "INFEASIBLE":
            return _infeasible_result(built, solver)
        else:
            return _timeout_result()

    # 3. 无 max_waves：在 [理论下界, 贪心上界] 中二分证明最少可行波次。
    #    每轮可行性证明分配剩余总预算；证明超时则回退确定的贪心方案。
    lo = max(depth, -(-n // k))
    if fd_members:
        lo = max(lo, max(len(v) for v in fd_members.values()))

    if lo >= greedy_W:
        # 下界与构造上界相等 => 贪心方案即最少波次
        return SolveResult(
            feasible=True,
            total_waves=greedy_W,
            assignments=greedy_assignment,
        )

    remaining_budget = tl
    timed_out = False
    feasible_assignments: dict[int, dict[str, int]] = {}

    def check(W: int, per_call: float) -> str:
        nonlocal remaining_budget, timed_out
        use = max(0.01, min(per_call, remaining_budget))
        name, solver, built = _solve_feasibility(
            ids, edges, domains, k, W, earliest, use
        )
        remaining_budget -= use
        if name not in ("OPTIMAL", "FEASIBLE", "INFEASIBLE"):
            timed_out = True
            return "TIMEOUT"
        if name in ("OPTIMAL", "FEASIBLE"):
            feasible_assignments[W] = {
                s: solver.Value(built.wave[s]) for s in ids
            }
            return "FEASIBLE"
        return "INFEASIBLE"

    # 二分；最多 ceil(log2(区间长度)) 轮，每轮平分剩余预算
    rounds = max(1, math.ceil(math.log2(greedy_W - lo + 1)))
    l, h = lo, greedy_W - 1
    optimal_W: Optional[int] = None
    done_rounds = 0
    while l <= h and not timed_out:
        mid = (l + h) // 2
        left_rounds = max(1, rounds - done_rounds)
        name = check(mid, remaining_budget / left_rounds)
        done_rounds += 1
        if name == "FEASIBLE":
            h = mid - 1
            optimal_W = mid
        elif name == "INFEASIBLE":
            l = mid + 1
        else:
            break

    if timed_out and optimal_W is None:
        # 未能在预算内改进：交付贪心（波次连续、满足全部约束、确定）
        return SolveResult(
            feasible=True,
            total_waves=greedy_W,
            assignments=greedy_assignment,
        )

    final_W = optimal_W if optimal_W is not None else greedy_W

    # 4. 在最少波次数下求顺序确定的具体排法（短时预算即可，实例已可行）
    built = _build_model(
        ids,
        edges,
        domains,
        k,
        final_W,
        earliest,
        with_objective=True,
        with_assumptions=False,
    )
    opt_budget = min(
        settings.solver_optimize_seconds, max(0.01, remaining_budget)
    )
    solver = _new_solver(opt_budget)
    status = solver.Solve(built.model)
    if status in (cp.OPTIMAL, cp.FEASIBLE):
        assignment = {s: solver.Value(built.wave[s]) for s in ids}
        return SolveResult(
            feasible=True,
            total_waves=max(assignment.values()),
            assignments=assignment,
        )
    # 优化阶段超时：使用二分中已证明可行的排法；否则回退贪心
    fallback = feasible_assignments.get(final_W)
    if fallback is not None and max(fallback.values()) == final_W:
        return SolveResult(
            feasible=True, total_waves=final_W, assignments=fallback
        )
    return SolveResult(
        feasible=True,
        total_waves=greedy_W,
        assignments=greedy_assignment,
    )


def _timeout_result() -> SolveResult:
    return SolveResult(
        feasible=False,
        error=PlanningError(
            code="SOLVER_TIMEOUT",
            message=(
                f"求解器在 {settings.solver_time_limit_seconds} 秒内未能确定方案，"
                "可调大 SOLVER_TIME_LIMIT_SECONDS 后重试"
            ),
            detail={"time_limit_seconds": settings.solver_time_limit_seconds},
        ),
    )


def build_waves(assignments: dict[str, int]) -> list[WaveResult]:
    """把 服务->波次 映射整理为连续编号、服务排序确定的波次列表。"""
    total = max(assignments.values())
    waves: list[WaveResult] = []
    for w in range(1, total + 1):
        members = sorted(s for s, ww in assignments.items() if ww == w)
        waves.append(WaveResult(wave=w, services=members, concurrent=len(members)))
    return waves
