from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from .capabilities import CapabilityState, EvidenceContract
from .components import ComponentRegistry


def normalize_resource_path(value: object) -> str:
    path = str(value or "").replace("\\", "/").strip()
    if path.startswith("/workspace/"):
        path = path[len("/workspace/"):]
    elif path == "/workspace":
        path = "."
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/") or "."


def coverage_labels(path: str, resource: dict[str, Any] | None = None) -> list[str]:
    """Extract conservative topic labels without asking the model to summarize state."""
    values: list[str] = []
    source = path
    if isinstance(resource, dict):
        for representation in resource.get("representations", []):
            if isinstance(representation, dict) and isinstance(representation.get("text"), str):
                source += "\n" + representation["text"][:2000]
    for match in re.finditer(r"(?<![A-Za-z])([A-Za-z])\s*[题題](?![A-Za-z])", source, re.IGNORECASE):
        values.append(match.group(1).upper() + "题")
    # Acronyms make useful coverage hints for generically named documents such as 题目分析.md.
    for match in re.finditer(r"\b[A-Z][A-Z0-9-]{2,11}\b", source):
        values.append(match.group(0))
    return list(dict.fromkeys(values))[:4]


class SituationResolver:
    """Build the small, dynamic task world exposed to the model."""

    NARRATIVE_SUFFIXES = {".md", ".txt", ".pdf", ".docx", ".html"}
    BROAD_SCOPE_MARKERS = (
        "整个", "全部", "所有", "文件夹", "目录", "资料", "folder", "directory", "all files",
    )
    INSPECTION_MARKERS = (
        "读取", "阅读", "总结", "分析", "理解", "概括", "梳理",
        "read", "review", "summar", "analy", "understand", "inspect",
    )

    def __init__(self, components: ComponentRegistry):
        self.components = components

    def resolve(
        self,
        request: str,
        inventory: dict[str, object],
        contract: EvidenceContract,
        working_state: dict[str, object],
        skills: list[dict[str, Any]],
        environment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        coverage_scope = self._coverage_scope(request, inventory)
        resources = self._resources(
            request, inventory, working_state,
            coverage_enabled=not bool(contract.artifacts),
            coverage_scope=coverage_scope,
        )
        coverage_targets = self._coverage_targets(
            request, resources,
            coverage_enabled=not bool(contract.artifacts),
            coverage_scope=coverage_scope,
        )
        selected_paths = {
            path
            for target in coverage_targets
            for path in target.get("evidence_paths", [])
        }
        target_by_path: dict[str, list[str]] = {}
        for target in coverage_targets:
            for path in target.get("evidence_paths", []):
                target_by_path.setdefault(path, []).append(str(target["id"]))
        for resource in resources:
            path = str(resource["path"])
            selected = path in selected_paths
            resource["selected_for_evidence"] = selected
            resource["evidence_for_targets"] = target_by_path.get(path, [])
            # Compatibility projection for existing model prompts and telemetry.
            # Coverage truth is owned by coverage_targets, not by enumerating files.
            resource["required_for_coverage"] = selected
        capabilities = []
        authority = self.components.authority.as_dict()
        required_names = {item.name for item in contract.capabilities}
        required_names.add("resource.read")
        for name in sorted(required_names):
            value = authority.get(name, {})
            declared = self.components.resolve_provider(name)
            available = self.components.resolve_available_provider(name)
            capabilities.append({
                "name": name,
                "state": value.get("state", "unregistered"),
                "interface": value.get("interface"),
                "required": name in {item.name for item in contract.capabilities},
                "provider_declared": declared is not None,
                "provider_available": available is not None,
            })
        constraints = [
            {"name": name, "state": value.get("state"), "detail": value.get("detail")}
            for name, value in authority.items()
            if value.get("state") not in {
                CapabilityState.AVAILABLE.value, CapabilityState.COMPOSABLE.value,
            }
        ]
        procedures = self._procedures(request, skills)
        operational = working_state.get("operational")
        if not isinstance(operational, dict):
            operational = {}
        return {
            "schema": "situation/v1",
            "coverage_scope": coverage_scope,
            "coverage_targets": coverage_targets,
            "resources": resources,
            "capabilities": capabilities,
            "procedures": procedures,
            "operational": {
                "resources_accessed": operational.get("resources", {}),
                "environment": operational.get(
                    "environment", working_state.get("execution_environment", {})
                ),
                "artifacts": operational.get(
                    "artifacts", working_state.get("available_artifacts", [])
                ),
            },
            "constraints": constraints,
            "guidance": [
                "Use read for workspace inspection; do not use cat/head/file when read supports the resource.",
                "Do not reread a complete resource unless a conflicting fact or explicit range requires it.",
                "Use a complete resource's semantic_residue before requesting the same full content again.",
                "Cover every required topic before claiming completion.",
            ],
            "environment": environment or {},
        }

    @classmethod
    def assess_coverage(cls, situation: dict[str, Any], final_output: str) -> dict[str, Any]:
        resources = {
            str(item["path"]): item for item in situation.get("resources", [])
            if isinstance(item, dict) and item.get("path")
        }
        targets = [
            item for item in situation.get("coverage_targets", [])
            if isinstance(item, dict) and item.get("id")
        ]
        target_ids = [str(item["id"]) for item in targets]
        assessed_targets = []
        unread: list[str] = []
        missing_evidence_targets: list[str] = []
        missing_topics: list[str] = []
        for target in targets:
            target_id = str(target["id"])
            evidence_paths = [str(path) for path in target.get("evidence_paths", [])]
            evidence = [resources.get(path, {}) for path in evidence_paths]
            has_evidence = any(
                bool(item.get("complete") and item.get("evidence_ref"))
                for item in evidence
            )
            answer_covered = cls._target_answer_covered(target_id, final_output, target_ids)
            if not has_evidence:
                missing_evidence_targets.append(target_id)
                unread.extend(path for path in evidence_paths if not resources.get(path, {}).get("complete"))
            if not answer_covered:
                missing_topics.append(target_id)
            assessed_targets.append({
                "target": target_id,
                "evidence_paths": evidence_paths,
                "evidence_refs": [
                    item.get("evidence_ref") for item in evidence if item.get("evidence_ref")
                ],
                "has_evidence": has_evidence,
                "covered_in_answer": answer_covered,
            })
        return {
            "required": bool(targets),
            "passed": not missing_evidence_targets and not missing_topics,
            "coverage_model": "goal_oriented",
            "required_targets": target_ids,
            "required_resources": list(dict.fromkeys(
                path for target in targets for path in target.get("evidence_paths", [])
            )),
            "unread_resources": list(dict.fromkeys(unread)),
            "missing_evidence_targets": missing_evidence_targets,
            "targets": assessed_targets,
            "topics": assessed_targets,
            "missing_answer_topics": missing_topics,
        }

    def classify_action(
        self, tool: str, arguments: dict[str, Any], working_state: dict[str, object],
    ) -> dict[str, Any] | None:
        operational = working_state.get("operational")
        resource_states = operational.get("resources", {}) if isinstance(operational, dict) else {}
        if tool == "read":
            path = normalize_resource_path(arguments.get("path"))
            previous = resource_states.get(path) if isinstance(resource_states, dict) else None
            explicit_range = "offset" in arguments or "limit" in arguments
            if isinstance(previous, dict) and previous.get("complete") and not explicit_range:
                return {
                    "kind": "repeated_resource_read", "path": path,
                    "prior_evidence_ref": previous.get("evidence_ref"),
                }
            return None
        if tool != "bash":
            return None
        command = str(arguments.get("command", "")).strip()
        match = re.search(
            r"(?:^|[;&|]\s*)(cat|head|tail|file)\s+(?:-[A-Za-z0-9 -]+\s+)?[\"']?([^\s\"';&|]+)",
            command,
        )
        if match:
            path = normalize_resource_path(match.group(2))
            previous = resource_states.get(path) if isinstance(resource_states, dict) else None
            return {
                "kind": "redundant_resource_bypass", "command": match.group(1), "path": path,
                "already_read": bool(isinstance(previous, dict) and previous.get("complete")),
                "prior_evidence_ref": previous.get("evidence_ref") if isinstance(previous, dict) else None,
                "preferred_interface": "read",
            }
        if re.search(r"(?:^|[;&|]\s*)(?:ls|find|pwd|which|whereis|type)\b", command):
            return {"kind": "environment_probe", "command": command[:500]}
        return None

    def _resources(
        self, request: str, inventory: dict[str, object], working_state: dict[str, object],
        *, coverage_enabled: bool, coverage_scope: dict[str, Any],
    ) -> list[dict[str, Any]]:
        text = request.casefold()
        broad = (
            any(marker in text for marker in self.BROAD_SCOPE_MARKERS)
            and any(marker in text for marker in self.INSPECTION_MARKERS)
        )
        operational = working_state.get("operational")
        resource_states = operational.get("resources", {}) if isinstance(operational, dict) else {}
        entries = inventory.get("files", []) if isinstance(inventory, dict) else []
        ranked: list[tuple[int, dict[str, Any]]] = []
        request_tokens = self._tokens(text)
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("path"):
                continue
            path = normalize_resource_path(entry["path"])
            scope_root = str(coverage_scope.get("root", "."))
            if scope_root != "." and not (
                path == scope_root or path.startswith(scope_root.rstrip("/") + "/")
            ):
                continue
            suffix = PurePosixPath(path).suffix.casefold()
            path_text = path.casefold()
            score = sum(4 for token in request_tokens if token in path_text)
            narrative = suffix in self.NARRATIVE_SUFFIXES
            if broad and narrative:
                score += 3
            if any(marker in path_text for marker in ("题", "分析", "problem", "readme")):
                score += 2
            state = resource_states.get(path, {}) if isinstance(resource_states, dict) else {}
            if isinstance(state, dict) and state:
                score += 5
            if score <= 0:
                continue
            labels = state.get("coverage_labels", []) if isinstance(state, dict) else []
            if not labels:
                labels = coverage_labels(path)
            ranked.append((score, {
                "path": path,
                "size": entry.get("size"),
                "status": state.get("status", "unread") if isinstance(state, dict) else "unread",
                "complete": bool(state.get("complete")) if isinstance(state, dict) else False,
                "representation": state.get("representation") if isinstance(state, dict) else None,
                "evidence_ref": state.get("evidence_ref") if isinstance(state, dict) else None,
                "coverage_labels": labels,
                "required_for_coverage": bool(coverage_enabled and broad and narrative),
            }))
        if broad and not ranked:
            for entry in entries[:20]:
                if isinstance(entry, dict) and entry.get("path"):
                    path = normalize_resource_path(entry["path"])
                    scope_root = str(coverage_scope.get("root", "."))
                    if scope_root != "." and not path.startswith(scope_root.rstrip("/") + "/"):
                        continue
                    ranked.append((0, {"path": path, "size": entry.get("size"), "status": "unread", "complete": False, "representation": None, "evidence_ref": None, "coverage_labels": [], "required_for_coverage": coverage_enabled}))
        return [item for _, item in sorted(ranked, key=lambda value: (-value[0], value[1]["path"]))[:24]]

    def _coverage_targets(
        self, request: str, resources: list[dict[str, Any]], *,
        coverage_enabled: bool, coverage_scope: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Resolve semantic goals and one deterministic best evidence source per goal."""
        text = request.casefold()
        broad = (
            any(marker in text for marker in self.BROAD_SCOPE_MARKERS)
            and any(marker in text for marker in self.INSPECTION_MARKERS)
        )
        if not coverage_enabled or not broad:
            return []

        grouped: dict[str, list[dict[str, Any]]] = {}
        for resource in resources:
            if PurePosixPath(str(resource.get("path", ""))).suffix.casefold() not in self.NARRATIVE_SUFFIXES:
                continue
            labels = resource.get("coverage_labels")
            if not isinstance(labels, list) or not labels:
                continue
            # The first conservative label is the structural topic (for example
            # C题); later labels such as NIPT are content hints, not peer goals.
            label = labels[0]
            if label:
                grouped.setdefault(str(label), []).append(resource)

        # If filenames expose no semantic topics, retain one bounded scope goal
        # instead of turning every file into an independent obligation.
        if not grouped:
            candidates = [
                item for item in resources
                if PurePosixPath(str(item.get("path", ""))).suffix.casefold() in self.NARRATIVE_SUFFIXES
            ]
            if not candidates:
                return []
            target_id = str(coverage_scope.get("root", "workspace"))
            selected = self._best_evidence(target_id, candidates, request)
            return [{
                "id": target_id,
                "evidence_paths": [str(selected["path"])],
                "candidate_count": len(candidates),
                "selection": "deterministic_best_evidence",
                "synthetic_scope_target": True,
            }]

        targets = []
        for target_id, candidates in sorted(grouped.items()):
            selected = self._best_evidence(target_id, candidates, request)
            targets.append({
                "id": target_id,
                "evidence_paths": [str(selected["path"])],
                "candidate_count": len(candidates),
                "selection": "deterministic_best_evidence",
            })
        return targets

    @staticmethod
    def _best_evidence(
        target_id: str, candidates: list[dict[str, Any]], request: str,
    ) -> dict[str, Any]:
        target = re.sub(r"\s+", "", target_id.casefold())
        request_text = request.casefold()
        suffix_scores = {".pdf": 30, ".docx": 24, ".txt": 16, ".md": 10, ".html": 8}
        derived_markers = ("分析", "建模", "总结", "报告", "笔记", "analysis", "summary", "report", "result")

        def rank(item: dict[str, Any]) -> tuple[int, int, str]:
            path = str(item["path"])
            value = PurePosixPath(path)
            stem = re.sub(r"\s+", "", value.stem.casefold())
            parent = re.sub(r"\s+", "", value.parent.name.casefold())
            score = suffix_scores.get(value.suffix.casefold(), 0)
            if path.casefold() in request_text:
                score += 100
            if stem == target:
                score += 80
            if parent == target:
                score += 50
            if target in stem:
                score += 20
            if any(marker in stem for marker in derived_markers):
                score -= 35
            return score, -len(value.parts), path.casefold()

        return max(candidates, key=rank)

    def _coverage_scope(
        self, request: str, inventory: dict[str, object],
    ) -> dict[str, Any]:
        """Resolve one bounded resource root before selecting coverage obligations."""
        text = request.casefold()
        entries = [item for item in inventory.get("files", []) if isinstance(item, dict)]
        roots = sorted({
            normalize_resource_path(item.get("path", "")).split("/", 1)[0]
            for item in entries
            if "/" in normalize_resource_path(item.get("path", ""))
        })
        explicit = [root for root in roots if root.casefold() in text]
        if explicit:
            root = max(explicit, key=len)
            reason = "explicit_directory_match"
        else:
            request_tokens = self._tokens(text)
            scores: dict[str, int] = {candidate: 0 for candidate in roots}
            for item in entries:
                path = normalize_resource_path(item.get("path", ""))
                if "/" not in path:
                    continue
                candidate = path.split("/", 1)[0]
                lowered = path.casefold()
                scores[candidate] += sum(3 for token in request_tokens if token in lowered)
                scores[candidate] += sum(
                    2 for marker in ("题", "分析", "problem", "readme")
                    if marker in lowered and marker in text
                )
            # Common bilingual directory naming is resolved at the scope boundary,
            # never by widening Coverage to every workspace file.
            if any(marker in text for marker in ("数模", "数学建模")):
                for candidate in roots:
                    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", candidate.casefold())
                    if any(marker in normalized for marker in ("mathmodel", "modeling", "数模", "数学建模")):
                        scores[candidate] += 100
            ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0].casefold()))
            if ranked and ranked[0][1] > 0:
                root = ranked[0][0]
                reason = "task_relevance_root"
            elif len(roots) == 1 and any(marker in text for marker in self.BROAD_SCOPE_MARKERS):
                root = roots[0]
                reason = "single_workspace_subtree"
            else:
                root = "."
                reason = "workspace_root_fallback"
        outside = sum(
            1 for item in entries
            if root != "."
            and not normalize_resource_path(item.get("path", "")).startswith(root.rstrip("/") + "/")
        )
        return {
            "root": root,
            "recursive": True,
            "include": "relevant_documents",
            "exclude": ["outside_root"],
            "resolution_reason": reason,
            "outside_root_files_excluded": outside,
        }

    def _procedures(self, request: str, skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        request_tokens = self._tokens(request.casefold())
        values = []
        for skill in skills:
            haystack = f"{skill.get('name', '')} {skill.get('description', '')}".casefold()
            score = sum(1 for token in request_tokens if token in haystack)
            if score:
                values.append((score, {
                    "name": skill.get("name"), "version": skill.get("version"),
                    "available": bool(skill.get("available")),
                }))
        return [item for _, item in sorted(values, key=lambda value: (-value[0], str(value[1]["name"])))[:6]]

    @staticmethod
    def _tokens(text: str) -> set[str]:
        words = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text))
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
        words.update(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
        return {item for item in words if item}

    @staticmethod
    def _label_mentioned(label: str, output: str) -> bool:
        if label.endswith("题") and len(label) == 2:
            letter = re.escape(label[0])
            return re.search(rf"(?<![A-Za-z]){letter}\s*[题題]", output, re.IGNORECASE) is not None
        return label.casefold() in output.casefold()

    @classmethod
    def _target_answer_covered(
        cls, label: str, output: str, all_labels: list[str],
    ) -> bool:
        if not cls._label_mentioned(label, output):
            return False
        if label.endswith("题") and len(label) == 2:
            match = re.search(
                rf"(?<![A-Za-z]){re.escape(label[0])}\s*[题題]",
                output, re.IGNORECASE,
            )
        else:
            match = re.search(re.escape(label), output, re.IGNORECASE)
        if match is None:
            return False
        end = len(output)
        for other in all_labels:
            if other == label:
                continue
            if other.endswith("题") and len(other) == 2:
                candidate = re.search(
                    rf"(?<![A-Za-z]){re.escape(other[0])}\s*[题題]",
                    output[match.end():], re.IGNORECASE,
                )
            else:
                candidate = re.search(re.escape(other), output[match.end():], re.IGNORECASE)
            if candidate is not None:
                end = min(end, match.end() + candidate.start())
        segment = output[match.start():end]
        compact = re.sub(r"[\s#>*_`\-—：:（）()]+", "", segment)
        insufficient_markers = (
            "未完整", "无法总结", "无法获取", "正文未", "重新读取",
            "未能在此轮完整呈现", "具体文字内容未能", "待下一轮", "待补充",
            "无法提供", "not fully", "unable to summarize", "unavailable",
            "to be completed", "in a later round",
        )
        if any(marker in segment.casefold() for marker in insufficient_markers) and len(compact) < 300:
            return False
        return len(compact) >= 6
