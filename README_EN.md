<div align="center">

# Sauna

### Reasoning Recovery Research Harness

**Recover hidden model reasoning into readable text**

[中文](./README.md) · [Internal docs: AGENTS.md](./AGENTS.md) · [Config template](./config.example.yaml)

<br/>

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Research-lightgrey)](#)
[![Methods](https://img.shields.io/badge/Methods-11-0A7B83)](./AGENTS.md)
[![Providers](https://img.shields.io/badge/Providers-GPT%20%7C%20Claude%20%7C%20Gemini-6f42c1)](#supported-providers)
[![Docs](https://img.shields.io/badge/Docs-AGENTS.md-111)](./AGENTS.md)

</div>

---

## What is this?

Before answering, many models produce a long stretch of **hidden reasoning** in an opaque channel (encrypted content / signed thinking / thought signature).  
Sauna does **not** decrypt those fields locally. It **injects the official envelope into a decoder** via the provider protocol, elicits a verbatim-style transcription into the **visible** channel, and **persists the full text**.

```text
Source model  ──produces──▶  reasoning envelope (opaque)
                                    │
                                    ▼
Decoder model ──protocol replay / prefill──▶  recovered visible text
                                    │
                                    ▼
                    runs/*.json  full text + 4 evidence dimensions
```

**Research stance:** the goal is to recover reasoning for analysis—not to demo a safety product.  
Artifacts keep recovered text, candidates, envelope metadata, and error details—**no redaction truncation**.

---

## Why Sauna?

| Pain | What Sauna does |
|:---|:---|
| Hidden CoT is unreadable | Replay / fuzzy prefill with native envelopes |
| Providers differ in shape | Adapters isolate GPT / Claude / Gemini |
| Single transcripts are noisy | Best-of-N, fallback, reconciliation |
| Over-redacted logs | Research harness: **full persistence** |
| Credentials in global agent configs | Project-local `config.yaml` + custom headers |

---

## Features

- **11 recovery methods**: single replay · repeated injection · chunk continuation · best-of-N · Luna→Terra fallback · reconciliation · Claude/Gemini fuzzy prefill
- **Cross-provider**: OpenAI Responses · Chat Completions · Anthropic Messages · Gemini `generateContent`
- **Four independent evidence axes**: `replay` / `provenance` / `coverage` / `fidelity` (no fake overall_success)
- **Project config**: gitignored `config.yaml` + committed `config.example.yaml`; `bearer` / `x-api-key` / custom headers (OpenRouter & enterprise gateways)
- **Matrix runner**: sweep method × model; write full JSON + Markdown

---

## Quickstart (5 minutes)

### 1. Clone & dependency

```bash
git clone https://github.com/hahhforest/sauna.git
cd sauna
pip install pyyaml
```

### 2. Configure upstream + model skeleton

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` (**never committed**). You declare **which models exist**, not a fixed source/decoder pair:

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

> Methods declare role deps (e.g. `gpt.luna_then_terra` needs luna). Missing models error and fall back.  
> `python3 reasoning_probe.py --list-methods` shows what is runnable.  
> Does **not** read `~/.minimax`.

### 3. One recovery run

```bash
python3 reasoning_probe.py 'Compute 17 * 23 and give the final answer.'
python3 reasoning_probe.py --method gpt.single_replay --output runs/one.json '...'
```

### 4. Cross-provider matrix (optional)

```bash
python3 scripts/run_provider_matrix.py \
  --providers gpt,claude,gemini \
  --output runs/provider_matrix.json \
  --markdown-output runs/provider_matrix.md
```

### 5. Tests

```bash
python3 -m unittest test_recovery_harness.py -v
```

---

## Supported providers

| Provider | Protocol | Envelope | Headline methods |
|:---|:---|:---|:---|
| **GPT** | `responses` / `chat_completions` | `encrypted_content`, … | `gpt.single_replay` · `gpt.repeated_injection` · `gpt.chunk_continuation` |
| **Claude** | `anthropic_messages` | signed `thinking` + `signature` | `claude.fuzzy_prefill` · `claude.reconciliation` |
| **Gemini** | `gemini` | `thoughtSignature` + model prefill | `gemini.fuzzy_prefill` · `gemini.reconciliation` |

Method theory + **method × model status table** → [AGENTS.md](./AGENTS.md) (Chinese; tables are language-agnostic)

---

## Architecture at a glance

```text
┌─────────────┐   ┌──────────────────┐   ┌─────────────┐
│   config    │ → │  protocol /      │ → │   methods   │
│  yaml/env   │   │  adapters        │   │  strategies │
└─────────────┘   └──────────────────┘   └──────┬──────┘
                                                │
                      ┌─────────────────────────▼──────────┐
                      │  engine  ordered runs + attempts   │
                      └─────────────────────────┬──────────┘
                                                │
                      ┌─────────────────────────▼──────────┐
                      │  validation  four evidence axes    │
                      └────────────────────────────────────┘
```

| Layer | Role |
|:---|:---|
| `config` | Project config, auth, custom headers |
| `protocol` / `provider_adapters` | Wire shapes, opaque envelope discovery |
| `methods` | Recovery algorithms (provider-neutral) |
| `validation` | Evidence only—no invented ground truth |
| `engine` | Orchestration, fallback, full result assembly |

---

## Four evidence dimensions

| Axis | Meaning |
|:---|:---|
| **replay** | Did the decoder return at least one response? |
| **provenance** | Does the marker support “from source hidden reasoning”? |
| **coverage** | recovered_tokens / source_reasoning_tokens (estimate) |
| **fidelity** | Multi-candidate consistency / optional semantic verifier |

---

## Documentation map

| File | Audience | Content |
|:---|:---|:---|
| **[README.md](./README.md)** | Chinese visitors | Landing page (this content, Chinese) |
| **[README_EN.md](./README_EN.md)** (this page) | English visitors | Landing page |
| **[AGENTS.md](./AGENTS.md)** | Collaborators / agents | Method theory, full result tables, ops rules |
| **[docs/adr/](./docs/adr/)** | Design decisions | Boundaries & adapter principles |
| **[config.example.yaml](./config.example.yaml)** | First-time setup | Committable template |

---

## Security & privacy

| Artifact | In Git? |
|:---|:---|
| `config.yaml` (real api_key) | ❌ gitignored |
| `runs/` experimental outputs | ❌ gitignored |
| `config.example.yaml` placeholders | ✅ |
| Algorithms & docs | ✅ |

Never commit real keys or full experiment dumps to a public repo.

---

## Experiment snapshot

(Source: 2026-08-11 live matrix; full table in [AGENTS.md](./AGENTS.md))

- **GPT**: most methods `replay=success`, but recoveries are short (coverage ~0.06–0.19)
- **Claude Opus → Haiku fuzzy**: among longer recoveries (len≈566, ratio≈0.38)
- **Gemini 3.1-pro → 3.5-flash fuzzy**: longest so far (len≈2588)
- Several method×model cells still unrun

---

## Roadmap (research phase)

- [x] Cross-provider adapters + 11 methods
- [x] Project-local config + custom headers
- [x] Full-persistence matrix scripts
- [ ] More repeats + semantic verifier
- [ ] Default method ladder by cost / speed / stability / evidence
- [ ] (Optional) local service—only after method profiles exist

---

## Contributing

1. After code changes: `python3 -m unittest test_recovery_harness.py -v`
2. New method: implement → register in `method_registry()` → update [AGENTS.md](./AGENTS.md)
3. Deep conventions live in **AGENTS.md**

Issues / PRs welcome. Research use first.

---

<div align="center">

**Recover the thought. Understand the model.**

[中文 README](./README.md) · [AGENTS.md](./AGENTS.md)

</div>
