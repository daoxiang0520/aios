from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ModelConfig:
    provider: str = "mock"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5-mini"
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: int = 60
    max_tokens: int = 2048
    temperature: float = 0.1
    thinking: str = "disabled"
    protocol: str = "tool_calling"


@dataclass(slots=True)
class PermissionConfig:
    allowed_tools: list[str] = field(
        default_factory=lambda: ["read", "write", "edit", "bash"]
    )
    allow_writes: bool = True
    max_read_bytes: int = 1_048_576
    max_write_bytes: int = 1_048_576


@dataclass(slots=True)
class BudgetConfig:
    max_model_calls_per_cycle: int = 6
    max_tool_calls_per_cycle: int = 8
    max_model_calls_per_task: int = 24
    max_tool_calls_per_task: int = 32
    max_tokens_per_task: int = 300_000
    soft_model_calls_per_task: int = 12
    soft_tokens_per_task: int = 120_000
    max_cycles_per_task: int = 6
    reserved_completion_tool_calls: int = 1
    tool_observation_characters: int = 12_000
    hot_tool_results: int = 2
    working_state_characters: int = 8_000


@dataclass(slots=True)
class EvolutionConfig:
    enabled: bool = False
    auto_promote: bool = True
    trigger_repetitions: int = 2
    retry_after_evolution: bool = True
    extensions_path: str = "./extensions"


@dataclass(slots=True)
class CapabilityConfig:
    network_enabled: bool = False
    allowed_domains: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SandboxConfig:
    backend: str = "docker"
    image: str = "python:3.12-slim"
    default_timeout_seconds: int = 60
    max_timeout_seconds: int = 300
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 128
    health_ttl_seconds: int = 30
    root: str = "./sandbox"


@dataclass(slots=True)
class SkillConfig:
    enabled: bool = True
    root: str = "./skills"
    require_human_promotion: bool = True
    max_source_bytes: int = 131_072
    benchmark_timeout_seconds: int = 30


@dataclass(slots=True)
class ExperimentConfig:
    root: str = "./experiments"
    default_runs_per_variant: int = 3
    keep_worlds: bool = False
    semantic_judge_enabled: bool = False


@dataclass(slots=True)
class Settings:
    root: Path
    database: Path
    workspace: Path
    poll_interval_seconds: float = 2.0
    max_actions_per_cycle: int = 8
    model: ModelConfig = field(default_factory=ModelConfig)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    capabilities: CapabilityConfig = field(default_factory=CapabilityConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    skills: SkillConfig = field(default_factory=SkillConfig)
    experiments: ExperimentConfig = field(default_factory=ExperimentConfig)

    @classmethod
    def load(cls, path: str | Path) -> "Settings":
        config_path = Path(path).resolve()
        raw: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
        root = config_path.parent

        def resolved(value: str) -> Path:
            candidate = Path(value)
            return (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

        permissions = PermissionConfig(**raw.get("permissions", {}))
        legacy = {"list_files": "read", "read_file": "read", "write_file": "write", "append_file": "edit", "echo": "write"}
        for old, new in legacy.items():
            if old in permissions.allowed_tools and new not in permissions.allowed_tools:
                permissions.allowed_tools.append(new)

        return cls(
            root=root,
            database=resolved(raw.get("database", "./data/aios.db")),
            workspace=resolved(raw.get("workspace", "./workspace")),
            poll_interval_seconds=float(raw.get("poll_interval_seconds", 2.0)),
            max_actions_per_cycle=int(raw.get("max_actions_per_cycle", 8)),
            model=ModelConfig(**raw.get("model", {})),
            permissions=permissions,
            budget=BudgetConfig(**raw.get("budget", {})),
            evolution=EvolutionConfig(**raw.get("evolution", {})),
            capabilities=CapabilityConfig(**raw.get("capabilities", {})),
            sandbox=SandboxConfig(**cls._sandbox_values(raw.get("sandbox", {}))),
            skills=SkillConfig(**raw.get("skills", {})),
            experiments=ExperimentConfig(**raw.get("experiments", {})),
        )

    @staticmethod
    def _sandbox_values(value: dict[str, Any]) -> dict[str, Any]:
        """Accept the pre-v0.6.6.1 timeout key without preserving its broken semantics."""
        result = dict(value)
        legacy = result.pop("timeout_seconds", None)
        if legacy is not None:
            result.setdefault("default_timeout_seconds", int(legacy))
            result.setdefault("max_timeout_seconds", max(300, int(legacy)))
        return result

    @property
    def extensions(self) -> Path:
        candidate = Path(self.evolution.extensions_path)
        return (self.root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

    @property
    def sandbox_root(self) -> Path:
        candidate = Path(self.sandbox.root)
        return (self.root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

    @property
    def skills_root(self) -> Path:
        candidate = Path(self.skills.root)
        return (self.root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

    @property
    def experiments_root(self) -> Path:
        candidate = Path(self.experiments.root)
        return (self.root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

    def ensure_directories(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.extensions.mkdir(parents=True, exist_ok=True)
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self.skills_root.mkdir(parents=True, exist_ok=True)
        self.experiments_root.mkdir(parents=True, exist_ok=True)
