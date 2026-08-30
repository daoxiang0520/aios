from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .answers import CanonicalAnswer
from .config import Settings
from .capabilities import CapabilityRegistry, EvidenceContract
from .controller import LLMController
from .diagnostics import Diagnoser
from .evolution import EvolutionManager
from .evaluation import Verifier
from .experiments import (
    CapsuleManager, ExperimentOrchestrator, ExperimentVariant, ModelSemanticJudge,
    PairwiseSemanticJudge, RuntimeVariantRunner,
)
from .plugins import PluginManager
from .sandbox import DockerSandboxBroker
from .skills import SkillManager
from .runtime import AIOSRuntime
from .self_evolution import ExperienceAnalyzer, ModelEvolutionReasoner, SelfEvolutionLoop
from .runtime_evolution import (
    ExternalRuntimeEvaluator, ModelRuntimeMutationReasoner, RuntimeCandidateManager,
    RuntimeDiagnosisBenchmark,
)
from .runtime_provenance import RuntimeProvenanceManager
from .situation import SituationResolver, normalize_resource_path
from .storage import StateStore
from .types import Action, ActionResult, Event, Goal, GoalStatus, GoalType, Memory, MemoryType, Task, TaskStatus
from .utility import SkillUtilityEvaluator


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
    ui = commands.add_parser("ui", help="Open the local Agent Workbench")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
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
    evolution_auto = evolution_commands.add_parser(
        "auto-run", help="Run the slow self-evolution loop without production activation",
    )
    evolution_auto.add_argument("--capsule", action="append", default=[])
    evolution_auto.add_argument("--runs", type=int)
    evolution_auto.add_argument("--task-limit", type=int, default=100)
    evolution_auto.add_argument("--trace-limit", type=int, default=1000)
    runtime_observe = evolution_commands.add_parser(
        "runtime-observe", help="Build a fact-only cross-layer Runtime experience capsule",
    )
    runtime_observe.add_argument("task_id", type=int)
    runtime_propose = evolution_commands.add_parser(
        "runtime-propose", help="Let the model diagnose and patch an isolated Runtime candidate",
    )
    runtime_propose.add_argument("task_id", type=int)
    runtime_benchmark = evolution_commands.add_parser(
        "runtime-benchmark", help="Score a completed Runtime experiment against blind historical annotations",
    )
    runtime_benchmark.add_argument("task_id", type=int)
    runtime_benchmark_suite = evolution_commands.add_parser(
        "runtime-benchmark-suite", help="Measure diagnosis/localization/mutation/gate stages separately",
    )
    runtime_benchmark_suite.add_argument("--task-id", action="append", type=int, default=[])
    runtime_provenance = evolution_commands.add_parser(
        "runtime-provenance", help="Inspect failure-time source bindings and repair eligibility",
    )
    runtime_provenance.add_argument("task_id", type=int)
    evolution_commands.add_parser("runtime-list")
    runtime_show = evolution_commands.add_parser("runtime-show")
    runtime_show.add_argument("candidate_id")
    runtime_evaluate = evolution_commands.add_parser(
        "runtime-evaluate", help="Run Host-owned immutable gates against a Runtime candidate",
    )
    runtime_evaluate.add_argument("candidate_id")
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
    skill_telemetry = skill_commands.add_parser("telemetry")
    skill_telemetry.add_argument("--name")
    skill_telemetry.add_argument("--limit", type=int, default=50)
    skill_replay = skill_commands.add_parser("replay")
    skill_replay.add_argument("candidate_id")
    skill_replay.add_argument("--runs", type=int, default=3)
    skill_compare = skill_commands.add_parser("compare")
    skill_compare.add_argument("candidate_id")
    skill_utility = skill_commands.add_parser("utility")
    skill_utility.add_argument("name")
    skill_utility.add_argument("--limit", type=int, default=500)
    skill_counterfactual = skill_commands.add_parser("counterfactual-replay")
    skill_counterfactual.add_argument("candidate_id")
    skill_counterfactual.add_argument("--capsule", required=True)
    skill_counterfactual.add_argument("--runs", type=int)
    skill_commands.add_parser("bootstrap")

    capsule = commands.add_parser("capsule", help="Manage immutable task execution capsules")
    capsule_commands = capsule.add_subparsers(dest="capsule_command", required=True)
    capsule_capture = capsule_commands.add_parser("capture")
    capsule_capture.add_argument("task_id", type=int)
    capsule_commands.add_parser("list")
    capsule_show = capsule_commands.add_parser("show")
    capsule_show.add_argument("capsule_id")
    capsule_verify = capsule_commands.add_parser("verify")
    capsule_verify.add_argument("capsule_id")
    capsule_fork = capsule_commands.add_parser("fork")
    capsule_fork.add_argument("capsule_id")
    capsule_archive = capsule_commands.add_parser("archive")
    capsule_archive.add_argument("capsule_id")
    capsule_delete = capsule_commands.add_parser("delete")
    capsule_delete.add_argument("capsule_id")

    experiment = commands.add_parser("experiment", help="Run paired counterfactual experiments")
    experiment_commands = experiment.add_subparsers(dest="experiment_command", required=True)
    experiment_run = experiment_commands.add_parser("run")
    experiment_run.add_argument("--capsule", required=True)
    experiment_run.add_argument("--baseline", default="primitive", choices=["primitive"])
    experiment_run.add_argument("--candidate", required=True, help="skill:<candidate_id>")
    experiment_run.add_argument("--runs", type=int)
    experiment_show = experiment_commands.add_parser("show")
    experiment_show.add_argument("experiment_id")
    experiment_compare = experiment_commands.add_parser("compare")
    experiment_compare.add_argument("experiment_id")
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
    broker = DockerSandboxBroker(
        settings.sandbox_root, settings.sandbox, manager.runtime,
        network_enabled=settings.capabilities.network_enabled,
    )
    capabilities = CapabilityRegistry.default(
        sandbox_available=broker.available(),
        network_enabled=settings.capabilities.network_enabled,
        allowed_domains=settings.capabilities.allowed_domains,
        http_read_available=broker.available(),
    )
    return manager, broker, capabilities


def _experiment_services(
    settings: Settings, store: StateStore,
) -> tuple[SkillManager, CapsuleManager, RuntimeVariantRunner]:
    manager, _, capabilities = _skill_services(settings)
    capsules = CapsuleManager(settings, store, manager, capabilities)
    return manager, capsules, RuntimeVariantRunner(settings, manager)


def _run_counterfactual(
    settings: Settings, store: StateStore, candidate_id: str, capsule_id: str, runs: int | None,
) -> dict[str, Any]:
    manager, capsules, runner = _experiment_services(settings, store)
    benchmark_path = manager.reports / f"{candidate_id}.json"
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8")) if benchmark_path.is_file() else {}
    if not benchmark.get("passed"):
        raise SystemExit("Candidate must pass the Docker benchmark before counterfactual replay")
    semantic = PairwiseSemanticJudge(
        ModelSemanticJudge(settings.model) if settings.experiments.semantic_judge_enabled else None
    )
    report = ExperimentOrchestrator(store, capsules, runner, semantic_judge=semantic).run(
        capsule_id,
        ExperimentVariant("baseline", mutation={}),
        ExperimentVariant("candidate", mutation={"candidate_id": candidate_id}),
        runs_per_variant=runs or settings.experiments.default_runs_per_variant,
        keep_worlds=settings.experiments.keep_worlds,
    )
    (manager.reports / f"{candidate_id}.counterfactual.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _historical_contract_evidence(
    store: StateStore, task_id: int, contract: EvidenceContract,
) -> list[dict[str, Any]]:
    cycle_ids = list(dict.fromkeys(
        str(checkpoint["data"]["cycle_id"])
        for checkpoint in store.task_checkpoints(task_id)
        if checkpoint["data"].get("cycle_id")
    ))
    traces = store.traces_for_cycles(cycle_ids)
    plans: dict[tuple[str, int], list[Action]] = {}
    results: dict[tuple[str, int], list[tuple[int, ActionResult]]] = {}
    for trace in traces:
        data = trace["data"]
        round_number = int(data.get("round", 0))
        key = (trace["cycle_id"], round_number)
        if trace["kind"] == "plan_created":
            plans[key] = [Action(**item) for item in data.get("actions", [])]
        elif trace["kind"] == "action_result":
            payload = {name: value for name, value in data.items() if name != "round"}
            results.setdefault(key, []).append((int(trace["id"]), ActionResult(**payload)))
    ledger: list[dict[str, Any]] = []
    for key, actions in plans.items():
        for action, (trace_id, result) in zip(actions, results.get(key, []), strict=False):
            for item in Verifier.collect_evidence(
                contract, action, result, f"trace:{trace_id}",
            ):
                ledger[:] = [
                    previous for previous in ledger
                    if not (
                        previous.get("kind") == item["kind"]
                        and previous.get("value") == item["value"]
                    )
                ]
                ledger.append(item)
    return ledger


def main(argv: list[str] | None = None) -> int:
    _configure_windows_stdio()
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
            contract = EvidenceContract.from_request(
                task.request, Verifier._expected_artifacts(task.request),
            )
            canonical = CanonicalAnswer.bind(
                str(task.result.get("summary") or task.result.get("user_message") or ""),
                actions, results, contract,
            )
            coverage = SituationResolver.assess_coverage(
                task.result.get("situation_map", {}), canonical.body,
            )
            if coverage.get("required") and not coverage.get("passed"):
                _print_json({
                    "task_id": task.id, "reconciled": False,
                    "reason": "canonical answer still fails semantic coverage",
                    "coverage_assessment": coverage,
                })
                return 1
            recovered_artifacts: list[str] = []
            workspace_root = settings.workspace.resolve()
            for action, result in zip(actions, results, strict=False):
                if action.tool not in {"write", "write_file"} or not result.ok:
                    continue
                content = action.arguments.get("content")
                relative = normalize_resource_path(action.arguments.get("path", ""))
                if not relative or not isinstance(content, str):
                    continue
                target = (workspace_root / relative).resolve()
                try:
                    target.relative_to(workspace_root)
                except ValueError:
                    continue
                if not target.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                    recovered_artifacts.append(relative)
                if isinstance(result.output, dict):
                    result.output["path"] = str(target)
            prior_verification = evidence.get("verification")
            established_evidence = _historical_contract_evidence(
                store, int(task.id), contract,
            )
            prior_done = any(
                check.get("name") == "task_declared_done" and check.get("passed")
                for check in (prior_verification or {}).get("checks", [])
                if isinstance(check, dict)
            )
            verification = Verifier().verify(
                actions,
                results,
                planned_count=int(evidence.get("planned_actions", len(actions))),
                task_done=prior_done,
                request=task.request,
                contract=contract,
                final_output=canonical.body,
                completion_metadata=evidence.get("completion_metadata"),
                coverage_assessment=coverage,
                established_evidence=established_evidence,
            )
            if not verification["passed"]:
                _print_json({"task_id": task.id, "reconciled": False, "verification": verification})
                return 1
            canonical.mark_committed()
            task.result["user_message"] = canonical.user_message
            task.result["artifacts"] = [
                {"path": item.path, "role": item.role, "content_ref": item.content_ref,
                 "content_digest": item.content_digest, "state": item.state}
                for item in canonical.artifacts
            ]
            task.result["canonical_answer"] = canonical.as_dict()
            task.result["action_results"] = [asdict(item) for item in results]
            if recovered_artifacts:
                committed = list(task.result.get("committed_files", []))
                task.result["committed_files"] = list(dict.fromkeys([
                    *committed, *recovered_artifacts,
                ]))
                if len(recovered_artifacts) == 1:
                    task.result["final_output"] = str(
                        (workspace_root / recovered_artifacts[0]).resolve()
                    )
            task.result.setdefault("evidence", {})["previous_verification"] = prior_verification
            task.result["evidence"]["verification"] = verification
            task.result["evidence"]["coverage_assessment"] = coverage
            task.result["evidence"]["established_evidence"] = established_evidence
            task.result["evidence"]["success"] = True
            task.result["reverification"] = {
                "reason": "runtime_fix",
                "previous_outcome": task.status.value,
                "new_outcome": "completed",
                "model_calls": 0,
                "recovered_artifacts": recovered_artifacts,
            }
            prior_failed_checks = {
                item.get("name") for item in (prior_verification or {}).get("checks", [])
                if isinstance(item, dict) and not item.get("passed")
            }
            evidence_persistence_failure = bool(
                prior_failed_checks & {"network_request", "source_domain"}
                and established_evidence
            )
            task.result["experience_validity"] = {
                "agent_behavior": "invalid_for_learning",
                "runtime_regression": True,
                "root_surface": (
                    "cross_cycle_evidence_persistence"
                    if evidence_persistence_failure else "verifier_input_binding"
                ),
                "cost_metrics": (
                    "contaminated" if evidence_persistence_failure else "valid"
                ),
            }
            store.update_task(int(task.id), TaskStatus.COMPLETED, result=task.result)
            store.add_checkpoint(int(task.id), "reconciled", {
                "reason": "runtime_fix", "previous_verification": prior_verification,
                "verification": verification, "model_calls": 0,
                "recovered_artifacts": recovered_artifacts,
            })
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

    if args.command == "ui":
        from .webui import serve_ui

        serve_ui(settings, store, host=args.host, port=args.port)
        return 0
    if args.command == "capsule":
        _, capsules, _ = _experiment_services(settings, store)
        if args.capsule_command == "capture":
            _print_json(capsules.capture(args.task_id))
        elif args.capsule_command == "list":
            _print_json(store.list_task_capsules())
        elif args.capsule_command == "show":
            _print_json(capsules.show(args.capsule_id))
        elif args.capsule_command == "verify":
            _print_json(capsules.verify_integrity(args.capsule_id))
        elif args.capsule_command == "fork":
            _print_json(capsules.fork(args.capsule_id))
        elif args.capsule_command == "archive":
            _print_json(capsules.archive(args.capsule_id))
        elif args.capsule_command == "delete":
            _print_json(capsules.delete(args.capsule_id))
        return 0
    if args.command == "experiment":
        if args.experiment_command == "run":
            prefix, separator, candidate_id = args.candidate.partition(":")
            if prefix != "skill" or not separator or not candidate_id:
                raise SystemExit("--candidate must use skill:<candidate_id>")
            _print_json(_run_counterfactual(
                settings, store, candidate_id, args.capsule, args.runs
            ))
        else:
            experiment = store.get_experiment(args.experiment_id)
            if experiment is None:
                raise SystemExit(f"Unknown experiment: {args.experiment_id}")
            _print_json(experiment if args.experiment_command == "show" else experiment.get("report"))
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
        elif args.evolution_command == "auto-run":
            skill_manager, capsules, runner = _experiment_services(settings, store)
            semantic = PairwiseSemanticJudge(
                ModelSemanticJudge(settings.model)
                if settings.experiments.semantic_judge_enabled else None
            )
            orchestrator = ExperimentOrchestrator(
                store, capsules, runner, semantic_judge=semantic,
            )
            loop = SelfEvolutionLoop(
                store, ExperienceAnalyzer(store),
                ModelEvolutionReasoner(LLMController(settings.model)),
                manager, orchestrator,
            )
            _print_json(loop.run(
                capsule_ids=args.capsule or None,
                runs_per_variant=args.runs or settings.experiments.default_runs_per_variant,
                task_limit=args.task_limit, trace_limit=args.trace_limit,
            ))
        elif args.evolution_command in {
            "runtime-observe", "runtime-propose", "runtime-list",
            "runtime-show", "runtime-evaluate", "runtime-benchmark",
            "runtime-benchmark-suite",
            "runtime-provenance",
        }:
            if args.evolution_command == "runtime-provenance":
                provenance = RuntimeProvenanceManager(settings, store)
                _print_json({
                    "bindings": provenance.bindings(args.task_id),
                    "eligibility": provenance.assess(args.task_id),
                })
                return 0
            runtime_candidates = RuntimeCandidateManager(
                settings, store, ModelRuntimeMutationReasoner(LLMController(settings.model)),
            )
            if args.evolution_command == "runtime-observe":
                _print_json(runtime_candidates.observe(args.task_id))
            elif args.evolution_command == "runtime-propose":
                _print_json(runtime_candidates.propose(args.task_id))
            elif args.evolution_command == "runtime-benchmark":
                _print_json(RuntimeDiagnosisBenchmark(store).latest(args.task_id))
            elif args.evolution_command == "runtime-benchmark-suite":
                _print_json(RuntimeDiagnosisBenchmark(store).suite(args.task_id or None))
            elif args.evolution_command == "runtime-list":
                _print_json(runtime_candidates.list())
            elif args.evolution_command == "runtime-show":
                _print_json(runtime_candidates.show(args.candidate_id))
            else:
                _print_json(ExternalRuntimeEvaluator(settings, runtime_candidates).evaluate(
                    args.candidate_id
                ))
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
        elif args.skill_command == "telemetry":
            _print_json(store.list_skill_usage(limit=args.limit, skill_name=args.name))
        elif args.skill_command == "replay":
            _print_json(SkillUtilityEvaluator(store, manager, broker).replay(
                args.candidate_id, runs=args.runs
            ))
        elif args.skill_command == "compare":
            report = SkillUtilityEvaluator(store, manager, broker).latest_comparison(args.candidate_id)
            if report is None:
                raise SystemExit("No replay report exists; run skill replay first")
            _print_json(report)
        elif args.skill_command == "utility":
            _print_json(SkillUtilityEvaluator(store, manager, broker).observed_utility(
                args.name, limit=args.limit
            ))
        elif args.skill_command == "counterfactual-replay":
            _print_json(_run_counterfactual(
                settings, store, args.candidate_id, args.capsule, args.runs
            ))
        elif args.skill_command == "bootstrap":
            manager.bootstrap_builtins()
            _print_json(manager.catalog(capabilities))
        return 0
    return 2


def _configure_windows_stdio() -> None:
    """Keep JSON, Chinese text and trace symbols printable in Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


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
        "experiments": {
            "root": "./experiments",
            "default_runs_per_variant": 3,
            "keep_worlds": False,
            "semantic_judge_enabled": False,
        },
        "evolution": {"enabled": False, "auto_promote": False},
    }


if __name__ == "__main__":
    raise SystemExit(main())
