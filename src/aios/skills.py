from __future__ import annotations

import json
import hashlib
import re
import shlex
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .capabilities import CapabilityRegistry, CapabilityRequirement, EvidenceContract
from .config import SkillConfig
from .sandbox import DockerSandboxBroker


class SkillValidationError(RuntimeError):
    pass


class SkillPromotionError(RuntimeError):
    pass


@dataclass(slots=True)
class SkillManifest:
    name: str
    version: str
    description: str
    entrypoint: str = "skill.py"
    required_capabilities: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    tests: list[dict[str, Any]] = field(default_factory=list)
    origin: str = "user"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillManifest":
        return cls(
            name=str(value.get("name", "")),
            version=str(value.get("version", "")),
            description=str(value.get("description", "")),
            entrypoint=str(value.get("entrypoint", "skill.py")),
            required_capabilities=[str(item) for item in value.get("required_capabilities", [])],
            input_schema=dict(value.get("input_schema") or {"type": "object"}),
            tests=[dict(item) for item in value.get("tests", []) if isinstance(item, dict)],
            origin=str(value.get("origin", "user")),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SkillManager:
    """Versioned executable assets. Skills never become model Tool Schemas."""

    SAFE_CAPABILITIES = {
        "filesystem.read", "filesystem.write", "process.sandbox_exec",
        "state.task_read", "state.trace_read", "state.dead_letter_read", "state.memory_read",
        "network.external",
    }

    def __init__(self, root: Path, config: SkillConfig):
        self.root = root.resolve()
        self.config = config
        self.candidates = self.root / "candidates"
        self.active = self.root / "active"
        self.history = self.root / "history"
        self.deprecated = self.root / "deprecated"
        self.reports = self.root / "reports"
        # Only this projection is mounted into an Agent sandbox. Candidates,
        # reports, history and deprecated code remain host-side lifecycle data.
        self.runtime = self.root / "runtime"
        self.runtime_active = self.runtime / "active"
        for directory in (self.candidates, self.active, self.history, self.deprecated, self.reports):
            directory.mkdir(parents=True, exist_ok=True)
        self.runtime_active.mkdir(parents=True, exist_ok=True)
        self._write_dispatcher()
        self._sync_runtime()

    def validate(self, manifest_value: dict[str, Any], source: str) -> SkillManifest:
        manifest = SkillManifest.from_dict(manifest_value)
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", manifest.name):
            raise SkillValidationError("Skill name must be lowercase snake_case")
        if not re.fullmatch(r"\d+\.\d+\.\d+", manifest.version):
            raise SkillValidationError("Skill version must use MAJOR.MINOR.PATCH")
        if not manifest.description or len(manifest.description) > 500:
            raise SkillValidationError("Skill description is required and limited to 500 characters")
        if manifest.entrypoint != "skill.py":
            raise SkillValidationError("v0.6 supports only the skill.py entrypoint")
        if manifest.input_schema.get("type") != "object":
            raise SkillValidationError("Skill input_schema must describe an object")
        unknown = set(manifest.required_capabilities) - self.SAFE_CAPABILITIES
        if unknown:
            raise SkillValidationError(f"Unknown required capabilities: {sorted(unknown)}")
        if "process.sandbox_exec" not in manifest.required_capabilities:
            raise SkillValidationError("Executable skills must declare process.sandbox_exec")
        if len(source.encode("utf-8")) > self.config.max_source_bytes:
            raise SkillValidationError("Skill source exceeds configured size limit")
        try:
            compile(source, manifest.entrypoint, "exec")
        except SyntaxError as exc:
            raise SkillValidationError(f"Skill source does not compile: {exc}") from exc
        for test in manifest.tests:
            if not isinstance(test.get("input", {}), dict):
                raise SkillValidationError("Each skill test input must be an object")
            if not isinstance(test.get("expect_exit", 0), int):
                raise SkillValidationError("expect_exit must be an integer")
        return manifest

    def propose(self, manifest_value: dict[str, Any], source: str) -> dict[str, Any]:
        manifest = self.validate(manifest_value, source)
        fingerprint = json.dumps(manifest.as_dict(), ensure_ascii=False, sort_keys=True) + "\n" + source
        candidate_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
        package = self.candidates / candidate_id / manifest.name
        if not package.exists():
            package.mkdir(parents=True)
            self._write_package(package, manifest, source)
        return {"candidate_id": candidate_id, "status": "candidate", "manifest": manifest.as_dict()}

    def ingest_workspace_candidates(
        self,
        workspace: Path,
        broker: DockerSandboxBroker,
    ) -> list[dict[str, Any]]:
        """Register Agent-authored packages after the task workspace is committed."""
        root = workspace.resolve() / "skill_candidates"
        if not root.is_dir():
            return []
        ingested: list[dict[str, Any]] = []
        for manifest_path in sorted(root.glob("*/manifest.json")):
            try:
                value = json.loads(manifest_path.read_text(encoding="utf-8"))
                value["origin"] = "agent"
                source_path = manifest_path.parent / str(value.get("entrypoint", "skill.py"))
                proposal = self.propose(value, source_path.read_text(encoding="utf-8"))
                record: dict[str, Any] = proposal
                if not self.config.require_human_promotion:
                    report = self.benchmark(proposal["candidate_id"], broker)
                    record = {**record, "benchmark": report}
                    if report["passed"]:
                        record["promotion"] = self.promote(proposal["candidate_id"], approved=True)
                ingested.append(record)
            except (OSError, json.JSONDecodeError, SkillValidationError, SkillPromotionError) as exc:
                ingested.append({"package": str(manifest_path.parent), "status": "rejected", "error": f"{type(exc).__name__}: {exc}"})
        return ingested

    def benchmark(self, candidate_id: str, broker: DockerSandboxBroker) -> dict[str, Any]:
        package, manifest, _ = self._candidate(candidate_id)
        tests = manifest.tests or [{"input": {}, "expect_exit": 0}]
        checks: list[dict[str, Any]] = []
        for index, test in enumerate(tests, 1):
            payload = json.dumps(test.get("input", {}), ensure_ascii=False)
            result = broker.run_candidate(
                package,
                f"python /candidate/{manifest.entrypoint} --input-json {shlex.quote(payload)}",
                timeout_seconds=self.config.benchmark_timeout_seconds,
            )
            expected_exit = int(test.get("expect_exit", 0))
            contains = test.get("stdout_contains")
            passed = result["exit_code"] == expected_exit and (
                not isinstance(contains, str) or contains in result["stdout"]
            )
            checks.append({
                "name": f"test_{index}", "passed": passed,
                "expected_exit": expected_exit, "actual_exit": result["exit_code"],
                "stdout_contains": contains, "stdout": result["stdout"][:2000],
                "stderr": result["stderr"][:2000],
            })
        report = {"candidate_id": candidate_id, "skill": manifest.name, "version": manifest.version, "passed": all(item["passed"] for item in checks), "checks": checks}
        (self.reports / f"{candidate_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def promote(self, candidate_id: str, *, approved: bool) -> dict[str, Any]:
        if self.config.require_human_promotion and not approved:
            raise SkillPromotionError("Human approval is required for skill promotion")
        package, manifest, _ = self._candidate(candidate_id)
        report_path = self.reports / f"{candidate_id}.json"
        if not report_path.is_file() or not json.loads(report_path.read_text(encoding="utf-8")).get("passed"):
            raise SkillPromotionError("Candidate must pass sandbox benchmark before promotion")
        destination = self.active / manifest.name
        if destination.exists():
            old_manifest = self._load_manifest(destination)
            if _version_key(manifest.version) <= _version_key(old_manifest.version):
                raise SkillPromotionError(
                    f"Promotion requires a newer version than active {old_manifest.version}"
                )
            archive = self.history / manifest.name / old_manifest.version
            if archive.exists():
                shutil.rmtree(archive)
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(archive))
        shutil.copytree(package, destination)
        self._sync_runtime_skill(manifest.name)
        return {"name": manifest.name, "version": manifest.version, "status": "active"}

    def rollback(self, name: str, *, approved: bool) -> dict[str, Any]:
        if self.config.require_human_promotion and not approved:
            raise SkillPromotionError("Human approval is required for skill rollback")
        versions = sorted((self.history / name).glob("*"), key=lambda item: _version_key(item.name), reverse=True)
        if not versions:
            raise SkillPromotionError(f"No rollback version exists for skill: {name}")
        destination = self.active / name
        if destination.exists():
            current = self._load_manifest(destination)
            replaced = self.history / name / current.version
            if replaced.exists():
                shutil.rmtree(replaced)
            shutil.move(str(destination), str(replaced))
        selected = versions[0]
        shutil.move(str(selected), str(destination))
        manifest = self._load_manifest(destination)
        self._sync_runtime_skill(name)
        return {"name": name, "version": manifest.version, "status": "active", "rolled_back": True}

    def deprecate(self, name: str, *, approved: bool) -> dict[str, Any]:
        if self.config.require_human_promotion and not approved:
            raise SkillPromotionError("Human approval is required for skill deprecation")
        source = self.active / name
        if not source.exists():
            raise KeyError(f"Unknown active skill: {name}")
        manifest = self._load_manifest(source)
        destination = self.deprecated / name / manifest.version
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(source), str(destination))
        self._sync_runtime_skill(name)
        return {"name": name, "version": manifest.version, "status": "deprecated"}

    def active_skills(self) -> list[SkillManifest]:
        result: list[SkillManifest] = []
        for package in sorted(self.active.iterdir()):
            if package.is_dir():
                try:
                    result.append(self._load_manifest(package))
                except (OSError, json.JSONDecodeError, SkillValidationError):
                    continue
        return result

    def list_candidates(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for candidate_root in sorted(self.candidates.iterdir(), reverse=True):
            if not candidate_root.is_dir():
                continue
            packages = [item for item in candidate_root.iterdir() if item.is_dir()]
            if len(packages) != 1:
                continue
            try:
                manifest = self._load_manifest(packages[0])
            except (OSError, json.JSONDecodeError, SkillValidationError):
                continue
            report = self.reports / f"{candidate_root.name}.json"
            benchmark = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else None
            values.append({"candidate_id": candidate_root.name, "manifest": manifest.as_dict(), "benchmark": benchmark})
        return values

    def catalog(self, capabilities: CapabilityRegistry) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for manifest in self.active_skills():
            contract = EvidenceContract(
                request=f"skill:{manifest.name}",
                capabilities=[CapabilityRequirement(name, f"Required by skill {manifest.name}") for name in manifest.required_capabilities],
            )
            assessment = capabilities.assess(contract)
            catalog.append({**manifest.as_dict(), "available": assessment["satisfied"], "capability_assessment": assessment})
        return catalog

    def versions(self, name: str) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        active = self.active / name
        if active.exists():
            values.append({**self._load_manifest(active).as_dict(), "status": "active"})
        for path in sorted((self.history / name).glob("*"), key=lambda item: _version_key(item.name), reverse=True):
            values.append({**self._load_manifest(path).as_dict(), "status": "history"})
        for path in sorted((self.deprecated / name).glob("*"), key=lambda item: _version_key(item.name), reverse=True):
            values.append({**self._load_manifest(path).as_dict(), "status": "deprecated"})
        return values

    def bootstrap_builtins(self) -> None:
        builtins = (_workspace_search_builtin(), _state_query_builtin(), _trace_analyzer_builtin())
        for manifest_value, source in builtins:
            manifest = self.validate(manifest_value, source)
            destination = self.active / manifest.name
            if not destination.exists():
                destination.mkdir(parents=True)
                self._write_package(destination, manifest, source)
            self._sync_runtime_skill(manifest.name)

    def _candidate(self, candidate_id: str) -> tuple[Path, SkillManifest, str]:
        root = self.candidates / candidate_id
        packages = [item for item in root.iterdir()] if root.exists() else []
        if len(packages) != 1 or not packages[0].is_dir():
            raise KeyError(f"Unknown skill candidate: {candidate_id}")
        package = packages[0]
        manifest = self._load_manifest(package)
        source = (package / manifest.entrypoint).read_text(encoding="utf-8")
        return package, self.validate(manifest.as_dict(), source), source

    def _load_manifest(self, package: Path) -> SkillManifest:
        value = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        source = (package / str(value.get("entrypoint", "skill.py"))).read_text(encoding="utf-8")
        return self.validate(value, source)

    @staticmethod
    def _write_package(package: Path, manifest: SkillManifest, source: str) -> None:
        (package / "manifest.json").write_text(json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        (package / manifest.entrypoint).write_text(source, encoding="utf-8")

    def _write_dispatcher(self) -> None:
        dispatcher = '''import argparse,json,subprocess,sys
from pathlib import Path
ROOT=Path("/skills/active")
def manifest(name):
    path=ROOT/name/"manifest.json"
    if not path.is_file(): raise SystemExit("unknown skill: "+name)
    return json.loads(path.read_text(encoding="utf-8"))
def capability_states():
    path=Path("/aios-state/state.json")
    return json.loads(path.read_text(encoding="utf-8")).get("capabilities",{}) if path.is_file() else {}
p=argparse.ArgumentParser(prog="skill")
s=p.add_subparsers(dest="command",required=True)
s.add_parser("list")
show=s.add_parser("show"); show.add_argument("name")
run=s.add_parser("run"); run.add_argument("name"); run.add_argument("--input-json",default="{}")
a=p.parse_args()
if a.command=="list":
    print(json.dumps([json.loads((x/"manifest.json").read_text(encoding="utf-8")) for x in sorted(ROOT.iterdir()) if (x/"manifest.json").is_file()],ensure_ascii=False,indent=2)); raise SystemExit()
m=manifest(a.name)
if a.command=="show": print(json.dumps(m,ensure_ascii=False,indent=2)); raise SystemExit()
states=capability_states(); blocked=[]
for required in m.get("required_capabilities",[]):
    state=states.get(required,{}).get("state","missing")
    if state not in ("available","composable"): blocked.append({"name":required,"state":state})
if blocked: print(json.dumps({"error":"blocked_capability","requirements":blocked},ensure_ascii=False)); raise SystemExit(3)
payload=json.loads(a.input_json)
r=subprocess.run([sys.executable,str(ROOT/a.name/m.get("entrypoint","skill.py")),"--input-json",json.dumps(payload,ensure_ascii=False)],text=True,encoding="utf-8",errors="replace")
raise SystemExit(r.returncode)
'''
        (self.runtime / "skill.py").write_text(dispatcher, encoding="utf-8")

    def _sync_runtime(self) -> None:
        active_names = {item.name for item in self.active_skills()}
        for package in self.runtime_active.iterdir():
            if package.is_dir() and package.name not in active_names:
                shutil.rmtree(package)
        for name in active_names:
            self._sync_runtime_skill(name)

    def _sync_runtime_skill(self, name: str) -> None:
        source = self.active / name
        destination = self.runtime_active / name
        if destination.exists():
            shutil.rmtree(destination)
        if source.is_dir():
            shutil.copytree(source, destination)


def _version_key(value: str) -> tuple[int, int, int]:
    try:
        return tuple(int(item) for item in value.split("."))  # type: ignore[return-value]
    except (TypeError, ValueError):
        return (0, 0, 0)


def _workspace_search_builtin() -> tuple[dict[str, Any], str]:
    manifest = {
        "name": "workspace_search", "version": "1.0.0",
        "description": "Search workspace paths and UTF-8 text without adding a Tool Schema.",
        "required_capabilities": ["filesystem.read", "process.sandbox_exec"],
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}},
        "tests": [{"input": {"query": "unlikely_fixture"}, "expect_exit": 0, "stdout_contains": "[]"}],
        "origin": "builtin",
    }
    source = '''import argparse,json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument("--input-json",default="{}"); a=p.parse_args(); data=json.loads(a.input_json)
query=str(data.get("query","")).casefold(); limit=max(1,min(int(data.get("limit",20)),100)); found=[]
for path in sorted(Path("/workspace").rglob("*")):
    if not path.is_file(): continue
    rel=path.relative_to("/workspace").as_posix(); matched=query in rel.casefold(); snippet=None
    if not matched and path.stat().st_size<=256000:
        try: text=path.read_text(encoding="utf-8")
        except (OSError,UnicodeDecodeError): text=""
        i=text.casefold().find(query); matched=i>=0; snippet=text[max(0,i-80):i+len(query)+160] if matched else None
    if matched: found.append({"path":rel,"size":path.stat().st_size,"snippet":snippet})
    if len(found)>=limit: break
print(json.dumps(found,ensure_ascii=False))
'''
    return manifest, source


def _trace_analyzer_builtin() -> tuple[dict[str, Any], str]:
    manifest = {
        "name": "trace_failure_analyzer", "version": "1.0.0",
        "description": "Summarize recent trace kinds, failed tools, and cycle failure types.",
        "required_capabilities": ["state.trace_read", "process.sandbox_exec"],
        "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        "tests": [{"input": {"limit": 10}, "expect_exit": 0, "stdout_contains": "trace_kinds"}],
        "origin": "builtin",
    }
    source = '''import argparse,collections,json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument("--input-json",default="{}"); a=p.parse_args(); inp=json.loads(a.input_json); limit=max(1,min(int(inp.get("limit",100)),500))
state=json.loads(Path("/aios-state/state.json").read_text(encoding="utf-8")); traces=state.get("traces",[])[:limit]
kinds=collections.Counter(t.get("kind","unknown") for t in traces); tools=collections.Counter(); failures=collections.Counter()
for t in traces:
    d=t.get("data",{})
    if t.get("kind")=="action_result" and not d.get("ok",False): tools[d.get("tool","unknown")]+=1
    if t.get("kind")=="cycle_failed": failures[str(d.get("error","unknown")).split(":",1)[0]]+=1
print(json.dumps({"trace_count":len(traces),"trace_kinds":dict(kinds),"failed_tools":dict(tools),"cycle_failure_types":dict(failures)},ensure_ascii=False))
'''
    return manifest, source


def _state_query_builtin() -> tuple[dict[str, Any], str]:
    manifest = {
        "name": "state_query", "version": "1.0.0",
        "description": "Query tasks, traces, dead letters, or memory through one reusable Skill.",
        "required_capabilities": [
            "state.task_read", "state.trace_read", "state.dead_letter_read",
            "state.memory_read", "process.sandbox_exec",
        ],
        "input_schema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "enum": ["tasks", "traces", "dead-letters", "memory"]},
                "limit": {"type": "integer"},
            },
            "required": ["resource"],
        },
        "tests": [{"input": {"resource": "tasks", "limit": 1}, "expect_exit": 0, "stdout_contains": "[]"}],
        "origin": "builtin",
    }
    source = '''import argparse,json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument("--input-json",default="{}"); a=p.parse_args(); inp=json.loads(a.input_json)
resource=str(inp.get("resource","")); allowed={"tasks","traces","dead-letters","memory"}
if resource not in allowed: print(json.dumps({"error":"invalid resource"})); raise SystemExit(2)
limit=max(1,min(int(inp.get("limit",20)),500)); state=json.loads(Path("/aios-state/state.json").read_text(encoding="utf-8"))
print(json.dumps(state.get(resource,[])[:limit],ensure_ascii=False))
'''
    return manifest, source
