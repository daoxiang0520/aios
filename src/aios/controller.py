from __future__ import annotations

import ast
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from .config import ModelConfig
from .tools import CORE_TOOL_SCHEMAS
from .protocol import contains_serialized_tool_call
from .types import Action, Goal, Intent, Plan


JSON_SYSTEM_PROMPT = """You are the planning controller of a permission-gated AI runtime.
Return JSON only with this shape:
{"summary":"...","done":false,"actions":[{"tool":"read|write|edit|bash","arguments":{},"reason":"..."}]}
Use only the listed tools. All paths are relative to the workspace. Prefer inspection before writing.
Never invent extra permissions, credentials, host execution, or absolute paths.
You operate in rounds. Results from previous actions appear under context.observations.
Set done=true only when the user goal is actually fulfilled. Listing or reading files is observation,
not completion. If the user requested an artifact, do not set done=true until write succeeds.
"""

TOOL_SYSTEM_PROMPT = """You are the controller of a permission-gated AI runtime.
Use the provided tools whenever workspace evidence or file changes are required.
Reusable skills are listed in context.skills. Discover them with `python /skills/skill.py list`
and invoke them through bash as `python /skills/skill.py run NAME --input-json '{...}'`.
Never list, read, write, or inspect `/skills` directly, and never combine a Skill dispatcher
invocation with another shell command, pipe, or redirection.
Skills do not grant permissions; if a required capability is unavailable, report the block.
Do not merely describe a tool call: call the tool. Tool results will be returned to you.
Listing or reading files is observation, not task completion. Continue until the user goal is fulfilled.
For a requested artifact, call write and only finish after its successful tool result.
Use edit for exact modifications. Use bash only for commands that are necessary and verifiable.
Respect context.budget, preserve reserved completion calls, and stop broad inspection before the
tool budget is exhausted. Prefer task and trace query tools over unrelated workspace scans.
When the task is complete, return a concise final answer with no tool call.
On the final call tools are disabled. Never print XML, DSML, tool-call tags, or a serialized
tool request as text; synthesize the best natural-language answer from existing observations.
Never request credentials, absolute paths, host execution, or unregistered tools. Network is usable only when host policy grants it.
"""

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Return a final textual answer to the user.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories at a workspace-relative path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file at a workspace-relative path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write UTF-8 text under the workspace. Existing files require overwrite=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
]

# Public compatibility alias; the runtime now supplies registry schemas dynamically.
TOOL_SCHEMAS = CORE_TOOL_SCHEMAS


def _normalized_usage(value: Any) -> dict[str, int]:
    result: dict[str, int] = {"model_calls": 1}
    if not isinstance(value, dict):
        return result
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        item = value.get(key)
        if isinstance(item, int) and item >= 0:
            result[key] = item
    return result


def _merge_usage(*values: dict[str, int]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        for key, item in value.items():
            result[key] = result.get(key, 0) + item
    return result


class ControllerError(RuntimeError):
    pass


class LLMController:
    def __init__(self, config: ModelConfig):
        self.config = config
        self.tool_schemas = list(CORE_TOOL_SCHEMAS)

    def set_tool_schemas(self, schemas: list[dict[str, Any]]) -> None:
        self.tool_schemas = list(schemas)

    def plan(
        self,
        intent: Intent,
        goals: list[Goal],
        context: dict[str, Any] | None = None,
    ) -> Plan:
        if self.config.provider == "mock":
            return self._mock_plan(intent)
        if self.config.provider in {"openai_compatible", "deepseek"}:
            return self._remote_plan(intent, goals, context or {})
        raise ControllerError(f"Unsupported model provider: {self.config.provider}")

    @staticmethod
    def _mock_plan(intent: Intent) -> Plan:
        event = intent.payload.get("event", {})
        requested = event.get("actions")
        if isinstance(requested, list):
            return Plan(
                summary="Execute actions supplied by a trusted local event producer",
                actions=[LLMController._parse_action(item) for item in requested],
                done=True,
            )
        message = event.get("message") or f"Handled event: {intent.name}"
        return Plan(
            summary="Mock controller acknowledgement",
            actions=[],
            done=True,
        )

    def _remote_plan(
        self,
        intent: Intent,
        goals: list[Goal],
        context: dict[str, Any] | None = None,
    ) -> Plan:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.config.api_key_env):
            raise ControllerError(
                "model.api_key_env must be an environment-variable name, not an API key"
            )
        key = os.environ.get(self.config.api_key_env)
        if not key:
            raise ControllerError(f"Missing API key environment variable: {self.config.api_key_env}")
        user_payload = {
            "intent": {
                "name": intent.name,
                "reason": intent.reason,
                "goal_id": intent.goal_id,
                "payload": intent.payload,
            },
            "active_goals": [
                {"id": goal.id, "title": goal.title, "type": goal.type.value, "priority": goal.priority}
                for goal in goals
            ],
            "context": {},
        }
        request_context = dict(context or {})
        protocol_messages = request_context.pop("_protocol_messages", [])
        use_tool_calling = self.config.protocol == "tool_calling"
        if use_tool_calling:
            request_context.pop("observations", None)
            request_context.pop("round", None)
        user_payload["context"] = request_context
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (TOOL_SYSTEM_PROMPT if use_tool_calling else JSON_SYSTEM_PROMPT)
                + self._prompt_append(context or {}),
            },
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        if use_tool_calling and isinstance(protocol_messages, list):
            messages.extend(item for item in protocol_messages if isinstance(item, dict))
        request_data: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        force_final = False
        if use_tool_calling:
            request_data["tools"] = self.tool_schemas
            budget = (context or {}).get("budget", {})
            force_final = budget.get("remaining_model_calls_after_this") == 0
            request_data["tool_choice"] = "none" if force_final else "auto"
        else:
            request_data["response_format"] = {"type": "json_object"}
        if self.config.provider == "deepseek":
            thinking = self.config.thinking if self.config.thinking in {"enabled", "disabled"} else "disabled"
            request_data["thinking"] = {"type": thinking}
        response_data = self._send_request(request_data, key)
        model_usage = _normalized_usage(response_data.get("usage"))
        try:
            choice = response_data["choices"][0]
            message = choice["message"]
            content = message.get("content")
            tool_calls = message.get("tool_calls")
            if use_tool_calling and isinstance(tool_calls, list) and tool_calls:
                actions = self._parse_tool_calls(tool_calls)
                protocol_message = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                }
                reasoning = message.get("reasoning_content")
                if reasoning is not None:
                    protocol_message["reasoning_content"] = reasoning
                return Plan(
                    summary=f"Model requested {len(actions)} tool call(s)",
                    actions=actions,
                    done=False,
                    protocol_message=protocol_message,
                    model_usage=model_usage,
                )
            if use_tool_calling and isinstance(content, str) and content.strip():
                if contains_serialized_tool_call(content):
                    budget = (context or {}).get("budget", {})
                    repairs = int(budget.get("protocol_repairs_remaining", 0))
                    if force_final and repairs > 0:
                        return self._repair_final_answer(
                            request_data, content, key, initial_usage=model_usage
                        )
                    raise ControllerError(
                        "Model emitted serialized tool-call markup instead of a native tool call or final answer"
                    )
                return Plan(
                    summary=content.strip(),
                    actions=[],
                    done=True,
                    model_usage=model_usage,
                )
            try:
                plan = self._parse_plan_content(content)
                plan.model_usage = model_usage
                return plan
            except ControllerError as exc:
                content_chars = len(content) if isinstance(content, str) else 0
                finish_reason = choice.get("finish_reason", "unknown")
                raise ControllerError(
                    f"Invalid structured plan: {exc}; finish_reason={finish_reason}; "
                    f"content_chars={content_chars}"
                ) from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise ControllerError("Model returned an invalid plan") from exc

    def _send_request(self, request_data: dict[str, Any], key: str) -> dict[str, Any]:
        body = json.dumps(request_data).encode("utf-8")
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ControllerError(f"Model request failed: {exc}") from exc
        if not isinstance(response_data, dict):
            raise ControllerError("Model returned an invalid response object")
        return response_data

    def _repair_final_answer(
        self,
        original_request: dict[str, Any],
        invalid_content: str,
        key: str,
        initial_usage: dict[str, int],
    ) -> Plan:
        """Perform one no-tools synthesis retry without re-running completed actions."""
        messages = list(original_request.get("messages", []))
        messages.extend([
            {"role": "assistant", "content": invalid_content},
            {
                "role": "user",
                "content": (
                    "Protocol correction: tools are unavailable. Do not emit XML, DSML, tool-call "
                    "tags, JSON tool requests, or describe another command. Using only the existing "
                    "conversation and tool results, return the best concise natural-language final answer."
                ),
            },
        ])
        repair_request = {
            "model": original_request["model"],
            "messages": messages,
            "max_tokens": original_request["max_tokens"],
            "temperature": original_request["temperature"],
        }
        if "thinking" in original_request:
            repair_request["thinking"] = original_request["thinking"]
        response_data = self._send_request(repair_request, key)
        combined_usage = _merge_usage(initial_usage, _normalized_usage(response_data.get("usage")))
        try:
            choice = response_data["choices"][0]
            message = choice["message"]
            content = message.get("content")
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                raise ControllerError("Model protocol repair failed: native tool calls remained")
            if not isinstance(content, str) or not content.strip() or contains_serialized_tool_call(content):
                raise ControllerError("Model protocol repair failed: no valid final answer")
            return Plan(
                summary=content.strip(), actions=[], done=True,
                model_usage=combined_usage,
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise ControllerError("Model protocol repair failed: invalid response") from exc

    @classmethod
    def _parse_tool_calls(cls, tool_calls: Any) -> list[Action]:
        if not isinstance(tool_calls, list) or not tool_calls:
            raise ControllerError("Model returned no tool calls")
        actions: list[Action] = []
        for call in tool_calls:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise ControllerError("Invalid native tool call")
            function = call["function"]
            name = function.get("name")
            raw_arguments = function.get("arguments", "{}")
            if not isinstance(name, str) or not isinstance(raw_arguments, str):
                raise ControllerError("Invalid native tool call function")
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                try:
                    arguments = ast.literal_eval(raw_arguments)
                except (ValueError, SyntaxError) as exc:
                    raise ControllerError("Native tool call arguments are not valid JSON") from exc
            if not isinstance(arguments, dict):
                raise ControllerError("Native tool call arguments must be an object")
            actions.append(
                Action(
                    tool=name,
                    arguments=arguments,
                    reason="Native model tool call",
                    call_id=str(call.get("id") or ""),
                )
            )
        return actions

    @staticmethod
    def _prompt_append(context: dict[str, Any]) -> str:
        harness = context.get("harness", {})
        if not isinstance(harness, dict):
            return ""
        value = harness.get("prompt_append", "")
        return f"\nAdditional approved policy:\n{value}" if isinstance(value, str) and value else ""

    @classmethod
    def _parse_plan_content(cls, content: Any) -> Plan:
        """Accept common OpenAI-compatible text shapes while validating the plan."""
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    text = block.get("text") or block.get("content")
                    if isinstance(text, str):
                        parts.append(text)
            content = "".join(parts)

        if not isinstance(content, str) or not content.strip():
            raise ControllerError("Model returned empty plan content")

        text = content.strip()
        text = re.sub(r"<(?:think|analysis)>.*?</(?:think|analysis)>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()

        data: Any = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            for index, character in enumerate(text):
                if character != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(text[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and (
                    isinstance(candidate.get("actions"), list)
                    or isinstance(candidate.get("steps"), list)
                    or isinstance(candidate.get("plan"), dict)
                ):
                    data = candidate
                    break

        if data is None:
            try:
                data = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                pass

        if isinstance(data, list):
            data = {"summary": "Model-generated plan", "actions": data}
        if isinstance(data, dict) and isinstance(data.get("plan"), dict):
            data = data["plan"]
        if isinstance(data, dict) and "actions" not in data and isinstance(data.get("steps"), list):
            data["actions"] = data["steps"]

        if not isinstance(data, dict):
            raise ControllerError("Model response did not contain a JSON plan object")
        actions = data.get("actions")
        if not isinstance(actions, list):
            raise ControllerError("Model plan must contain an actions array")
        parsed_actions = [cls._parse_action(item) for item in actions]
        done = data.get("done")
        if not isinstance(done, bool):
            done = not parsed_actions or any(action.tool in {"echo", "write_file"} for action in parsed_actions)
        return Plan(
            summary=str(data.get("summary", "Model-generated plan")),
            actions=parsed_actions,
            done=done,
        )

    @staticmethod
    def _parse_action(item: Any) -> Action:
        if not isinstance(item, dict):
            raise ControllerError("Invalid action in plan")
        if isinstance(item.get("action"), dict):
            item = item["action"]
        tool = item.get("tool") or item.get("name") or item.get("tool_name")
        if not isinstance(tool, str):
            raise ControllerError("Invalid action in plan")
        arguments = item.get("arguments", item.get("args", {}))
        if not isinstance(arguments, dict):
            raise ControllerError("Action arguments must be an object")
        return Action(tool=tool, arguments=arguments, reason=str(item.get("reason", "")))
