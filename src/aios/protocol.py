from __future__ import annotations

import re


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
