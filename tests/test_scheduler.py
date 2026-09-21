"""scheduler 单元测试：环检测、预检、最优性、约束满足与确定性。"""
from __future__ import annotations

from collections import Counter

import pytest

from app.schemas import PlanIn, ServiceIn
from app.scheduler import build_waves, solve_plan


def _plan(services, dependencies=(), k=1, max_waves=None, domains=None):
    svc = []
    for s in services:
        svc.append(ServiceIn(id=s, failure_domain=(domains or {}).get(s)))
    return PlanIn(
        services=svc,
        dependencies=list(dependencies),
        max_concurrent=k,
        max_waves=max_waves,
    )


def _validate(result, plan, edges):
    assert result.feasible
    w = result.assignments
    for u, v in edges:
        assert w[u] < w[v]
    counts = Counter(w.values())
    assert max(counts.values()) <= plan.max_concurrent
    assert sorted(counts) == list(range(1, result.total_waves + 1))
    fmap = {s.id: s.failure_domain for s in plan.services}
    for wave_no in range(1, result.total_waves + 1):
        fds = [fmap[s] for s in w if w[s] == wave_no and fmap[s]]
        assert len(fds) == len(set(fds))


def test_single_service():
    r = solve_plan(_plan(["a"], k=1))
    assert r.feasible and r.total_waves == 1
    assert build_waves(r.assignments)[0].services == ["a"]


def test_chain_minimum_waves():
    edges = [("a", "b"), ("b", "c"), ("c", "d")]
    r = solve_plan(_plan("abcd", edges, k=4))
    assert r.feasible and r.total_waves == 4
    _validate(r, _plan("abcd", edges, k=4), edges)


def test_independent_capacity_bound():
    # 6 个独立服务、每波 2 个 => 最少 3 波
    r = solve_plan(_plan("abcdef", k=2))
    assert r.total_waves == 3
    assert all(w.concurrent == 2 for w in build_waves(r.assignments))


def test_failure_domain_spreads_services():
    plan = _plan("abc", k=3, domains={"a": "fd", "b": "fd", "c": "fd"})
    r = solve_plan(plan)
    assert r.total_waves == 3
    _validate(r, plan, [])


def test_dependency_and_domain_combined():
    edges = {("a", "b"), ("b", "c")}
    plan = _plan("abcd", edges, k=2,
                 domains={"a": "z1", "b": "z1"})
    r = solve_plan(plan)
    _validate(r, plan, edges)
    assert r.total_waves == 3


def test_deterministic_same_input():
    plan = _plan("abcdefgh", [("a", "c"), ("b", "d"), ("d", "e")], k=3,
                 domains={"c": "x", "d": "x"})
    r1 = solve_plan(plan)
    r2 = solve_plan(plan)
    assert r1.assignments == r2.assignments


def test_dependency_cycle_localized():
    r = solve_plan(_plan("abc", [("a", "b"), ("b", "c"), ("c", "a")], k=2))
    assert not r.feasible
    assert r.error.code == "DEPENDENCY_CYCLE"
    assert r.error.detail["cycle"]["cycles"] == [["a", "b", "c"]]


def test_self_loop_is_cycle():
    r = solve_plan(_plan("a", [("a", "a")], k=2))
    assert r.error.code == "DEPENDENCY_CYCLE"
    assert r.error.detail["cycle"]["cycles"] == [["a"]]


def test_two_disjoint_cycles():
    edges = [("a", "b"), ("b", "a"), ("c", "d"), ("d", "c")]
    r = solve_plan(_plan("abcd", edges, k=4))
    cycles = r.error.detail["cycle"]["cycles"]
    assert cycles == [["a", "b"], ["c", "d"]]


def test_precheck_chain_exceeds_max_waves():
    edges = [("a", "b"), ("b", "c")]
    r = solve_plan(_plan("abc", edges, k=3, max_waves=2))
    assert r.error.code == "MAX_WAVES_CHAIN"
    assert r.error.detail["chain"]["service_ids"] == ["a", "b", "c"]
    assert r.error.detail["chain"]["required"] == 3


def test_precheck_capacity_exceeds_max_waves():
    r = solve_plan(_plan("abcde", k=2, max_waves=2))
    assert r.error.code == "MAX_WAVES_CAPACITY"
    assert r.error.detail["capacity"]["required"] == 3


def test_precheck_failure_domain_exceeds_max_waves():
    r = solve_plan(_plan("abc", k=3, max_waves=2,
                         domains={"a": "fd", "b": "fd", "c": "fd"}))
    assert r.error.code == "MAX_WAVES_FAILURE_DOMAIN"
    assert r.error.detail["failure_domain"]["required"] == 3


def test_mus_locates_interacting_constraints():
    # A 必须先于 B、C；B、C 同故障域；容量 3、5 个服务、上限 2 波
    plan = _plan(["A", "B", "C", "D", "E"],
                 [("A", "B"), ("A", "C")], k=3, max_waves=2,
                 domains={"B": "f", "C": "f"})
    r = solve_plan(plan)
    assert not r.feasible
    assert r.error.code == "CONSTRAINTS_INFEASIBLE"
    violations = r.error.conflicting_constraints
    types = sorted(v.type for v in violations)
    # 两条依赖、故障域互斥与 2 波上限共同构成根因
    assert "dependency" in types
    assert "failure_domain" in types
    assert "max_waves_limit" in types
    deps = {(v.from_service, v.to_service) for v in violations
            if v.type == "dependency"}
    assert deps == {("A", "B"), ("A", "C")}
    fd = [v for v in violations if v.type == "failure_domain"]
    assert fd and fd[0].failure_domain == "f"


def test_bounded_feasible_plan():
    edges = [("a", "b"), ("b", "c")]
    r = solve_plan(_plan("abc", edges, k=3, max_waves=3))
    assert r.feasible and r.total_waves == 3


def test_bounded_tight_feasible():
    # 独立服务恰好填满
    r = solve_plan(_plan("abcd", k=2, max_waves=2))
    assert r.feasible and r.total_waves == 2
