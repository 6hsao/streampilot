# StreamPilot 接口契约

> 版本：v1（Draft）· 相关：[ARCHITECTURE.md](ARCHITECTURE.md#3-运行时数据流)

## 1. 通用约定

| 项 | 约定 |
| --- | --- |
| Base URL | `https://api.<domain>/v1` |
| 传输 | HTTPS，JSON（`application/json; charset=utf-8`） |
| 时间 | ISO-8601 UTC，如 `2026-01-01T12:00:00Z` |
| 金额 | 整数，单位「分」 |
| 标识 | `snake_case` 字段；ID 为字符串（ULID） |
| 版本 | 路径版本 `/v1`；破坏性变更升 `/v2` 并保留旧版至少一个里程碑 |
| 追踪 | 请求头 `X-Trace-Id`；未提供则由服务端生成并回传 |
| 幂等 | 写接口支持 `Idempotency-Key`（建议 ULID），24 小时内重复请求返回首次结果 |

### 1.1 鉴权方式

| 调用方 | 方式 |
| --- | --- |
| 控制台用户 | OIDC 登录后携带 `Authorization: Bearer <JWT>`，RBAC 校验 |
| 平台回调 | `X-Signature` HMAC-SHA256 + `X-Timestamp` + `X-Nonce` |
| 本地桥 | mTLS 客户端证书 + `Authorization: Bearer <短期令牌>`（15 分钟） |
| 服务间 | 内网 + 服务身份令牌（后续可切换 mTLS） |

### 1.2 统一错误响应

```json
{
  "error": {
    "code": "SAFETY_TIMEOUT",
    "message": "content safety check timed out after 300ms",
    "retryable": false,
    "trace_id": "01J8ZQ7K3M9WQ2V6H4T8R1X5B0",
    "details": {"stage": "input", "session_id": "01J8ZQ..."}
  }
}
```

### 1.3 错误码

| HTTP | code | 含义 | 调用方处置 |
| --- | --- | --- | --- |
| 400 | `INVALID_ARGUMENT` | 参数缺失或格式错误 | 修正请求，不重试 |
| 401 | `UNAUTHENTICATED` | 缺少或无效凭证 | 重新认证 |
| 403 | `PERMISSION_DENIED` | 无权限或资质缺失 | 联系管理员 |
| 404 | `NOT_FOUND` | 资源不存在 | 检查 ID |
| 409 | `STATE_CONFLICT` | 状态机非法迁移 | 重新拉取状态 |
| 409 | `ALREADY_TAKEOVER` | 已被人工接管 | 先释放接管 |
| 422 | `SAFETY_BLOCKED` | 内容被安全策略拦截 | 不得重试同一内容 |
| 429 | `RATE_LIMITED` | 超限 | 按 `Retry-After` 退避 |
| 503 | `SAFETY_UNAVAILABLE` | 审核不可用（fail-closed） | 停止发声，告警 |
| 504 | `UPSTREAM_TIMEOUT` | 上游超时 | 按策略降级 |

## 2. 签名算法（平台回调与本地桥）

```
canonical = method + "\n" + path + "\n" + timestamp + "\n" + nonce + "\n" + body_sha256_hex
signature = hex(hmac_sha256(secret, canonical))
```

- `timestamp` 为秒级 Unix 时间；与服务端偏差超过 **300s** 拒绝。
- `nonce` 在 10 分钟内不可重复（Redis SETNX）。
- 签名校验失败统一返回 `401 UNAUTHENTICATED`，不泄露具体原因。

## 3. 平台接入

### 3.1 `POST /v1/ingest/douyin/events`

接收开放平台评论/弹幕事件。平台原始字段可能与示例不同，适配层负责映射为内部 schema。

请求（示意）：

```json
{
  "schema_version": "1",
  "event_type": "comment.created",
  "event_id": "evt_01J8ZQ7K3M9WQ2V6H4T8R1X5B0",
  "occurred_at": "2026-01-01T12:00:00Z",
  "room_ref": "room_123456",
  "message": {
    "msg_id": "msg_987654321",
    "user_ref": "hashed_user_abc",
    "text": "主播这个怎么买？",
    "nickname_ref": "hashed_nick_xyz"
  }
}
```

响应：

```json
{"accepted": true, "event_id": "evt_01J8ZQ...", "trace_id": "..."}
```

规则：

- 成功受理返回 `202 Accepted`；
- 重复 `event_id` / `msg_id` 返回 `accepted: true` 且不重复处理；
- 限流按 `room_ref` 窗口计数，超限返回 429；
- 原始用户 ID 在适配层即哈希，不进入下游。

## 4. 场次与控制面

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/streams` | 创建场次并固化配置快照 |
| POST | `/v1/streams/{id}/start` | 开播，自检通过后生效 |
| POST | `/v1/streams/{id}/stop` | 收播，记录原因码（异常收播） |
| POST | `/v1/streams/{id}/resume` | 恢复开播，复用配置快照并重建状态机 |
| GET | `/v1/streams/{id}/state` | 实时状态 |
| POST | `/v1/streams/{id}/control` | 运行控制 |
| POST | `/v1/streams/{id}/takeover` | 人工接管/释放 |
| GET | `/v1/streams/{id}/audit` | 操作审计 |

> 控制面请求需携带 `X-Operator-Id` 作为操作人标识（OIDC 接入前替代 JWT 主体），缺失返回 `401 UNAUTHENTICATED`。场次的创建、开播、收播、恢复均写入审计（`session.create` / `session.start` / `session.stop` / `session.resume`，被拒绝时以 `.rejected` 结尾）。

### 4.1 `POST /v1/streams`

创建场次并**固化配置快照**：人设版本、提示词摘要、音色、形象、标题、平台账号与直播间引用在创建时确定，后续开播与恢复都复用该快照，保证事后可追溯（人设发布新版本不影响已创建场次）。

```json
{
  "persona_id": "p_01J...",
  "persona_version": 3,
  "prompt_digest": "sha256:...",
  "voice_id": "voice_authorized_001",
  "avatar_id": "avatar_01J...",
  "title": "晚间答疑",
  "platform_account_id": "acc_01J...",
  "room_ref": "room_123456"
}
```

响应 `201 Created`：

```json
{
  "stream_id": "st_01J...",
  "session_id": "ss_01J...",
  "status": "created",
  "state": "idle",
  "title": "晚间答疑",
  "persona_id": "p_01J...",
  "persona_version": 3,
  "prompt_digest": "sha256:...",
  "voice_id": "voice_authorized_001",
  "avatar_id": "avatar_01J...",
  "platform_account_id": "acc_01J...",
  "room_ref": "room_123456",
  "created_at": "2026-01-01T12:00:00Z",
  "started_at": null,
  "ended_at": null,
  "stop_reason": null,
  "stop_detail": ""
}
```

`status` 为场次生命周期（`created` / `live` / `stopped`），`state` 为控制面状态机实时值（`idle` / `interacting` / `speaking` / `cooldown` / `circuit_open` / `takeover` / `ended`）。

### 4.2 `POST /v1/streams/{id}/start`

```json
{"expected_start_at": "2026-01-01T12:00:00Z"}
```

人设已在创建时固化，开播请求不再传 `persona_version_id`；`expected_start_at` 仅作审计记录，可省略。

前置校验（任一失败返回 `403 PERMISSION_DENIED`）：

1. 平台账号已授权且在有效期；
2. 形象与音色授权有效；
3. AIGC 标识配置已启用；
4. 内容安全服务健康；
5. RTMP 凭据已就绪且连通性通过；
6. **云端推流许可/报备记录在有效期内**；
7. **Live2D 模型与动作集可用，Cubism 商用许可判定已归档**。

> 自检由服务端强制执行：授权到期时间、AIGC 标识、内容安全健康、RTMP 凭据与 TCP 连通性、推流许可（编号 + 有效期）、Live2D/Cubism 许可。任一项失败返回 `403 PERMISSION_DENIED`，`details.failed_checks` 给出逐项原因，并写入 `session.start.rejected` 审计；非 `created` 状态返回 `409 STATE_CONFLICT`。

失败示例：

```json
{
  "error": {
    "code": "PERMISSION_DENIED",
    "message": "preflight failed",
    "details": {
      "failed_checks": {
        "push_permit": "missing push permit record",
        "rtmp_credential": "missing rtmp credential"
      }
    }
  }
}
```

> 本地伴侣推流（架构 B）设置 `BROADCAST_MODE=local` 后，只校验 `aigc_label`、`safety_health`、`local_broadcast` 三项；`local_broadcast` 对应 `LOCAL_BROADCAST_READY`，用于确认 OBS 虚拟摄像头与直播伴侣已就绪。审计记录会带上 `mode`，便于区分本地联调与云端直推。

### 4.3 `POST /v1/streams/{id}/stop`

```json
{"reason": "upstream", "detail": "rtmp disconnected"}
```

- `reason` 取值：`operator`（人工下播，缺省）/ `compliance`（合规处置）/ `upstream`（上游断流）/ `timeout`（超时）；
- `detail` 为具体说明，长度 ≤ 255；
- 收播后场次状态为 `stopped`、控制面状态机进入 `ended`，待发声队列清空并通知下游静默；
- 接管期间收播会同时结束接管记录，结论记为 `stream ended: {reason}`；
- 非 `live` 状态收播返回 `409 STATE_CONFLICT`，并写入 `session.stop.rejected` 审计。

### 4.4 `POST /v1/streams/{id}/resume`

```json
{"reason": "推流地址已更新"}
```

- 仅 `stopped` 状态可恢复；复用原配置快照，重建控制面状态机（回到 `idle`），清空 `stop_reason` / `stop_detail`；
- 恢复开播同样执行完整自检，未通过返回 `403 PERMISSION_DENIED` 并保持 `stopped`；
- 非 `stopped` 状态恢复返回 `409 STATE_CONFLICT`。

### 4.5 `POST /v1/streams/{id}/control`

```json
{
  "action": "pause | resume | mute | unmute | switch_persona | say_template | reconnect",
  "params": {"persona_version_id": "pv_01J...", "template_id": "tpl_welcome"},
  "reason": "operator action"
}
```

> `reconnect` 仅由控制台或运维调用，用于云端 Streamer 重连；不会重新获取推流凭据。

约束：

- `params` 按动作取用：`switch_persona` 需 `persona_version_id`，`say_template` 需 `template_id`，缺失返回 `400 INVALID_ARGUMENT`；
- `pause` 保留待发声队列但停止出队；`mute` 期间 AI 与预设话术都不出声；
- 接管期间允许切人设、预设话术与重连，AI 自动回复完全静默；
- 所有动作（含被拒绝的尝试）写入 `audit_logs`。

响应（`latency_ms` 为服务端处理耗时，用于核对接管时效）：

```json
{
  "stream_id": "st_01J...",
  "action": "pause",
  "state": "takeover",
  "applied": true,
  "ai_silent": true,
  "latency_ms": 0.8
}
```

### 4.6 `POST /v1/streams/{id}/takeover`

```json
{"action": "take | release", "reason": "敏感话题升级处理"}
```

约束：

- `take` 后 AI 停止消费队列、清空待发声并静默，Streamer 保持推流；
- `release` 需在 `reason` 中填写处置结论；
- 接管期间重复 `take` 返回 `409 ALREADY_TAKEOVER`；未接管时 `release` 返回 `409 STATE_CONFLICT`；
- 若接管前处于熔断，释放后恢复到熔断状态，仍需人工确认后才能发声。

响应：

```json
{
  "stream_id": "st_01J...",
  "action": "take",
  "state": "takeover",
  "takeover": {
    "active": true,
    "operator_id": "op_01J...",
    "reason": "敏感话题升级处理",
    "started_at": "2026-01-01T12:00:00Z",
    "conclusion": null
  },
  "cleared_queue": 3,
  "ai_silent": true,
  "latency_ms": 0.9
}
```

### 4.7 `GET /v1/streams/{id}/state`

```json
{
  "stream_id": "st_01J...",
  "session_id": "ss_01J...",
  "state": "SPEAKING",
  "queue_depth": 3,
  "latency": {"first_audio_p50_ms": 1680, "first_audio_p95_ms": 3120},
  "safety": {"circuit": "CLOSED", "block_rate_5m": 0.031},
  "stream": {"connected": true, "bitrate_kbps": 4200, "fps": 30},
  "takeover": {"active": false, "operator_id": null}
}
```

> 控制面当前实现返回 `state`、`queue_depth`、`paused`、`muted`、`ai_silent`、`persona_version_id`、`templates` 与 `takeover`；`latency`/`safety`/`stream` 由对应服务接入后补齐。

### 4.8 `GET /v1/streams/{id}/audit`

返回时间倒序的操作审计，`limit` 默认 50、上限 200。被拒绝的操作以 `.rejected` 结尾，失败原因在 `after.error`：

```json
{"entries": [{"id": "01J...", "org_id": "org_01J...", "actor_id": "op_01J...", "action": "takeover.take", "target": "st_01J...", "before": {"state": "speaking", "queue_depth": 3}, "after": {"state": "takeover", "queue_cleared": 3}, "created_at": "2026-01-01T12:00:00Z"}]}
```

### 4.9 控制台页面

控制面服务在 `/console` 托管零构建控制台页面，只调用本节接口。PLAN 中的 React SPA 落地后复用同一 REST 契约，无需改动服务端。

## 5. 安全、生成与语音（内部）

### 5.1 `POST /v1/safety/check`

```json
{
  "direction": "input | output",
  "session_id": "ss_01J...",
  "text": "待审核文本",
  "context": {"stage": "reply_sentence", "persona_id": "p_01J..."}
}
```

```json
{
  "verdict": "allow | block | rewrite",
  "categories": ["politics"],
  "rule_ids": ["sw_1024"],
  "action": "drop_sentence",
  "latency_ms": 87,
  "providers": [{"name": "dfa", "verdict": "allow"}, {"name": "aliyun_green", "verdict": "block"}]
}
```

### 5.2 `POST /v1/replies:generate`

仅用于调试、回放与评测；生产主路径由编排器内部触发。

```json
{
  "session_id": "ss_01J...",
  "comment_id": "c_01J...",
  "persona_version_id": "pv_01J...",
  "max_tokens": 200,
  "stream": true
}
```

### 5.3 `POST /v1/tts:synthesize`

```json
{
  "session_id": "ss_01J...",
  "text": "欢迎来到直播间～",
  "voice_id": "voice_authorized_001",
  "format": "pcm_16k_mono",
  "stream": true
}
```

响应为分块音频流，头部带 `X-Audio-Duration-Ms` 与分块时间戳，供唇形驱动使用。

## 6. 本地桥

### 6.1 `POST /v1/bridge/heartbeat`

```json
{
  "bridge_id": "br_01J...",
  "host_fingerprint": "sha256:...",
  "companion_running": true,
  "captured_at": "2026-01-01T11:59:50Z",
  "version": "0.1.0"
}
```

### 6.2 `POST /v1/bridge/rtmp`

**仅上报凭据，不推流。** 推流地址只在内存与 KMS 密文引用中使用，**不得**写入数据库明文字段或日志，也不得回显到前端。本地桥不具备推流能力。

请求（mTLS + 15 分钟短期令牌；`rtmp.key` 只出现一次，云端立即转 KMS 密文）：

```json
{
  "bridge_id": "br_01J...",
  "stream_id": "st_01J...",
  "rtmp": {
    "url": "rtmp://push.example.com/live",
    "key": "平台下发的推流密钥",
    "key_ref": "up_01J..."
  },
  "expires_at": "2026-01-01T20:00:00Z"
}
```

- `key_ref` 由桥生成，仅作一次性上报引用，供审计与幂等去重，不含密钥信息；
- 云端校验后返回 `{"stored": true, "expires_at": "..."}`，不回显地址；
- `stored` 不为 `true` 时桥按失败处理，不重试携带同一 `key_ref` 的请求。

## 7. 内部事件总线（Redis Streams）

| Stream | 生产者 | 消费者 | 关键字段 |
| --- | --- | --- | --- |
| `sp.comment.accepted` | Ingest GW | Orchestrator | session_id, comment_id, text, priority |
| `sp.reply.generated` | LLM GW | Orchestrator / Safety | reply_id, sentence, seq |
| `sp.audio.ready` | TTS GW | Renderer | reply_id, oss_key 或 chunk, duration_ms |
| `sp.render.ready` | Renderer | Streamer | frame_batch, pts |
| `sp.stream.status` | Streamer | Console / Orchestrator | connected, bitrate, fps, error |
| `sp.safety.verdict` | Safety | Orchestrator / Audit | verdict, rule_ids, direction |

约定：每条事件含 `event_id`、`schema_version`、`session_id`、`trace_id`、`occurred_at`；消费组按 `session_id` 分区，单并发消费；处理失败进入 `*-dlq` 并告警。

## 8. 限流与配额

| 范围 | 限制（初始值，可配） |
| --- | --- |
| 平台回调 | 50 req/s per room；突发 100 |
| 控制面写操作 | 10 req/s per user |
| LLM | 按租户 token/分钟与并发数 |
| TTS | 按音色并发 2，字符/分钟配额 |
| 本地桥 | 心跳 1 次/10s，凭据上报 1 次/场次（可刷新） |
