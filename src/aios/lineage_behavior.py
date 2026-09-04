"""Bounded, factual projections of lineage behavior; no Skill recommendation policy."""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
import re
import shlex
from typing import Any
from urllib.parse import urlsplit


BEHAVIOR_POLICY = {
    "schema": "lineage_behavior_policy/v1",
    "max_tasks": 20,
    "max_trace_records_per_task": 400,
    "total_digest_characters": 24000,
    "cross_task_characters": 8000,
    "normalization": "operational signatures, not semantic diagnosis or proof of internal execution",
    "sequence_basis": "action_result order; unexecuted plans excluded; cache hits included as requests",
    "pattern_basis": "contiguous family ngrams of length 2-4, within one cycle; overlapping occurrences counted",
    "interpretation": "shared shape does not prove equivalent purpose; repetition and cost do not imply a Skill is needed",
    "missing_traces": "unknown history, not evidence of no activity",
    "resource_types_basis": "suffixes of explicitly attempted paths, not proof of successful or complete reads",
    "inspection": "trace:N refers to existing audit records; this one-shot version adds no model inspection tools",
}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _call_name(node.value) + "." + node.attr
    return ""


def action_family(tool: str, arguments: dict[str, Any], truncated: bool = False) -> str:
    """Only recognize explicit operation syntax; never interpret errors or intent."""
    path = str(arguments.get("path") or "")
    if tool in {"read", "read_file"}:
        return "network_fetch" if path.startswith(("https://", "http://")) else "resource_read"
    if tool in {"list_files", "search_files"}:
        return "file_search"
    if tool in {"write", "write_file", "append_file"}:
        return "file_write"
    if tool == "edit":
        return "file_edit"
    if tool != "bash":
        return "other_tool"
    if truncated:
        return "shell_exec"
    try:
        words = shlex.split(str(arguments.get("command") or ""))
    except ValueError:
        return "shell_exec"
    if not words:
        return "shell_exec"
    executable = words[0].rsplit("/", 1)[-1]
    if executable in {"curl", "wget"} and any(word.startswith(("http://", "https://")) for word in words[1:]):
        return "network_fetch"
    if executable in {"grep", "rg", "find", "ls"}:
        return "file_search"
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        # Inline Python only: a source string/comment mentioning requests.get is
        # not a call signature. Script-file contents are deliberately not read.
        if "-c" in words and words.index("-c") + 1 < len(words):
            try:
                tree = ast.parse(words[words.index("-c") + 1])
                calls = {_call_name(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
                if calls & {"requests.get", "requests.request", "urllib.request.urlopen"}:
                    return "network_fetch"
            except (SyntaxError, ValueError, RecursionError):
                pass
        return "python_exec"
    return "shell_exec"


def _resource(path: str) -> tuple[str | None, str | None]:
    """Project type and workspace-relative write path; never emit a URL or host path."""
    if not path:
        return None, None
    is_url = path.startswith(("http://", "https://"))
    try:
        normalized = urlsplit(path).path if is_url else path.replace("\\", "/")
    except ValueError:
        return None, None
    extension = normalized.rsplit("/", 1)[-1].rsplit(".", 1)
    kind = extension[-1].lower() if len(extension) == 2 else None
    kind = kind if kind and re.fullmatch(r"[a-z0-9]{1,10}", kind) else None
    if is_url:
        return kind, None
    if "/workspace/" in normalized:
        normalized = normalized.split("/workspace/", 1)[1]
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return kind, None
    if ".." in normalized.split("/") or len(normalized) > 120:
        return kind, None
    return kind, normalized.removeprefix("./")


def summarize_behavior(task_id: int, sample: dict[str, Any]) -> tuple[dict[str, Any], dict]:
    plans: dict[tuple[str, Any], list] = {}
    offsets: Counter = Counter()
    actions = []
    families: Counter = Counter()
    failures: Counter = Counter()
    exits: Counter = Counter()
    written: set[str] = set()
    types: set[str] = set()
    unpaired = 0
    shortened_arguments = 0
    cache_hits = 0
    for trace in sample["traces"]:
        data = trace["data"]
        key = (trace["cycle_id"], data.get("round"))
        if trace["kind"] == "plan_created":
            plans[key] = data.get("actions", [])
            offsets[key] = 0
            continue
        if trace["kind"] != "action_result":
            continue
        offset = offsets[key]
        offsets[key] += 1
        planned = plans.get(key, [])
        action = planned[offset] if offset < len(planned) else None
        tool = data.get("tool") or "unknown"
        # No searching for a convenient later plan: mismatches stay unknown.
        matched = isinstance(action, dict) and action.get("tool") == tool
        if not matched:
            unpaired += 1
        arguments = action.get("arguments", {}) if matched else {}
        truncated = bool(action.get("arguments_truncated")) if matched else False
        shortened_arguments += int(truncated)
        family = action_family(tool, arguments, truncated) if matched else "unknown"
        families[family] += 1
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        cache = output.get("observation_cache") or {}
        reused = cache.get("hit") in (True, 1)
        cache_hits += int(reused)
        if data.get("ok") in (False, 0):
            error = str(data.get("error") or "")
            error_class = re.match(r"^([A-Za-z][A-Za-z0-9]*(?:Error|Exception|Denied|Unavailable))(?=:)", error)
            failures[error_class.group(1) if error_class else "tool_failure"] += 1
        exit_code = output.get("exit_code")
        if type(exit_code) is int:
            exits[str(exit_code)] += 1
        if matched and not truncated:
            kind, path = _resource(str(arguments.get("path") or ""))
            if kind:
                types.add(kind)
            if data.get("ok") in (True, 1) and family in {"file_write", "file_edit"} and path:
                written.add(path)
        actions.append({
            "tool": tool, "family": family, "cycle": trace["cycle_id"],
            "trace_ref": f"trace:{trace['id']}",
        })

    patterns: dict[tuple, dict] = {}
    for width in (2, 3, 4):
        for start in range(len(actions) - width + 1):
            window = actions[start:start + width]
            shape = tuple(item["family"] for item in window)
            if "unknown" in shape or len({item["cycle"] for item in window}) != 1:
                continue
            item = patterns.setdefault(shape, {"count": 0, "examples": []})
            item["count"] += 1
            if len(item["examples"]) < 2:
                item["examples"].append([entry["trace_ref"] for entry in window])
    ordered = sorted(patterns, key=lambda shape: (-patterns[shape]["count"], -len(shape), shape))
    repeated = [shape for shape in ordered if patterns[shape]["count"] > 1]
    digest = {
        "schema": "task_behavior_digest/v1", "behavior_ref": f"behavior:task:{task_id}",
        "source": {
            "records_observed": len(sample["traces"]), "record_limit": sample["limit"],
            "trace_truncated": sample["truncated"], "sample_order": "oldest_first",
            "scope": "all recorded task cycles/attempts, not just the final result",
            "first_trace_ref": f"trace:{sample['traces'][0]['id']}" if sample["traces"] else None,
            "last_trace_ref": f"trace:{sample['traces'][-1]['id']}" if sample["traces"] else None,
            "history_available": bool(sample["traces"]),
            "unpaired_results": unpaired, "arguments_truncated": shortened_arguments,
        },
        "observed_action_results": len(actions),
        "tool_sequence": [item["tool"] for item in actions[:24]],
        "sequence_trace_refs": [item["trace_ref"] for item in actions[:24]],
        "sequence_omitted": max(0, len(actions) - 24),
        "action_families": dict(sorted(families.items())),
        "failure_families": dict(sorted(failures.items())),
        "observed_exit_codes": dict(sorted(exits.items())),
        "reused_observation_requests": cache_hits,
        "repeated_patterns": [
            {"pattern": list(shape), **patterns[shape]} for shape in repeated[:4]
        ],
        "repeated_patterns_omitted": max(0, len(repeated) - 4),
        "artifact_paths_written": sorted(written)[:8],
        "artifact_paths_omitted": max(0, len(written) - 8),
        "artifact_scope": "successful primitive writes/edits; creation vs overwrite and final publication unknown",
        "resource_types_touched": sorted(types)[:12],
        "resource_types_omitted": max(0, len(types) - 12),
    }
    return digest, patterns


def bound_digest(digest: dict[str, Any], max_characters: int) -> dict[str, Any]:
    """Budget only projections, preserving counts, provenance and explicit omissions."""
    digest["projection_truncated"] = False
    digest["projection_omitted_fields"] = []
    while len(json.dumps(digest, ensure_ascii=False)) > max_characters:
        digest["projection_truncated"] = True
        if digest["tool_sequence"]:
            digest["tool_sequence"].pop()
            digest["sequence_trace_refs"].pop()
            digest["sequence_omitted"] += 1
        elif digest["repeated_patterns"]:
            digest["repeated_patterns"].pop()
            digest["repeated_patterns_omitted"] += 1
        elif digest["artifact_paths_written"]:
            digest["artifact_paths_written"].pop()
            digest["artifact_paths_omitted"] += 1
        else:
            # Untrusted tool/error/extension names cannot grow the residual maps
            # without bound. An omitted map means unavailable in this projection.
            for key in ("failure_families", "observed_exit_codes", "resource_types_touched", "action_families"):
                if digest.get(key):
                    digest[key] = None
                    digest["projection_omitted_fields"].append(key)
                    break
            else:
                break
    return digest


def cross_task_patterns(observations: list[tuple[int, str, dict]]) -> dict[str, Any]:
    shared: dict[tuple, list] = {}
    for task_id, status, patterns in observations:
        for shape, detail in patterns.items():
            shared.setdefault(shape, []).append({
                "task_id": task_id, "status": status, "count": detail["count"],
                "example_trace_refs": detail["examples"][0],
            })
    # This is the definition of a cross-task occurrence, not an evolution trigger.
    shapes = [shape for shape in shared if len(shared[shape]) > 1]
    shapes.sort(key=lambda shape: (-len(shared[shape]), -sum(x["count"] for x in shared[shape]), -len(shape), shape))
    selected = []
    for shape in shapes[:8]:
        selected.append({
            "pattern_id": "P" + hashlib.sha256(json.dumps(shape).encode()).hexdigest()[:12],
            "action_shape": list(shape),
            "observed_in_tasks": sorted(x["task_id"] for x in shared[shape]),
            "occurrences": sorted(shared[shape], key=lambda item: item["task_id"]),
        })
    result = {"patterns": selected, "omitted": len(shapes) - len(selected)}
    while len(json.dumps(result, ensure_ascii=False)) > BEHAVIOR_POLICY["cross_task_characters"] and selected:
        selected.pop()
        result["omitted"] += 1
    return result
