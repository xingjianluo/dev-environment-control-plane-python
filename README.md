# 本地开发环境控制面（Local Dev Environment Control Plane）

一个本地可运行的 HTTP 服务，用本地 JSON 状态文件模拟开发环境的**申请、占用与释放**。
目标是让以下场景都能得到一致且可解释的结论：

- 同一 `request_id` 的重复或并发提交，只产生一个环境，返回一致结果（幂等）；
- **请求结论一旦产生即永久固定**：申请成功永远返回同一个环境；因归属方/全局配额满被拒，
  之后无论隔多久、容量是否因释放/回收而恢复、进程是否重启，同一 `request_id` 永远返回
  当初同一条拒绝结论（同一个 `code`/`reason`/`decided_at`），不会翻盘成成功；
- 被拒请求同样落盘并可按 `request_id` 追溯（成功/被拒、拒绝原因、结论时间），
  与“从未提交过”的标识严格区分；
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
| `GET  /requests/{request_id}` | 按幂等键查询**冻结结论**（成功带 env 实时状态；被拒带 code/reason/decided_at；未提交 404） |
| `GET  /quota` | 当前全局/各 owner 配额用量 |

申请请求示例：

```bash
curl -s -X POST localhost:8000/environments/apply \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-001","owner":"alice","lease_seconds":60}'
```

申请成功返回 `200`；容量不足返回 `409`，但**判定依据以响应体 `code` 为准**。
每个响应都带 `quota_after`，可直接解释“判定后配额长什么样”，并带 `decided_at`
（结论做出时间，ISO-8601）。重复/回放的请求带 `deduplicated=true`：成功回放返回原环境，
拒绝回放返回原拒绝。

## 请求结论冻结规则（核心语义）

每个 `request_id` 在**第一次提交**时产生且仅产生一条结论（verdict），随后永久固定：

| 首次结论 | 之后同 id 再提交（任意时间/容量/重启后） |
| --- | --- |
| 成功（`accepted=true`） | 永远返回**同一个 `env_id`**；响应中的环境 `state`/`lease_expires_at` 是实时值（环境可能已走到 ACTIVE/RELEASED），但“成功且对应这个环境”永不变 |
| 归属方配额满（`OWNER_QUOTA_EXHAUSTED`） | 永远返回**同一条拒绝**：相同 `code`、相同 `reason`、相同 `decided_at`，`deduplicated=true`；即使中途有环境释放、容量空出也**不会翻盘成成功** |
| 全局配额满（`GLOBAL_QUOTA_EXHAUSTED`） | 同上，永久固定为该全局拒绝结论 |

补充约束：

- 结论只与 `request_id` 绑定。老标识被冻结不影响**新标识**——只要容量够，新 id 照常成功。
  被拒的归属方想再申请，必须使用新的 `request_id`。
- 被拒结论**不占任何容量**：它只在 `decisions` 表占一行，全局与归属方配额都不增加；
  拒绝回放同样不占容量。因此冻结拒绝不会让配额约束被突破。
- “从未提交过”与“被拒过”严格区分：不在 `decisions` 表中的 id 查询返回 `404 UNKNOWN_REQUEST`；
  被拒过的 id 查询返回 `200` 且 `accepted=false`。
- 所有首次判定都在单一状态锁（`store.lock`）内完成并原子落盘；同一 id 大量并发提交只会
  生成一条结论，所有调用方拿到的 `env_id`（成功）或 `code/reason/decided_at`（拒绝）完全一致。
- 入参非法（空 `request_id`/`owner`、非正租约）在进入判定前直接返回 `INVALID_REQUEST`，
  不产生结论、不占用该 id。

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
- 申请判定顺序：**先查冻结结论 → 再 owner 配额 → 再全局配额**，因此 owner 满员时即便全局有空位也稳定返回 `OWNER_QUOTA_EXHAUSTED`。
- 所有判定在单一状态锁内完成（`store.lock`，可重入），并在持锁期间原子落盘；
  并发申请者要么看到“有名额”并占用，要么看到满员而被拒绝，不会超额。
- **配额类拒绝结论会被冻结并永久占用该 `request_id` 的结论位**：重试同 id 只回放原拒绝，
  不再重新参与争抢（这是本轮新增语义，区别于旧版本“拒绝不占 id、可重试翻盘”）。
  被拒结论不占任何环境配额；想在容量恢复后再次申请，请使用新的 `request_id`。
- 释放或回收后，配额立刻可被**新标识**的申请使用（见测试 `test_released/reclaimed_quota_can_be_reassigned`，
  以及 `test_rejected_verdict_is_frozen_after_quota_recovers` 证明老被拒 id 不会借容量恢复翻盘）。

## 租约规则

- 租约只在 `ACTIVE` 状态运行；`occupy` 时设定到期时间，`renew` 从当前到期时间（未到期则顺延，已过期则从 `now`）延长。
- `reclaim` 是系统操作，只对“`ACTIVE` 且 `lease_expires_at <= now`”生效；未到期返回 `LEASE_NOT_EXPIRED`。
- 续约与回收在到期边界并发时，由锁保证唯一结论：续约赢则仍为 `ACTIVE`，回收赢则为 `RECLAIMED`（见 `test_concurrent_reclaim_and_renew_has_one_winner`）。
- 回收与释放均幂等，重复调用返回一致视图。

## 持久化与重启一致

- 每次状态变更后，先在内存状态上完成判定，再以“临时文件 + `os.replace`”原子写入 JSON，避免半文件。
- 当前磁盘格式为 **v3**，在 v2 的 `environments` / `request_index` 之外新增 `decisions` 结论表，
  每行记录 `request_id, accepted, owner, decided_at, env_id?, code?, reason?`。
- 进程启动时从文件重建 `environments`、`request_id → env_id` 索引与 `decisions` 结论表；
  重启后配额视图、归属、状态以及每条结论（含拒绝原因与结论时间）都与重启前一致。

## 存量状态文件兼容范围（v1/v2 升级到 v3）

加载旧版本状态文件时不会报错，按下述**确定性回填**规则工作，首次落盘即升级为 v3：

| 旧文件中的内容 | 升级后行为 |
| --- | --- |
| `request_index` 中映射到环境的 id（历史成功申请） | 在 `decisions` 中回填一条 `accepted=true` 结论，`env_id` 指向原环境，`decided_at` 取该环境的 `created_at`；之后可在 `GET /requests/{id}` 查到，重复申请仍返回原环境 |
| 旧文件里没有、也无法表达的“被拒请求”（v1/v2 本就不持久化拒绝） | 不凭空捏造拒绝结论；这些 id 升级后按“从未在新规则下提交”处理，首次提交照常参与判定 |
| 环境记录缺少的新增字段 | 继续沿用下表的环境记录缺省值 |

也就是说：**升级后老的成功请求在同样的地方（`GET /requests/{request_id}`）可查，
语义不变；“从未提交”与“被拒过”的区分只对升级后产生的结论有意义。**

### 环境记录缺省值（v1/v2 记录缺字段）

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
- `basis`：判定依据（如 `owner used 1 >= per-owner limit 1`、
  `frozen ACCEPTED verdict -> replay same env`、`frozen REJECTED verdict -> replay same rejection`）；
- `quota_before` / `quota_after`：动作前后的全局与各 owner 配额变化（可直接核对被拒不占容量）；
- `accepted`、`code`、`deduplicated`、`env_id`、`decided_at` 等。

被拒结论首次产生与后续回放各打一条日志：两者 `request_id`/`code` 相同，
回放那条带 `deduplicated=true` 且 basis 为 `frozen REJECTED ...`，便于对账时区分“首次判定”与“老结论回放”。

## 本地验证

```bash
uv run pytest -q                       # 全量单测（44 项）
uv run pytest tests/test_decisions.py # 结论冻结/重启恢复/并发收敛/兼容回填
uv run pytest tests/test_quota.py     # 仅并发争抢/配额
uv run pytest tests/test_lease.py     # 仅租约回收/兼容读取
```

测试覆盖（本轮新增见 `tests/test_decisions.py`）：

- **拒绝冻结、容量恢复后不翻盘**：同一 id 首次被拒，释放环境让出容量后再提交仍返回原
  `code/reason/decided_at`；同场景下新 id 正常成功（`test_rejected_verdict_is_frozen_after_quota_recovers`、
  `test_global_rejection_is_frozen_after_global_capacity_recovers`）；
- **重启后成功/拒绝结论一致**：对同一状态文件新建 manager，两类结论回放与直接查询都与重启前一致
  （`test_verdict_survives_restart_for_accepted_and_rejected`）；
- **容量紧张时同 id 并发收敛**：占满 owner 名额后 20 线程同 id 并发，全部拿到同一条拒绝、
  只落一条结论、不多建环境；有名额时 15 线程同 id 只建一个环境
  （`test_concurrent_same_id_under_pressure_converges_to_one_verdict`、
  `test_concurrent_same_id_with_capacity_creates_one_environment`）；
- **存量兼容**：手写无 `decisions` 的 v2 文件，验证成功结论回填、语义不漂移、再写回为 v3
  （`test_legacy_v2_state_without_decisions_backfills_and_keeps_working`）；
- **未提交 ≠ 被拒**：未提交 id 查询抛 `UNKNOWN_REQUEST`，被拒 id 可查到冻结结论
  （`test_never_submitted_is_distinct_from_rejected`）；
- 原生命周期、两级配额、租约回收、重启与 HTTP 用例全部保留无回归。

### 手工复现（curl）

默认 `GLOBAL=2 / OWNER=1`，先启动：`uv run uvicorn main:app --reload`。

**1) 构造“被拒 → 容量恢复 → 同 id 仍拒绝，新 id 成功”**

```bash
curl -s -X POST localhost:8000/environments/apply -H 'Content-Type: application/json' \
  -d '{"request_id":"a-ok","owner":"alice"}'                       # 200，记录 env_id
curl -s -X POST localhost:8000/environments/apply -H 'Content-Type: application/json' \
  -d '{"request_id":"a-no","owner":"alice"}'                       # 409 OWNER_QUOTA_EXHAUSTED
curl -s localhost:8000/requests/a-no                               # 200，accepted=false + 拒绝原因/时间
curl -s -X POST localhost:8000/environments/<env_id>/release \
  -H 'Content-Type: application/json' -d '{"owner":"alice"}'       # 让出容量
curl -s -X POST localhost:8000/environments/apply -H 'Content-Type: application/json' \
  -d '{"request_id":"a-no","owner":"alice"}'                       # 仍是同一条 409（deduplicated=true）
curl -s -X POST localhost:8000/environments/apply -H 'Content-Type: application/json' \
  -d '{"request_id":"a-fresh","owner":"alice"}'                    # 200，新 id 正常成功
curl -s localhost:8000/requests/never-seen                         # 404 UNKNOWN_REQUEST
```

**2) 构造“重启后结论一致”**

```bash
# 用上一步产生的 data/state.json，Ctrl-C 停服再重启：
uv run uvicorn main:app --reload
curl -s localhost:8000/requests/a-no        # 拒绝原因、decided_at 与重启前完全一致
curl -s localhost:8000/requests/a-ok        # 仍是同一个 env_id
```

**3) 构造“同 id 并发只收敛一个结论”**

```bash
# 先用 b-hold 占满 alice 的 owner 名额，再用同 id 并发猛打：
for i in $(seq 1 20); do
  curl -s -X POST localhost:8000/environments/apply -H 'Content-Type: application/json' \
    -d '{"request_id":"b-hot","owner":"alice"}' -o /tmp/out-$i -w '%{http_code}\n' &
done; wait
# 20 个响应应全部为 409，且 body 的 reason/decided_at 完全一致；环境总数不变
grep -h decided_at /tmp/out-* | sort -u     # 只有一个不同的 decided_at
```

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
tests/                       # 生命周期/配额/租约/HTTP/结论冻结 五组测试
```
