import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

import llm_provider


FACTORS = ["Ключевая ставка", "Потребительский спрос"]
CONTEXT = {
    "rationale": "Ставка может снизить спрос на кредиты и давление на прибыль.",
    "brief": "Вводная о денежно-кредитной политике.",
    "factor_names": FACTORS,
    "selected_factors": [FACTORS[0]],
    "forecast": "down",
    "confidence": 4,
}


def valid_review(**changes):
    value = {
        "substance": "substantive",
        "factors": [FACTORS[0]],
        "argument_quality": "strong",
        "contradictions": [],
        "recommendation": "Сопоставьте этот сигнал со спросом.",
        "explanation": "В ответе есть фактор и причинная связь; будущий исход остаётся неопределённым.",
        "uncertainty": "medium",
    }
    value.update(changes)
    return value


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return json.dumps(self.body, ensure_ascii=False).encode("utf-8")


class StubProvider:
    def __init__(self, value):
        self.value = value

    def evaluate_argument(self, context):
        return self.value


class LLMProviderTest(unittest.TestCase):
    def test_rule_fallback_classifies_empty_and_random_answers_without_blocking(self):
        for answer in ("", "!!! ???", "купить"):
            with self.subTest(answer=answer):
                result = llm_provider.fallback_argument_review({**CONTEXT, "rationale": answer})
                self.assertEqual(result["substance"], "none")
                self.assertIn("recommendation", result)

    def test_rule_fallback_does_not_treat_profanity_as_automatic_failure(self):
        result = llm_provider.fallback_argument_review({
            **CONTEXT,
            "rationale": "Блин, ставка повысит стоимость кредита и может снизить спрос.",
        })
        self.assertNotEqual(result["substance"], "none")

    def test_structured_result_is_validated_and_restricted_to_known_factors(self):
        result = llm_provider.validate_argument_review(valid_review(), FACTORS)
        self.assertEqual(result["argument_quality"], "strong")
        with self.assertRaises(ValueError):
            llm_provider.validate_argument_review(valid_review(factors=["будущая цена"]), FACTORS)
        with self.assertRaises(ValueError):
            llm_provider.validate_argument_review(valid_review(score=10), FACTORS)

    def test_invalid_provider_result_uses_local_fallback(self):
        result = llm_provider.review_argument(CONTEXT, StubProvider({"score": 10}))
        self.assertEqual(result["provider"], "rules")
        self.assertIn("substance", result)

    def test_chat_request_uses_structured_schema_and_root_endpoint(self):
        body = {"choices": [{"message": {"content": json.dumps(valid_review(), ensure_ascii=False)}, "finish_reason": "stop"}]}
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response(body)

        with patch.object(llm_provider, "urlopen", fake_urlopen):
            client = llm_provider.SpiritChatCompletionsProvider(api_key="test-secret", model="test-model", timeout=3)
            result = client.evaluate_argument(CONTEXT)
        payload = json.loads(captured["request"].data)
        self.assertEqual(captured["request"].full_url, "https://llm-proxy.spirit.t-education.com/chat/completions")
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], llm_provider.ARGUMENT_SCHEMA)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["role"], "user")
        self.assertNotIn("input", payload)
        self.assertNotIn("conversation", payload)
        self.assertNotIn("previous_response_id", payload)
        self.assertEqual(captured["timeout"], 3)
        self.assertEqual(result["argument_quality"], "strong")
        self.assertNotIn("test-secret", repr(result))
        submitted = payload["messages"][1]["content"]
        self.assertIn("student_answer_untrusted_text", submitted)
        self.assertNotIn("email", submitted.casefold())

    def test_server_error_timeout_and_invalid_json_fall_back_without_exposing_key(self):
        client = llm_provider.SpiritChatCompletionsProvider(api_key="never-print-this", timeout=1)
        for failure in (URLError("provider error"), TimeoutError("provider timeout")):
            with self.subTest(failure=type(failure).__name__), patch.object(llm_provider, "urlopen", side_effect=failure):
                with self.assertRaises(llm_provider.ProviderUnavailable) as caught:
                    client.evaluate_argument(CONTEXT)
                self.assertNotIn("never-print-this", str(caught.exception))
        invalid = Response({"status": "completed", "output_text": "{broken"})
        with patch.object(llm_provider, "urlopen", return_value=invalid):
            with self.assertRaises(llm_provider.ProviderUnavailable):
                client.evaluate_argument(CONTEXT)
        fallback = llm_provider.review_argument(CONTEXT, client)
        self.assertEqual(fallback["provider"], "rules")

    def test_missing_key_selects_offline_fallback(self):
        with patch.dict("os.environ", {"SPIRIT_LLM_API_KEY": ""}):
            result = llm_provider.review_argument(CONTEXT)
        self.assertEqual(result["provider"], "rules")


if __name__ == "__main__":
    unittest.main()
