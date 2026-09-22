# 本地开发环境控制面（Python / FastAPI）

用本地状态（内存 + 单个 JSON 文件持久化）模拟开发环境的**申请、占用与释放**。
目标是让并发提交、重复请求、配额争抢与进程重启后的资源归属，都有
**一致且可解释**的结论。

- 无外部依赖（数据库/消息队列都不需要），状态落一个 JSON 文件
- 同一 `request_id` 的重复或并发提交只产生一个环境，返回一致结果
- 全局配额 + 归属方（owner）配额两级约束，并发不超额
- 租约（lease）超时自动可回收，回收与释放均幂等
- 进程重启后状态与对外视图一致；历史快照缺新增字段时按缺省语义兼容读取
- 拒绝原因以**独立错误码**区分（见下文错误码表）

---

## 1. 快速开始

```bash
uv sync
uv run uvicorn main:app --reload          # 启动服务（默认 :8000）
# 交互式文档： http://127.0.0.1:8000/docs
```

可选环境变量：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `CONTROL_PLANE_STATE_FILE` | `control_plane_state.json` | 状态文件路径；仅本地文件持久化 |
| `CONTROL_PLANE_GLOBAL_LIMIT` | `5` | 全局同时占用（ACTIVE）环境上限 |
| `CONTROL_PLANE_DEFAULT_LEASE_TTL` | `60` | 默认租约 TTL（秒） |

运行测试：

```bash
uv run pytest                      # 全部用例（并发争抢/重复提交/配额耗尽/租约回收/重启兼容）
uv run pytest --show-logs -s       # 额外在终端回显每个用例的判定日志
```

每个用例的判定日志同时写入 `tests/logs/<测试名>.log`，包含
**请求标识、判定依据、配额变化前后值**。

---

## 2. 状态机

环境只有四种状态：

```
                 allocate（申请，内部一步激活）
   (不存在)  ───────────────────────────────▶  ACTIVE
                                                  │
                         release（持有方释放）      │  reclaim（租约到期回收）
                                                  ▼
                                            RELEASED / EXPIRED（均为终态）
```

| 状态 | 含义 | 是否占配额 |
| --- | --- | --- |
| `ACTIVE` | 已分配且租约有效，被 owner 占用 | 是 |
| `RELEASED` | 持有方显式释放（终态） | 否 |
| `EXPIRED` | 租约超时被回收（终态） | 否 |
| `ALLOWED` 迁移表 | `ALLOCATED→{ACTIVE,RELEASED}`，`ACTIVE→{RELEASED,EXPIRED}`，终态不可迁出 | — |

迁移规则（非法迁移返回 `INVALID_TRANSITION`）：

- 申请成功即 `ACTIVE`（代码中保留 `ALLOCATED` 枚举以表达状态机定义，实际一步激活）。
- `ACTIVE` 可 `release → RELEASED`；租约到期可回收为 `EXPIRED`。
- `RELEASED` / `EXPIRED` 为终态：
  - 对终态**重复释放/回收是幂等**的，直接返回当前视图，不报错、不重复扣配额。
  - 对 `RELEASED` 续约 → `INVALID_TRANSITION`；对 `EXPIRED` 续约 → `LEASE_EXPIRED`，两者错误码不同。
- 非持有方操作 → `OWNER_MISMATCH`（403）。

---

## 3. 幂等与并发模型

- 进程内所有「判定 + 变更」在**同一把可重入锁**内完成，因此
  「检查配额 → 扣配额 → 建环境 → 落盘」是原子的，并发争抢不会超额。
- 每个申请由 `request_id` 标识，请求记录与结论被缓存：
  - **相同参数的重复/并发提交**：折叠为同一个环境；首个请求 `replayed=false`，
    其余等待锁后回放，`replayed=true`，返回同一个 `env_id`。
  - **同一 `request_id` 但意图不一致**（owner 不同、TTL 不同）→
    `REQUEST_CONFLICT`（409），不折叠。
  - **确定性拒绝也缓存并回放**：例如因配额不足失败的 `request_id`，
    在配额释放前重复提交得到完全一致的拒绝（同 scope / used / limit）；
    配额释放后仍回放原结论——要重新申请请使用**新的 `request_id`**。
  - 参数非法（空 id、TTL ≤ 0、TTL 超上限）属调用方错误，返回
    `INVALID_REQUEST`（422），不占用、不缓存。

---

## 4. 配额规则

- 两级配额：全局上限（池容量）与 owner 上限。owner 未单独配置时，
  默认上限等于全局上限。
- 只有 `ACTIVE` 环境计入用量；`RELEASED` / `EXPIRED` 立即归还配额，
  归还后的配额可被**新的 `request_id`** 立即再次分配。
- 申请时**先检查全局、再检查 owner**；当两级同时耗尽，结论为
  `GLOBAL` 原因（检查顺序固定，保证拒绝方可解释、可复现）。
- 错误负载带 `scope`（`global`/`owner`）、`used`、`limit`、`owner`，
  两种配额拒绝可直接区分。
- 申请/查询/续约前都会先做一次过期扫描（隐式回收），因此过期环境
  不会一直占着配额。

---

## 5. 租约与回收

- 每个 `ACTIVE` 环境带 `lease_expires_at = 创建/续约时间 + ttl`。
- `renew`：仅持有方、且租约未过期时成功，过期时间顺延一个 TTL，
  `renew_count + 1`；可传 `lease_ttl_seconds` 指定新 TTL（不得超过上限）。
- **到期判定确定**：`now >= lease_expires_at` 即到期。
- 回收途径：
  - 显式 `POST /v1/reclaim-expired`；
  - 任意申请/查询/续约/释放操作前隐式扫描。
- 回收把到期环境 `ACTIVE → EXPIRED` 并归还配额；对同一批环境重复
  回收返回空列表，天然幂等。
- 已回收环境不能续约（`LEASE_EXPIRED`），需要新环境请重新申请。

---

## 6. 持久化与兼容约定

- 状态全量写入单个 JSON 文件，采用「临时文件 + `os.replace`」原子替换：
  崩溃时要么是旧文件要么是新文件，不会出现半截 JSON。
- 每次结论产生后落盘；启动时加载并重建内存状态。
- **进程重启一致性**：
  - 未过期的 `ACTIVE` 环境、归属、续约次数、幂等记录原样恢复；
  - 加载时按当前时间立即回收已过期租约，使配额与对外视图重启后即一致；
  - 被缓存的成功/拒绝结论重启后继续按原结果回放。
  - 旧快照中残留的 `PENDING` 请求（正常流程不会落盘）按确定语义标记为
    `REQUEST_INTERRUPTED`，调用方应换新 `request_id` 重试。
- **历史记录兼容（缺字段不失败，按确定缺省补全）**：

  | 缺失字段 | 缺省语义 |
  | --- | --- |
  | `state` | `ACTIVE`（历史环境默认处于占用中） |
  | `lease_ttl_seconds` | `60.0` |
  | `lease_expires_at` / `released_at` / `expired_at` | `null` |
  | `created_at` / `updated_at` | `0.0` |
  | `renew_count` | `0` |
  | 请求记录的 `intent` / `status` | `ALLOCATE` / `PENDING` |
  | 顶层 `config` / `environments` / `requests` | 空配置 / 空列表 |

  身份字段（`env_id` / `owner` / `request_id`）缺失仍视为数据损坏。
  注意：历史环境若缺 `lease_expires_at`，不会被过期回收（无到期点），
  仍计入占用，保证重启后配额视图与历史归属一致。

---

## 7. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/environments` | 申请（body: `request_id`,`owner`,`lease_ttl_seconds?`），201 返回 `replayed` 与环境 |
| POST | `/v1/environments/{env_id}/renew` | 持有方续约 |
| POST | `/v1/environments/{env_id}/release` | 持有方释放（终态幂等） |
| POST | `/v1/reclaim-expired` | 回收所有到期租约，返回 `env_ids`（幂等） |
| GET | `/v1/environments` | 列表（默认仅 ACTIVE，`?include_terminal=true` 含终态） |
| GET | `/v1/environments/{env_id}` | 环境详情 |
| GET | `/v1/requests/{request_id}` | 幂等记录（含缓存的成功/拒绝结论） |
| GET | `/v1/quota?owner=team-a` | 全局/归属方配额用量 |

错误响应统一为：

```json
{ "detail": { "error": "QUOTA_EXHAUSTED", "message": "...", "request_id": "r1",
              "scope": "owner", "used": 1, "limit": 1, "owner": "team-a" } }
```

### 错误码（拒绝原因可区分）

| error | HTTP | 触发场景 |
| --- | --- | --- |
| `INVALID_REQUEST` | 422 | 空 id、TTL 非法/超上限 |
| `REQUEST_CONFLICT` | 409 | 同 `request_id` 意图不一致 |
| `QUOTA_EXHAUSTED` | 409 | 全局/归属方配额不足（`scope` 区分） |
| `INVALID_TRANSITION` | 409 | 非法状态迁移（如对 RELEASED 续约） |
| `LEASE_EXPIRED` | 409 | 对已回收（EXPIRED）环境续约 |
| `OWNER_MISMATCH` | 403 | 非持有方操作 |
| `ENVIRONMENT_NOT_FOUND` | 404 | 环境/请求不存在 |

### curl 示例

```bash
curl -s -X POST localhost:8000/v1/environments \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-1","owner":"team-a","lease_ttl_seconds":60}'

curl -s -X POST localhost:8000/v1/environments/<env_id>/renew \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-1","owner":"team-a"}'

curl -s -X POST localhost:8000/v1/reclaim-expired
curl -s "localhost:8000/v1/quota?owner=team-a"
```

---

## 8. 本地验证方法

```bash
uv run pytest -q                       # 31 个用例
uv run pytest --show-logs -s           # 同时回显判定日志
ls tests/logs/                         # 每个用例一份日志：请求标识/判定依据/配额变化
```

关键场景与可复现性：

- **并发争抢**：`test_global_quota_concurrent_contention_never_overcommits`
  （32 线程抢 4 个名额，恰 4 个成功、其余全部 `scope=global` 拒绝）；
  并发结论不依赖调度，不变量恒成立。
- **重复/并发同请求**：相同 `request_id` 恒返回同一 `env_id`，
  且只有 1 个 `replayed=false`。
- **配额耗尽与区分**：owner 与 global 两种拒绝分别断言 `scope/used/limit`。
- **租约回收**：测试注入虚拟时钟（`FakeClock`），到期点、幂等回收、
  配额归还与再分配全部确定性复现。
- **重启与兼容**：基于临时状态文件构造新实例，验证归属/视图/幂等结论一致，
  并直接读取缺字段的历史快照验证兼容补全。

---

## 9. 代码结构

```
controlplane/
  models.py     # 状态机、Environment / RequestRecord、缺省字段兼容反序列化
  errors.py     # 错误码体系（拒绝原因一一对应）
  clock.py      # 时钟抽象（测试用 FakeClock 确定性推进时间）
  storage.py    # JSON 原子写 / 兼容读
  service.py    # 核心控制面：单锁原子判定、幂等记录、配额、租约回收、判定日志
  http_api.py   # FastAPI 路由与统一错误响应
main.py         # 入口：组装控制面并暴露 app
tests/          # 并发 / 幂等 / 配额 / 租约 / 持久化兼容 / HTTP 端到端
```
