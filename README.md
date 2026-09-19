# 展柜环境变点分析 API

面向文物展柜的温湿度环境监测服务。客户端按设备序号上报带时间戳的温湿度读数，服务对每个展柜的时间线执行**带惩罚项的确定性变点分段**，输出各段起止、均值、相对上一段的变化幅度与置信标记；支持锚点校准、迟到读数重放与不写库的重放预览。

技术栈：FastAPI + SQLite + SQLAlchemy，单容器运行。

## 快速启动

```bash
docker compose up --build
```

服务固定在 **8000** 端口：

- OpenAPI 文档：http://localhost:8000/docs （Swagger UI）
- OpenAPI JSON：http://localhost:8000/openapi.json
- 健康检查：http://localhost:8000/health

SQLite 数据落在命名卷 `cpc-data`（容器内 `/data/cpc.db`），重建容器不丢数据。

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --port 8000
pytest                 # 运行测试（独立临时库，不影响业务数据）
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/readings` | 批量上报读数；同设备同时间戳自动合并；迟到读数触发受影响时间线重放 |
| GET | `/api/v1/devices` | 列出全部展柜（读数/分段/锚点计数、首末时间） |
| GET | `/api/v1/devices/{serial}/readings` | 查询原始读数（`from_ts`/`to_ts`/`limit`/`offset`） |
| GET | `/api/v1/devices/{serial}/segments` | 查询当前分段：起止、条数、均值、变化幅度、置信标记 |
| GET | `/api/v1/devices/{serial}/replays` | 时间线重放审计记录 |
| POST | `/api/v1/devices/{serial}/anchors` | 新增校准锚点（强制分段边界），触发重放 |
| GET | `/api/v1/devices/{serial}/anchors` | 列出锚点 |
| DELETE | `/api/v1/devices/{serial}/anchors/{id}` | 删除锚点，触发重放 |
| POST | `/api/v1/replay-preview` | 重放预览：试算假设读数补入后的分段，**绝不写库** |
| GET | `/health` | 健康检查 |

### 上报读数

```bash
curl -X POST http://localhost:8000/api/v1/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "readings": [
      {"device_serial": "CAB-A", "ts": "2026-09-10T08:00:00Z", "temperature": 20.4, "humidity": 51.2},
      {"device_serial": "CAB-A", "ts": "2026-09-10T08:05:00Z", "temperature": 20.5, "humidity": 51.0}
    ]
  }'
```

- 设备序号首次出现时自动注册。
- 同一 `(设备, 时间戳)` 重复上报会**合并**为一行（后者覆盖数值，`merged_count` 累加）；数值完全一致的重复上报幂等，不触发重放。
- 响应按设备汇总：`inserted / merged / unchanged / duplicates_in_batch / late / replay`。

### 迟到读数与时间线重放

当补入的读数改写了已存在的读数（`changed_from <= 已存最大时间戳`），即判定为**迟到（late）**，服务会：

1. 定位受影响时间线起点：`changed_from` 之前最近的**锚点**（锚点是强制断点，断点前的分段不受影响；无锚点则从头重放）；
2. 在同一事务内删除该窗口旧分段、重算并写入新分段；
3. 写入一条 `ReplayRun` 审计记录（触发源、窗口、读数与分段数），可通过 `/api/v1/devices/{serial}/replays` 查询。

### 锚点校准

```bash
curl -X POST http://localhost:8000/api/v1/devices/CAB-A/anchors \
  -H 'Content-Type: application/json' \
  -d '{"ts": "2026-09-10T04:00:00Z", "note": "开柜更换展品"}'
```

锚点表示一次已知的环境事件（开柜、维护、设备校准），在该时刻之后的第一条读数处强制切分时间线。同时间戳重复提交仅更新备注，幂等不触发重放。

### 重放预览（不写库）

```bash
curl -X POST http://localhost:8000/api/v1/replay-preview \
  -H 'Content-Type: application/json' \
  -d '{
    "device_serial": "CAB-A",
    "readings": [{"ts": "2026-09-11T02:00:00Z", "temperature": 31.0, "humidity": 85.0}],
    "penalty": 12.0
  }'
```

将假设读数与库内数据在内存中合并去重后试算分段，返回 `preview` 分段列表与 `would_change`（若真实补入分段是否会变化）。`persisted` 恒为 `false`，端点不产生任何写库操作；`penalty`/`min_size` 可临时覆盖默认值做 what-if 分析。

## 分段算法

- **PELT**（Pruned Exact Linear Time）：在 `min Σ cost(段) + β·段数` 目标下求精确最优分段，带剪枝；惩罚项 `β` 越大分段越保守。
- 代价函数为段内温湿度二维高斯对数似然（对角方差），并设**方差下限** `min_var`（默认 0.01，相当于 0.1℃/0.1%RH 的传感器分辨率），避免零方差恒定段使代价无界、令惩罚项失效。
- **确定性**：浮点求和顺序固定、并列取较小索引——相同输入（读数 + 锚点 + 参数）必然得到相同分段；每段记录 `algo_version`（含全部参数）便于复核。
- **最小段长** `min_size`（默认 3）：段内读数不得少于此值。
- **变化幅度**：每段给出相对上一段的均值差 `delta_temperature / delta_humidity`（首段为 `null`）。
- **置信标记**：段长达到 `min_confident_len`（默认 6）且（非首段时）与上一段的均值跳变 t 统计量 ≥ `confidence_t`（默认 2.0）记为 `high`，否则 `low`。

## 配置

均通过环境变量覆盖（见 `docker-compose.yml` 示例）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_DATABASE_URL` | `sqlite:////data/cpc.db`（容器） | SQLAlchemy 连接串 |
| `APP_PENALTY` | `12.0` | PELT 惩罚项 β |
| `APP_MIN_SIZE` | `3` | 每段最少读数条数 |
| `APP_MIN_VAR` | `0.01` | 代价函数方差下限 |
| `APP_MIN_CONFIDENT_LEN` | `6` | 高置信所需最小段长 |
| `APP_CONFIDENCE_T` | `2.0` | 置信判定 t 统计量阈值 |

## 项目结构

```
app/
├── main.py            # FastAPI 入口（/docs、/health）
├── config.py          # APP_* 环境变量配置
├── database.py        # 引擎/会话（WAL、外键、busy_timeout）
├── models.py          # Device / Reading / Anchor / Segment / ReplayRun
├── changepoint.py     # 确定性 PELT
├── segmentation.py    # 锚点切分、置信标记、受影响时间线重放
└── routers/           # readings / devices / anchors / replay
tests/                 # pytest：算法单测 + 端到端 API 测试
Dockerfile / docker-compose.yml / requirements*.txt
```

## 设计要点

- **合并去重**：`readings` 表对 `(device_id, ts)` 建唯一约束，上报路径在单事务内完成"查重 → 插入/覆盖 → 重放"，批次内同刻重复以后到者为准。
- **重放的精确性**：锚点是强制断点，断点两侧分段相互独立，因此只需重放最近锚点之后的时间线，结果与全量重算一致；无锚点时自动从头重放。
- **一致性**：写路径（上报/锚点增删）经进程内互斥锁串行化，分段替换与审计记录同事务提交；SQLite 开启 WAL 保证读写并发。
- **预览零副作用**：预览端点只读取库内数据、纯内存试算，显式 `rollback` 收尾。
