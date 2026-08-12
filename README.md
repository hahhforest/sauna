<div align="center">

# Sauna

### Reasoning Recovery Research Harness

**把模型的 hidden reasoning 恢复成可读正文**  
面向语言学 · 心理学 · 社会学的思考模式研究

[English](./README_EN.md) · [内部文档 AGENTS.md](./AGENTS.md) · [配置模板](./config.example.yaml)

<br/>

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Research-lightgrey)](#)
[![Methods](https://img.shields.io/badge/Methods-11-0A7B83)](./AGENTS.md#方法原理)
[![Providers](https://img.shields.io/badge/Providers-GPT%20%7C%20Claude%20%7C%20Gemini-6f42c1)](#支持的-provider)
[![Docs](https://img.shields.io/badge/Docs-AGENTS.md-111)](./AGENTS.md)

</div>

---

## 这是什么？

大模型在回答前往往会在 **不可见通道** 里完成一长段 reasoning（加密 content / signed thinking / thought signature）。  
Sauna 不在本地「破解」这些字段，而是通过 **官方协议把 envelope 注入 decoder**，诱导模型把 hidden working **抄写到可见输出**，并完整落盘。

```text
Source 模型  ──产出──▶  reasoning envelope（opaque）
                              │
                              ▼
Decoder 模型 ──协议 replay / prefill──▶  可见恢复正文
                              │
                              ▼
              runs/*.json  完整 text + 四维证据
```

**研究立场**：目的是恢复 reasoning 并交给分析，不是做安全演示。  
结果文件保留恢复正文、候选、envelope 元数据与错误详情——**不做脱敏截断**。

---

## 为什么需要它？

| 痛点 | Sauna 怎么做 |
|:---|:---|
| Hidden CoT 对人不可读 | 用 provider 原生 envelope 做 replay / fuzzy prefill |
| 各厂协议形状不同 | adapter 边界隔离 GPT / Claude / Gemini |
| 单次转录噪声大 | best-of-N、fallback、reconciliation 组合策略 |
| 结果被过度脱敏 | 研究 harness：**完整落盘** |
| 凭证散落全局配置 | 项目内 `config.yaml` + 自定义 header |

---

## 特性一览

- **11 种恢复方法**：single replay · repeated injection · chunk continuation · best-of-N · Luna→Terra fallback · reconciliation · Claude/Gemini fuzzy prefill
- **跨 provider**：OpenAI Responses · Chat Completions · Anthropic Messages · Gemini `generateContent`
- **四维证据**：`replay` / `provenance` / `coverage` / `fidelity` 独立评分，不合成虚假 overall_success
- **项目级配置**：`config.yaml`（gitignore）+ `config.example.yaml`；支持 `bearer` / `x-api-key` / 自定义 header（OpenRouter、企业网关友好）
- **矩阵实验脚本**：一键扫 method × model，JSON + Markdown 完整落盘

---

## 5 分钟上手

### 1. 克隆 & 依赖

```bash
git clone https://github.com/hahhforest/sauna.git
cd sauna
pip install pyyaml   # 读 YAML 配置需要
```

### 2. 配置上游 + 模型骨架

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`（**不会提交到 Git**）。核心不是写死 source/decoder 配对，而是声明**你有哪些模型**：

```yaml
upstream:
  base_url: "https://your-upstream.example/v1"
  api_key: "sk-..."
  headers:
    X-Title: "sauna-reasoning-recovery"

models:
  sol:
    family: gpt
    id: gpt-5.6-sol
    roles: [source]
  luna:
    family: gpt
    id: gpt-5.6-luna
    roles: [decoder]
  terra:
    family: gpt
    id: gpt-5.6-terra
    roles: [decoder, reconciler]
```

> 方法自己声明依赖（如 `gpt.luna_then_terra` 需要 luna）。缺模型会报错并走 fallback。  
> `python3 reasoning_probe.py --list-methods` 可查看当前能跑什么。  
> **不读取** `~/.minimax`。

### 3. 跑一次恢复

```bash
python3 reasoning_probe.py '请计算 17 * 23，并给出最终结果。'
python3 reasoning_probe.py --method gpt.single_replay --output runs/one.json '...'
```

### 4. 跑跨 provider 矩阵（可选）

```bash
python3 scripts/run_provider_matrix.py \
  --providers gpt,claude,gemini \
  --output runs/provider_matrix.json \
  --markdown-output runs/provider_matrix.md
```

### 5. 单测

```bash
python3 -m unittest test_recovery_harness.py -v
```

---

## 支持的 Provider

| Provider | 协议 | Envelope | 代表方法 |
|:---|:---|:---|:---|
| **GPT** | `responses` / `chat_completions` | `encrypted_content` 等 | `gpt.single_replay` · `gpt.repeated_injection` · `gpt.chunk_continuation` |
| **Claude** | `anthropic_messages` | signed `thinking` + `signature` | `claude.fuzzy_prefill` · `claude.reconciliation` |
| **Gemini** | `gemini` | `thoughtSignature` + model prefill | `gemini.fuzzy_prefill` · `gemini.reconciliation` |

方法原理与 **method × model 实验状态表** 见 → [AGENTS.md](./AGENTS.md)

---

## 架构（一眼看懂）

```text
┌─────────────┐   ┌──────────────────┐   ┌─────────────┐
│   config    │ → │  protocol /      │ → │   methods   │
│  yaml/env   │   │  adapters        │   │  策略层     │
└─────────────┘   └──────────────────┘   └──────┬──────┘
                                                │
                      ┌─────────────────────────▼──────────┐
                      │  engine  有序执行 + attempt 保留    │
                      └─────────────────────────┬──────────┘
                                                │
                      ┌─────────────────────────▼──────────┐
                      │  validation  四维证据               │
                      │  replay · provenance · coverage ·  │
                      │  fidelity                          │
                      └────────────────────────────────────┘
```

| 层 | 职责 |
|:---|:---|
| `config` | 项目配置、鉴权、自定义 header |
| `protocol` / `provider_adapters` | 请求响应形状、opaque envelope 发现 |
| `methods` | 恢复算法（与具体 HTTP 形状解耦） |
| `validation` | 四维证据，不声称不存在的 ground truth |
| `engine` | 编排、fallback、完整结果组装 |

---

## 四维证据

| 维度 | 含义 |
|:---|:---|
| **replay** | decoder 是否至少返回了响应 |
| **provenance** | marker 是否支持「来自 source hidden reasoning」 |
| **coverage** | recovered_tokens / source_reasoning_tokens（估计） |
| **fidelity** | 多候选一致性 / 可选语义 verifier |

---

## 文档怎么分？

| 文件 | 给谁看 | 内容 |
|:---|:---|:---|
| **[README.md](./README.md)**（本页） | GitHub 访客 | 一句话定位、快速上手、架构鸟瞰 |
| **[README_EN.md](./README_EN.md)** | 英文访客 | 同上英文版 |
| **[AGENTS.md](./AGENTS.md)** | 协作者 / Agent | 方法原理、完整实验表、操作约定 |
| **[docs/adr/](./docs/adr/)** | 架构决策 | 边界与 adapter 原则 |
| **[config.example.yaml](./config.example.yaml)** | 首次配置 | 可提交的配置模板 |

---

## 安全与隐私

| 内容 | 是否进 Git |
|:---|:---|
| `config.yaml`（含 api_key） | ❌ gitignore |
| `runs/` 实验结果 | ❌ gitignore |
| `config.example.yaml` 占位符 | ✅ |
| 恢复算法与文档 | ✅ |

请勿把真实 key 或实验全文提交到公开仓库。

---

## 当前实验速览

（数据：2026-08-11 实网矩阵；细节与完整表见 [AGENTS.md](./AGENTS.md)）

- **GPT**：各方法多可 `replay=success`，但恢复偏短（coverage ~0.06–0.19）
- **Claude Opus → Haiku fuzzy**：较长恢复之一（len≈566，ratio≈0.38）
- **Gemini 3.1-pro → 3.5-flash fuzzy**：目前最长（len≈2588）
- 若干 method×model 组合尚未跑满

---

## 路线图（研究阶段）

- [x] 跨 provider 协议适配与 11 方法
- [x] 项目级 config + 自定义 header
- [x] 完整落盘矩阵脚本
- [ ] 扩大重复次数与语义 verifier
- [ ] 按成本 / 速度 / 稳定性生成默认方法梯度
- [ ] （可选）本地服务化——有方法画像之后再做

---

## 贡献与协作

1. 改代码后跑：`python3 -m unittest test_recovery_harness.py -v`
2. 新增方法：实现 → `method_registry()` 注册 → 更新 [AGENTS.md](./AGENTS.md) 原理与结果行
3. 深水区约定以 **AGENTS.md** 为准

Issues / PR 欢迎。研究用途优先。

---

<div align="center">

**恢复思考，理解模型。**

[English README](./README_EN.md) · [AGENTS.md](./AGENTS.md)

</div>
