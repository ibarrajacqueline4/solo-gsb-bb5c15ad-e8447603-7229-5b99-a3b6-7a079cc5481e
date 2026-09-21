"""API 集成测试：建表、方案创建/查询/列表/删除与错误持久化。"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("PLANNER_DB", str(db_file))
    # settings 在导入时读取环境变量；按依赖顺序重载模块
    import importlib

    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.scheduler as scheduler
    importlib.reload(scheduler)
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_get_list_delete(client):
    body = {
        "name": "demo",
        "services": [
            {"id": "gateway", "failure_domain": "az1"},
            {"id": "auth", "failure_domain": "az1"},
            {"id": "orders"},
            {"id": "payments"},
        ],
        "dependencies": [
            ["gateway", "auth"],
            ["auth", "orders"],
            ["auth", "payments"],
        ],
        "max_concurrent": 2,
    }
    r = client.post("/api/plans", json=body)
    assert r.status_code == 201
    data = r.json()
    pid = data["id"]
    assert data["status"] == "feasible"
    assert data["total_waves"] == 3
    # 依赖顺序
    wave_of = {a["id"]: a["wave"] for a in data["assignments"]}
    assert wave_of["gateway"] < wave_of["auth"] < wave_of["orders"]

    assert client.get(f"/api/plans/{pid}").json()["id"] == pid
    listing = client.get("/api/plans").json()
    assert any(p["id"] == pid for p in listing)

    assert client.delete(f"/api/plans/{pid}").status_code == 204
    assert client.get(f"/api/plans/{pid}").status_code == 404
    assert client.delete(f"/api/plans/{pid}").status_code == 404


def test_infeasible_cycle_is_persisted(client):
    body = {
        "services": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "dependencies": [["a", "b"], ["b", "c"], ["c", "a"]],
        "max_concurrent": 2,
    }
    r = client.post("/api/plans", json=body)
    assert r.status_code == 422
    data = r.json()
    assert data["error"]["code"] == "DEPENDENCY_CYCLE"
    assert data["error"]["detail"]["cycle"]["cycles"] == [["a", "b", "c"]]
    fetched = client.get(f"/api/plans/{data['id']}").json()
    assert fetched["status"] == "infeasible"
    assert fetched["error"]["code"] == "DEPENDENCY_CYCLE"


def test_infeasible_mus_response(client):
    body = {
        "services": [
            {"id": "A"},
            {"id": "B", "failure_domain": "f"},
            {"id": "C", "failure_domain": "f"},
            {"id": "D"},
            {"id": "E"},
        ],
        "dependencies": [["A", "B"], ["A", "C"]],
        "max_concurrent": 3,
        "max_waves": 2,
    }
    r = client.post("/api/plans", json=body)
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "CONSTRAINTS_INFEASIBLE"
    assert error["conflicting_constraints"]
    for v in error["conflicting_constraints"]:
        assert v["description"]


def test_validation_errors(client):
    cases = [
        {"services": [{"id": "a"}, {"id": "a"}],
         "dependencies": [], "max_concurrent": 2},
        {"services": [{"id": "a"}],
         "dependencies": [["a", "unknown"]], "max_concurrent": 2},
        {"services": [{"id": "a"}], "dependencies": [], "max_concurrent": 0},
        {"services": [], "dependencies": [], "max_concurrent": 2},
        {"services": [{"id": "a"}], "dependencies": [],
         "max_concurrent": 2, "max_waves": 0},
    ]
    for body in cases:
        assert client.post("/api/plans", json=body).status_code == 422, body


def test_get_missing_returns_404(client):
    assert client.get("/api/plans/missing").status_code == 404
