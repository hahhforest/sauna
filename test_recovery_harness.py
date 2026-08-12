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
            "gpt.single_best_of_3",
            "gpt.repeated_best_of_3",
            "gpt.luna_then_terra",
            "gpt.reconcile_with_terra",
            "claude.fuzzy_prefill",
            "claude.reconciliation",
            "gemini.fuzzy_prefill",
            "gemini.reconciliation",
        } <= names)


if __name__ == "__main__":
    unittest.main()
