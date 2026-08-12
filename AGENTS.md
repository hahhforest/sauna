# AGENTS.md — Reasoning Recovery 研究项目

研究目标：恢复模型 hidden reasoning，完整落盘，供语言学、心理学、社会学分析思考模式。

对外入口（GitHub 首页预览）：[README.md](./README.md)（中文）· [README_EN.md](./README_EN.md)（English）。  
**本文件**是协作者 / Agent 的操作手册：方法原理、完整实验表、硬约定。README 保持轻量，细节以本文件为准。

```text
Source 模型 → reasoning envelope → Decoder replay → 完整恢复正文 + 四维证据
```

本地**不解密** reasoning。验证的是 decoder 能否通过官方协议“读到/续写”被注入的 envelope。

## 第一性原则

1. **目的是恢复 reasoning**，不是做安全产品演示。
2. **结果完整落盘**：恢复正文、候选文本、envelope 元数据、错误 details 全部写入 `runs/`；不做脱敏截断。
3. **凭证只放项目 `config.yaml` 或环境变量**，不写进仓库；不读取 `~/.minimax` 等全局 agent 配置。
4. 文档与注释默认**中文**；与上游 API 交互的 prompt 字符串可保持英文（协议兼容）。
5. 每个脚本有模块 docstring；函数有简洁中文注释。
6. **文档分工**：`README.md` / `README_EN.md` = 对外落地页（互指）；`AGENTS.md` = 方法原理 + 实验状态 + agent 约定。改对外介绍时同步中英文 README；改实验表/方法时改本文件。

## 目录与架构

```text
config.example.yaml             # 配置模板（可提交）
config.yaml                     # 本地真实配置（gitignore，含 api_key）
reasoning_probe.py              # 薄 CLI
reasoning_recovery/             # 核心包
  config.py                     # 加载 config.yaml / 环境变量 / headers
  protocol.py                   # Responses / Chat / envelope 发现 / HTTP
  provider_adapters.py          # Claude / Gemini 原生形状
  methods/{gpt,provider,composition,base}.py
  engine.py / validation.py / models.py / errors.py
scripts/
  live_gpt_check.py             # 实网单次检查（完整正文）
  run_provider_matrix.py        # 跨 provider 矩阵（完整落盘）
  merge_matrix_results.py       # 分片合并
docs/adr/                       # 架构决策（补充，非日常入口）
runs/                           # 实验结果（含正文；gitignore）
test_recovery_harness.py
```

| 层 | 职责 |
|---|---|
| `config` | 项目配置、鉴权、自定义 header |
| `protocol` / `provider_adapters` | provider 请求响应、opaque envelope |
| `methods` | 有界单次策略 + best-of-N / fallback / reconciliation |
| `validation` | replay / provenance / coverage / fidelity |
| `engine` | 有序执行，保留全部 attempt |

`Settings.model_config` 可覆盖 signature 字段、Claude thinking、Gemini thinking level、prefill 标签。协议形状变化应加 adapter，不要在方法层堆 provider 分支。

## 配置

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml：填 base_url、api_key、需要的 headers
```

配置优先级（后者覆盖前者）：

1. `config.yaml`（或 `SAUNA_CONFIG` / `--config`）
2. 环境变量 `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY`
3. CLI 参数（`--base-url`、`--api-key`、`--header` 等）

`config.yaml` 结构要点：

| 段 | 作用 |
|---|---|
| `upstream` | `base_url`、`api_key`、`auth`（bearer / x-api-key / header / none）、`headers` |
| `defaults` | 默认 source/decoder/protocol/effort/timeout |
| `protocols.<name>` | 按协议附加 header / 改 auth / `model_config` |
| `models.<profile>` | 命名模型档案：`id`、`protocol`、`headers`、`model_config` |

自定义 header 示例（OpenRouter / 企业网关常见）：

```yaml
upstream:
  base_url: "https://openrouter.ai/api/v1"
  api_key: "sk-or-..."
  headers:
    HTTP-Referer: "https://github.com/hahhforest/sauna"
    X-Title: "sauna-reasoning-recovery"
```

Anthropic 风格网关：

```yaml
upstream:
  base_url: "https://your-gateway.example"
  api_key: "sk-ant-..."
  auth: x-api-key
protocols:
  anthropic_messages:
    headers:
      anthropic-version: "2023-06-01"
```

依赖：读取 YAML 需要 `PyYAML`（`pip install pyyaml`）。

## 使用

```bash
# 单次恢复（读 config.yaml）
python3 reasoning_probe.py '请计算 17 * 23，并给出最终结果。'
python3 reasoning_probe.py \
  --decoder-model gpt-5.6-terra \
  --method gpt.repeated_injection \
  --fallback gpt.single_best_of_3,gpt.chunk_continuation \
  --header 'X-Title:sauna' \
  --output runs/one.json \
  '请解决一个多步数学问题。'

# 换协议 / 模型配置
python3 reasoning_probe.py --protocol gemini \
  --model-config '{"prefill_tag":"<thought>","thinking_level_field":"thinkingLevel"}' \
  '请解决一个简单的代数问题。'

# 实网检查
python3 scripts/live_gpt_check.py \
  --methods gpt.single_replay,gpt.repeated_injection \
  --output runs/live_check.json \
  '请计算 17 * 23，只返回最终结果。'

# 跨 provider 矩阵（完整落盘）
python3 scripts/run_provider_matrix.py \
  --providers gpt,claude,gemini \
  --candidate-pool 3 --selection-count 3 \
  --output runs/provider_matrix.json \
  --markdown-output runs/provider_matrix.md

python3 scripts/merge_matrix_results.py runs/gpt.json runs/claude.json runs/gemini.json \
  --output runs/provider_matrix_combined.json \
  --markdown-output runs/provider_matrix_combined.md

# 测试
python3 -m unittest test_recovery_harness.py -v
```

退出码：`0` 有非空恢复正文；`3` replay 成功但正文空；`1` 协议/上游错误；`2` 配置缺失。

常见错误码：`SOURCE_NO_REASONING_ENVELOPE`、`UPSTREAM_AUTH_ERROR`、`UPSTREAM_HTTP_ERROR`、`CONFIG_MISSING`。`details.phase` 标记 source 或 replay。

## 方法原理

### GPT 系（Responses / encrypted_content）

| 方法 | 原理 |
|---|---|
| `gpt.single_replay` | 把 source 返回的完整 reasoning item（含 `encrypted_content`）注入 decoder 上下文，再发一条 elicitation，要求原样抄写 hidden working。 |
| `gpt.repeated_injection` | 同一 envelope 在对话里注入两次，强化 decoder 对 opaque 内容的“可见性”，再 elicitation。 |
| `gpt.chunk_continuation` | 每次只要约 N token 的片段；把上一段尾部当锚，循环续写并去词重叠，拼成长文本。适合一次吐不全的长 reasoning。 |
| `gpt.single_best_of_3` | 对 `single_replay` 独立采 3 次；按 marker 命中 > 长度接近 source reasoning tokens > 绝对长度选最优。 |
| `gpt.repeated_best_of_3` | 同上，底座换成 `repeated_injection`。 |
| `gpt.luna_then_terra` | 先用主 decoder（默认 Luna）单次 replay；空/拒答再换 Terra。不改 source envelope。 |
| `gpt.reconcile_with_terra` | 多次 single_replay 得候选 → 按 token 误差筛 top-k → Terra 对照候选合并一份。候选进了 reconciler prompt，故 `provenance_safe=False`。 |

### Claude 系（signed thinking + assistant prefill）

| 方法 | 原理 |
|---|---|
| `claude.fuzzy_prefill` | source 产出带 `signature` 的 thinking block；decoder 轮以 thinking block + 文本 prefill `<thinking-copy>` 开头，诱导模型把 attached reasoning 抄进可见通道。 |
| `claude.reconciliation` | 多次 fuzzy 采样 → 选 top-k → 默认用 `claude-opus-4-8` 做 reconciler，在原生消息形状里合并候选。provenance 标 invalidated。 |

### Gemini 系（thoughtSignature + model prefill）

| 方法 | 原理 |
|---|---|
| `gemini.fuzzy_prefill` | source 产出 `thoughtSignature`；decoder 以 model prefill `<thought>` 续写，要求 duplicate attached thought。 |
| `gemini.reconciliation` | 多次 fuzzy（默认 pool 较大）→ 筛 top-k → `gemini-3.5-flash` reconciler，prefill `<reconciliation>`。provenance 标 invalidated。 |

### 四维证据

| 维度 | 判定要点 |
|---|---|
| **replay** | 有无 decoder 原始响应 |
| **provenance** | marker 是否只来自 source instruction 且出现在恢复正文；reconciliation 固定 invalidated |
| **coverage** | `recovered_tokens / source_reasoning_tokens`（空白分词估计，非逐 token GT） |
| **fidelity** | 多候选 Jaccard；可选语义 verifier |

## 当前实验结果

数据源：`runs/provider_matrix_combined.json`（2026-08-11 实网矩阵，23 条有效记录）。  
表内缩写：`r`=replay，`p`=provenance，`f`=fidelity，`ratio`=coverage ratio，`len`=恢复正文长度。  
**注意**：该轮矩阵脚本当时只落盘元数据，**没有保存恢复正文**；后续重跑会按新逻辑写入完整 `text` / `candidate_texts`。

### 主表：method × (source → decoder)，effort=high

| provider | source | decoder | method | 结果 |
|---|---|---|---|---|
| gpt | gpt-5.6-sol | gpt-5.6-luna | gpt.single_replay | ok · r=success · p=not_evaluated · f=unknown · ratio=0.13 · len=58 |
| gpt | gpt-5.6-sol | gpt-5.6-luna | gpt.repeated_injection | ok · r=success · p=not_evaluated · f=unknown · ratio=0.07 · len=58 |
| gpt | gpt-5.6-sol | gpt-5.6-luna | gpt.chunk_continuation | ok · r=success · p=not_evaluated · f=unknown · ratio=0.09 · len=44 |
| gpt | gpt-5.6-sol | gpt-5.6-luna | gpt.single_best_of_3 | ok · r=success · p=not_evaluated · f=unknown · ratio=0.16 · len=53 |
| gpt | gpt-5.6-sol | gpt-5.6-luna | gpt.repeated_best_of_3 | ok · r=success · p=not_evaluated · f=unknown · ratio=0.11 · len=58 |
| gpt | gpt-5.6-sol | gpt-5.6-luna | gpt.luna_then_terra | ok · r=success · p=not_evaluated · f=unknown · ratio=0.06 · len=42 |
| gpt | gpt-5.6-sol | gpt-5.6-luna | gpt.reconcile_with_terra | ok · r=success · p=invalidated · f=unknown · ratio=0.07 · len=24 |
| gpt | gpt-5.6-sol | gpt-5.6-terra | gpt.single_replay | ok · r=success · p=not_evaluated · f=unknown · ratio=0.10 · len=43 |
| gpt | gpt-5.6-sol | gpt-5.6-terra | gpt.repeated_injection | ok · r=success · p=not_evaluated · f=unknown · ratio=0.13 · len=58 |
| gpt | gpt-5.6-sol | gpt-5.6-terra | gpt.chunk_continuation | ok · r=success · p=not_evaluated · f=unknown · ratio=0.06 · len=33 |
| gpt | gpt-5.6-sol | gpt-5.6-terra | gpt.single_best_of_3 | **未跑** |
| gpt | gpt-5.6-sol | gpt-5.6-terra | gpt.repeated_best_of_3 | **未跑** |
| gpt | gpt-5.6-sol | gpt-5.6-terra | gpt.luna_then_terra | **未跑** |
| gpt | gpt-5.6-sol | gpt-5.6-terra | gpt.reconcile_with_terra | **未跑** |
| claude | claude-fable-5 | claude-haiku-4-5 | claude.fuzzy_prefill | method_empty · r=fail · len=0 |
| claude | claude-fable-5 | claude-haiku-4-5 | claude.reconciliation | method_empty · r=fail · p=invalidated · f=fail · len=0 |
| claude | claude-opus-4-8 | claude-haiku-4-5 | claude.fuzzy_prefill | ok · r=success · p=not_evaluated · f=unknown · ratio=0.38 · len=566 |
| claude | claude-opus-4-8 | claude-haiku-4-5 | claude.reconciliation | ok · r=success · p=invalidated · f=unknown · ratio=0.55 · len=385 |
| claude | claude-sonnet-5 | claude-haiku-4-5 | claude.fuzzy_prefill | error · SOURCE_NO_REASONING_ENVELOPE |
| claude | claude-sonnet-5 | claude-haiku-4-5 | claude.reconciliation | **未跑** |
| gemini | gemini-3.1-pro-preview | gemini-3.1-flash-lite | gemini.fuzzy_prefill | ok · r=success · ratio=0.78 · len=1108 |
| gemini | gemini-3.1-pro-preview | gemini-3.1-flash-lite | gemini.reconciliation | method_empty · r=success · p=invalidated · f=fail · ratio=0.00 · len=0 |
| gemini | gemini-3.1-pro-preview | gemini-3.5-flash | gemini.fuzzy_prefill | ok · r=success · ratio=1.68 · len=2588 |
| gemini | gemini-3.1-pro-preview | gemini-3.5-flash | gemini.reconciliation | ok · r=success · p=invalidated · ratio=0.50 · len=381 |
| gemini | gemini-3.6-flash | gemini-3.1-flash-lite | gemini.fuzzy_prefill | method_empty · r=fail · len=0 |
| gemini | gemini-3.6-flash | gemini-3.1-flash-lite | gemini.reconciliation | **未跑** |
| gemini | gemini-3.6-flash | gemini-3.5-flash | gemini.fuzzy_prefill | method_empty · r=fail · len=0 |
| gemini | gemini-3.6-flash | gemini-3.5-flash | gemini.reconciliation | **未跑** |

### GPT effort 消融（source=sol → decoder=luna，method=single_replay）

| effort | 结果 |
|---|---|
| low | ok · ratio=0.19 · len=53 |
| medium | ok · ratio=0.07 · len=31 |
| high | ok · ratio=0.13 · len=58 |

### 状态解读

1. **GPT**：各方法几乎都能 replay success，但恢复长度很短（几十字符）、coverage 约 0.06–0.19。更像“短摘要/片段”而非完整 CoT。provenance 未评估（该轮无 baseline）。
2. **Claude Opus→Haiku fuzzy**：目前最长恢复之一（len=566，ratio≈0.38）；Fable empty；Sonnet 无 envelope。
3. **Gemini 3.1-pro→3.5-flash fuzzy**：最长（len=2588，ratio>1，可能扩写/复述而非严格逐 token）；3.6-flash 本轮 fail。
4. **reconciliation**：按设计 provenance=invalidated；Gemini lite reconciler 曾空结果。
5. **空白格**：Terra 上若干 composition 方法、Sonnet reconciliation、3.6 reconciliation 尚未跑。

## 边界与下一步

仍是研究 harness，不是本地服务。下一步：扩大重复次数、补语义 verifier、按成本/速度/稳定性/四维证据做默认方法梯度。细节见 `docs/adr/`。

## 给后续 agent 的操作约定

1. 改代码后跑：`python3 -m unittest test_recovery_harness.py -v`
2. 重跑矩阵用 `scripts/run_provider_matrix.py`；确认 JSON 里有 `text` / `candidate_texts`。
3. 更新**本文件**结果表：以最新 `runs/provider_matrix_combined.json` 为准。
4. 不要重新引入“只存 text_length、不存 text”的脱敏逻辑。
5. 新增方法：`methods/` 实现 + `method_registry()` 注册 + 本文件补原理行与矩阵结果行；若对外特性列表变化，同步 `README.md` / `README_EN.md` 的简短 bullet。
