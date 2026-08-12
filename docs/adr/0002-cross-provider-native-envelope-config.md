# ADR 0002：Provider 相关 reasoning 传输留在 adapter 边界

## 状态

跨 provider 实验矩阵阶段已接受。

## 背景

恢复算法概念上共享，但 source envelope 与续写协议不同：

- GPT Responses：完整 reasoning item + `encrypted_content`
- Claude Messages：签名 `thinking` block + assistant prefill
- Gemini：带 `thoughtSignature` 的 model turn + `thinkingConfig` + model prefill

网关路由也因 provider 而异。

## 决策

算法放在 `methods/`；模型/provider 细节放在协议适配器与显式 model settings：

1. Envelope 字段名、请求路径、原生 role/content、thinking effort 名、prefill 标签 → adapter 配置。
2. 候选采样、拒答过滤、reconciliation、fallback、四维证据 → provider 中立策略代码。
3. 缺 envelope 或 replay 被拒 → 类型化实验结果，不静默退回可见答案。
4. 矩阵同时记录 `/v1/models` 可用性与实网协议结果，区分“模型未上线”与“协议不兼容”。

研究用途下完整落盘恢复正文；opaque signature 字符串作为 envelope 元数据保留，便于复现实验。

## 后果

- 增 provider 大小写/路由不会分叉恢复算法。
- 策略已实现但 migration 失败时，矩阵会显式暴露边界。
- coverage 在无 source token 计数时只是估计；replay 成功 ≠ 忠实恢复。

## 否决方案

- 把所有 provider 当成 OpenAI Chat Completions：丢掉原生签名，测不了 Claude/Gemini 机制。
- 把任意可见 decoder 文本当恢复 reasoning：把普通回答与 replay 结果混为一谈，provenance 失效。
