from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .capabilities import EvidenceContract
from .situation import normalize_resource_path
from .types import Action, ActionResult


@dataclass(slots=True)
class AnswerArtifact:
    path: str
    role: str
    content_ref: str
    content_digest: str
    state: str = "staged"


@dataclass(slots=True)
class CanonicalAnswer:
    """The substantive answer surface evaluated by deterministic verifiers."""

    user_message: str
    body: str
    artifacts: list[AnswerArtifact] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_message": self.user_message,
            "body": self.body,
            "artifacts": [asdict(item) for item in self.artifacts],
        }

    def mark_committed(self) -> None:
        for artifact in self.artifacts:
            artifact.state = "committed"

    @classmethod
    def bind(
        cls,
        user_message: str,
        actions: list[Action],
        results: list[ActionResult],
        contract: EvidenceContract,
        snapshot: Path | None = None,
    ) -> "CanonicalAnswer":
        candidates: list[tuple[int, str, str]] = []
        for index, (action, result) in enumerate(zip(actions, results, strict=False)):
            if (
                action.tool not in {"write", "write_file", "append_file", "edit"}
                or not result.ok
            ):
                continue
            output_path = result.output.get("path") if isinstance(result.output, dict) else None
            raw_path = action.arguments.get("path") or output_path
            path = normalize_resource_path(raw_path)
            if not path:
                continue
            content = action.arguments.get("content")
            if not isinstance(content, str) and snapshot is not None:
                target = (snapshot / path).resolve()
                try:
                    target.relative_to(snapshot.resolve())
                    content = target.read_text(encoding="utf-8")
                except (OSError, UnicodeError, ValueError):
                    content = None
            if isinstance(content, str):
                candidates.append((index, path, content))

        expected = {Path(name).name.casefold() for name in contract.artifacts}
        mentioned = {
            path for _, path, _ in candidates
            if path in user_message or Path(path).name in user_message
        }
        selected = [
            item for item in candidates
            if (expected and Path(item[1]).name.casefold() in expected)
            or (not expected and item[1] in mentioned)
        ]
        if not selected and candidates:
            selected = [candidates[-1]]

        artifacts: list[AnswerArtifact] = []
        bodies = [user_message.strip()] if user_message.strip() else []
        for index, path, content in selected:
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            artifacts.append(AnswerArtifact(
                path=path,
                role="final_deliverable",
                content_ref=f"action:{index}:content:{digest[:16]}",
                content_digest=digest,
            ))
            bodies.append(f"[Final deliverable: {path}]\n{content}")
        return cls(user_message=user_message, body="\n\n".join(bodies), artifacts=artifacts)
