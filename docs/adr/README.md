# 架构决策记录（ADR）

本目录记录**影响面较大、后续难以更改**的技术与产品决策。每条 ADR 一旦「Accepted」，不直接改写结论；需要推翻时新建一条并标注 supersedes。

## 索引

| 编号 | 决策 | 状态 |
| --- | --- | --- |
| [0001](0001-primary-cloud-alibaba.md) | 生产主链路采用阿里云 + 百炼托管模型 | Accepted |
| 0002 | 渲染技术路线（2D 视频驱动 / Live2D / 3D） | Proposed（待评估） |
| 0003 | 云端推流 vs 本地伴侣推流（架构 A/B） | Proposed（待平台结论） |
| 0004 | 发言队列采用 Redis Streams + ZSet | Proposed（随 M3 落地确认） |
| 0005 | 开源许可证（MIT / Apache-2.0） | **Pending（未决策）** |

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
