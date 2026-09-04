from __future__ import annotations

import copy
import http.client
import json
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from aios.config import ModelConfig
from aios.controller import ControllerError, LLMController
from aios.lineage import ModelLineageReasoner
from aios.types import Intent


def response(content="done", *, finish="stop", tool_calls=None, reasoning=None, usage=None):
    value = {
        "choices": [{"finish_reason": finish, "message": {
            "content": content, "tool_calls": tool_calls, "reasoning_content": reasoning,
        }}],
        "usage": usage,
    }
    result = MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = json.dumps(value).encode()
    return result


class ModelRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.controller = LLMController(ModelConfig(
            provider="deepseek", model="deepseek-v4-flash", thinking="enabled",
            api_key_env="AIOS_RECOVERY_TEST_KEY", max_tokens=31296,
        ))
        self.request = {
            "model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "task"}],
            "max_tokens": 31296, "thinking": {"type": "enabled"},
        }
        env = patch.dict(os.environ, {"AIOS_RECOVERY_TEST_KEY": "test-only"})
        env.start()
        self.addCleanup(env.stop)
        sleep = patch("aios.controller.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def test_interrupted_read_retries_same_request_and_counts_calls(self):
        broken = response()
        broken.read.side_effect = http.client.IncompleteRead(b" ")
        before = copy.deepcopy(self.request)
        with patch("urllib.request.urlopen", side_effect=[broken, response()]) as send:
            result = self.controller._request_with_recovery(self.request, "test-only")
        self.assertEqual(send.call_count, 2)
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.assertEqual(self.request, before)
        self.assertEqual(send.call_args_list[0].args[0].data, send.call_args_list[1].args[0].data)
        broken.__exit__.assert_called_once()

    def test_transport_exhaustion_is_bounded_and_sanitized(self):
        for error in (TimeoutError("secret payload"), ConnectionResetError("secret payload"),
                      http.client.IncompleteRead(b"secret payload")):
            with self.subTest(error=type(error).__name__):
                with patch("urllib.request.urlopen", side_effect=error) as send:
                    with self.assertRaises(ControllerError) as raised:
                        self.controller._request_with_recovery(self.request, "test-only")
                self.assertEqual(send.call_count, 3)
                self.assertEqual(raised.exception.model_usage["model_calls"], 3)
                self.assertNotIn("secret payload", str(raised.exception))

    def test_auth_and_invalid_requests_are_not_retried(self):
        for code in (400, 401, 403):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://test.invalid", code, "secret", {}, None)
                with patch("urllib.request.urlopen", side_effect=error) as send:
                    with self.assertRaisesRegex(ControllerError, f"HTTP {code}"):
                        self.controller._request_with_recovery(self.request, "test-only")
                self.assertEqual(send.call_count, 1)

    def test_temporary_http_failure_is_retried(self):
        for code in (429, 502, 503):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://test.invalid", code, "busy", {}, None)
                with patch("urllib.request.urlopen", side_effect=[error, response()]) as send:
                    self.controller._request_with_recovery(self.request, "test-only")
                self.assertEqual(send.call_count, 2)

    def test_truncated_json_body_is_discarded(self):
        broken = response()
        broken.read.return_value = b'{"choices":'
        with patch("urllib.request.urlopen", side_effect=[broken, response()]) as send:
            result = self.controller._request_with_recovery(self.request, "test-only")
        self.assertEqual(result["choices"][0]["message"]["content"], "done")
        self.assertEqual(send.call_count, 2)

    def test_lineage_empty_stop_recovers_without_reusing_reasoning(self):
        first = response("", reasoning="private reasoning", usage={"total_tokens": 100})
        second = response('{"action":"CONTINUE","reason":"wait"}', usage={"total_tokens": 20})
        with patch("urllib.request.urlopen", side_effect=[first, second]) as send:
            decision = ModelLineageReasoner(self.controller).reason({"schema": "test"})
        self.assertEqual(decision["action"], "CONTINUE")
        self.assertEqual(decision["model_usage"]["model_calls"], 2)
        self.assertEqual(decision["model_usage"]["total_tokens"], 120)
        retry = json.loads(send.call_args_list[1].args[0].data)
        self.assertEqual(retry["thinking"], {"type": "enabled"})
        self.assertEqual(retry["max_tokens"], 31296)
        self.assertIn("final JSON", retry["messages"][-1]["content"])
        self.assertNotIn("private reasoning", json.dumps(retry))

    def test_repeated_empty_answer_stays_protocol_failure_and_counts_usage(self):
        blank = response("", finish="length", reasoning="secret reasoning",
                         usage={"total_tokens": 10})
        with patch("urllib.request.urlopen", return_value=blank) as send:
            with self.assertRaisesRegex(ControllerError, "finish_reason=length") as raised:
                self.controller.plan(Intent("test", "test", None, []), [])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(raised.exception.model_usage, {"model_calls": 2, "total_tokens": 20})
        self.assertNotIn("secret reasoning", str(raised.exception))

    def test_native_tool_call_is_not_mistaken_for_empty_answer(self):
        calls = [{"id": "call1", "type": "function", "function": {
            "name": "read", "arguments": '{"path":"example.txt"}',
        }}]
        with patch("urllib.request.urlopen", return_value=response(
            None, finish="tool_calls", tool_calls=calls,
        )) as send:
            plan = self.controller.plan(Intent("test", "test", None, []), [])
        self.assertEqual(send.call_count, 1)
        self.assertFalse(plan.done)
        self.assertEqual(plan.actions[0].tool, "read")

    def test_no_recovery_when_call_budget_is_exhausted(self):
        with patch("urllib.request.urlopen", return_value=response("", finish="length")) as send:
            with self.assertRaises(ControllerError):
                self.controller.plan(Intent("test", "test", None, []), [], {
                    "budget": {"enabled": True, "remaining_model_calls_after_this": 0},
                })
        self.assertEqual(send.call_count, 1)

    def test_empty_then_timeout_then_success_uses_one_global_attempt_limit(self):
        before = copy.deepcopy(self.request)
        with patch("urllib.request.urlopen", side_effect=[
            response("", finish="length"), TimeoutError(), response(),
        ]) as send:
            result = self.controller._request_with_recovery(self.request, "test-only")
        self.assertEqual(send.call_count, 3)
        self.assertEqual(result["usage"]["model_calls"], 3)
        self.assertEqual(self.request, before)
        retry = json.loads(send.call_args_list[-1].args[0].data)
        self.assertEqual(len(retry["messages"]), 2)

    def test_content_filter_does_not_trigger_empty_response_recovery(self):
        with patch("urllib.request.urlopen", return_value=response("", finish="content_filter")) as send:
            self.controller._request_with_recovery(self.request, "test-only")
        self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
