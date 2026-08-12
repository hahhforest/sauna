# ADR 0001：研究 harness 边界

## 状态

当前实验阶段已接受。

## 背景

在上本地服务或自动生产策略之前，必须先对固定 source 模型比较多种 reasoning 恢复策略。
一次恢复尝试有四个独立证据维度：replay、provenance、coverage、fidelity。
Provider 载荷形状、恢复 prompt、候选选择、fallback 路由与验证的失败模式不同，必须可独立测试。

## 决策

使用小型内存 Python 包，五层：

1. `protocol`：provider 请求/响应适配与 opaque envelope 解析。
2. `methods`：有界的单次恢复策略。
3. `composition`：best-of-N、fallback、分块、reconciliation 编排。
4. `validation`：四维证据；验证器不声称不存在的 ground truth。
5. `engine`：有序执行、方法错误与不可变 attempt 记录。

CLI 保持薄入口：选方法与 fallback，不含 provider 载荷逻辑。

**研究落盘**：结果文件完整保存恢复正文、候选文本与错误详情，不做脱敏截断。凭证仍从环境变量读取，不写进代码。

## 后果

- 可新增 provider 方法而不改 engine/validator。
- 单方法失败不吞掉后续 fallback。
- reconciliation 因候选进入 reconciler prompt，对 hidden-only marker 实验标为 provenance-unsafe。
- 本阶段刻意没有 Flask、持久化服务或学习到的默认策略；那些需要先有方法画像。

## 否决方案

- 单一 `reasoning_probe.py` 堆 provider 分支：起步快，但耦合协议、重试与证据语义，比较不可靠。
- 单一 `overall_success` 布尔：掩盖 replay 接受、provenance、coverage、fidelity 的差异。
