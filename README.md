# 本地开发环境控制面（Local Dev Environment Control Plane）

一个本地可运行的 HTTP 服务，用本地 JSON 状态文件模拟开发环境的**申请、占用与释放**。
目标是让以下场景都能得到一致且可解释的结论：

- 同一 `request_id` 的重复或并发提交，只产生一个环境，返回一致结果（幂等）；
- 非法状态迁移、容量不足被拒绝，且拒绝原因通过**稳定错误码**区分；
- 按归属方（owner）与全局两级配额约束，多线程并发争抢不超额；
- 占用带租约（lease），超时未续约可回收并归还配额，回收/释放幂等；
- 进程重启后从状态文件恢复，归属与对外视图保持一致；
- 旧版本历史记录缺少新字段时，按确定的缺省语义兼容读取，不直接失败。

## 快速开始

```bash
uv sync
uv run uvicorn main:app --reload
# 或：uv run fastapi dev main.py
```

可选环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CONTROLPLANE_STATE` | `data/state.json` | 状态文件路径（原子写入） |
| `CONTROLPLANE_GLOBAL_QUOTA` | `2` | 全局同时占用配额 |
| `CONTROLPLANE_OWNER_QUOTA` | `1` | 每个 owner 同时占用配额 |

## HTTP 接口

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /environments/apply` | 申请环境；body: `request_id, owner, lease_seconds?` |
| `POST /environments/{env_id}/occupy` | 占用（PENDING → ACTIVE，租约开始） |
| `POST /environments/{env_id}/renew` | 续约；body: `owner, extend_seconds?`（默认沿用申请时租约长度） |
| `POST /environments/{env_id}/release` | 显式释放（持有者调用） |
| `POST /environments/{env_id}/reclaim` | 系统回收（仅租约已到期的 ACTIVE） |
| `GET  /environments/{env_id}` | 查询单个环境 |
| `GET  /environments?owner=alice` | 列表，可按 owner 过滤 |
| `GET  /requests/{request_id}` | 按幂等键查询结果 |
| `GET  /quota` | 当前全局/各 owner 配额用量 |

申请请求示例：

```bash
curl -s -X POST localhost:8000/environments/apply \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-001","owner":"alice","lease_seconds":60}'
```

申请成功返回 `200`；容量不足返回 `409`，但**判定依据以响应体 `code` 为准**。
每个响应都带 `quota_after`，可直接解释“判定后配额长什么样”。

## 状态机

```
              apply                         occupy
  (不存在) ───────────▶ PENDING ───────────────────────▶ ACTIVE
                           │                              │  │
                           │ release                      │  │ renew (延长 lease_expires_at)
                           ▼                              │  │
                        RELEASED ◀──── release ──────────┘  │
                                                          │
                              reclaim（lease 已到期）       ▼
                       RELEASED/RECLAIMED ◀──────────── RECLAIMED
```

| 当前状态 | 操作 | 结果 |
| --- | --- | --- |
| — | `apply` | 创建 `PENDING`，立即预留配额；租约尚未开始 |
| `PENDING` | `occupy`（owner 匹配） | → `ACTIVE`，置 `lease_expires_at = now + lease_seconds` |
| `ACTIVE` | `occupy` | 幂等 no-op，返回当前视图 |
| `ACTIVE` | `renew`（owner 匹配） | `lease_expires_at = max(now, 旧到期) + extend_seconds` |
| `PENDING/ACTIVE` | `release`（owner 匹配） | → `RELEASED`，归还配额 |
| `RELEASED` | `release` | 幂等 no-op（`deduplicated=true`） |
| `RELEASED` | `reclaim` | 幂等 no-op（已释放，无需回收） |
| `RECLAIMED` | `reclaim` | 幂等 no-op |
| `ACTIVE`（未到期） | `reclaim` | 拒绝 `LEASE_NOT_EXPIRED` |
| `ACTIVE`（已到期） | `reclaim` | → `RECLAIMED`，归还配额 |
| `PENDING` | `reclaim` | 拒绝 `INVALID_STATE_TRANSITION`（无租约） |
| `RECLAIMED` | `release` | 拒绝 `INVALID_STATE_TRANSITION` |
| 任意终态 | `occupy`/`renew` | 拒绝 `INVALID_STATE_TRANSITION` |

## 拒绝原因（稳定错误码）

| code | HTTP | 含义 |
| --- | --- | --- |
| `GLOBAL_QUOTA_EXHAUSTED` | 409 | 全局配额已满 |
| `OWNER_QUOTA_EXHAUSTED` | 409 | 该 owner 配额已满（先于全局判定） |
| `INVALID_STATE_TRANSITION` | 409 | 当前状态不允许该迁移 |
| `LEASE_NOT_EXPIRED` | 409 | 租约尚未到期，不能回收 |
| `OWNER_MISMATCH` | 403 | 调用方不是持有方 |
| `UNKNOWN_REQUEST` | 404 | 环境/请求标识不存在 |
| `INVALID_REQUEST` | 400 | 入参非法（空标识、非正租约秒数等） |

`reason` 面向人阅读（包含如 `2/2` 的用量），`code` 面向程序分支，两者使拒绝原因可区分、可复现。

## 配额规则

- 两级限制：`PENDING`（已预留）与 `ACTIVE` 都计入配额；`RELEASED`/`RECLAIMED` 立即归还。
- 申请判定顺序：**先幂等去重 → 再 owner 配额 → 再全局配额**，因此 owner 满员时即便全局有空位也稳定返回 `OWNER_QUOTA_EXHAUSTED`。
- 所有判定在单一状态锁内完成（`store.lock`，可重入），并在持锁期间原子落盘；
  并发申请者要么看到“有名额”并占用，要么看到满员而被拒绝，不会超额。
- 被拒绝的申请**不占用** `request_id` 索引，重试时会重新参与争抢；成功的申请占用索引并永久幂等。
- 释放或回收后，配额立刻可被新申请使用（见测试 `test_released/reclaimed_quota_can_be_reassigned`）。

## 租约规则

- 租约只在 `ACTIVE` 状态运行；`occupy` 时设定到期时间，`renew` 从当前到期时间（未到期则顺延，已过期则从 `now`）延长。
- `reclaim` 是系统操作，只对“`ACTIVE` 且 `lease_expires_at <= now`”生效；未到期返回 `LEASE_NOT_EXPIRED`。
- 续约与回收在到期边界并发时，由锁保证唯一结论：续约赢则仍为 `ACTIVE`，回收赢则为 `RECLAIMED`（见 `test_concurrent_reclaim_and_renew_has_one_winner`）。
- 回收与释放均幂等，重复调用返回一致视图。

## 持久化与重启一致

- 每次状态变更后，先在内存状态上完成判定，再以“临时文件 + `os.replace`”原子写入 JSON，避免半文件。
- 进程启动时从文件重建 `environments` 与 `request_id → env_id` 索引；重启后配额视图、归属、状态与重启前一致。

## 向后兼容约定（旧记录缺字段）

加载记录时，缺失的新字段按下表填入**确定性缺省值**，而不是报错：

| 缺失字段 | 缺省语义 |
| --- | --- |
| `updated_at` | 继承 `created_at` |
| `lease_expires_at` / `occupied_at` / `released_at` / `reclaimed_at` | `null` |
| `release_count` / `reclaim_count` / `renew_count` / `occupy_count` | `0` |
| `lease_seconds` | `60` |

特别地，历史遗留的 `ACTIVE` 记录若没有 `lease_expires_at`，统一按“租约已到期”处理，
即下一次 `reclaim` 必然可回收并归还配额——这是有意的确定性兼容语义（旧系统无租约时间，
安全做法是允许回收）。记录仅缺少 `env_id/owner/request_id/state/created_at` 等基础字段时才视为损坏并拒绝加载。

## 判定日志

每次状态变更向 stderr 输出一行 JSON，字段包含：

- `request_id`：请求标识；
- `action`：`APPLY/OCCUPY/RENEW/RELEASE/RECLAIM`；
- `basis`：判定依据（如 `global used 2 >= limit 2`、`request_id already mapped -> return same env`）；
- `quota_before` / `quota_after`：动作前后的全局与各 owner 配额变化；
- `accepted`、`code`、`deduplicated`、`env_id` 等。

## 本地验证

```bash
uv run pytest -q                    # 全量单测（35 项）
uv run pytest tests/test_quota.py  # 仅并发争抢/配额
uv run pytest tests/test_lease.py  # 仅租约回收/兼容读取
```

测试覆盖：

- **并发争抢**：20 线程争 2 个全局名额，恰好 2 成功、18 个 `GLOBAL_QUOTA_EXHAUSTED`，不超额；
- **重复提交**：同一 `request_id` 串行与 10 线程并发都只创建一个环境，结果一致；
- **配额耗尽**：owner 与全局两类拒绝码可区分；释放/回收后配额可再次分配；
- **租约回收**：未到期拒绝（`LEASE_NOT_EXPIRED`）、到期回收、续约续命、回收幂等、续约/回收竞争唯一赢家；
- **重启一致与兼容读取**：换一个新 manager 从同一文件恢复；手工构造缺字段的 v1 记录验证缺省语义。

测试使用可控的假时钟（`FakeClock`）和屏障（`Barrier`）放大竞争，因此结果可复现；
断言同时校验日志中的请求标识、判定依据与配额变化。

## 代码结构

```
main.py                      # FastAPI 入口（re-export app）
controlplane/models.py       # 状态、错误码、记录与请求类型
controlplane/store.py        # JSON 原子持久化、旧记录兼容归一化
controlplane/manager.py      # 状态机、配额、租约、幂等判定（全部在锁内）
controlplane/service.py      # HTTP 层、应用工厂、错误码→HTTP 状态映射
controlplane/logging_util.py # 结构化判定日志
tests/                       # 生命周期/配额/租约/HTTP 四组测试
```
