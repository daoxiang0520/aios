from __future__ import annotations

import argparse
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .config import Settings
from .capabilities import CapabilityRegistry
from .diagnostics import Diagnoser
from .evolution import EvolutionManager
from .evaluation import Verifier
from .plugins import PluginManager
from .sandbox import DockerSandboxBroker
from .skills import SkillManager
from .runtime import AIOSRuntime
from .storage import StateStore
from .types import Action, ActionResult, Event, Goal, GoalStatus, GoalType, Memory, MemoryType, Task, TaskStatus


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aios", description="Self-Evolving AIOS MVP")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Create config, database and workspace")

    goal = commands.add_parser("goal", help="Manage goals")
    goal_commands = goal.add_subparsers(dest="goal_command", required=True)
    add_goal = goal_commands.add_parser("add")
    add_goal.add_argument("title")
    add_goal.add_argument("--type", choices=[item.value for item in GoalType], default="user")
    add_goal.add_argument("--priority", type=int, default=50)
    goal_commands.add_parser("list")
    complete_goal = goal_commands.add_parser("complete")
    complete_goal.add_argument("id", type=int)

    event = commands.add_parser("event", help="Manage events")
    event_commands = event.add_subparsers(dest="event_command", required=True)
    emit = event_commands.add_parser("emit")
    emit.add_argument("type")
    emit.add_argument("--message")
    emit.add_argument("--payload", help="JSON object")
    emit.add_argument("--priority", type=int, default=50)

    run = commands.add_parser("run", help="Run the event loop")
    run.add_argument("--once", action="store_true")
    commands.add_parser("status", help="Show queue and goals")
    trace = commands.add_parser("trace", help="Show recent traces")
    trace.add_argument("--limit", type=int, default=20)

    task = commands.add_parser("task", help="Submit and inspect durable tasks")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    submit = task_commands.add_parser("submit")
    submit.add_argument("request")
    submit.add_argument("--title")
    submit.add_argument("--priority", type=int, default=50)
    submit.add_argument("--max-attempts", type=int, default=3)
    task_list = task_commands.add_parser("list")
    task_list.add_argument("--status", choices=[item.value for item in TaskStatus])
    task_list.add_argument("--limit", type=int, default=50)
    task_show = task_commands.add_parser("show")
    task_show.add_argument("id", type=int)
    task_retry = task_commands.add_parser("retry")
    task_retry.add_argument("id", type=int)
    task_reconcile = task_commands.add_parser("reconcile")
    task_reconcile.add_argument("id", type=int)

    result = commands.add_parser("result", help="Inspect the result inbox")
    result_commands = result.add_subparsers(dest="result_command", required=True)
    result_list = result_commands.add_parser("list")
    result_list.add_argument("--limit", type=int, default=20)
    result_show = result_commands.add_parser("show")
    result_show.add_argument("id", type=int)
    result_answer = result_commands.add_parser("answer")
    result_answer.add_argument("id", type=int)

    memory = commands.add_parser("memory", help="Manage persistent memories")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_add = memory_commands.add_parser("add")
    memory_add.add_argument("type", choices=[item.value for item in MemoryType])
    memory_add.add_argument("content")
    memory_add.add_argument("--key")
    memory_add.add_argument("--importance", type=float, default=0.5)
    memory_list = memory_commands.add_parser("list")
    memory_list.add_argument("--type", choices=[item.value for item in MemoryType])
    memory_list.add_argument("--limit", type=int, default=50)

    dead = commands.add_parser("dead-letter", help="Inspect exhausted tasks")
    dead_commands = dead.add_subparsers(dest="dead_command", required=True)
    dead_list = dead_commands.add_parser("list")
    dead_list.add_argument("--limit", type=int, default=50)
    commands.add_parser("diagnose", help="Summarize traces and failure patterns")

    evolution = commands.add_parser("evolution", help="Manage constrained Harness candidates")
    evolution_commands = evolution.add_subparsers(dest="evolution_command", required=True)
    propose = evolution_commands.add_parser("propose")
    propose.add_argument("--rationale", required=True)
    propose.add_argument("--prompt-append")
    propose.add_argument("--max-actions", type=int)
    propose.add_argument("--memory-chars", type=int)
    evolution_commands.add_parser("list")
    benchmark = evolution_commands.add_parser("benchmark")
    benchmark.add_argument("id", type=int)
    promote = evolution_commands.add_parser("promote")
    promote.add_argument("id", type=int)
    promote.add_argument("--approve", action="store_true")
    evolution_commands.add_parser("versions")
    evolution_runs = evolution_commands.add_parser("runs")
    evolution_runs.add_argument("--limit", type=int, default=50)
    evolution_commands.add_parser("tools")
    rollback = evolution_commands.add_parser("rollback")
    rollback.add_argument("version", type=int)
    rollback.add_argument("--approve", action="store_true")

    skill = commands.add_parser("skill", help="Manage versioned sandbox skills")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    skill_commands.add_parser("list")
    skill_commands.add_parser("candidates")
    skill_show = skill_commands.add_parser("show")
    skill_show.add_argument("name")
    skill_versions = skill_commands.add_parser("versions")
    skill_versions.add_argument("name")
    skill_propose = skill_commands.add_parser("propose")
    skill_propose.add_argument("--manifest", required=True, help="Path to manifest.json")
    skill_propose.add_argument("--source", required=True, help="Path to skill.py")
    skill_benchmark = skill_commands.add_parser("benchmark")
    skill_benchmark.add_argument("candidate_id")
    skill_promote = skill_commands.add_parser("promote")
    skill_promote.add_argument("candidate_id")
    skill_promote.add_argument("--approve", action="store_true")
    skill_rollback = skill_commands.add_parser("rollback")
    skill_rollback.add_argument("name")
    skill_rollback.add_argument("--approve", action="store_true")
    skill_deprecate = skill_commands.add_parser("deprecate")
    skill_deprecate.add_argument("name")
    skill_deprecate.add_argument("--approve", action="store_true")
    skill_commands.add_parser("bootstrap")
    return parser


def _load(path: str) -> tuple[Settings, StateStore]:
    settings = Settings.load(path)
    settings.ensure_directories()
    store = StateStore(settings.database)
    store.initialize()
    return settings, store


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _skill_services(settings: Settings) -> tuple[SkillManager, DockerSandboxBroker, CapabilityRegistry]:
    manager = SkillManager(settings.skills_root, settings.skills)
    if settings.skills.enabled:
        manager.bootstrap_builtins()
    broker = DockerSandboxBroker(settings.sandbox_root, settings.sandbox, manager.runtime)
    capabilities = CapabilityRegistry.default(
        sandbox_available=broker.available(),
        network_enabled=settings.capabilities.network_enabled,
        allowed_domains=settings.capabilities.allowed_domains,
    )
    return manager, broker, capabilities


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "init":
        target = Path(args.config).resolve()
        if not target.exists():
            template = Path(__file__).resolve().parents[2] / "config.example.json"
            if template.exists():
                shutil.copyfile(template, target)
            else:
                target.write_text(json.dumps(_default_config(), indent=2), encoding="utf-8")
        settings, _ = _load(str(target))
        _skill_services(settings)
        print(f"Initialized AIOS at {settings.root}")
        return 0

    settings, store = _load(args.config)
    if args.command == "goal":
        if args.goal_command == "add":
            goal_id = store.add_goal(Goal(args.title, GoalType(args.type), args.priority))
            print(f"Goal created: {goal_id}")
        elif args.goal_command == "list":
            _print_json([_goal_dict(goal) for goal in store.list_goals()])
        elif args.goal_command == "complete":
            store.update_goal_status(args.id, GoalStatus.COMPLETED)
            print(f"Goal completed: {args.id}")
        return 0

    if args.command == "event" and args.event_command == "emit":
        payload = json.loads(args.payload) if args.payload else {}
        if not isinstance(payload, dict):
            raise SystemExit("--payload must be a JSON object")
        if args.message:
            payload["message"] = args.message
        event_id = store.add_event(Event(args.type.upper(), payload, args.priority))
        print(f"Event queued: {event_id}")
        return 0

    if args.command == "run":
        runtime = AIOSRuntime(settings)
        if args.once:
            worked = runtime.run_once()
            print("Processed one cycle" if worked else "No pending events")
        else:
            runtime.run_forever()
        return 0

    if args.command == "status":
        skill_manager, _, skill_capabilities = _skill_services(settings)
        _print_json(
            {
                "pending_events": store.count_pending_events(),
                "active_goals": [_goal_dict(goal) for goal in store.list_goals(active_only=True)],
                "database": str(settings.database),
                "workspace": str(settings.workspace),
                "autonomous_evolution": settings.evolution.enabled,
                "active_generated_tools": [
                    plugin.name
                    for plugin in PluginManager(settings.extensions, store, settings.workspace).active_plugins()
                ],
                "active_skills": [
                    item["name"]
                    for item in skill_manager.catalog(skill_capabilities)
                ],
            }
        )
        return 0

    if args.command == "trace":
        _print_json(store.recent_traces(args.limit))
        return 0
    if args.command == "task":
        if args.task_command == "submit":
            task = Task(
                title=args.title or args.request[:120],
                request=args.request,
                priority=args.priority,
                max_attempts=max(1, args.max_attempts),
            )
            task_id = store.create_task(task)
            event_id = store.add_event(
                Event("TASK_REQUEST", {"task_id": task_id, "message": task.request}, task.priority)
            )
            _print_json({"task_id": task_id, "event_id": event_id, "status": "queued"})
        elif args.task_command == "list":
            status = TaskStatus(args.status) if args.status else None
            _print_json([_task_dict(task) for task in store.list_tasks(args.limit, status)])
        elif args.task_command == "show":
            task = store.get_task(args.id)
            if task is None:
                raise SystemExit(f"Unknown task: {args.id}")
            data = _task_dict(task)
            data["checkpoints"] = store.task_checkpoints(args.id)
            _print_json(data)
        elif args.task_command == "retry":
            event_id = store.retry_task(args.id)
            _print_json({"task_id": args.id, "event_id": event_id, "status": "queued"})
        elif args.task_command == "reconcile":
            task = store.get_task(args.id)
            if task is None:
                raise SystemExit(f"Unknown task: {args.id}")
            if not task.result:
                raise SystemExit(f"Task {args.id} has no recorded result to reconcile")
            actions = [Action(**item) for item in task.result.get("actions", [])]
            results = [ActionResult(**item) for item in task.result.get("action_results", [])]
            evidence = task.result.get("evidence", {})
            verification = Verifier().verify(
                actions,
                results,
                planned_count=int(evidence.get("planned_actions", len(actions))),
                task_done=False,
                request=task.request,
            )
            if not verification["passed"]:
                _print_json({"task_id": task.id, "reconciled": False, "verification": verification})
                return 1
            task.result.setdefault("evidence", {})["verification"] = verification
            task.result["evidence"]["success"] = True
            store.update_task(int(task.id), TaskStatus.COMPLETED, result=task.result)
            store.add_checkpoint(int(task.id), "reconciled", {"verification": verification})
            _print_json({"task_id": task.id, "reconciled": True, "status": "completed"})
        return 0
    if args.command == "result":
        if args.result_command == "list":
            tasks = store.list_tasks(args.limit)
            _print_json(
                [_task_summary_dict(task) for task in tasks if task.result is not None or task.status in {TaskStatus.COMPLETED, TaskStatus.DEAD_LETTER}]
            )
        elif args.result_command == "show":
            task = store.get_task(args.id)
            if task is None:
                raise SystemExit(f"Unknown task: {args.id}")
            _print_json({"task_id": task.id, "status": task.status.value, "result": task.result, "error": task.error})
        elif args.result_command == "answer":
            task = store.get_task(args.id)
            if task is None:
                raise SystemExit(f"Unknown task: {args.id}")
            if task.result and task.result.get("final_output"):
                print(task.result["final_output"])
            elif task.error:
                print(f"Task {task.id} {task.status.value}: {task.error}")
            else:
                print(f"Task {task.id} has no final answer yet (status={task.status.value})")
        return 0
    if args.command == "memory":
        if args.memory_command == "add":
            memory_id = store.add_memory(
                Memory(
                    MemoryType(args.type),
                    args.content,
                    args.key,
                    max(0.0, min(1.0, args.importance)),
                )
            )
            print(f"Memory created: {memory_id}")
        elif args.memory_command == "list":
            type = MemoryType(args.type) if args.type else None
            _print_json([_memory_dict(memory) for memory in store.list_memories(args.limit, type)])
        return 0
    if args.command == "dead-letter" and args.dead_command == "list":
        _print_json(store.list_dead_letters(args.limit))
        return 0
    if args.command == "diagnose":
        _print_json(Diagnoser(store).report())
        return 0
    if args.command == "evolution":
        manager = EvolutionManager(store)
        if args.evolution_command == "propose":
            mutation = {}
            if args.prompt_append is not None:
                mutation["prompt_append"] = args.prompt_append
            if args.max_actions is not None:
                mutation["max_actions_per_cycle"] = args.max_actions
            if args.memory_chars is not None:
                mutation["memory_context_characters"] = args.memory_chars
            candidate_id = manager.propose(mutation, args.rationale)
            _print_json({"candidate_id": candidate_id, "status": "proposed", "mutation": mutation})
        elif args.evolution_command == "list":
            _print_json(store.list_candidates())
        elif args.evolution_command == "benchmark":
            _print_json(manager.benchmark(args.id))
        elif args.evolution_command == "promote":
            _print_json(manager.promote(args.id, approved=args.approve))
        elif args.evolution_command == "versions":
            _print_json(store.list_harness_versions())
        elif args.evolution_command == "runs":
            _print_json(store.list_evolution_runs(args.limit))
        elif args.evolution_command == "tools":
            plugins = PluginManager(settings.extensions, store, settings.workspace)
            _print_json([plugin.as_dict() for plugin in plugins.active_plugins()])
        elif args.evolution_command == "rollback":
            _print_json(manager.rollback(args.version, approved=args.approve))
        return 0
    if args.command == "skill":
        manager, broker, capabilities = _skill_services(settings)
        if args.skill_command == "list":
            _print_json(manager.catalog(capabilities))
        elif args.skill_command == "candidates":
            _print_json(manager.list_candidates())
        elif args.skill_command == "show":
            match = next((item for item in manager.catalog(capabilities) if item["name"] == args.name), None)
            if match is None:
                raise SystemExit(f"Unknown active skill: {args.name}")
            _print_json(match)
        elif args.skill_command == "versions":
            _print_json(manager.versions(args.name))
        elif args.skill_command == "propose":
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            source = Path(args.source).read_text(encoding="utf-8")
            _print_json(manager.propose(manifest, source))
        elif args.skill_command == "benchmark":
            _print_json(manager.benchmark(args.candidate_id, broker))
        elif args.skill_command == "promote":
            _print_json(manager.promote(args.candidate_id, approved=args.approve))
        elif args.skill_command == "rollback":
            _print_json(manager.rollback(args.name, approved=args.approve))
        elif args.skill_command == "deprecate":
            _print_json(manager.deprecate(args.name, approved=args.approve))
        elif args.skill_command == "bootstrap":
            manager.bootstrap_builtins()
            _print_json(manager.catalog(capabilities))
        return 0
    return 2


def _goal_dict(goal: Goal) -> dict[str, Any]:
    return {
        "id": goal.id,
        "title": goal.title,
        "type": goal.type.value,
        "priority": goal.priority,
        "status": goal.status.value,
        "metadata": goal.metadata,
        "created_at": goal.created_at,
    }


def _task_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "request": task.request,
        "priority": task.priority,
        "status": task.status.value,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "result": task.result,
        "error": task.error,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _task_summary_dict(task: Task) -> dict[str, Any]:
    result = task.result or {}
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status.value,
        "attempts": task.attempts,
        "summary": result.get("summary"),
        "final_output": result.get("final_output"),
        "error": task.error,
        "updated_at": task.updated_at,
    }


def _memory_dict(memory: Memory) -> dict[str, Any]:
    return {
        "id": memory.id,
        "type": memory.type.value,
        "key": memory.key,
        "content": memory.content,
        "importance": memory.importance,
        "metadata": memory.metadata,
        "created_at": memory.created_at,
    }


def _default_config() -> dict[str, Any]:
    return {
        "database": "./data/aios.db",
        "workspace": "./workspace",
        "model": {"provider": "mock"},
        "permissions": {"allowed_tools": ["read", "write", "edit", "bash"]},
        "skills": {
            "enabled": True,
            "root": "./skills",
            "require_human_promotion": True,
        },
        "evolution": {"enabled": False, "auto_promote": False},
    }


if __name__ == "__main__":
    raise SystemExit(main())
