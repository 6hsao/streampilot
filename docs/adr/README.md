# 架构决策记录（ADR）

本目录记录**影响面较大、后续难以更改**的技术与产品决策。每条 ADR 一旦「Accepted」，不直接改写结论；需要推翻时新建一条并标注 supersedes。

## 索引

| 编号 | 决策 | 状态 |
| --- | --- | --- |
| [0001](0001-primary-cloud-alibaba.md) | 生产主链路采用阿里云 + 百炼托管模型 | Accepted |
| [0002](0002-live2d-renderer.md) | 数字人渲染采用 Live2D | Accepted |
| [0003](0003-cloud-rtmp-push.md) | 云端直接推 RTMP（架构 A） | Accepted |
| [0004](0004-single-platform-single-room.md) | 首发只做抖音单平台、单直播间 | Accepted |
| [0005](0005-mit-license.md) | 开源许可证采用 MIT | Accepted |
| [0006](0006-oss-reuse-policy.md) | 开源复用与许可证门禁策略 | Accepted |
| 0007 | 发言队列采用 Redis Streams + ZSet | Proposed（随 M3 落地确认） |

## 模板

```markdown
# NNNN. 标题

- 状态：Proposed | Accepted | Superseded by NNNN | Deprecated
- 日期：YYYY-MM-DD
- 决策人：

## 背景
需要解决什么问题，受哪些约束。

## 决策
选择什么，明确边界。

## 备选方案
- 方案 A：优缺点
- 方案 B：优缺点

## 影响
正面/负面影响，需要跟随的改动，回滚成本。

## 依据
数据、测试、外部结论或链接。
```
