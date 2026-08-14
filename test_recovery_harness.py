"""Reasoning 恢复 harness 单元测试。

覆盖协议适配、各恢复方法、四维验证与引擎 fallback。
"""

import unittest
from dataclasses import replace
from unittest.mock import patch

from reasoning_probe import method_registry
from reasoning_recovery.engine import RecoveryEngine
from reasoning_recovery.errors import ProbeError
from reasoning_recovery.methods import (
    BestOfNMethod,
    ChunkContinuationMethod,
    ClaudeFuzzyExtractionMethod,
    ClaudeReconciliationMethod,
    GeminiFuzzyExtractionMethod,
    GeminiReconciliationMethod,
    ReconciliationMethod,
    RepeatedInjectionMethod,
    SingleReplayMethod,
    TerraFallbackMethod,
)
from reasoning_recovery.models import MethodContext, MethodResult, Settings
from reasoning_recovery.protocol import (
    OpenAIResponsesAdapter,
    OpenAIChatCompletionsAdapter,
    adapter_for,
    find_envelopes,
    make_source_prompt,
)
from reasoning_recovery.provider_adapters import (
    AnthropicMessagesAdapter,
    GeminiGenerateContentAdapter,
)
from reasoning_recovery.validation import (
    validate_coverage,
    validate_fidelity,
    validate_provenance,
)


def message_payload(text: str) -> dict:
    return {"output": [{"type": "message", "content": [{"text": text}]}]}


def source_payload(marker: str = "LAB-MARKER-ABC", reasoning_tokens: int = 10) -> dict:
    return {
        "output": [
            {"type": "reasoning", "encrypted_content": "opaque-value"},
            {"type": "message", "content": [{"text": "OK"}]},
        ],
        "usage": {"output_tokens_details": {"reasoning_tokens": reasoning_tokens}},
    }


def chat_source_payload(reasoning_tokens: int = 10) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "OK"},
                        {"type": "thinkingSignature", "thinkingSignature": "opaque-chat"},
                    ]
                }
            }
        ],
        "usage": {"completion_tokens_details": {"reasoning_tokens": reasoning_tokens}},
    }


def anthropic_source_payload() -> dict:
    return {
        "content": [
            {"type": "thinking", "thinking": "", "signature": "opaque-anthropic"},
            {"type": "text", "text": "OK"},
        ],
        "usage": {"output_tokens_details": {"thinking_tokens": 10}},
    }


def gemini_source_payload() -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "OK"},
                        {"thought_signature": "opaque-gemini"},
                    ]
                }
            }
        ],
        "usageMetadata": {"thoughtsTokenCount": 10},
    }


class FakeClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict, float]] = []

    def post(self, path: str, body: dict, timeout: float) -> dict:
        self.requests.append((path, body, timeout))
        if not self.responses:
            raise ProbeError("FAKE_NO_RESPONSE", "fake client response queue is empty")
        return self.responses.pop(0)


class FlakyMethod:
    name = "flaky"

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.calls = 0

    def run(self, context: MethodContext) -> MethodResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise ProbeError("FAKE_CANDIDATE_ERROR", "candidate failed")
        return MethodResult(self.name, "candidate", raw_outputs=({"ok": True},))


class HarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            base_url="https://example.test/v1",
            api_key="test-only",
            source_model="gpt-5.6-sol",
            decoder_model="gpt-5.6-luna",
            effort="high",
            max_output_tokens=128,
            timeout=5.0,
        )

    def harvest(self, client: FakeClient):
        adapter = OpenAIResponsesAdapter(client)
        return adapter.harvest(self.settings, "solve", "LAB-MARKER-ABC")

    def context(self, adapter, harvest, client, decoder: str = "gpt-5.6-luna"):
        return MethodContext(
            harvest=harvest,
            decoder_model=decoder,
            max_output_tokens=128,
            timeout=5.0,
            client=adapter,
            elicitation="copy",
            chunk_tokens=3,
            max_chunks=4,
        )

    def test_protocol_harvest_keeps_marker_out_of_user_prompt_and_finds_envelope(self) -> None:
        client = FakeClient([source_payload()])
        harvest = self.harvest(client)
        body = client.requests[0][1]
        self.assertEqual(harvest.envelope.path, "$.output[0].encrypted_content")
        self.assertIn("LAB-MARKER-ABC", body["instructions"])
        self.assertNotIn("LAB-MARKER-ABC", body["input"][0]["content"])
        self.assertNotIn("LAB-MARKER-ABC", make_source_prompt("solve"))

    def test_model_config_can_register_an_additional_signature_field(self) -> None:
        client = FakeClient([{"custom_signature": "opaque", "output": []}])
        settings = Settings(**{**self.settings.__dict__, "model_config": {"signature_fields": ["custom_signature"]}})
        harvest = OpenAIResponsesAdapter(client).harvest(settings, "solve", "LAB-MARKER-ABC")
        self.assertEqual(harvest.envelope.field, "custom_signature")

    def test_protocol_replay_builders_are_distinct(self) -> None:
        client = FakeClient([source_payload()])
        adapter = OpenAIResponsesAdapter(client)
        harvest = self.harvest(client)
        context = self.context(adapter, harvest, client)
        self.assertEqual(len(adapter.single_items(context, "copy")), 4)
        repeated = adapter.repeated_items(context, "copy")
        self.assertEqual(sum(item.get("type") == "reasoning" for item in repeated), 2)
        continuation = adapter.continuation_items(context, "copy", "tail")
        self.assertEqual(continuation[2]["content"][0]["text"], "OK")

    def test_responses_replay_preserves_reasoning_item_structure(self) -> None:
        payload = {
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_test",
                    "summary": [],
                    "content": [],
                    "encrypted_content": "opaque-value",
                },
                {"type": "message", "content": [{"text": "OK"}]},
            ]
        }
        client = FakeClient([payload])
        adapter = OpenAIResponsesAdapter(client)
        harvest = self.harvest(client)
        # Replace the helper's minimal envelope with a strict Responses item.
        harvest = harvest.__class__(
            **{**harvest.__dict__, "envelope": find_envelopes(payload)[0]}
        )
        context = self.context(adapter, harvest, client)
        reasoning = adapter.single_items(context, "copy")[1]
        self.assertEqual(reasoning["id"], "rs_test")
        self.assertEqual(reasoning["summary"], [])
        self.assertEqual(reasoning["content"], [])
        self.assertEqual(reasoning["encrypted_content"], "opaque-value")

    def test_chat_completions_adapter_builds_developer_source_and_replay(self) -> None:
        client = FakeClient([chat_source_payload(), {"choices": [{"message": {"content": "decoded"}}]}])
        settings = Settings(**{**self.settings.__dict__, "protocol": "chat_completions"})
        adapter = OpenAIChatCompletionsAdapter(client)
        harvest = adapter.harvest(settings, "solve", "LAB-MARKER-ABC")
        self.assertEqual(harvest.envelope.field, "thinkingSignature")
        source_body = client.requests[0][1]
        self.assertEqual(source_body["messages"][0]["role"], "developer")
        self.assertNotIn("LAB-MARKER-ABC", source_body["messages"][1]["content"])
        context = self.context(adapter, harvest, client)
        payload = adapter.replay(context, adapter.single_items(context, "copy"))
        self.assertEqual(payload["choices"][0]["message"]["content"], "decoded")
        self.assertEqual(client.requests[1][1]["model"], "gpt-5.6-luna")

    def test_claude_fuzzy_prefill_adapter_and_method(self) -> None:
        client = FakeClient([anthropic_source_payload(), {"content": [{"type": "text", "text": "decoded"}]}])
        settings = Settings(**{**self.settings.__dict__, "protocol": "anthropic_messages"})
        adapter = AnthropicMessagesAdapter(client)
        harvest = adapter.harvest(settings, "solve", "LAB-MARKER-ABC")
        context = self.context(adapter, harvest, client)
        result = ClaudeFuzzyExtractionMethod().run(context)
        self.assertEqual(result.text, "decoded")
        self.assertIn("reasoning attached", client.requests[1][1]["messages"][0]["content"])
        self.assertEqual(client.requests[1][1]["messages"][1]["content"][1]["text"], "<thinking-copy>")
        self.assertEqual(client.requests[1][1]["messages"][1]["content"][0]["signature"], "opaque-anthropic")

    def test_gemini_fuzzy_prefill_adapter_and_method(self) -> None:
        client = FakeClient(
            [
                gemini_source_payload(),
                {"candidates": [{"content": {"parts": [{"text": "decoded"}]}}]},
            ]
        )
        settings = Settings(**{**self.settings.__dict__, "protocol": "gemini"})
        adapter = GeminiGenerateContentAdapter(client)
        harvest = adapter.harvest(settings, "solve", "LAB-MARKER-ABC")
        self.assertEqual(client.requests[0][0], "models/gpt-5.6-sol:generateContent")
        self.assertEqual(
            client.requests[0][1]["generationConfig"]["thinkingConfig"]["thinkingLevel"],
            "high",
        )
        context = self.context(adapter, harvest, client)
        result = GeminiFuzzyExtractionMethod().run(context)
        self.assertEqual(result.text, "decoded")
        self.assertIn("Duplicate attached", client.requests[1][1]["contents"][0]["parts"][0]["text"])
        self.assertEqual(client.requests[1][0], "models/gpt-5.6-luna:generateContent")
        self.assertEqual(client.requests[1][1]["contents"][1]["parts"][0]["thought_signature"], "opaque-gemini")

    def test_gemini_camel_case_thought_signature_is_discoverable(self) -> None:
        payload = {
            "candidates": [{"content": {"parts": [{"thoughtSignature": "opaque"}]}}]
        }
        envelopes = find_envelopes(payload)
        self.assertEqual(envelopes[0].field, "thoughtSignature")

    def test_model_config_can_change_provider_prefill_tag(self) -> None:
        client = FakeClient([anthropic_source_payload(), {"content": [{"type": "text", "text": "decoded"}]}])
        settings = Settings(**{**self.settings.__dict__, "protocol": "anthropic_messages"})
        adapter = AnthropicMessagesAdapter(client)
        harvest = adapter.harvest(settings, "solve", "LAB-MARKER-ABC")
        context = replace(
            self.context(adapter, harvest, client),
            model_config={"prefill_tag": "<custom-copy>"},
        )
        result = ClaudeFuzzyExtractionMethod().run(context)
        self.assertEqual(result.text, "decoded")
        self.assertEqual(client.requests[1][1]["messages"][1]["content"][1]["text"], "<custom-copy>")

    def test_provider_reconciliation_methods_use_native_output_parsers(self) -> None:
        client = FakeClient(
            [
                anthropic_source_payload(),
                {"content": [{"type": "text", "text": "a"}]},
                {"content": [{"type": "text", "text": "b"}]},
                {"content": [{"type": "text", "text": "c"}]},
                {"content": [{"type": "text", "text": "merged"}]},
            ]
        )
        settings = Settings(**{**self.settings.__dict__, "protocol": "anthropic_messages"})
        adapter = AnthropicMessagesAdapter(client)
        harvest = adapter.harvest(settings, "solve", "LAB-MARKER-ABC")
        context = self.context(adapter, harvest, client)
        result = ClaudeReconciliationMethod().run(context)
        self.assertEqual(result.text, "merged")
        self.assertFalse(result.metadata["provenance_safe"])
        final_messages = client.requests[-1][1]["messages"]
        self.assertEqual([item["role"] for item in final_messages], ["user", "assistant", "user"])
        self.assertEqual(final_messages[1]["content"][0]["signature"], "opaque-anthropic")
        self.assertIn("Some noisy", final_messages[1]["content"][1]["text"])
        self.assertIn("a", final_messages[1]["content"][1]["text"])
        self.assertIn("thinking-copy", final_messages[2]["content"])

        gemini_client = FakeClient(
            [
                gemini_source_payload(),
                {"candidates": [{"content": {"parts": [{"text": "a"}]}}]},
                {"candidates": [{"content": {"parts": [{"text": "b"}]}}]},
                {"candidates": [{"content": {"parts": [{"text": "c"}]}}]},
                {"candidates": [{"content": {"parts": [{"text": "merged"}]}}]},
            ]
        )
        gemini_settings = Settings(**{**self.settings.__dict__, "protocol": "gemini"})
        gemini_adapter = GeminiGenerateContentAdapter(gemini_client)
        gemini_harvest = gemini_adapter.harvest(gemini_settings, "solve", "LAB-MARKER-ABC")
        gemini_context = self.context(gemini_adapter, gemini_harvest, gemini_client)
        gemini = GeminiReconciliationMethod(candidate_pool=3, selection_count=2)
        gemini_result = gemini.run(gemini_context)
        self.assertEqual(gemini_result.text, "merged")
        final_contents = gemini_client.requests[-1][1]["contents"]
        self.assertEqual([item["role"] for item in final_contents], ["user", "model", "user", "model"])
        self.assertIn("CANDIDATE", final_contents[1]["parts"][0]["text"])
        self.assertEqual(final_contents[1]["parts"][0]["thought_signature"], "opaque-gemini")
        self.assertEqual(final_contents[3]["parts"][0]["text"], "<reconciliation>")
        self.assertEqual(final_contents[3]["parts"][0]["thought_signature"], "opaque-gemini")
        self.assertFalse(gemini_result.metadata["provenance_safe"])

        gemini_default = GeminiReconciliationMethod()
        self.assertEqual(gemini_default.name, "gemini.reconciliation")

    def test_single_and_repeated_methods_return_decoder_text(self) -> None:
        client = FakeClient([source_payload(), message_payload("single"), message_payload("repeat")])
        adapter = OpenAIResponsesAdapter(client)
        harvest = self.harvest(client)
        context = self.context(adapter, harvest, client)
        single = SingleReplayMethod().run(context)
        repeated = RepeatedInjectionMethod().run(context)
        self.assertEqual(single.text, "single")
        self.assertEqual(repeated.text, "repeat")
        self.assertEqual(repeated.metadata["injection_count"], 2)

    def test_chunk_method_stitches_word_overlap(self) -> None:
        client = FakeClient(
            [source_payload(), message_payload("alpha beta gamma"), message_payload("gamma delta epsilon")]
        )
        adapter = OpenAIResponsesAdapter(client)
        harvest = self.harvest(client)
        context = self.context(adapter, harvest, client)
        result = ChunkContinuationMethod().run(context)
        self.assertEqual(result.text, "alpha beta gamma\ndelta epsilon")
        self.assertEqual(result.metadata["chunks"], 2)

    def test_best_of_n_prefers_marker_candidate(self) -> None:
        client = FakeClient(
            [
                source_payload(),
                message_payload("short"),
                message_payload("LAB-MARKER-ABC longer candidate"),
            ]
        )
        adapter = OpenAIResponsesAdapter(client)
        harvest = self.harvest(client)
        context = self.context(adapter, harvest, client)
        result = BestOfNMethod(SingleReplayMethod(), n=2).run(context)
        self.assertIn("LAB-MARKER-ABC", result.text)
        self.assertEqual(result.metadata["candidate_count"], 2)

    def test_best_of_n_isolates_candidate_errors(self) -> None:
        client = FakeClient([source_payload()])
        adapter = OpenAIResponsesAdapter(client)
        harvest = self.harvest(client)
        method = BestOfNMethod(FlakyMethod(), n=2, name="flaky.best_of_2")
        context = self.context(adapter, harvest, client)
        result = method.run(context)
        self.assertEqual(result.text, "candidate")
        self.assertEqual(result.metadata["candidate_errors"], ["FAKE_CANDIDATE_ERROR"])
        with self.assertRaises(ProbeError) as raised:
            BestOfNMethod(FlakyMethod(failures=2), n=2).run(context)
        self.assertEqual(raised.exception.code, "BEST_OF_N_EXHAUSTED")

    def test_terra_fallback_changes_decoder_only_after_empty_primary(self) -> None:
        client = FakeClient([source_payload(), message_payload(""), message_payload("terra")])
        adapter = OpenAIResponsesAdapter(client)
        harvest = self.harvest(client)
        context = self.context(adapter, harvest, client)
        result = TerraFallbackMethod(
            SingleReplayMethod(), SingleReplayMethod(), "gpt-5.6-terra"
        ).run(context)
        self.assertEqual(result.text, "terra")
        self.assertTrue(result.metadata["fallback_used"])
        self.assertEqual(client.requests[-1][1]["model"], "gpt-5.6-terra")

    def test_reconciliation_marks_provenance_as_contaminated(self) -> None:
        client = FakeClient(
            [
                source_payload(),
                message_payload("candidate one"),
                message_payload("candidate two"),
                message_payload("candidate three"),
                message_payload("reconciled"),
            ]
        )
        adapter = OpenAIResponsesAdapter(client)
        harvest = self.harvest(client)
        context = self.context(adapter, harvest, client)
        result = ReconciliationMethod(SingleReplayMethod(), "gpt-5.6-sol").run(context)
        self.assertEqual(result.text, "reconciled")
        self.assertFalse(result.metadata["provenance_safe"])
        self.assertIn("candidate one", str(client.requests[-1][1]))

    def test_validators_expose_four_independent_dimensions(self) -> None:
        client = FakeClient([source_payload()])
        harvest = self.harvest(client)
        provenance = validate_provenance(
            harvest, "prefix LAB-MARKER-ABC suffix", baseline_text="no marker"
        )
        coverage = validate_coverage(harvest, "one two three four five")
        fidelity = validate_fidelity(harvest, "same text", candidate_texts=["same text"])
        self.assertEqual(provenance.status, "supported")
        self.assertAlmostEqual(coverage.evidence["ratio"], 0.5)
        self.assertEqual(fidelity.status, "unknown")

    def test_engine_falls_back_and_returns_four_fields(self) -> None:
        client = FakeClient([source_payload(), message_payload(""), message_payload("LAB-MARKER-ABC")])
        adapter = OpenAIResponsesAdapter(client)
        engine = RecoveryEngine(
            adapter,
            {"single": SingleReplayMethod(), "repeat": RepeatedInjectionMethod()},
        )
        with patch("reasoning_recovery.engine.new_marker", return_value="LAB-MARKER-ABC"):
            result = engine.recover(
                self.settings,
                "solve",
                method="single",
                fallback=("repeat",),
                baseline_text="no marker",
            )
        payload = result.as_dict()
        self.assertEqual(payload["replay"]["status"], "success")
        self.assertEqual(payload["provenance"]["status"], "supported")
        self.assertIn("coverage", payload)
        self.assertIn("fidelity", payload)
        self.assertEqual(len(payload["attempts"]), 2)

    def test_engine_reports_method_exhaustion_without_hiding_attempts(self) -> None:
        client = FakeClient([source_payload(), message_payload(""), message_payload("")])
        adapter = OpenAIResponsesAdapter(client)
        engine = RecoveryEngine(adapter, {"single": SingleReplayMethod()})
        result = engine.recover(
            self.settings,
            "solve",
            method="single",
            fallback=("missing",),
        )
        self.assertEqual(result.metadata["terminal_error"], "RECOVERY_METHODS_EXHAUSTED")
        self.assertEqual(len(result.attempts), 2)

    def test_find_envelopes_does_not_print_or_mutate_payload(self) -> None:
        payload = {"nested": [{"signature": "opaque"}]}
        found = find_envelopes(payload)
        self.assertEqual(found[0].path, "$.nested[0].signature")
        self.assertEqual(payload["nested"][0]["signature"], "opaque")

    def test_cli_registry_exposes_all_research_methods(self) -> None:
        names = set(method_registry())
        self.assertTrue({
            "gpt.single_replay",
            "gpt.repeated_injection",
            "gpt.chunk_continuation",
            "gpt.single_best_of_n",
            "gpt.repeated_best_of_n",
            "gpt.luna_then_terra",
            "gpt.reconcile_with_terra",
            "claude.fuzzy_prefill",
            "claude.reconciliation",
            "gemini.fuzzy_prefill",
            "gemini.reconciliation",
        } <= names)

    def test_http_client_supports_custom_headers_and_auth_modes(self) -> None:
        from reasoning_recovery.protocol import UrllibJsonClient

        bearer = UrllibJsonClient(
            "https://example.test/v1",
            "secret",
            headers={"X-Title": "sauna", "HTTP-Referer": "https://example.test"},
        )
        headers = bearer._build_headers()
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(headers["X-Title"], "sauna")
        self.assertEqual(headers["HTTP-Referer"], "https://example.test")

        xkey = UrllibJsonClient(
            "https://example.test/v1",
            "secret",
            auth="x-api-key",
            headers={"anthropic-version": "2023-06-01"},
        )
        xheaders = xkey._build_headers()
        self.assertEqual(xheaders["x-api-key"], "secret")
        self.assertNotIn("Authorization", xheaders)
        self.assertEqual(xheaders["anthropic-version"], "2023-06-01")

        custom = UrllibJsonClient(
            "https://example.test/v1",
            "secret",
            auth="header",
            auth_header="X-Api-Key",
            auth_prefix="",
        )
        self.assertEqual(custom._build_headers()["X-Api-Key"], "secret")

    def test_model_skeleton_resolves_methods_and_fallbacks(self) -> None:
        import tempfile
        from pathlib import Path

        from reasoning_recovery.config import list_runnable_methods, load_app_config, resolve_method_run
        from reasoning_recovery.errors import ProbeError

        # 只配 sol + terra：无 luna → luna_then_terra 应 unresolved 并 fallback 到 single_replay
        text = """
upstream:
  base_url: https://gateway.example/v1
  api_key: cfg-key
  headers:
    X-Title: sauna
runtime:
  effort: high
models:
  sol:
    family: gpt
    id: gpt-5.6-sol
    protocol: responses
    roles: [source]
  terra:
    family: gpt
    id: gpt-5.6-terra
    protocol: responses
    roles: [decoder, reconciler]
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(text, encoding="utf-8")
            app = load_app_config(path, require=True)
            self.assertEqual(app.families(), {"gpt"})

            with self.assertRaises(ProbeError) as raised:
                resolve_method_run(app, method="claude.fuzzy_prefill")
            self.assertIn(raised.exception.code, {"FAMILY_NOT_CONFIGURED", "METHOD_UNRESOLVED"})

            run = resolve_method_run(app, method="gpt.single_replay")
            self.assertEqual(run.role_names["source"], "sol")
            self.assertEqual(run.role_names["decoder"], "terra")
            self.assertEqual(run.settings.source_model, "gpt-5.6-sol")
            self.assertEqual(run.settings.decoder_model, "gpt-5.6-terra")
            self.assertEqual(run.settings.extra_headers["X-Title"], "sauna")

            run2 = resolve_method_run(app, method="gpt.luna_then_terra")
            self.assertEqual(run2.method, "gpt.single_replay")
            self.assertTrue(any("unresolved" in line for line in run2.resolution_log))

            runnable = list_runnable_methods(app)
            self.assertIn("gpt.single_replay", runnable)
            self.assertNotIn("gpt.luna_then_terra", runnable)
            self.assertNotIn("claude.fuzzy_prefill", runnable)

        text2 = text + """
  luna:
    family: gpt
    id: gpt-5.6-luna
    protocol: responses
    roles: [decoder]
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(text2, encoding="utf-8")
            app = load_app_config(path, require=True)
            run = resolve_method_run(app, method="gpt.luna_then_terra")
            self.assertEqual(run.method, "gpt.luna_then_terra")
            self.assertEqual(run.role_names["decoder"], "luna")
            self.assertEqual(run.role_names["fallback_decoder"], "terra")
            self.assertEqual(run.role_ids["fallback_decoder"], "gpt-5.6-terra")

    def test_targets_section_pins_source_and_method_chain(self) -> None:
        """targets 段：source 固定为目标模型，方法链按目标定制。"""
        import tempfile
        from pathlib import Path

        from reasoning_recovery.config import load_app_config, resolve_method_run

        text = """upstream:
  base_url: https://example.test/v1
  api_key: sk-test
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
    roles: [decoder]
targets:
  sol:
    decoder: [terra]
    methods: [gpt.single_replay]
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(text, encoding="utf-8")
            app = load_app_config(path, require=True)
            run = resolve_method_run(app, target="sol")
            self.assertEqual(run.method, "gpt.single_replay")
            self.assertEqual(run.role_names["source"], "sol")
            self.assertEqual(run.role_names["decoder"], "terra")


class EnvelopeInspectTests(unittest.TestCase):
    """envelope 只读取证：protobuf 头部与熵。"""

    def _varint(self, value: int) -> bytes:
        out = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                out.append(byte | 0x80)
            else:
                out.append(byte)
                return bytes(out)

    def _field(self, num: int, wire: int, payload: bytes) -> bytes:
        """构造字段；wire 2 需自行带长度前缀。"""
        return self._varint((num << 3) | wire) + payload

    def _blob(self, payload: bytes) -> bytes:
        """wire 2 载荷：varint 长度 + 内容。"""
        return self._varint(len(payload)) + payload

    def _synthetic_envelope(self) -> str:
        """按实测结构构造：外层 #1=2, #2=inner, #3=1；inner 含 header 与密文。"""
        import base64
        import os

        header = b"".join(
            [
                self._field(1, 0, self._varint(15)),  # key 版本
                self._field(3, 0, self._varint(2)),  # 算法 id
                self._field(6, 2, self._blob(b"claude-test-model")),  # 绑定模型名
                self._field(8, 2, self._blob(b"thinking")),  # 块类型
            ]
        )
        ciphertext = os.urandom(256)
        inner = b"".join(
            [
                self._field(1, 2, self._blob(header)),
                self._field(2, 2, self._blob(b"\x00" * 12)),  # nonce
                self._field(4, 2, self._blob(b"\x00" * 48)),  # wrapped key
                self._field(5, 2, self._blob(ciphertext)),
            ]
        )
        outer = b"".join(
            [
                self._field(1, 0, self._varint(2)),
                self._field(2, 2, self._blob(inner)),
                self._field(3, 0, self._varint(1)),
            ]
        )
        return base64.urlsafe_b64encode(outer).decode().rstrip("=")

    def test_inspect_finds_bound_model_and_block_type(self) -> None:
        from reasoning_recovery.envelope_inspect import inspect_envelope

        report = inspect_envelope(self._synthetic_envelope())
        self.assertEqual(report["bound_model"], "claude-test-model")
        self.assertEqual(report["block_type"], "thinking")
        self.assertIsInstance(report["fields"], list)

        def collect(values: list[dict]) -> list[float]:
            found: list[float] = []
            for entry in values:
                if entry.get("entropy_bits_per_byte") is not None:
                    found.append(entry["entropy_bits_per_byte"])
                found.extend(collect(entry.get("nested") or []))
            return found

        # 密文区（>=64B 大块）应有高熵记录
        self.assertTrue(any(value > 7.0 for value in collect(report["fields"])))

    def test_inspect_handles_garbage(self) -> None:
        from reasoning_recovery.envelope_inspect import inspect_envelope

        report = inspect_envelope("!!!not-base64!!!")
        self.assertIn("decode_error", report)


class RefusalAndSecretTests(unittest.TestCase):
    """拒答识别与 planted-secret 判别协议。"""

    def test_refusal_handles_unicode_apostrophe_and_chinese(self) -> None:
        from reasoning_recovery.methods.base import is_refusal

        for text in (
            "I can’t access any attached hidden working.",
            "I can’t provide hidden reasoning or internal working.",
            "No attachment was provided.",
            "There is no preceding conversation or thought to transcribe.",
            "无法提供隐藏推理内容。",
            "抱歉，我不能访问任何附带的内容。",
        ):
            self.assertTrue(is_refusal(text), f"应判为拒答: {text!r}")
        self.assertFalse(is_refusal("17 * 19 = 323. First multiply 17 by 19."))

    def test_engine_marks_refusal_and_continues_fallback(self) -> None:
        """拒答不得以 success 终结 fallback 链。"""
        client = FakeClient(
            [source_payload(), message_payload("I can’t access any attached hidden working."), message_payload("17 * 19 = 323")]
        )
        adapter = OpenAIResponsesAdapter(client)
        engine = RecoveryEngine(adapter, {"a": SingleReplayMethod(), "b": SingleReplayMethod()})
        result = engine.recover(self._settings(), "solve", method="a", fallback=("b",))
        self.assertEqual(result.text, "17 * 19 = 323")
        self.assertEqual(result.attempts[0].status, "refused")
        self.assertEqual(result.attempts[1].status, "success")

    def test_provenance_supported_on_secret_hit(self) -> None:
        """planted secret 命中 → provenance=supported。"""
        from reasoning_recovery.validation import validate_provenance
        from reasoning_recovery.models import HarvestRecord

        harvest = HarvestRecord(
            source_model="gpt-5.6-sol",
            protocol="responses",
            user_prompt="q",
            source_prompt="q",
            marker="LAB-MARKER-ABC",
            secret="COBALT-AB12-VIOLET-42",
            visible_answer="OK",
            envelope=None,  # type: ignore[arg-type]
            source_payload={},
        )
        result = validate_provenance(harvest, "we used COBALT-AB12-VIOLET-42 here")
        self.assertEqual(result.status, "supported")
        self.assertTrue(result.evidence["secret_hit"])

    def _settings(self) -> Settings:
        return Settings(
            base_url="https://example.test/v1",
            api_key="sk-test",
            source_model="gpt-5.6-sol",
            decoder_model="gpt-5.6-luna",
        )


if __name__ == "__main__":
    unittest.main()
