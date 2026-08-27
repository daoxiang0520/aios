from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .capabilities import CapabilityRegistry
from .config import Settings
from .sandbox import DockerSandboxBroker
from .storage import StateStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiosctl", description="Read-only AIOS state interface")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("resource", choices=["tasks", "traces", "dead-letters", "memory", "skill-usage", "capabilities"])
    parser.add_argument("action", choices=["list", "show"], nargs="?", default="list")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    settings = Settings.load(args.config)
    store = StateStore(settings.database)
    store.initialize()
    if args.resource == "tasks":
        payload = [asdict(item) for item in store.list_tasks(limit=args.limit)]
    elif args.resource == "traces":
        payload = store.recent_traces(limit=args.limit)
    elif args.resource == "dead-letters":
        payload = store.list_dead_letters(limit=args.limit)
    elif args.resource == "memory":
        payload = [asdict(item) for item in store.list_memories(limit=args.limit)]
    elif args.resource == "skill-usage":
        payload = store.list_skill_usage(limit=args.limit)
    else:
        sandbox = DockerSandboxBroker(
            settings.sandbox_root, settings.sandbox,
            network_enabled=settings.capabilities.network_enabled,
        )
        payload = CapabilityRegistry.default(
            sandbox_available=sandbox.available(),
            network_enabled=settings.capabilities.network_enabled,
            allowed_domains=settings.capabilities.allowed_domains,
        ).as_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
