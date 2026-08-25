from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from .storage import StateStore
from .types import Memory, MemoryType
from .protocol import contains_serialized_tool_call


def _terms(text: str) -> set[str]:
    normalized = text.lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    words.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return {term for term in words if term}


class MemoryManager:
    def __init__(self, store: StateStore):
        self.store = store

    def remember(
        self,
        type: MemoryType,
        content: str,
        *,
        key: str | None = None,
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        return self.store.add_memory(
            Memory(type, content, key, max(0.0, min(1.0, importance)), metadata or {})
        )

    def retrieve(self, query: str, limit: int = 6) -> list[Memory]:
        query_terms = _terms(query)
        candidates = [
            memory
            for memory in self.store.list_memories(limit=200)
            if not contains_serialized_tool_call(memory.content)
        ]

        def score(memory: Memory) -> tuple[float, int]:
            overlap = len(query_terms & _terms(memory.content + " " + (memory.key or "")))
            return (overlap * 2.0 + memory.importance, memory.id or 0)

        ranked = sorted(candidates, key=score, reverse=True)
        relevant = [memory for memory in ranked if score(memory)[0] > memory.importance]
        return (relevant or ranked)[:limit]


class ContextComposer:
    def __init__(self, memories: MemoryManager, max_characters: int = 6000):
        self.memories = memories
        self.max_characters = max_characters

    def compose(self, request: str) -> dict[str, Any]:
        selected = self.memories.retrieve(request)
        items: list[dict[str, Any]] = []
        used = 0
        for memory in selected:
            remaining = self.max_characters - used
            if remaining <= 0:
                break
            data = asdict(memory)
            content = memory.content[:remaining]
            data["content"] = content
            used += len(content)
            items.append(data)
        return {"retrieved_memories": items, "characters": used}
