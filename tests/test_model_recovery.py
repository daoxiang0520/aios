from __future__ import annotations

import copy
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import requests

from aios.config import ModelConfig
from aios.controller import ControllerError, LLMController
from aios.lineage import ModelLineageReasoner
from aios.types import Intent


def response(content="done", *, finish="stop", tool_calls=None, reasoning=None, usage=None,
             status=200):
    value = {
        "choices": [{"finish_reason": finish, "message": {
            "content": content, "tool_calls": tool_calls, "reasoning_content": reasoning,
        }}],
        "usage": usage,
    }
    result = MagicMock()
    result.__enter__.return_value = result
    result.status_code = status
    result.json.return_value = value
    delta = {"role": "assistant", "content": content}
    if tool_calls is not None:
        delta["tool_calls"] = [dict(item, index=index) for index, item in enumerate(tool_calls)]
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    events = [
        {"choices": [{"finish_reason": finish, "delta": delta}]},
        {"choices": [], "usage": usage},
    ]
    result.iter_lines.return_value = iter([
        *(f"data: {json.dumps(event)}\n\n".encode() for event in events),
        b"data: [DONE]\n\n",
    ])
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
        broken.iter_lines.side_effect = requests.exceptions.ChunkedEncodingError("broken")
        before = copy.deepcopy(self.request)
        with patch.object(self.controller._session, "post", side_effect=[broken, response()]) as send:
            result = self.controller._request_with_recovery(self.request, "test-only")
        self.assertEqual(send.call_count, 2)
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.assertEqual(self.request, before)
        self.assertEqual(send.call_args_list[0].kwargs["data"], send.call_args_list[1].kwargs["data"])
        broken.__exit__.assert_called_once()

    def test_transport_exhaustion_is_bounded_and_sanitized(self):
        for error in (requests.exceptions.Timeout("secret payload"),
                      requests.exceptions.ConnectionError("secret payload"),
                      requests.exceptions.ChunkedEncodingError("secret payload")):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.controller._session, "post", side_effect=error) as send:
                    with self.assertRaises(ControllerError) as raised:
                        self.controller._request_with_recovery(self.request, "test-only")
                self.assertEqual(send.call_count, 3)
                self.assertEqual(raised.exception.model_usage["model_calls"], 3)
                self.assertNotIn("secret payload", str(raised.exception))

    def test_auth_and_invalid_requests_are_not_retried(self):
        for code in (400, 401, 403):
            with self.subTest(code=code):
                with patch.object(
                    self.controller._session, "post", return_value=response(status=code),
                ) as send:
                    with self.assertRaisesRegex(ControllerError, f"HTTP {code}"):
                        self.controller._request_with_recovery(self.request, "test-only")
                self.assertEqual(send.call_count, 1)

    def test_temporary_http_failure_is_retried(self):
        for code in (429, 502, 503):
            with self.subTest(code=code):
                with patch.object(
                    self.controller._session, "post",
                    side_effect=[response(status=code), response()],
                ) as send:
                    self.controller._request_with_recovery(self.request, "test-only")
                self.assertEqual(send.call_count, 2)

    def test_truncated_json_body_is_discarded(self):
        broken = response()
        broken.iter_lines.return_value = iter([b'data: {"choices":\n'])
        with patch.object(self.controller._session, "post", side_effect=[broken, response()]) as send:
            result = self.controller._request_with_recovery(self.request, "test-only")
        self.assertEqual(result["choices"][0]["message"]["content"], "done")
        self.assertEqual(send.call_count, 2)

    def test_lineage_empty_stop_recovers_without_reusing_reasoning(self):
        first = response("", reasoning="private reasoning", usage={"total_tokens": 100})
        second = response('{"action":"CONTINUE","reason":"wait"}', usage={"total_tokens": 20})
        with patch.object(self.controller._session, "post", side_effect=[first, second]) as send:
            decision = ModelLineageReasoner(self.controller).reason({"schema": "test"})
        self.assertEqual(decision["action"], "CONTINUE")
        self.assertEqual(decision["model_usage"]["model_calls"], 2)
        self.assertEqual(decision["model_usage"]["total_tokens"], 120)
        retry = json.loads(send.call_args_list[1].kwargs["data"])
        self.assertEqual(retry["thinking"], {"type": "enabled"})
        self.assertEqual(retry["max_tokens"], 31296)
        self.assertIn("final JSON", retry["messages"][-1]["content"])
        self.assertNotIn("private reasoning", json.dumps(retry))

    def test_repeated_empty_answer_stays_protocol_failure_and_counts_usage(self):
        blanks = [response("", finish="length", reasoning="secret reasoning",
                           usage={"total_tokens": 10}) for _ in range(2)]
        with patch.object(self.controller._session, "post", side_effect=blanks) as send:
            with self.assertRaisesRegex(ControllerError, "finish_reason=length") as raised:
                self.controller.plan(Intent("test", "test", None, []), [])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(raised.exception.model_usage, {"model_calls": 2, "total_tokens": 20})
        self.assertNotIn("secret reasoning", str(raised.exception))

    def test_length_exhaustion_recovery_keeps_reasoning_but_requests_action_without_thinking(self):
        calls = [{"id": "call1", "type": "function", "function": {
            "name": "read", "arguments": '{"path":"example.txt"}',
        }}]
        with patch.object(self.controller._session, "post", side_effect=[
            response("", finish="length", reasoning="long private analysis",
                     usage={"total_tokens": 100}),
            response(None, finish="tool_calls", tool_calls=calls,
                     usage={"total_tokens": 20}),
        ]) as send:
            plan = self.controller.plan(Intent("test", "test", None, []), [])

        self.assertEqual(send.call_count, 2)
        self.assertEqual(plan.actions[0].tool, "read")
        self.assertEqual(plan.reasoning, "long private analysis")
        self.assertEqual(plan.model_usage["total_tokens"], 120)
        retry = json.loads(send.call_args_list[1].kwargs["data"])
        self.assertEqual(retry["thinking"], {"type": "disabled"})
        self.assertEqual(retry["max_tokens"], 2048)
        self.assertIn("native tool call", retry["messages"][-1]["content"])
        self.assertNotIn("long private analysis", json.dumps(retry))

    def test_native_tool_call_is_not_mistaken_for_empty_answer(self):
        calls = [{"id": "call1", "type": "function", "function": {
            "name": "read", "arguments": '{"path":"example.txt"}',
        }}]
        with patch.object(self.controller._session, "post", return_value=response(
            None, finish="tool_calls", tool_calls=calls,
            reasoning="The file must be inspected before answering.",
        )) as send:
            plan = self.controller.plan(Intent("test", "test", None, []), [])
        self.assertEqual(send.call_count, 1)
        self.assertFalse(plan.done)
        self.assertEqual(plan.actions[0].tool, "read")
        self.assertEqual(
            plan.reasoning, "The file must be inspected before answering.",
        )

    def test_final_answer_preserves_provider_reasoning_as_observable_output(self):
        with patch.object(self.controller._session, "post", return_value=response(
            "finished", reasoning="The requested evidence is sufficient.",
        )):
            plan = self.controller.plan(Intent("test", "test", None, []), [])
        self.assertTrue(plan.done)
        self.assertEqual(plan.reasoning, "The requested evidence is sufficient.")

    def test_malformed_native_tool_arguments_get_one_unexecuted_protocol_correction(self):
        bad = [{"id": "bad", "type": "function", "function": {
            "name": "bash", "arguments": '{"command":"unterminated',
        }}]
        good = [{"id": "good", "type": "function", "function": {
            "name": "bash", "arguments": json.dumps({"command": "echo ok"}),
        }}]
        with patch.object(self.controller._session, "post", side_effect=[
            response(None, finish="tool_calls", tool_calls=bad, usage={"total_tokens": 10}),
            response(None, finish="tool_calls", tool_calls=good, usage={"total_tokens": 20}),
        ]) as send:
            plan = self.controller.plan(Intent("test", "test", None, []), [])

        self.assertEqual(send.call_count, 2)
        self.assertEqual(plan.actions[0].tool, "bash")
        self.assertEqual(plan.actions[0].arguments, {"command": "echo ok"})
        self.assertEqual(plan.model_usage["model_calls"], 2)
        self.assertEqual(plan.model_usage["total_tokens"], 30)
        corrected_request = json.loads(send.call_args_list[1].kwargs["data"])
        self.assertIn("was not executed", corrected_request["messages"][-1]["content"])
        self.assertIn("valid JSON object", corrected_request["messages"][-1]["content"])
        self.assertFalse(any(
            item.get("tool_calls") == bad for item in corrected_request["messages"]
        ))
        self.assertEqual(corrected_request["thinking"], {"type": "disabled"})

    def test_repeated_malformed_native_tool_arguments_fail_after_one_correction(self):
        bad = [{"id": "bad", "type": "function", "function": {
            "name": "bash", "arguments": '{"command":"unterminated',
        }}]
        with patch.object(self.controller._session, "post", side_effect=[
            response(None, finish="tool_calls", tool_calls=bad, usage={"total_tokens": 10}),
            response(None, finish="tool_calls", tool_calls=bad, usage={"total_tokens": 20}),
        ]) as send:
            with self.assertRaisesRegex(
                ControllerError, "Native tool call arguments are not valid JSON",
            ) as raised:
                self.controller.plan(Intent("test", "test", None, []), [])

        self.assertEqual(send.call_count, 2)
        self.assertEqual(raised.exception.model_usage["model_calls"], 2)
        self.assertEqual(raised.exception.model_usage["total_tokens"], 30)

    def test_length_then_malformed_arguments_get_independent_recoveries(self):
        bad = [{"id": "bad", "type": "function", "function": {
            "name": "bash", "arguments": '{"command":"unterminated',
        }}]
        good = [{"id": "good", "type": "function", "function": {
            "name": "write", "arguments": json.dumps({
                "path": "result.txt", "content": "ok",
            }),
        }}]
        with patch.object(self.controller._session, "post", side_effect=[
            response("", finish="length", reasoning="long private analysis",
                     usage={"total_tokens": 100}),
            response(None, finish="tool_calls", tool_calls=bad,
                     usage={"total_tokens": 20}),
            response(None, finish="tool_calls", tool_calls=good,
                     usage={"total_tokens": 10}),
        ]) as send:
            plan = self.controller.plan(Intent("test", "test", None, []), [])

        self.assertEqual(send.call_count, 3)
        self.assertEqual(plan.actions[0].tool, "write")
        self.assertEqual(plan.actions[0].arguments, {
            "path": "result.txt", "content": "ok",
        })
        self.assertEqual(plan.reasoning, "long private analysis")
        self.assertEqual(plan.model_usage["model_calls"], 3)
        self.assertEqual(plan.model_usage["total_tokens"], 130)
        last_request = json.loads(send.call_args_list[-1].kwargs["data"])
        self.assertIn("valid JSON object", last_request["messages"][-1]["content"])
        self.assertEqual(last_request["thinking"], {"type": "disabled"})

    def test_streaming_reasoning_text_and_usage_are_folded(self):
        streamed = MagicMock()
        streamed.__enter__.return_value = streamed
        streamed.status_code = 200
        streamed.iter_lines.return_value = iter([
            b'data: {"choices":[{"delta":{"role":"assistant","reasoning_content":"think "},"finish_reason":null}]}\n',
            b'data: {"choices":[{"delta":{"reasoning_content":"more","content":"final "},"finish_reason":null}]}\n',
            b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n',
            b'data: [DONE]\n',
        ])
        with patch.object(self.controller._session, "post", return_value=streamed) as send:
            result = self.controller._send_request(self.request, "test-only")
        sent = json.loads(send.call_args.kwargs["data"])
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["stream_options"], {"include_usage": True})
        self.assertEqual(send.call_args.kwargs["headers"]["User-Agent"], "AIOS/0.10")
        self.assertTrue(send.call_args.kwargs["stream"])
        message = result["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "think more")
        self.assertEqual(message["content"], "final answer")
        self.assertEqual(result["usage"]["total_tokens"], 15)

    def test_streaming_fragmented_tool_call_is_folded(self):
        streamed = MagicMock()
        streamed.__enter__.return_value = streamed
        streamed.status_code = 200
        streamed.iter_lines.return_value = iter([
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"read","arguments":"{\\\"pa"}}]},"finish_reason":null}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"th\\\":\\\"a.txt\\\"}"}}]},"finish_reason":"tool_calls"}]}\n',
            b'data: [DONE]\n',
        ])
        with patch.object(self.controller._session, "post", return_value=streamed):
            result = self.controller._send_request(self.request, "test-only")
        call = result["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(call["id"], "call_1")
        self.assertEqual(call["function"]["name"], "read")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "a.txt"})

    def test_no_recovery_when_call_budget_is_exhausted(self):
        with patch.object(self.controller._session, "post", return_value=response("", finish="length")) as send:
            with self.assertRaises(ControllerError):
                self.controller.plan(Intent("test", "test", None, []), [], {
                    "budget": {"enabled": True, "remaining_model_calls_after_this": 0},
                })
        self.assertEqual(send.call_count, 1)

    def test_empty_then_timeout_then_success_uses_one_global_attempt_limit(self):
        before = copy.deepcopy(self.request)
        with patch.object(self.controller._session, "post", side_effect=[
            response("", finish="length"), requests.exceptions.Timeout(), response(),
        ]) as send:
            result = self.controller._request_with_recovery(self.request, "test-only")
        self.assertEqual(send.call_count, 3)
        self.assertEqual(result["usage"]["model_calls"], 3)
        self.assertEqual(self.request, before)
        retry = json.loads(send.call_args_list[-1].kwargs["data"])
        self.assertEqual(len(retry["messages"]), 2)

    def test_content_filter_does_not_trigger_empty_response_recovery(self):
        with patch.object(self.controller._session, "post", return_value=response("", finish="content_filter")) as send:
            self.controller._request_with_recovery(self.request, "test-only")
        self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
