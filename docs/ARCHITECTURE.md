# StreamPilot 架构设计

> 上游文档：[PLAN.md](PLAN.md) · 接口契约：[API.md](API.md) · 数据模型：[DATA-MODEL.md](DATA-MODEL.md)

## 1. 架构原则

| 原则 | 含义 | 落地方式 |
| --- | --- | --- |
| 合规优先 | 与平台规则冲突的能力一律不做 | 只接官方开放平台；审核 fail-closed |
| 单写者 | 一场直播同一时刻只有一个发声者与一个推流者 | `session_id` 维度独占锁；队列串行消费 |
| 流式优先 | 不等全文，尽早出声 | LLM 流式 + 句级切分 + TTS 流式 |
| 可接管 | 每个自动化环节都有人工入口 | 控制面统一 `/control` 与 `/takeover` |
| 可回放 | 每个决策点留痕 | `trace_id` 贯穿 + 审核/生成/合成日志落库 |
| 云边分离 | 本地只做必须本机的操作 | 本地仅登录态与凭据桥，无业务数据 |
| 能力可替换 | 外部模型与审核可切换 | 所有外部调用收敛到 Gateway |

## 2. 组件与状态归属

| 状态 | 归属 | 生命周期 | 一致性要求 |
| --- | --- | --- | --- |
| 场次状态机 | Redis `sp:session:{id}:state` + MySQL `stream_sessions` | 场次级 | 强一致（单写者） |
| 发言队列 | Redis ZSet | 分钟级 | 单消费者顺序消费 |
| 幂等记录 | Redis SETNX | 1 小时 | 最终一致可接受 |
| 会话上下文 | Redis List + MySQL 摘要 | 场次级 | 允许丢失最近 1 轮 |
| 人设与配置 | MySQL + 版本快照 | 长期 | 强一致，变更需审批 |
| 审核事件 | MySQL `moderation_events` | 90 天~1 年 | 写入成功才可发声（fail-closed） |
| 音频缓存 | OSS + Redis 指针 | 7 天 | 可重建 |
| 推流凭据 | 内存 + KMS 密文引用 | 场次内 | 严禁落库明文；仅 Streamer 可解密 |
| 推流状态 | Redis + Streamer 内存 | 实时 | 允许秒级延迟 |

**关键约束**：`orchestrator` 是唯一能推进发言状态的服务；`streamer` 是唯一能写 RTMP 的服务。其他服务只能通过事件或控制 API 间接影响。

## 3. 运行时数据流

### 3.1 正常路径

```
平台 → Ingest GW →(event.comment.accepted)→ Orchestrator
Orchestrator → Safety(check input) → 允许 → 入队
Orchestrator → LLM GW(stream) → 句级 chunk
  → Safety(check output) → 通过 → TTS GW(stream)
  → Renderer(audio chunk + timestamps) → Streamer → 平台
```

实现：`src/streampilot/orchestrator/runtime.py` 的 `DialogueRuntime` 每场次一个，评论经输入审核后入队；后台线程统一经 `ControlService.dequeue()` 取话，LLM 流式输出由 `OutputGuard` 句级审核后逐句合成与播放。TTS 失败降级字幕，LLM 最终失败播放预设话术；接管、暂停、静音或熔断时取话返回空并中止在途生成。装配与降级链见 `orchestrator/bootstrap.py`。

### 3.2 人工接管路径

```
操作者 → Console → POST /v1/streams/{id}/takeover
  → Orchestrator 置 state=HUMAN_TAKEOVER（优先级最高）
  → 停止消费发言队列、清空待发声、通知 TTS/Renderer 静默
  → Streamer 保持推流但只输出静音/待机画面（可配置）
  → 全量操作写 audit_logs 与 takeovers
```

实现：`src/streampilot/control/service.py` 的 `ControlService` 提供接管/释放、暂停/恢复、静音、切人设、预设话术与重连；`ControlSink` 向 TTS/Renderer/Streamer 下发静默与恢复。`dequeue()` 是编排器唯一取话入口，接管、暂停、静音或熔断时直接返回空，保证接管期间 AI 完全静默。审计与接管记录通过 `AuditStore` / `TakeoverStore` 落库（首发为进程内实现）。释放接管时若接管前处于熔断，恢复到 `circuit_open`，仍需人工确认才能发声。控制台页面由控制面服务在 `/console` 托管，只调用 REST 接口。

### 3.3 云端推流与凭据流

```
运营 → 直播伴侣（人工登录）→ 读取/确认 RTMP 地址与 key
     → Local Bridge（mTLS + 15min 令牌）→ POST /v1/bridge/rtmp
     → 云端 KMS 加密短存（只保留密文引用 + 过期时间）
     → Streamer 启动时解密到内存 → RTMP 直推平台
     → 会话结束/收播 → 立即清除内存与密文引用

失败分支：
  凭据过期/平台拒绝 → Streamer 停止推流 → 告警 → 人工重新获取
  平台限流/警告     → 降码率/降帧 → 仍失败则停播 → 人工接管
```

**约束**：本地桥不具备推流能力；推流地址不落库明文、不进日志、不回显前端；任何时刻只有一个 Streamer 持有该场次凭据。

### 3.4 熔断路径

```
Safety 命中高危 / 错误率超阈值
  → 置 sp:safety:circuit=OPEN（按场或全局）
  → Orchestrator 立即停止生成与发声
  → Renderer 切静态合规画面 + 标识
  → 告警（Pager）+ 记录 incidents
  → 恢复需人工确认（默认）或冷却期后自动探测
```

## 4. 延迟预算

### 4.1 测量点

| 编号 | 测量点 | 数据来源 |
| --- | --- | --- |
| T0 | 网关收到回调 | `ingest.received_at` |
| T1 | 输入审核完成 | `moderation_events.finished_at` (direction=input) |
| T2 | 进入发言队列 | `orchestrator.enqueued_at` |
| T3 | LLM 首 token | `llm.first_token_at` |
| T4 | 首句输出审核完成 | `moderation_events.finished_at` (direction=output) |
| T5 | TTS 首包 | `tts.first_chunk_at` |
| T6 | 渲染首帧 | `renderer.first_frame_at` |
| T7 | 推流写出首帧 | `streamer.first_frame_written_at` |

**核心指标**：`T7 - T0`（首帧音频/画面延迟）、`T3 - T0`（思考延迟）、`T5 - T3`（合成延迟）。

### 4.2 预算拆解（与 PLAN 第 6 节一致）

| 区段 | p50 | p95 | 主要风险 |
| --- | --- | --- | --- |
| T0→T1 输入审核 | 195ms | 490ms | 云审核抖动、词库过大 |
| T1→T2 排队 | 300ms | 800ms | 聚合窗口配置过大 |
| T2→T3 LLM 首 token | 470ms | 950ms | 提示词过长、模型限流 |
| T3→T5 TTS 首包 | 400ms | 850ms | 冷启动、并发排队 |
| T5→T7 渲染+推流 | 380ms | 800ms | 缓冲过大、帧率抖动 |
| **合计** | **≈1.75s** | **≈3.9s** | 预留 10% 余量 |

> 若 p95 连续 3 天超过 4.5s，触发架构评审：优先削减聚合窗口、缩短提示词、扩大 TTS 缓存。

### 4.3 优化手段优先级

1. **预热**：开播前预建立 LLM/TTS 连接，加载形象与音色。
2. **缓存**：欢迎语、常见问答、固定话术直接命中音频缓存。
3. **并行**：输出审核与 TTS 合成分块并行（审核通过一块、合成一块）。
4. **裁剪**：上下文只保留最近 6 轮 + 摘要，提示词控制在预算内。
5. **降级**：延迟超阈值时切短回复模板，而非继续等待。

## 5. 并发、顺序与一致性

- **顺序**：同一 `session_id` 的事件由单消费者按 `received_at` 顺序处理；跨场次完全并行。
- **幂等**：`platform_msg_id` 与 `Idempotency-Key` 双保险；重复事件直接 ack 不处理。
- **并发上限**：单场同时最多 1 个 LLM 流、1 个 TTS 流、1 个渲染输出；不同观众请求在队列中串行化。
- **状态冲突**：所有状态迁移使用 Lua 脚本做 CAS，非法迁移返回 `409 STATE_CONFLICT`。
- **超时**：任何阶段超时都进入「丢弃该条 + 计数 + 告警」，不阻塞后续事件。

### 5.1 场次状态机

实现在 `src/streampilot/orchestrator/state.py`，配套单测 `tests/test_state_machine.py`。

| 当前状态 | 允许迁移到 | 触发条件 |
| --- | --- | --- |
| `idle` | `interacting` / `takeover` / `circuit_open` / `ended` | 新评论入队 / 人工接管 / 熔断 / 收播 |
| `interacting` | `speaking` / `cooldown` / `circuit_open` / `takeover` / `ended` | 首句审核通过 / 无话可说 / 熔断 / 接管 / 收播 |
| `speaking` | `interacting` / `cooldown` / `circuit_open` / `takeover` / `ended` | 有新评论 / 说完冷却 / 熔断 / 接管 / 收播 |
| `cooldown` | `idle` / `interacting` / `circuit_open` / `takeover` / `ended` | 冷却结束 / 新评论 / 熔断 / 接管 / 收播 |
| `circuit_open` | `idle` / `takeover` / `ended` | 人工确认解除 / 接管 / 收播 |
| `takeover` | `idle` / `ended` | 释放接管 / 收播 |
| `ended` | 无（终态） | — |

规则：

1. **优先级**：`takeover` 与 `circuit_open` 可打断任何非终态；`ended` 只能从非终态进入，进入后不可迁出。
2. **不可跳过**：`circuit_open` 不能直接回到业务流程状态，必须先经人工确认回到 `idle`，防止熔断未处置就恢复发声。
3. **非法迁移**：未在表中的迁移一律拒绝，控制面映射为 `409 STATE_CONFLICT`。
4. **审计**：每次迁移记录 `(目标状态, 原因)`，原因携带操作人或熔断依据，落 `audit_logs`。

**场次生命周期协作**：`src/streampilot/session/` 负责创建、开播、收播与恢复，运行态操作经 `ControlService` 在同一把锁内完成。收播时 `control/lifecycle.py` 结束状态机（进入终态 `ended`）、清空待发声并通知下游静默；接管中收播会先结束接管记录。恢复开播时直接替换为新的状态机实例（`ended` 仍为终态，不放宽迁移表），并复用创建时固化的配置快照（人设版本、提示词摘要、音色、形象、标题、平台账号、房间引用），保证事后追溯。

## 6. 失败模式与降级矩阵

| 失败点 | 检测方式 | 自动动作 | 是否需人工 | 对外表现 |
| --- | --- | --- | --- | --- |
| 平台回调中断 | 60s 无事件 | 探测 + 告警 + 切换等待话术 | 是（确认平台侧） | 互动暂停 |
| 验签失败激增 | 错误率 > 5% | 拒绝请求 + 告警 | 是 | 无（保护性拒绝） |
| 输入审核超时 | 300ms 超时 | 该条丢弃 + 计数 | 否 | 该条不回复 |
| 审核服务整体不可用 | 连续错误 | 全局熔断，停止发声 | 是（人工恢复） | 静默/安全话术 |
| LLM 限流 | 429/超时 | 降级快模型 → 模板 | 否 | 回复变短 |
| LLM 内容异常 | 输出审核不通过 | 丢弃该句 + 改写一次 | 否 | 该句不出现 |
| TTS 失败 | 首包超时 | 重试 1 次 → 字幕模式 | 否 | 无声有字 |
| 渲染崩溃 | 心跳 2s 丢失 | 重启渲染 + 静态形象 | 是（观察） | 画面短暂静止 |
| RTMP 断开 | 推流状态/帧率 | 指数退避重连 | 否 | 短暂中断 |
| 本地桥离线 | 心跳 10s | 禁止新开播 + 告警 | 是 | 在播不受影响 |
| 磁盘/内存压力 | 系统指标 | 降码率、清缓存、拒绝新场次 | 是 | 画质下降 |

### 6.1 重连与码率阶梯

实现在 `src/streampilot/streamer/reconnect.py`（`StreamerRecovery`），配套单测 `tests/test_streamer_recovery.py`。控制器由调度器每 1~2 秒调用一次 `tick()`，读取 `StreamerSupervisor.health()` 判断进程是否存活；`set_profile()` 只改配置不打断现有推流，需要立即生效时由控制器显式 `stop()` + `start()`。

| 环节 | 策略 | 取值 |
| --- | --- | --- |
| 断流检测 | 健康快照 `state != running` | 下一 tick 即判定 |
| 指数退避 | `min(base × factor^(n-1), max)` | 1s → 2s → 4s → 8s（封顶 8s） |
| 码率降档 | 每次失败降一档，不低于下限 | −400kbps，最低 800kbps |
| 节点切换 | 同节点连续失败达到阈值 | 3 次后切备用，用尽后回主节点 |
| 恢复回补 | 稳定运行达到阈值后升一档 | 60s 后 +400kbps，回到基准为止 |

约束：

1. **恢复时限**：退避封顶 8s，加上探测与启动开销，人为断网应在 15s 内恢复（LIV-03 验收口径）。
2. **单 tick 单动作**：一次 `tick()` 最多发起一次重启，避免重连风暴；重启成功后要等下一次健康探测才转回 `running`。
3. **回补方式**：升档以 `stop()` + `start()` 受控重启生效，存在亚秒级中断；仅在持续稳定后触发，避免抖动期反复切换。
4. **节点顺序**：备用节点用尽后回到主节点（环形），不因单节点持续失败而停止尝试。
5. **日志约束**：只记录尝试次数、等待时长、码率与节点序号，不记录推流地址与密钥。

## 7. 扩展路径

| 阶段 | 规模 | 架构形态 |
| --- | --- | --- |
| 首发 | 1 直播间（抖音） | 单台 ECS + GPU 渲染节点 + 单 Redis/RDS |
| 小规模（Post-1.0） | 3~5 间 | 应用与渲染分机；Redis 主从；按场次分区队列 |
| 多租户（Post-1.0） | 10+ 间 | ACK 集群；按租户限流与配额；渲染节点池 + 调度器 |
| 多平台（Post-1.0） | 抖音 + 其他官方开放平台 | 新增 `ingest-<platform>` 适配器，内核不变 |

**不提前做的事**：多租户计费、微服务拆分、独立消息中间件、跨云多活。首发只交付单平台单间，这些能力只保留扩展点。

## 8. 安全设计

- **网络**：只暴露 443；RDS/Redis 仅 VPC 内；推流走固定 EIP 便于平台侧稳定。
- **鉴权**：控制台 OIDC/JWT + RBAC；平台回调 HMAC 签名；本地桥 mTLS + 15 分钟短期令牌。
- **密钥**：本地 `.env`（忽略）、生产 KMS 注入；密钥轮换有流程；日志脱敏（手机号、用户 ID、推流地址）。
- **推流凭据**：只允许内存与 KMS 密文引用；Streamer 单写者独占；禁止日志、截图、前端回显与工单粘贴。
- **数据**：观众 ID 单向哈希；评论脱敏后入库；导出需审批。
- **供应链**：CI 密钥扫描 + 依赖漏洞扫描 + 镜像固定版本。
- **审计**：接管、配置变更、密钥操作、数据导出全部落 `audit_logs`。

## 9. 开放问题

1. 平台书面推流许可的取得时间与附加限制（决定 M0 出口条件）。
2. Live2D 模型复杂度、动作集数量与 1080p30 的 GPU 余量，需在 M4 压测确认。
3. 推流地址失效与平台限流的具体表现，需在 M6 灰度期采集并固化处置策略。
4. 是否引入实时语音互动（ASR + 回声消除），当前列为 Post-1.0。
