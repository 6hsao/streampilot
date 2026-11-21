# StreamPilot 部署手册

> 拓扑与资源决策见 [PLAN.md](PLAN.md#7-部署拓扑)；延迟与失败模式见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 1. 拓扑

```
Internet
   │ 443
   ▼
WAF + SLB（固定公网 IP）
   │
   ├──▶ ECS-A  app-1   4C8G   ingest-gateway / safety / orchestrator / llm-gateway / tts-gateway / control-api
   ├──▶ ECS-B  render  8C16G + GPU  avatar-renderer（Live2D 1080p30）/ streamer（RTMP 直推）
   │
   ├──▶ RDS MySQL 8    2C4G   业务库（仅 VPC）
   ├──▶ Redis 7        2GB    状态/队列（仅 VPC）
   ├──▶ OSS                   形象/音频/录像/归档
   └──▶ NAT + EIP            出站 DashScope / CosyVoice / 内容安全 / RTMP

本地 Windows（运营侧）
   直播伴侣（人工登录） ──▶ Local Bridge ──mTLS──▶ 云端 /v1/bridge/*
   Local Bridge 只上报推流地址与心跳，不推流
```

**关键网络约束**

- 入站仅 443；SSH 仅跳板机/VPN；
- RDS/Redis 不分配公网地址；
- 推流与外部 API 走固定 EIP，便于平台侧与供应商侧稳定；
- 本地桥只出站，不监听公网端口。

## 2. 资源规格

| 组件 | 起步规格 | 扩容触发条件 |
| --- | --- | --- |
| 应用 ECS | 4C8G × 1 | CPU > 60% 持续 10 分钟，或事件积压 > 1000 |
| 渲染节点 | 8C16G + T4/A10 级 GPU（Live2D 1080p30） | 掉帧 > 1%，或渲染延迟 p95 > 200ms |
| RDS | 2C4G / 100GB SSD | 连接数 > 70%，或慢查询增长 |
| Redis | 2GB 主从 | 内存 > 70%，或队列深度持续 > 10 |
| 带宽 | 5Mbps 起 | 推流码率 ≥ 4Mbps 时按需提升 |

单直播间采用「ECS-A 业务 + ECS-B GPU 渲染/推流」两机起步。开发环境可用 720p + CPU 软渲染，但**不得**作为生产容量依据。Live2D 模型、动作与贴图存放 OSS，部署时按版本下载到渲染节点本地缓存，**不入镜像、不入仓库**。

## 3. 环境与配置

| 环境 | 域名 | 数据 | 外部能力 | 备注 |
| --- | --- | --- | --- | --- |
| dev | localhost | 合成数据 | mock 优先 | Docker Compose 一键起 |
| staging | staging-api.* | 脱敏样本 | 真实 API，独立配额 | 与生产同构小规格 |
| prod | api.* | 真实数据 | 真实 API，独立密钥 | 变更需审批 |

配置来源优先级：环境变量 > 配置文件 > 默认值。生产密钥由 KMS/参数存储注入容器，不落镜像、不写 `.env` 到服务器磁盘明文。

必填变量见 [`.env.example`](../.env.example)。**任何真实密钥禁止入库、禁止写入日志。**

## 4. 部署流程（规划）

1. CI 构建镜像并打 git sha 标签；
2. 推送镜像仓库；
3. staging 部署并跑冒烟：`/healthz`、`/readyz`、一条合成事件端到端；
4. 发布审批；
5. 生产滚动发布（先应用层，再渲染层）；
6. 观察 10 分钟关键指标：首帧延迟、错误率、推流状态、审核错误率；
7. 异常则回滚。

**影子模式**：新版本先只生成不推流，对比回复采纳率与延迟，达标后切单间灰度。

## 5. 发布与回滚

| 对象 | 方式 | 回滚目标 |
| --- | --- | --- |
| 代码 | 镜像 + 滚动发布 | ≤ 5 分钟 |
| 配置（阈值/词库/提示词） | 配置中心 + 版本号 | ≤ 1 分钟 |
| 人设 | 版本切换 | 即时 |
| 数据库 | 兼容性迁移，先加后用 | 迁移脚本回滚或向前修复 |

回滚判定：首帧 p95 > 5s 持续 5 分钟、审核错误率 > 1%、推流中断 2 次以上、出现 P0 安全事故。

## 6. 监控与告警

| 指标 | 告警阈值 | 级别 |
| --- | --- | --- |
| 首帧音频延迟 p95 | > 4.5s 持续 5min | P1 |
| 审核错误率 | > 1% 持续 2min | P0 |
| 审核拦截率突增 | 5 分钟均值 > 20% | P1 |
| 推流断开 | 任意一次 | P1 |
| 事件积压 | > 1000 或持续增长 | P1 |
| 渲染掉帧 | > 1% | P2 |
| CPU/内存 | > 80% 持续 10min | P2 |
| 成本/小时 | > 预算 120% | P1 |

告警通道：值班群 + 电话升级（P0）。所有告警必须带 `session_id`、`trace_id` 与看板链接。

## 7. 应急 Runbook

### 7.1 推流断开

1. 查 `sp.stream.status` 与 Streamer 日志；
2. 确认 RTMP 凭据是否过期（本地桥/平台侧），过期则通知运营重新获取；
3. 自动重连失败则人工执行 `POST /control {action:"reconnect"}`；
4. 仍失败：切换到备用渲染/推流节点；若仍不可用，运营用直播伴侣人工开播作为应急（非常态架构）；
5. 记录 `incidents`，复盘网络、平台限流与服务端原因。

### 7.2 审核不可用

1. 系统应已自动 fail-closed（停止发声）；
2. 确认云审核配额与密钥；
3. 临时措施：启用本地词库严格模式 + 限制发言频率；
4. 恢复后先小流量验证，再全量；
5. 全程人工接管，禁止绕过审核直接发声。

### 7.3 模型限流

1. 检查 DashScope 配额与并发；
2. 自动降级 qwen-turbo 与模板话术；
3. 必要时降低回复频率（队列丢弃低优）；
4. 与供应商确认配额恢复时间。

### 7.4 误熔断

1. 在审核复核台抽样，判断规则误伤；
2. 调整词库/阈值（需复核人确认）；
3. 手动恢复熔断，观察 10 分钟拦截率；
4. 把样本加入回归集。

### 7.5 人工接管

1. 控制台点击「接管」，确认 AI 静默（≤ 3s）；
2. 人工接管话术与画面；
3. 处置完成后填写结论并释放；
4. 系统自动生成接管记录与复盘条目。

### 7.6 平台推流许可与限流

**许可登记（GOV-05）**：取得平台书面许可/报备材料后归档，并填写以下配置；缺任一项，开播自检 `push_permit` 不通过，禁止开播：

| 变量 | 说明 | 必填 |
| --- | --- | --- |
| `PUSH_PERMIT_NUMBER` | 许可/报备编号，审计引用 | 是 |
| `PUSH_PERMIT_EXPIRES_AT` | 到期时间（ISO-8601，含时区） | 是 |
| `PUSH_PERMIT_EGRESS_IPS` | 登记的固定出口 IP，逗号分隔 | 是 |
| `PUSH_PERMIT_ARCHIVE_REF` | 归档引用（工单/合同/文档路径） | 是 |
| `PUSH_PERMIT_ACCOUNT_ID` | 绑定平台账号；留空覆盖全部账号 | 否 |
| `PUSH_PERMIT_ISSUED_AT` | 生效时间 | 否 |
| `PUSH_PERMIT_PARAMS` / `PUSH_PERMIT_RESTRICTIONS` | 推流参数与附加限制摘要 | 否 |

其余自检项由 `LIV-07` 事实源提供：授权到期（`PLATFORM_AUTH_EXPIRES_AT`、`AVATAR_LICENSE_EXPIRES_AT`、`VOICE_LICENSE_EXPIRES_AT`）、`AIGC_LABEL_ENABLED`、`SAFETY_HEALTHY`、`RTMP_CREDENTIAL_READY`、`LIVE2D_MODEL_READY`、`CUBISM_LICENSE_RECORDED`；推流连通性默认按 `RTMP_URL` 做 TCP 探测（`RTMP_PROBE_TIMEOUT_S`），可用 `RTMP_REACHABLE` 覆盖。未接入真实探测的开关仅用于 staging，生产需替换为子系统实时查询。

1. 开播与恢复开播都会执行全部自检，任一失败返回 403 并列出 `failed_checks`；
2. 监测推流返回码、码率骤降、平台警告与观众端异常反馈；
3. 出现限流迹象：先降码率/帧率，再降低互动频率，不盲目重连；
4. 连续失败：停止推流并人工接管，保留日志与截图（不含推流 key）；
5. 每次事件更新 `incidents`，并在周会复核是否触发架构 B 回退条件。

**架构 B 回退条件与动作**（回退到自建推流机/本地推流）：

1. 触发条件：平台拒绝许可/报备、出口 IP 被限制、连续 3 次开播自检或推流失败且 24h 内无法恢复；
2. 动作：停止云端推流，将 `RTMP_URL` 切换到自建推流机，保留场次配置快照与审计；
3. 回退前由负责人确认成本与合规影响，回退后 24h 内复盘并更新 ADR 0003。

## 8. 备份与恢复

| 对象 | 策略 | RPO | RTO |
| --- | --- | --- | --- |
| RDS | 每日全备 + binlog | ≤ 5min | ≤ 1h |
| Redis | AOF everysec + 每日快照 | ≤ 1min | ≤ 30min |
| OSS | 版本控制 + 跨区复制（可选） | 近实时 | ≤ 1h |
| 配置 | Git 版本化 | 0 | ≤ 10min |

每季度做一次恢复演练，记录实际 RTO/RPO 与偏差。

## 9. 安全基线

- 主机：最小化安装、仅必要端口、自动安全更新窗口；
- 容器：非 root、只读根文件系统（能只读则只读）、固定镜像版本；
- 账号：SSH 密钥登录，禁用密码；控制台强制 MFA；
- 审计：所有登录、接管、配置变更、导出操作留痕；
- 扫描：镜像漏洞扫描 + 依赖扫描 + 密钥扫描纳入 CI。

## 10. 阿里云 staging 凭据配置指引

> 本节只给占位符与操作步骤，**任何真实值不得写入仓库、文档、工单或聊天**。需要凭据时由持有人在本机按本节步骤注入，不要通过消息索要明文。

### 10.1 凭据分类：三类，互相隔离

| 类别 | 典型载体 | 用途 | 边界（能做什么 / 不能做什么） |
| --- | --- | --- | --- |
| 百炼 API Key | 控制台生成的 `sk-` 开头 Key | LLM、CosyVoice、Fun-ASR 的模型调用 | 只能调用模型推理；**不能**管理 ECS/OSS/VPC，不能查看账单 |
| RAM 程序访问用户 AccessKey | `LTAI` 开头的 ID + Secret | 运行期调用内容安全、读写 OSS；`aliyun` CLI / Terraform | 只能做被策略授予的云资源操作；**不得**用于模型调用 |
| RAM 角色 / STS 临时凭据（推荐） | 实例角色或 AssumeRole | 生产运行期替代静态 AK | 有效期最长 12 小时，到期自动失效；staging 可先用静态 AK，生产必须切换 |

**为什么不共用同一套**

1. **爆炸半径**：模型 Key 泄漏只能消耗 token；RAM AK 泄漏可能被用来读 OSS、改配置甚至删资源。混用等于把后者的权限暴露在只该调模型的机器上。
2. **计费与配额归属**：百炼按 token 计费，staging 与 prod 需要独立配额；共用一套会导致限流互相影响，也无法按环境出账。
3. **轮换粒度**：模型 Key 与基础设施 AK 的更换节奏、审批人、影响面都不同，绑在一起会把一次普通轮换升级成全链路变更。
4. **审计可归因**：RAM AK 的 OpenAPI 调用进 ActionTrail，模型调用有自己的调用日志；混用后无法区分「谁在调模型」与「谁在改资源」。

RAM 用户至少拆成两个：**infra（Terraform/CLI 管理资源）** 与 **runtime（应用只读写 OSS、调内容安全）**。不要用 root 账号创建 AccessKey。

### 10.2 变量清单（与 `.env.example` 逐项对应）

「可选」列中带默认值表示可以不配；标「必填」表示缺少时对应能力无法启动。

**阿里云相关（本指引重点）**

| 变量 | 含义 | 示例占位 | 可选性 |
| --- | --- | --- | --- |
| `ALIYUN_ACCESS_KEY_ID` | RAM 用户 AccessKey ID（runtime/infra 各一套） | `<staging-access-key-id>` | 必填（内容安全、OSS、CLI） |
| `ALIYUN_ACCESS_KEY_SECRET` | 对应 Secret | `<staging-access-key-secret>` | 必填 |
| `ALIYUN_REGION` | 地域，staging 固定单地域 | `cn-hangzhou` | 可选（默认 `cn-hangzhou`） |
| `CONTENT_SAFETY_ENDPOINT` | 内容安全接入点 | `green-cip.cn-hangzhou.aliyuncs.com` | 可选（默认同值） |
| `OSS_BUCKET` | staging 专用 Bucket | `<staging-bucket>` | 必填（素材/录像） |
| `OSS_ENDPOINT` | OSS 接入点 | `oss-cn-hangzhou.aliyuncs.com` | 可选（默认同值） |
| `DASHSCOPE_API_KEY` | 百炼 API Key（LLM/TTS/ASR 共用） | `<dashscope-api-key>` | 必填（启用模型链路时） |
| `DASHSCOPE_BASE_URL` | OpenAI 兼容端点 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 可选（默认同值） |
| `LLM_MODEL_CHAIN` | 降级链，逗号分隔 | `qwen-plus,qwen-turbo` | 可选 |
| `LLM_MAX_TOKENS` | 单次回复上限 | `200` | 可选（默认 `200`） |
| `LLM_TEMPERATURE` | 采样温度 | `0.7` | 可选（默认 `0.7`） |
| `COSYVOICE_WS_URL` | 流式合成 WebSocket 地址 | `wss://dashscope.aliyuncs.com/api-ws/v1/inference` | 可选（默认同值） |
| `COSYVOICE_MODEL` | 合成模型 | `cosyvoice-v2` | 可选（默认同值） |
| `COSYVOICE_DEFAULT_VOICE` | 已获授权的音色 ID | `replace_with_authorized_voice_id` | 必填（开播前） |
| `TTS_AUDIO_FORMAT` | 合成音频格式 | `pcm_16k_mono` | 可选（默认同值） |
| `TTS_MAX_CONCURRENCY` | 进程内合成并发上限 | `2` | 可选（默认 `2`） |
| `FUN_ASR_MODEL` | 识别模型（非主链路） | `fun-asr` | 可选 |
| `FUN_ASR_ENABLED` | 是否启用识别 | `false` | 可选（首发关闭） |
| `SENSITIVE_WORD_DICT_PATH` | 本地词库文件 | `./data/safety/lexicon.yml` | 可选（内置词库默认生效） |
| `MYSQL_DSN` | 业务库连接串（VPC 内） | `mysql+pymysql://user:password@127.0.0.1:3306/streampilot` | 必填（接入与场次） |
| `REDIS_URL` | Redis 连接串（VPC 内） | `redis://127.0.0.1:6379/0` | 必填（接入网关/队列） |

**非阿里云相关（一并列出，避免漏配）**

| 变量 | 含义 | 示例占位 | 可选性 |
| --- | --- | --- | --- |
| `APP_ENV` | 运行环境 | `staging` | 必填（staging 固定 `staging`） |
| `APP_LOG_LEVEL` | 日志级别 | `INFO` | 可选 |
| `APP_HOST` / `APP_PORT` | 监听地址与端口 | `0.0.0.0` / `8000` | 可选 |
| `DOUYIN_APP_ID` | 开放平台应用 ID | `<douyin-app-id>` | 必填（接评论时） |
| `DOUYIN_APP_SECRET` | 应用密钥 | `<douyin-app-secret>` | 必填（接评论时） |
| `INGEST_CALLBACK_SECRET` | 回调签名密钥，≥16 位随机串 | `<random-32-chars>` | 必填（接评论时） |
| `VIEWER_ID_HASH_SALT` | 观众 ID 哈希盐，≥16 字节 | `<random-16-bytes>` | 必填；变更会使历史哈希失效 |
| `DOUYIN_WEBHOOK_SIGN_KEY` | Webhook 验签密钥 | `<webhook-sign-key>` | 必填（接评论时） |
| `STREAM_BRIDGE_SHARED_SECRET` | 本地桥共享密钥 | `<bridge-shared-secret>` | 必填（启用本地桥时） |
| `STREAM_BRIDGE_CLOUD_URL` | 本地桥云端地址（必须 https） | `https://staging-api.example.com` | 必填（启用本地桥时） |
| `RTMP_URL` / `RTMP_STREAM_KEY` | 推流地址与密钥 | 留空 | **staging 必须留空**；运行时由凭据桥下发 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 链路导出地址 | 留空 | 可选 |
| `METRICS_PORT` | 指标端口 | `9100` | 可选 |

### 10.3 staging 最小权限策略

**地域**：只授 `cn-hangzhou`，不授多地域；策略中显式写 region 条件，避免「换地域照样能用」。

**infra 用户（Terraform/CLI，按需临时提升）**

- 按产品授予具体的 `Create/Describe/Modify/Delete` 动作，且资源 ARN 限定 staging 前缀；
- 允许：VPC/交换机/安全组、ECS、SLB、RDS、Redis、OSS Bucket（仅 staging）、日志与监控；
- 关闭写操作以外的批量删除：`ecs:DeleteInstance`、`rds:DeleteDBInstance`、`oss:DeleteBucket`、`vpc:DeleteVpc` 需走人工审批角色，不放在日常凭据里。

**runtime 用户（应用与联调，长期存在）**

- OSS：仅 `oss:GetObject` / `oss:PutObject` / `oss:DeleteObject` / `oss:ListObjects`，资源限定 `acs:oss:*:*:<staging-bucket>/streampilot-staging/*`；
- 内容安全：仅文本审核对应的 `green:TextModeration`（或账号已开通的等价动作），region 限定 `cn-hangzhou`；
- KMS（启用密文引用后）：仅 `kms:Encrypt` / `kms:Decrypt` / `kms:GenerateDataKey`，资源限定单个 staging Key；
- 只读运维：`rds:Describe*`、`redis:Describe*`、`ecs:Describe*` 可给；**不给**任何 `Delete*`、`Create*`。

**明确避免的高危权限**

- `AdministratorAccess`、`*:*`、`AliyunRAMFullAccess`、`AliyunSTSAssumeRole` 到宽泛角色；
- `ram:CreateAccessKey` / `ram:AttachPolicyToUser`（可自我提权）；
- `oss:PutBucketAcl`、把 Bucket 设为公共读写的任何动作；
- `bss:*`（账单与充值）、`kms:ScheduleKeyDeletion`、`kms:DisableKey`；
- 生产资源与 staging 共用同一 AK；root 账号 AK（禁止创建）。

### 10.4 本机落地步骤（Windows / PowerShell）

```powershell
# 1) 从模板创建本地配置（.env 已被 .gitignore 忽略）
Copy-Item .env.example .env
# 2) 只在本地编辑器里把占位符替换为真实值，不要粘贴到聊天/工单
notepad .env
```

确认 `.env` 不会被提交：

```powershell
git check-ignore -v .env     # 期望输出命中 .gitignore 中的 .env 规则
git status --short           # 期望结果里没有 .env
git ls-files .env            # 期望输出为空
```

在当前终端临时注入（**仅当前进程有效，关闭终端即失效，不写任何文件**）：

```powershell
$env:ALIYUN_ACCESS_KEY_ID = 'staging-access-key-id-here'
$env:ALIYUN_ACCESS_KEY_SECRET = 'staging-access-key-secret-here'
$env:DASHSCOPE_API_KEY = 'dashscope-api-key-here'
```

- 不要用 `--access-key-id` 之类的命令行参数传密钥，以免进入 shell 历史；
- 不要把密钥写进 PowerShell Profile、任务计划或 IDE 运行配置；
- 若使用 `aliyun configure --profile staging`，凭据会存到用户目录（仓库之外），不要把该文件复制进工作区。

### 10.5 验证方式

1. **身份可用性**（不打印 Secret）：`aliyun sts GetCallerIdentity`，应返回 staging RAM 用户的 ARN；若报 `InvalidAccessKeyId` 说明注入未生效或 Key 已作废。
2. **OSS 权限**：用 `ossutil ls oss://<staging-bucket>/streampilot-staging/`（`aliyun` CLI 不覆盖 OSS 对象操作）应成功；对非授权前缀应返回 `AccessDenied`（这正说明策略生效）。
3. **百炼 Key**：用最小请求验证，不回显 Key：
   ```powershell
   curl.exe -sS -o NUL -w "%{http_code}`n" `
     -H "Authorization: Bearer $env:DASHSCOPE_API_KEY" `
     -H "Content-Type: application/json" `
     -d '{"model":"qwen-turbo","messages":[{"role":"user","content":"ping"}],"max_tokens":1}' `
     https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
   ```
   期望 `200`；`401` 为 Key 无效，`403` 多为业务空间未授权。
4. **内容安全**：按 [TASKS.md](TASKS.md) 中 SAF-02 的 staging 联调项，用真实凭据跑一次文本审核，确认签名通过、返回 `status` 与 `latency_ms`；这一步不做则 SAF-02 视为未验收。
5. **服务健康检查**：启动接入网关后 `curl.exe -fsS http://127.0.0.1:8000/healthz`，确认返回 200；`/readyz`（依赖 Redis/MySQL 的深度检查）在本文档中仍属规划项，当前版本未提供，不要据此判定凭据可用。
6. **本地闸门**：`uv run ruff check src tests; uv run mypy; uv run pytest`，任何真实 Key 都不应出现在仓库与测试输出中。

### 10.6 轮换与泄露处置

**轮换周期**

- 百炼 API Key：90 天，或人员/外包变动、环境迁移时立即更换；
- RAM AccessKey：90 天；生产建议改用实例角色/STS（≤12h）；
- 本地桥令牌：15 分钟自动轮换（已实现），无需人工干预；
- staging 与 prod 使用**不同** Key，禁止跨环境复用。

**正常轮换顺序**：新建 Key → 注入 staging（环境变量/KMS）→ 跑 10.5 的验证 → 观察 24 小时 → 停用旧 Key → 观察 24 小时 → 删除旧 Key。全程保留 ActionTrail 记录。

**误提交处置顺序（先作废，再清理）**

1. **立即作废**：控制台禁用/删除对应 AccessKey 与百炼 Key，不要先花时间改 Git 历史；
2. **止血**：注入新 Key 恢复 staging；检查 ActionTrail 与百炼调用日志是否有异常调用、账单是否突增；
3. **清理历史**：`git filter-repo` 重写受影响分支并强推，通知协作者重新克隆；确认 CI 密钥扫描（gitleaks）已覆盖该路径；
4. **假设已泄露**：公开仓库的 Key 一律按已泄露处理，检查 OSS 是否被设为公共读、是否有异常资源创建，必要时提交工单并保留证据；
5. **复盘**：在 `incidents` 记录时间线、影响与改进项；若属流程缺失，补充 pre-commit 密钥扫描。

**不要在** Issue、PR、群聊、截图、日志或工单里粘贴真实 Key；需要他人协助时只提供变量名与占位符。

### 10.7 架构 B：本地伴侣推流（个人主体 staging）

个人主体无法取得 `PUSH_PERMIT_*`，staging 真实开播改用本地官方客户端推流，**不启动云端推流会话**。

**链路**

```text
数字人渲染输出（窗口 / 本机程序）
  → OBS 窗口采集 + 虚拟摄像头
  → 虚拟声卡（VB-Audio Cable）
  → 抖音直播伴侣（个人实名账号，人工登录）
  → 抖音直播间
```

**配置约束**

- 编排服务启动时自动加载工作目录下的 `.env`（不覆盖已存在的进程环境变量）；
- `.env` 保持 `APP_ENV=staging`，`RTMP_URL`、`RTMP_STREAM_KEY` 留空；
- `PUSH_PERMIT_*` 全部留空，**不要填占位或虚假编号**；
- 设置 `BROADCAST_MODE=local`、`LOCAL_BROADCAST_READY=true`、`AIGC_LABEL_ENABLED=true`，并在本地词库就绪时设置 `SAFETY_HEALTHY=true`；
- 本地模式下 `POST /v1/streams/{id}/start` 只校验 AIGC 标识、内容安全与本地链路，不再要求云端许可与 RTMP 连通性；
- 切换回 `BROADCAST_MODE=cloud` 后恢复完整云端自检，缺少 `PUSH_PERMIT_*` 仍返回 403；
- 本地桥只负责登录态与凭据上报，不参与本方案推流。

**本机准备**

| 组件 | 用途 | 来源 |
| --- | --- | --- |
| OBS Studio | 窗口采集、虚拟摄像头 | <https://obsproject.com/download> |
| VB-Audio Virtual Cable | 数字人音频送入直播伴侣 | <https://vb-audio.com/Cable/> |
| 抖音直播伴侣 | 官方开播客户端 | <https://streamingtool.douyin.com/> |

> VB-Cable 安装后会把系统默认播放设备改成 `CABLE Input`，导致本机扬声器无声。
> 安装完成后需在「系统设置 → 声音」把默认输出切回物理扬声器；数字人音频由应用显式输出到 `CABLE Input`，不要依赖系统默认设备。

**OBS 设置**

1. 画布 `1080x1920`，输出 `30fps`，视频码率 `4000 Kbps`，编码 `x264` 或硬件编码；
2. 来源添加「窗口采集」，选择数字人渲染窗口；不要采集整个桌面；
3. 音频添加「音频输出采集」，设备选 `CABLE Input`；
4. 点击「启动虚拟摄像机」，确认设备列表出现 `OBS Virtual Camera`。

**直播伴侣设置**

1. 个人抖音号完成实名认证后登录；
2. 摄像头选择 `OBS Virtual Camera`，麦克风选择 `CABLE Output`；
3. 分辨率 `1080x1920`，码率与 OBS 一致；
4. 画面添加 AIGC 标识（角标或简介声明），确认虚拟人直播规范提示已阅读；
5. 先「预览」检查音画同步，再开播。

**验收清单**

- [ ] 音画同步误差 < 200ms，连续 10 分钟无漂移
- [ ] 直播伴侣预览画面无黑边、无拉伸
- [ ] 数字人静音/待机时直播伴侣无异常爆音
- [ ] 人工接管：云端停止生成后画面切待机，直播不中断
- [ ] 一键停播：关闭直播伴侣后平台侧确认下播
- [ ] 抖音直播间端到端延迟与弹幕互动正常

**已知限制**

- 本机需全程在线，断电/休眠即断播；
- 无法云端一键起停，也无法验证云端 RTMP 出口、固定 IP 与多节点容灾；
- 并发受本机 CPU/GPU 与上行带宽限制；
- 平台若对虚拟人直播追加资质要求，个人主体仍需转企业路径。
