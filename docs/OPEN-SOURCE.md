# 开源复用调研

> 调研日期：2026-11-15 · 许可证以各仓库 `LICENSE` 文件与 GitHub API 的 `license` 字段为准（快照）。
> 许可证会随上游变化，**引入前必须复核当期文本**，并通过 `INF-08` 的许可证门禁。
> 相关决策：[PLAN.md](PLAN.md) · [TASKS.md](TASKS.md) · [ADR 0001](adr/0001-primary-cloud-alibaba.md)

## 1. 调研范围与方法

覆盖直播链路的六个方向：评论/弹幕接入、LLM 对话与编排、TTS/ASR、Live2D 驱动与控制、FFmpeg/RTMP 推流、内容安全词库。

方法：

1. 以 GitHub 搜索与社区使用度为线索列出候选；
2. 通过 GitHub API 读取仓库元数据（许可证、归档状态、Star 快照）；
3. 对许可证识别为 `NOASSERTION` 的项目，读取 `LICENSE` 原文确认；
4. 结合本项目硬约束（只走官方开放平台、MIT 开源、商用直播）判断可用性。

**结论先行**：直播链路没有“开箱即用且合规”的完整开源方案。可复用的是**单点能力**（Live2D 渲染、DFA 词库、媒体管线），核心编排、安全策略、接入适配与运维必须自研。

## 2. 许可证兼容规则（以 MIT 项目为基准）

| 上游许可证 | 能否用于本项目 | 义务/限制 | 处理方式 |
| --- | --- | --- | --- |
| MIT / BSD-2/3 | ✅ 可以 | 保留版权与许可声明 | 直接依赖，NOTICE 归档 |
| Apache-2.0 | ✅ 可以 | 保留 NOTICE、专利授权条款、变更声明 | 直接依赖，归档 NOTICE |
| MPL-2.0 | ⚠️ 有条件 | 文件级 copyleft，修改过的文件需开源 | 不修改源码；或独立进程调用 |
| LGPL-2.1/3.0 | ⚠️ 有条件 | 允许动态链接/独立进程，需提供替换能力与声明 | **独立进程**调用（FFmpeg/GStreamer），不静态链接 |
| GPL-2.0/3.0 | ❌ 不兼容 | 衍生作品需以 GPL 发布 | 不引入产品；仅可作人工备用工具 |
| AGPL-3.0 | ❌ 不兼容 | 网络服务使用也触发开源义务 | 不引入 |
| 自定义/附加条件许可 | ⚠️ 需法务评审 | 常见多租户、SaaS、Logo、商用限制 | 默认拒绝嵌入；仅作参考 |
| 无许可证 | ❌ 不可用 | 默认保留所有权利 | 不复制任何代码 |
| 专有运行时（Cubism Core） | ⚠️ 需单独授权 | 营收阈值、发行许可、禁止再分发 | 不入库；按官方条款获取与部署 |
| 模型权重 | ⚠️ 独立于代码 | 常见非商用/研究限制 | 生产用托管 API；自托管需单独确认 |

## 3. 分方向调研结果

### 3.1 直播弹幕 / 评论接入

| 项目 | 能力 | 边界与问题 | 许可证 | MIT 兼容 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 抖音开放平台 OpenAPI | 官方评论/弹幕事件、鉴权、配额 | 需企业资质与能力申请；字段与频率受平台约束 | 平台协议 | 需遵守平台规则 | **唯一合规接入方式**，自研适配层 |
| saermart/DouyinLiveWebFetcher | 通过非官方方式获取直播信息 | 抓包/逆向，违反本项目硬约束 | AGPL-3.0 | ❌ | **禁用** |
| ihmily/DouyinLiveRecorder | 直播录制与流地址获取 | 非官方链路；录制版权与平台规则风险 | MIT | 代码兼容，但**合规不可用** | **禁用** |
| Ikaros-521/AI-Vtuber | 多平台 AI 直播弹幕互动 | GPL-3.0；依赖非官方弹幕接入；架构偏单体 | GPL-3.0 | ❌ | 仅作参考，不复制代码 |

**结论**：不存在既合规又可复用的弹幕 SDK。`ING-*` 全部自研，只对接官方 OpenAPI；任何抓包类项目进入黑名单。

### 3.2 LLM 对话与编排

| 项目 | 能力 | 边界与问题 | 许可证 | MIT 兼容 | 结论 |
| --- | --- | --- | --- | --- | --- |
| langchain-ai/langgraph | 有状态图编排、持久化、人工介入 | 抽象重、学习成本高；本项目的状态机更简单且强依赖 Redis 单写者 | MIT | ✅ | 参考其状态图思想，**不引入** |
| BerriAI/litellm | 多供应商模型网关、配额、成本 | 企业目录为单独许可；首发只用一个供应商，收益有限 | MIT（企业目录除外） | ✅（避开 enterprise） | **暂不引入**，作为多供应商备选 |
| microsoft/semantic-kernel | 插件/函数编排 | .NET 生态为主，Python 侧与本项目风格差异大 | MIT | ✅ | 不引入 |
| langgenius/dify | 可视化 LLM 应用平台 | 附加条款限制多租户/SaaS/Logo；体量大 | Apache-2.0 + 附加条件 | ⚠️ 需法务，默认不兼容 | 不引入 |
| labring/FastGPT | 知识库问答平台 | 同上，附加商用条件 | Apache-2.0 + 附加条件 | ⚠️ 默认不兼容 | 不引入 |

**结论**：`DLG-01/03` 自研轻量状态机与 DashScope 适配器；不引入编排框架。知识库（`DLG-08`）在 Post-1.0 再评估。

### 3.3 TTS / ASR

| 项目 | 能力 | 边界与问题 | 许可证 | MIT 兼容 | 结论 |
| --- | --- | --- | --- | --- | --- |
| QwenAudio/CosyVoice | 零样本 TTS、音色克隆、流式 | 自托管需 GPU 与模型运维；克隆需声音授权 | Apache-2.0 | ✅（代码） | 生产用**百炼托管 API**；仓库仅作离线实验参考 |
| RVC-Boss/GPT-SoVITS | 少样本音色克隆 | 需 GPU；音色与训练数据授权需自行保证 | MIT | ✅ | 不作生产主链路 |
| index-tts/index-tts | 高质量 TTS | 模型使用受 bilibili 自定义协议约束 | bilibili Model Use License | ⚠️ 需法务 | 不引入 |
| fishaudio/fish-speech | TTS/音色 | Fish Audio 社区许可，含商用条件 | 自定义 | ⚠️ 需法务 | 不引入 |
| 2noise/ChatTTS | 对话式 TTS | AGPL 传染 | AGPL-3.0 | ❌ | 不引入 |
| coqui-ai/TTS | 多模型 TTS 框架 | MPL-2.0 文件级 copyleft；维护节奏放缓 | MPL-2.0 | ⚠️ | 不引入 |

**结论**：TTS 走托管 API（`AVT-01`）；仅当成本或合规需要自托管时，才基于 CosyVoice（Apache-2.0）做独立服务评估。

### 3.4 Live2D 驱动与控制

| 项目 | 能力 | 边界与问题 | 许可证 | MIT 兼容 | 结论 |
| --- | --- | --- | --- | --- | --- |
| guansss/pixi-live2d-display | PixiJS 渲染 Live2D（Cubism 2.1/3/4/5） | 只负责渲染与基础动作；口型精度、场景合成、推流需自建；依赖 Cubism Core | MIT | ✅ | **采用**（`AVT-04/05`） |
| Live2D/CubismWebSamples | 官方示例与 Framework | 属于 Cubism SDK 组件；营收 ≥ 1000 万日元需 Release License；非开源 | Live2D 专有许可 | ❌ 非 OSI | 参考用法；**运行时不入库** |
| Open-LLM-VTuber/Open-LLM-VTuber | Live2D 桌宠 + ASR/TTS + 记忆 | 面向桌面陪伴，不含直播推流、评论接入、合规链路 | MIT | ✅ | **参考架构**，不整体 fork |
| moeru-ai/airi | Web 端 AI 角色（Live2D/VRM） | 前端交互为主，无直播编排与审核 | MIT | ✅ | 参考 Web 渲染方案 |
| semperai/amica | Live2D/VRM 网页形象 + TTS | 同上，缺少推流与运营能力 | MIT | ✅ | 参考 |
| uezo/ChatdollKit | Unity 语音对话角色框架 | Unity/3D 技术栈，与本项目 Web/Live2D 路线不一致 | Apache-2.0 | ✅ | 不采用 |
| hacxy/l2d-widget | 网页 Live2D 挂件 | 展示型组件，无口型驱动与推流 | MIT | ✅ | 参考 |

**结论**：渲染层复用 `pixi-live2d-display`；**Cubism Core 运行时单独获取、不入库**；模型与动作资产必须有商用授权。渲染服务、口型驱动、场景合成与推流调度自研。

### 3.5 FFmpeg / RTMP 推流与媒体

| 项目 | 能力 | 边界与问题 | 许可证 | MIT 兼容 | 结论 |
| --- | --- | --- | --- | --- | --- |
| FFmpeg/FFmpeg | 编解码、滤镜、RTMP 推流 | 默认 LGPL-2.1+；启用 `--enable-gpl` 后变 GPL | LGPL-2.1+ / GPL（构建决定） | ⚠️ 用 LGPL 构建 + 独立进程 | **采用**（`LIV-02`） |
| GStreamer/gstreamer | 低延迟管线、动态合成 | 插件多，需逐项核对许可证 | LGPL-2.0+ | ⚠️ 独立进程/动态链接 | 采用（合成与兜底） |
| bluenviron/mediamtx | RTMP/WebRTC/HLS 服务 | 是服务端，不负责推流客户端 | MIT | ✅ | 仅用于 staging 测试 |
| ossrs/srs | 直播服务器 | 同上 | MIT | ✅ | 备选测试服务 |
| obsproject/obs-studio | 采集与推流 | GPL-2.0；体积大，不适合嵌入 | GPL-2.0 | ❌ | **不嵌入**；仅人工应急 |

**结论**：媒体层用 FFmpeg（LGPL 构建）+ GStreamer，均以独立进程运行；发布包提供 LGPL 声明与替换说明（`LIV-09`）。

### 3.6 内容安全词库与审核

| 项目 | 能力 | 边界与问题 | 许可证 | MIT 兼容 | 结论 |
| --- | --- | --- | --- | --- | --- |
| houbb/sensitive-word | DFA 敏感词匹配、分词、替换 | 词库质量与领域覆盖有限；需自建分级词库 | Apache-2.0 | ✅ | **采用**（`SAF-01`） |
| toolgood/ToolGood.Words | 敏感词、拼音、繁简 | 多语言实现，Python 侧集成成本较高 | Apache-2.0 | ✅ | 备选 |
| observerss/textfilter | 敏感词过滤 | **无许可证文件** | 无 | ❌ | 不可复用 |
| PaddlePaddle/PaddleNLP | 文本分类、语义模型 | 需模型与推理资源；作为 L3 兜底候选 | Apache-2.0 | ✅ | Post-1.0 评估 |

**结论**：规则层复用 `sensitive-word` 的 DFA 内核；分级词库、白名单、熔断策略、云审核适配全部自研。云审核（阿里云内容安全）是 SaaS 调用，不涉及代码许可。

### 3.7 完整参考架构（不 fork）

| 项目 | 可借鉴点 | 不适合直接使用的原因 |
| --- | --- | --- |
| Open-LLM-VTuber（MIT） | Live2D 渲染、ASR/TTS 组合、记忆与会话管理 | 面向桌面陪伴；无评论接入、推流、审核、接管与成本核算 |
| moeru-ai/airi（MIT） | Web 端 Live2D/VRM 渲染与交互 | 无直播编排、合规与运维能力 |
| Ikaros-521/AI-Vtuber（GPL-3.0） | 弹幕互动流程、话术结构 | GPL 不兼容；依赖非官方接入 |
| Dify / FastGPT（附加条件） | 知识库与提示词管理界面 | 许可证附加条件，且体量与部署成本过高 |

## 4. 复用 / 自研 / 胶水 决策

| 模块 | 复用现成项目 | 自研 | 胶水层 |
| --- | --- | --- | --- |
| 评论接入 | 无（官方 SDK 不适用） | 官方 OpenAPI 适配、验签、幂等、限流 | 平台字段 → 内部事件 schema |
| 对话编排 | 无（参考 LangGraph 思想） | 状态机、队列、节奏、打断、接管 | 事件总线与 Redis 状态 |
| LLM 网关 | 暂不引入 LiteLLM | DashScope 适配、路由、降级、记账 | 统一 `LLMProvider` 接口 |
| 内容安全规则 | houbb/sensitive-word（Apache-2.0） | 分级词库、白名单、策略、熔断 | 云审核 + 模型兜底适配 |
| TTS | CosyVoice 托管 API | 文本规范化、缓存、计费 | 分块音频 → 时间戳 → 口型 |
| Live2D 渲染 | pixi-live2d-display（MIT）+ Cubism Core（专有） | 渲染服务、口型驱动、表情/动作状态机、场景合成 | 音频时间戳 → 口型参数 → 渲染 |
| 媒体与推流 | FFmpeg（LGPL）/ GStreamer（LGPL） | 推流守护、码率自适应、断流重连 | 帧/音频 → 管线 → RTMP |
| 推送测试 | MediaMTX / SRS（MIT） | 不使用于生产 | staging 环境 |
| 可观测 | OpenTelemetry / Prometheus（Apache-2.0） | 业务指标与告警规则 | 采集与看板 |

## 5. 引入第三方的硬性流程

1. **许可证核对**：确认 SPDX 标识与 `LICENSE` 原文一致，记录版本与哈希；
2. **兼容性判定**：按第 2 节规则给出结论，落入 `INF-08` 白名单；
3. **附随义务**：Apache-2.0 归档 NOTICE；LGPL 提供声明、源码获取方式与可替换性说明；
4. **安全审查**：依赖漏洞扫描、供应链来源可信、固定版本；
5. **边界隔离**：GPL 类工具只能以独立进程/人工工具方式存在，且不得进入发布包；
6. **资产分离**：模型、贴图、音色、字体不随代码入库，单独授权与存储；
7. **记录**：在 ADR 或组件清单中登记引入理由、替代方案与退出成本。

## 6. 待验证事项

| 编号 | 事项 | 影响 | 验证方式 |
| --- | --- | --- | --- |
| O-01 | pixi-live2d-display 对 Cubism 5 模型的兼容性与口型接口 | 渲染方案 | M4 前做原型验证 |
| O-02 | 本主体营收是否达到 Cubism Release License 门槛 | 授权成本 | 法务按最近财年确认（GOV-07） |
| O-03 | FFmpeg LGPL 构建是否满足推流与滤镜需求 | 媒体栈 | 构建清单 + 许可证扫描（LIV-09） |
| O-04 | GStreamer 插件清单中是否存在 GPL 插件 | 许可证 | 插件审计 |
| O-05 | sensitive-word 在中文直播语境下的误杀率 | 安全策略 | 用红队集回归（SAF-06） |
| O-06 | 是否存在官方认可的直播评论 SDK | 接入方式 | 向平台/服务商书面确认（GOV-01） |

## 7. 不采用清单（黑名单）

| 项目/类型 | 原因 |
| --- | --- |
| 抓包、逆向、非官方协议类弹幕工具 | 违反合规硬约束；且多为 AGPL/GPL |
| OBS 作为产品内嵌组件 | GPL-2.0 传染 |
| Dify / FastGPT 整体嵌入 | 附加许可条件，与 MIT 发布冲突 |
| index-tts / fish-speech / ChatTTS | 自定义或 AGPL 许可，商用受限 |
| 无 LICENSE 的代码片段 | 默认保留所有权利 |
| 未授权 Live2D 模型、名人音色 | 侵权风险 |

## 8. 对技术选型与任务的影响

- 技术选型：Live2D 渲染确定 `pixi-live2d-display`；媒体层确定 FFmpeg（LGPL）+ GStreamer；安全规则层确定 `sensitive-word`；编排与网关保持自研。
- 任务调整：新增 `INF-08`（许可证门禁）、`AVT-08`（GPU 渲染稳定性）、`LIV-08`（推流地址防泄露）、`LIV-09`（媒体栈许可证验证）；`GOV-07` 覆盖 Live2D 软件与模型授权；`OPS-03` 降级为 Post-1.0。
- 发布要求：仓库根目录维护第三方组件清单与 NOTICE；不含任何模型、字体、专有运行时与推流凭据。
