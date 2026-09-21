# 发布波次规划 API（Release Wave Planner）

基于 **Python + FastAPI + OR-Tools CP-SAT** 的发布波次规划服务。客户端提交：

- 服务列表及其**故障域**（同故障域的服务不能在同一波发布）；
- 服务间的**有向依赖图**（`from` 必须早于 `to` 发布）；
- **每波并发上限** `max_concurrent`；
- 可选的**波次硬上限** `max_waves`。

服务端生成**波次数最少**、且在相同输入下**顺序确定**的发布方案，使用 SQLite 持久化。
当依赖成环，或约束相互冲突导致任务无法排入时，返回**可定位到具体服务与约束**的结构化原因。

## 一条命令启动

```bash
docker compose up --build -d
```

启动时会自动安装依赖并完成数据库建表（FastAPI lifespan 钩子执行 `CREATE TABLE IF NOT EXISTS`）。

访问地址与端口：

| 用途 | 地址 |
| --- | --- |
| 健康检查 | http://localhost:8000/health |
| Swagger 交互文档 | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |
| OpenAPI 描述 | http://localhost:8000/openapi.json |

停止：`docker compose down`（数据保留在命名卷 `planner-data` 中；
加 `-v` 可连同数据卷一起删除）。

## 配置（环境变量，均有默认值）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PLANNER_DB` | `/data/planner.db` | SQLite 数据库文件路径，父目录会自动创建 |
| `SOLVER_TIME_LIMIT_SECONDS` | `30` | CP-SAT 可行性证明 / 冲突定位的单次时间预算（秒） |
| `SOLVER_OPTIMIZE_SECONDS` | `5` | 无波次上限时，在最少波次内做排序优化的预算（秒），超时交付确定性贪心方案 |
| `SOLVER_NUM_WORKERS` | `1` | CP-SAT 工作线程数，固定为 1 保证结果完全可复现 |
| `SOLVER_RANDOM_SEED` | `42` | CP-SAT 随机种子 |

> 无 `max_waves` 时，服务先用确定性贪心（list scheduling）构造近优可行解，
> 再用 CP-SAT 在“理论下界 ~ 贪心上界”之间二分**证明最少波次**。
> 若极端实例在预算内无法完成证明，接口仍会立即返回满足全部约束的贪心方案，
> 不会超时报错。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/plans` | 创建并求解方案（成功 `201`，不可行 `422`，求解器超时 `503`） |
| `GET` | `/api/plans` | 方案列表（分页 `limit`/`offset`） |
| `GET` | `/api/plans/{id}` | 查询单个方案（含波次明细与输入回显） |
| `DELETE` | `/api/plans/{id}` | 删除方案（`204`） |
| `GET` | `/health` | 健康检查 |

不可行的提交（成环、约束冲突）**同样会入库**，状态为 `infeasible`，可通过 `GET` 取回结构化原因。

### 请求字段

```json
{
  "name": "可选方案名",
  "services": [
    {"id": "gateway", "failure_domain": "az1"},
    {"id": "auth",    "failure_domain": "az1"},
    {"id": "orders"},
    {"id": "payments"}
  ],
  "dependencies": [
    ["gateway", "auth"],
    ["auth", "orders"],
    ["auth", "payments"]
  ],
  "max_concurrent": 2,
  "max_waves": null
}
```

- `dependencies` 为 `[from, to]` 二元组：`from` 必须至少早 `to` 一个波次。
- `failure_domain` 可省略；**同域服务不能同波**。
- `max_waves` 省略时求全局最少波次；给出时还会校验该硬上限。
- 入参非法（重复服务 id、依赖引用不存在的服务、容量 ≤ 0 等）返回 `422` 校验错误。

### 成功响应示例

```bash
curl -s -X POST http://localhost:8000/api/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "services": [
      {"id":"gateway","failure_domain":"az1"},
      {"id":"auth","failure_domain":"az1"},
      {"id":"orders"},{"id":"payments"}
    ],
    "dependencies":[["gateway","auth"],["auth","orders"],["auth","payments"]],
    "max_concurrent":2
  }'
```

```json
{
  "id": "f1c2…", "status": "feasible", "feasible": true,
  "total_waves": 3, "max_concurrent": 2,
  "waves": [
    {"wave": 1, "services": ["gateway"], "concurrent": 1},
    {"wave": 2, "services": ["auth"],    "concurrent": 1},
    {"wave": 3, "services": ["orders", "payments"], "concurrent": 2}
  ],
  "assignments": [
    {"id":"gateway","wave":1,"failure_domain":"az1"},
    {"id":"auth","wave":2,"failure_domain":"az1"},
    {"id":"orders","wave":3,"failure_domain":null},
    {"id":"payments","wave":3,"failure_domain":null}
  ]
}
```

## 结构化错误原因

### 1. 依赖成环：`DEPENDENCY_CYCLE`

用 Tarjan 强连通分量找出所有真实环（含自环），给出每条环上的服务序列：

```json
{
  "status": "infeasible",
  "error": {
    "code": "DEPENDENCY_CYCLE",
    "message": "服务依赖图存在环，环上的服务无法排出先后顺序",
    "detail": {"cycle": {"cycles": [["a", "b", "c"]]}}
  }
}
```

### 2. 给定 `max_waves` 时的快速结构性预检

预检可在不调用求解器的情况下立即指出是哪一类约束、哪些服务导致排不下：

| `code` | 触发条件 | `detail` 定位内容 |
| --- | --- | --- |
| `MAX_WAVES_CHAIN` | 最长依赖链长度超过波次上限 | 具体链上服务序列与所需波数 |
| `MAX_WAVES_CAPACITY` | 按每波容量至少需要的波数超限 | 服务总数、容量与所需波数 |
| `MAX_WAVES_FAILURE_DOMAIN` | 单个故障域服务数超过波次上限 | 故障域 id、域内服务列表与所需波数 |

### 3. 约束交互冲突：`CONSTRAINTS_INFEASIBLE`

预检无法识别的复合冲突（依赖 × 容量 × 故障域 × 波次上限相互作用），
由 CP-SAT 在**全部门控约束**下求解，提取 unsat core，并用删除法求**最小冲突约束集（MUS）**：
集合内任意一条约束放宽后冲突即解除。每条冲突都带类型、人话描述、相关服务 id
及约束参数（哪条依赖边、哪个波次、哪个故障域、多大的容量/上限）：

```json
{
  "error": {
    "code": "CONSTRAINTS_INFEASIBLE",
    "message": "约束之间相互冲突，无法排出可行的波次方案；conflicting_constraints 给出最小冲突约束集（其中任意一条放宽后该冲突即解除），可定位到具体服务与约束",
    "detail": {"constraint_count": 6},
    "conflicting_constraints": [
      {"type": "service_assignment", "service_ids": ["B"],
       "description": "服务 B 必须被排入某个波次"},
      {"type": "service_assignment", "service_ids": ["C"],
       "description": "服务 C 必须被排入某个波次"},
      {"type": "dependency", "from_service": "A", "to_service": "B",
       "service_ids": ["A", "B"],
       "description": "依赖约束：A 必须早于 B 至少一个波次发布"},
      {"type": "dependency", "from_service": "A", "to_service": "C",
       "service_ids": ["A", "C"],
       "description": "依赖约束：A 必须早于 C 至少一个波次发布"},
      {"type": "failure_domain", "failure_domain": "f", "wave": 2,
       "service_ids": ["B", "C"],
       "description": "故障域 'f' 的服务（B, C）不能在第 2 波同时发布"},
      {"type": "max_waves_limit", "max_waves": 2,
       "service_ids": ["A", "B", "C", "D", "E"],
       "description": "全部服务必须在第 2 波（含）之前发布"}
    ]
  }
}
```

### 4. 求解器超时：`SOLVER_TIMEOUT`（HTTP 503）

仅在给定 `max_waves` 且可行性证明超时（默认 30s）时出现，记录入库并提示调大
`SOLVER_TIME_LIMIT_SECONDS`。

## 确定性说明

相同请求体重复提交得到完全一致的波次分配，由以下措施共同保证：

- CP-SAT 固定单线程（`SOLVER_NUM_WORKERS=1`）与固定随机种子；
- 最终排法使用按输入服务顺序加权的字典序目标（先出现的服务优先排入更早波次）；
- 环与链的输出做了归一化（最小 id 起始、集合排序）；
- 贪心构造按服务 id 排序选取。

## 本地开发（不用 Docker）

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
pytest -q
```

## 项目结构

```
app/
  config.py     # 环境变量配置
  schemas.py    # 请求/响应与结构化错误模型
  scheduler.py  # 环检测、预检、贪心构造、CP-SAT 建模与 MUS 定位
  db.py         # SQLite 连接、启动建表、方案持久化与行→响应转换
  main.py       # FastAPI 路由（创建/查询/列表/删除）
tests/          # pytest 单元与接口测试
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
