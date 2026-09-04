from __future__ import annotations

import json
import mimetypes
import re
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import Settings
from .capabilities import CapabilityRegistry
from .components import build_component_registry
from .lineage import LineageManager
from .plugins import PluginManager
from .skills import SkillManager
from .self_versioning import SelfVersionManager
from .storage import StateStore
from .types import Event, Task, TaskStatus


TERMINAL = {
    TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.DEAD_LETTER.value,
    TaskStatus.DEGRADED.value, TaskStatus.BLOCKED_CAPABILITY.value,
    TaskStatus.NEEDS_AUTHORITY.value, TaskStatus.TERMINAL_FAILURE.value,
    TaskStatus.NEEDS_REVIEW.value, TaskStatus.STOPPED.value,
    TaskStatus.YIELDED.value, TaskStatus.ABANDONED.value,
}


class WebUIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class AIOSWebApplication:
    """Small projection layer over durable AIOS state; it owns no Runtime policy."""

    def __init__(self, settings: Settings, store: StateStore):
        self.settings = settings
        self.store = store
        self.csrf_token = secrets.token_urlsafe(24)
        self.assets = Path(__file__).with_name("web")
        self.self_versions = None
        if settings.self_modification.enabled:
            self.self_versions = SelfVersionManager(
                settings.self_root, settings.self_modification,
            )
            self.self_versions.initialize()
        skills = SkillManager(settings.skills_root, settings.skills)
        if settings.skills.enabled and settings.skills.bootstrap_builtins:
            skills.bootstrap_builtins()
        capabilities = CapabilityRegistry.default(
            sandbox_available=False,
            network_enabled=settings.capabilities.network_enabled,
            allowed_domains=settings.capabilities.allowed_domains,
        )
        components = build_component_registry(
            capabilities, store=store,
            skill_manifests=skills.component_manifests() if settings.skills.enabled else (),
            plugin_manifests=PluginManager(
                settings.extensions, store, settings.workspace,
            ).component_manifests(),
        )
        self.lineages = LineageManager(store, components, skills)

    def bootstrap(self) -> dict[str, Any]:
        lineage_manager = self.lineages
        lineage_manager.ensure_root()
        tasks = self.tasks()
        return {
            "app": "AIOS Agent Workbench", "version": "0.2",
            "workspace": self.settings.workspace.name,
            "workspace_path": str(self.settings.workspace),
            "model": self.settings.model.model,
            "provider": self.settings.model.provider,
            "pending_events": self.store.count_pending_events(),
            "runtime_state": self._runtime_state(tasks),
            "runtime_policy": self._runtime_policy(),
            "self_modification": {
                "enabled": self.settings.self_modification.enabled,
                "experiment_condition": self.settings.self_modification.experiment_condition,
                "current_version": self.self_versions.current_version()
                    if self.self_versions is not None else None,
            },
            "lineages": self.store.list_lineages(),
            "experimental_lineage_head": lineage_manager.current()["lineage_id"],
            "csrf_token": self.csrf_token,
            "tasks": tasks,
        }

    def tasks(self, limit: int = 80) -> list[dict[str, Any]]:
        return [self._task_summary(task) for task in self.store.list_tasks(limit)]

    def task_detail(self, task_id: int) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise WebUIError(404, f"Unknown task: {task_id}")
        checkpoints = self.store.task_checkpoints(task_id)
        cycle_ids = [
            str(item["data"].get("cycle_id")) for item in checkpoints
            if item.get("data", {}).get("cycle_id")
        ]
        traces = self.store.traces_for_cycles(list(dict.fromkeys(cycle_ids)))
        result = task.result or {}
        completion = self._completion_projection(task, result)
        return {
            "task": self._task_summary(task, include_request=True),
            "chat": self._chat(task.request, result, task.error),
            "activity": self._activity(traces, checkpoints),
            "budget": self._budget(result, checkpoints),
            "artifacts": self._artifacts(result),
            "evidence": self._evidence(result, traces, checkpoints),
            "completion": completion,
            "lineage": self.store.task_lineage(task_id),
            "evolution": self.evolution_runs(task_id),
        }

    def submit_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = str(payload.get("request", "")).strip()
        if not request:
            raise WebUIError(400, "request is required")
        if len(request) > 32_000:
            raise WebUIError(413, "request is too large")
        title = str(payload.get("title") or request[:120]).strip()
        priority = max(0, min(100, int(payload.get("priority", 50))))
        max_attempts = max(1, min(10, int(payload.get("max_attempts", 3))))
        lineage_id = str(payload.get("lineage_id", "")).strip()
        if lineage_id and self.settings.self_modification.enabled:
            raise WebUIError(
                400, "Experimental lineage binding is offline in minimal self-modification mode"
            )
        if lineage_id == "current":
            lineage_id = str(self.lineages.current()["lineage_id"])
        if lineage_id and self.store.get_lineage(lineage_id) is None:
            raise WebUIError(400, f"Unknown lineage: {lineage_id}")
        task = Task(title=title, request=request, priority=priority, max_attempts=max_attempts)
        task_id = self.store.create_task(task)
        if lineage_id:
            self.store.bind_task_lineage(task_id, lineage_id)
        event_id = self.store.add_event(
            Event("TASK_REQUEST", {"task_id": task_id, "message": request}, priority)
        )
        return {
            "task_id": task_id, "event_id": event_id, "status": "queued",
            "lineage": self.store.task_lineage(task_id),
        }

    def retry_task(self, task_id: int) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise WebUIError(404, f"Unknown task: {task_id}")
        if task.status.value not in TERMINAL:
            raise WebUIError(409, "Only a terminal task can be retried")
        event_id = self.store.retry_task(task_id)
        return {"task_id": task_id, "event_id": event_id, "status": "queued"}

    def evolution_runs(self, task_id: int | None = None) -> list[dict[str, Any]]:
        values = []
        marker = f"runtime:{task_id}" if task_id is not None else None
        eval_marker = f"runtime-eval:"
        for run in self.store.list_evolution_runs(100):
            report = run.get("report") if isinstance(run.get("report"), dict) else {}
            source_task = report.get("task_id") or report.get("source_task_id")
            if task_id is not None and not (
                source_task == task_id or run.get("trigger") == marker
                or (str(run.get("trigger", "")).startswith(eval_marker)
                    and report.get("source_task_id") == task_id)
            ):
                continue
            proposal = report.get("proposal") if isinstance(report.get("proposal"), dict) else {}
            final = proposal.get("final_disposition") if isinstance(proposal.get("final_disposition"), dict) else {}
            values.append({
                "id": run["id"], "trigger": run["trigger"], "status": run["status"],
                "created_at": run["created_at"],
                "candidate_id": report.get("candidate_id"),
                "model_intended_decision": proposal.get("model_intended_decision")
                    or proposal.get("rejected_invalid_patch_decision") or proposal.get("decision"),
                "effective_host_decision": proposal.get("decision") or final.get("action"),
                "reason": proposal.get("reason") or report.get("selection"),
                "causal_layer": proposal.get("causal_layer"),
            })
        return values

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        if re.fullmatch(r"rtc_[0-9a-f]{32}", candidate_id) is None:
            raise WebUIError(400, "Invalid candidate id")
        path = (self.settings.experiments_root / "runtime_candidates" / candidate_id / "candidate.json").resolve()
        expected = (self.settings.experiments_root / "runtime_candidates").resolve()
        if path.parent.parent != expected or not path.is_file():
            raise WebUIError(404, "Unknown candidate")
        metadata = json.loads(path.read_text(encoding="utf-8"))
        proposal = metadata.get("proposal") if isinstance(metadata.get("proposal"), dict) else {}
        edits = (proposal.get("patch") or {}).get("edits", [])
        diffs = []
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            old = str(edit.get("old_text", "")).splitlines()
            new = str(edit.get("new_text", "")).splitlines()
            diffs.append({"path": edit.get("path"), "old": old, "new": new})
        return {
            "candidate_id": candidate_id, "status": metadata.get("status"),
            "source_task_id": metadata.get("source_task_id"),
            "changed_paths": metadata.get("changed_paths", []),
            "proposal": proposal, "evaluation": metadata.get("evaluation"), "diffs": diffs,
        }

    def files(self) -> list[dict[str, Any]]:
        root = self.settings.workspace.resolve()
        result = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                continue
            result.append({
                "path": path.relative_to(root).as_posix(), "is_dir": path.is_dir(),
                "size": path.stat().st_size if path.is_file() else None,
            })
            if len(result) >= 500:
                break
        return result

    def file(self, relative: str) -> dict[str, Any]:
        root = self.settings.workspace.resolve()
        target = (root / unquote(relative)).resolve()
        if target != root and root not in target.parents:
            raise WebUIError(403, "Path escapes workspace")
        if not target.is_file() or target.is_symlink():
            raise WebUIError(404, "File not found")
        if target.stat().st_size > 256_000:
            raise WebUIError(413, "File is too large for inline preview")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WebUIError(415, "Binary file cannot be previewed") from exc
        return {"path": target.relative_to(root).as_posix(), "content": content}

    @staticmethod
    def _task_summary(task: Task, include_request: bool = False) -> dict[str, Any]:
        result = task.result or {}
        value = {
            "id": task.id, "title": task.title, "status": task.status.value,
            "attempts": task.attempts, "max_attempts": task.max_attempts,
            "summary": result.get("summary") or result.get("user_message"),
            "error": task.error, "created_at": task.created_at, "updated_at": task.updated_at,
        }
        if include_request:
            value["request"] = task.request
        return value

    @staticmethod
    def _runtime_state(tasks: list[dict[str, Any]]) -> str:
        if any(item["status"] == "running" for item in tasks):
            return "running"
        if any(item["status"] in {"queued", "retrying", "budget_deferred"} for item in tasks):
            return "waiting"
        return "idle"

    def _runtime_policy(self) -> dict[str, Any]:
        free = self.settings.runtime.completion_mode == "free"
        return {
            "mode": self.settings.runtime.completion_mode,
            "label": "FREE LOOP" if free else "VERIFIED LOOP",
            "online_verifier": not free,
            "description": (
                "Agent stop/yield is recorded without a Host completion judgment"
                if free else
                "Host Verifier decides completed, degraded, or rejected"
            ),
        }

    def _completion_projection(self, task: Task, result: dict[str, Any]) -> dict[str, Any]:
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        verification = (
            evidence.get("verification")
            if isinstance(evidence.get("verification"), dict) else {}
        )
        recorded_free = (
            evidence.get("online_verifier_enabled") is False
            or verification.get("mode") == "disabled"
            or task.status in {TaskStatus.STOPPED, TaskStatus.YIELDED, TaskStatus.ABANDONED}
        )
        recorded_verified = (
            isinstance(verification.get("passed"), bool)
            or task.status in {TaskStatus.COMPLETED, TaskStatus.DEGRADED, TaskStatus.DEAD_LETTER}
        )
        mode = "free" if recorded_free else "verified" if recorded_verified else self.settings.runtime.completion_mode
        status_meanings = {
            TaskStatus.STOPPED: "Agent declared stop; true completion was not judged",
            TaskStatus.YIELDED: "Agent yielded without declaring completion",
            TaskStatus.ABANDONED: "Runtime stopped investing after execution or protocol exhaustion",
            TaskStatus.COMPLETED: "Host Verifier accepted completion",
            TaskStatus.DEGRADED: "Host Verifier observed only partial or substitute satisfaction",
            TaskStatus.DEAD_LETTER: "Verified Runtime exhausted retries after rejection or failure",
        }
        return {
            "mode": mode,
            "label": "FREE LOOP" if mode == "free" else "VERIFIED LOOP",
            "online_verifier": mode == "verified",
            "task_status": task.status.value,
            "status_meaning": status_meanings.get(task.status, "Task is not at a terminal boundary"),
            "agent_declared_stop": evidence.get("agent_declared_stop"),
            "host_observed_completion": evidence.get("host_observed_completion"),
            "verifier_pass": verification.get("passed"),
            "true_completion_known": isinstance(evidence.get("success"), bool),
        }

    @staticmethod
    def _chat(request: str, result: dict[str, Any], error: str | None) -> list[dict[str, Any]]:
        values = [{"role": "user", "content": request}]
        final = result.get("final_output") or result.get("summary") or result.get("user_message")
        if final:
            values.append({"role": "assistant", "content": str(final)})
        elif error:
            values.append({"role": "system", "content": error})
        return values

    @staticmethod
    def _activity(traces: list[dict[str, Any]], checkpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
        attempt_by_cycle = {}
        for checkpoint in checkpoints:
            data = checkpoint.get("data", {})
            if data.get("cycle_id"):
                attempt_by_cycle[str(data["cycle_id"])] = data.get("attempt") or data.get("task_cycle")
        values = []
        for trace in traces:
            kind, data = trace["kind"], trace.get("data", {})
            actor = "RUNTIME"
            if any(word in kind for word in ("model", "plan", "controller")):
                actor = "MODEL"
            if any(word in kind for word in ("verification", "terminal", "arbiter", "provenance", "capability")):
                actor = "HOST"
            if any(word in kind for word in ("gate", "evaluation")):
                actor = "GATE"
            label = kind.replace("_", " ").title()
            if kind == "action_result":
                label = f"{data.get('tool', 'tool')} · {'succeeded' if data.get('ok') else 'failed'}"
            detail = data.get("error") or data.get("reason") or data.get("summary")
            values.append({
                "id": trace["id"], "cycle_id": trace["cycle_id"], "kind": kind,
                "actor": actor, "label": label, "detail": str(detail)[:500] if detail else None,
                "ok": data.get("ok"), "attempt": attempt_by_cycle.get(trace["cycle_id"]),
                "created_at": trace["created_at"],
            })
        return values

    def _budget(self, result: dict[str, Any], checkpoints: list[dict[str, Any]]) -> dict[str, Any]:
        budget = result.get("task_budget", result.get("budget", {}))
        budget = budget if isinstance(budget, dict) else {}
        for checkpoint in reversed(checkpoints):
            candidate = checkpoint.get("data", {}).get("budget")
            if isinstance(candidate, dict):
                budget = {**candidate, **budget}
                break
        evidence = result.get("evidence", {})
        enabled = evidence.get("budget_limits_enabled", budget.get(
            "enabled", True if result else self.settings.budget.enabled,
        ))
        return {
            "enabled": enabled,
            "tokens": evidence.get("model_tokens", budget.get("used_tokens", budget.get("tokens_used") or budget.get("total_tokens") or 0)),
            "token_limit": budget.get("max_tokens", self.settings.budget.max_tokens_per_task) if enabled else None,
            "model_calls": evidence.get("model_api_calls", budget.get("used_model_calls", budget.get("model_calls_used") or budget.get("model_calls") or 0)),
            "model_call_limit": budget.get("max_model_calls", self.settings.budget.max_model_calls_per_task) if enabled else None,
            "tool_calls": evidence.get("task_tool_calls", budget.get("used_tool_calls", budget.get("tool_calls_used") or budget.get("tool_calls") or 0)),
            "tool_call_limit": budget.get("max_tool_calls", self.settings.budget.max_tool_calls_per_task) if enabled else None,
            "cycles": len({c.get("data", {}).get("cycle_id") for c in checkpoints if c.get("data", {}).get("cycle_id")}),
            "cycle_limit": budget.get("max_cycles", self.settings.budget.max_cycles_per_task) if enabled else None,
        }

    @staticmethod
    def _artifacts(result: dict[str, Any]) -> list[str]:
        found: list[str] = []
        for value in result.get("artifacts", []):
            if isinstance(value, str):
                found.append(value)
            elif isinstance(value, dict) and value.get("path"):
                found.append(str(value["path"]))
        for action in result.get("actions", []):
            if isinstance(action, dict) and action.get("tool") in {"write", "edit"}:
                path = (action.get("arguments") or {}).get("path")
                if path:
                    found.append(str(path))
        return list(dict.fromkeys(found))

    @staticmethod
    def _evidence(result: dict[str, Any], traces: list[dict[str, Any]], checkpoints: list[dict[str, Any]]) -> dict[str, Any]:
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        return {
            "success": evidence.get("success"),
            "verification": evidence.get("verification"),
            "claims": evidence.get("claims") or result.get("claims") or [],
            "trace_count": len(traces), "checkpoint_count": len(checkpoints),
            "cycle_ids": list(dict.fromkeys(t["cycle_id"] for t in traces)),
        }


class AIOSRequestHandler(BaseHTTPRequestHandler):
    server_version = "AIOSWorkbench/0.1"

    @property
    def app(self) -> AIOSWebApplication:
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/bootstrap":
                return self._json(self.app.bootstrap())
            if parsed.path == "/api/tasks":
                return self._json(self.app.tasks())
            if match := re.fullmatch(r"/api/tasks/(\d+)", parsed.path):
                return self._json(self.app.task_detail(int(match.group(1))))
            if parsed.path == "/api/evolution":
                query = parse_qs(parsed.query)
                task_id = int(query["task_id"][0]) if query.get("task_id") else None
                return self._json(self.app.evolution_runs(task_id))
            if match := re.fullmatch(r"/api/candidates/(rtc_[0-9a-f]{32})", parsed.path):
                return self._json(self.app.candidate(match.group(1)))
            if parsed.path == "/api/files":
                return self._json(self.app.files())
            if parsed.path == "/api/file":
                query = parse_qs(parsed.query)
                return self._json(self.app.file(query.get("path", [""])[0]))
            self._asset(parsed.path)
        except WebUIError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json({"error": f"Invalid request: {exc}"}, 400)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not secrets.compare_digest(self.headers.get("X-AIOS-CSRF", ""), self.app.csrf_token):
                raise WebUIError(403, "Invalid CSRF token")
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65_536:
                raise WebUIError(413, "Request body is too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise WebUIError(400, "JSON object required")
            parsed = urlparse(self.path)
            if parsed.path == "/api/tasks":
                return self._json(self.app.submit_task(payload), 201)
            if match := re.fullmatch(r"/api/tasks/(\d+)/retry", parsed.path):
                return self._json(self.app.retry_task(int(match.group(1))))
            raise WebUIError(404, "Unknown endpoint")
        except WebUIError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json({"error": f"Invalid request: {exc}"}, 400)

    def _asset(self, path: str) -> None:
        names = {"/": "index.html", "/app.css": "app.css", "/app.js": "app.js"}
        name = names.get(path)
        if name is None:
            raise WebUIError(404, "Not found")
        data = (self.app.assets / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, value: Any, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'")

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve_ui(settings: Settings, store: StateStore, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    app = AIOSWebApplication(settings, store)
    server = ThreadingHTTPServer((host, port), AIOSRequestHandler)
    server.app = app  # type: ignore[attr-defined]
    print(f"AIOS Agent Workbench: http://{host}:{server.server_port}")
    print("Run `python -m aios --config config.json run` in another terminal to process queued tasks.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
