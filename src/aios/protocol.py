from __future__ import annotations

import re


def is_skill_authoring_request(request: str) -> bool:
    """Distinguish an imperative Skill build from a short capability question."""
    text = request.strip().casefold()
    if "skill" not in text:
        return False
    short_question = (
        len(text) <= 32
        and re.match(r"^(?:你)?(?:能|可以|是否|能否|可否)|^can you\b", text) is not None
        and re.search(r"[吗？?]\s*$", text) is not None
    )
    if short_question:
        return False
    authoring_words = (
        "创建", "设计", "生成", "编写", "开发", "产生", "实现", "改进", "修改", "迭代",
        "create", "design", "generate", "write", "develop", "implement", "improve", "mutate",
    )
    return any(word in text for word in authoring_words)


def contains_serialized_tool_call(text: str) -> bool:
    """Detect model-emitted tool protocol markup masquerading as final prose."""
    if not isinstance(text, str) or not text:
        return False
    lowered = text.casefold()
    if "dsml" in lowered and any(marker in lowered for marker in ("tool_calls", "invoke", "parameter")):
        return True
    return bool(
        re.search(
            r"<\s*(?:[|｜]+\s*)?(?:tool_calls?|tool_call|invoke|function_calls?)\b",
            text,
            flags=re.IGNORECASE,
        )
    )
