from __future__ import annotations

import tempfile
import unittest
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

from aios.capabilities import CapabilityRegistry, EvidenceContract
from aios.components import build_component_registry
from aios.config import PermissionConfig
from aios.evaluation import Verifier
from aios.resources import ResourceAdapter
from aios.runtime import AIOSRuntime
from aios.config import Settings
from aios.security import SecurityKernel
from aios.storage import StateStore
from aios.tools import ToolExecutor
from aios.types import Action, ActionResult, Task


class V0714OperationalCapabilityBindingTests(unittest.TestCase):
    def test_url_contract_requires_http_operation_not_bare_authority(self) -> None:
        contract = EvidenceContract.from_request(
            "完成 https://www.luogu.com.cn/problem/P1593 中的题目"
        )
        names = {item.name for item in contract.capabilities}
        self.assertIn("resource.http.read", names)
        self.assertNotIn("network.external", names)

    def test_effective_http_capability_requires_authority_provider_and_health(self) -> None:
        healthy = CapabilityRegistry.default(
            sandbox_available=True, network_enabled=True, http_read_available=True,
        )
        healthy_components = build_component_registry(healthy)
        provider = healthy_components.resolve_available_provider("resource.http.read")
        self.assertEqual(provider["metadata"]["name"], "http_reader")
        self.assertEqual(provider["interface"]["primitive"], "read")

        unhealthy = CapabilityRegistry.default(
            sandbox_available=True, network_enabled=True, http_read_available=False,
        )
        self.assertIsNone(
            build_component_registry(unhealthy).resolve_available_provider("resource.http.read")
        )
        self.assertEqual(unhealthy.get("resource.http.read").state.value, "missing")

        unauthorized = CapabilityRegistry.default(
            sandbox_available=True, network_enabled=False, http_read_available=True,
        )
        self.assertEqual(unauthorized.get("resource.http.read").state.value, "needs_authority")

    def test_security_preserves_http_url_but_still_resolves_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = PermissionConfig(allowed_tools=["read"])
            kernel = SecurityKernel(root, config)
            url = "https://www.luogu.com.cn/problem/P1593"
            self.assertEqual(kernel.authorize(Action("read", {"path": url}))["path"], url)
            self.assertEqual(
                kernel.authorize(Action("read", {"path": "a.txt"}))["path"],
                str((root / "a.txt").resolve()),
            )

    def test_read_url_routes_to_governed_http_provider(self) -> None:
        sandbox = Mock()
        sandbox.read_http.return_value = {
            "exit_code": 0,
            "stdout": (
                '{"resource":{"path":"https://luogu.com.cn/problem/P1593",'
                '"type":"text/html","metadata":{"status":200,"source_domain":"luogu.com.cn"},'
                '"representations":[{"kind":"text","offset":0,"text":"P1593 因子和",'
                '"truncated":false}]}}'
            ),
            "stderr": "",
        }
        adapter = ResourceAdapter(PermissionConfig(), sandbox)
        output = adapter.read("https://luogu.com.cn/problem/P1593")
        sandbox.read_http.assert_called_once()
        self.assertEqual(output["adapter_runtime"]["provider"], "http_reader")
        self.assertEqual(output["resource"]["metadata"]["status"], 200)

    def test_successful_read_url_establishes_network_and_domain_evidence(self) -> None:
        contract = EvidenceContract.from_request("读取 https://luogu.com.cn/problem/P1593")
        action = Action("read", {"path": "https://luogu.com.cn/problem/P1593"})
        result = ActionResult("read", True, {
            "resource": {
                "path": action.arguments["path"], "type": "text/html",
                "metadata": {"status": 200, "source_domain": "luogu.com.cn"},
                "representations": [{"kind": "text", "text": "P1593 因子和"}],
            }
        })
        collected = Verifier.collect_evidence(contract, action, result, "trace:1")
        self.assertEqual(
            {(item["kind"], item["value"]) for item in collected},
            {("network_request", None), ("source_domain", "luogu.com.cn")},
        )

    def test_exit_127_is_classified_as_missing_executable(self) -> None:
        registry = Mock()
        registry.get.return_value = lambda **_: {
            "exit_code": 127, "stdout": "", "stderr": "bash: curl: command not found"
        }
        security = Mock()
        security.authorize.return_value = {"command": "curl https://example.com"}
        result = ToolExecutor(registry, security).execute(
            Action("bash", {"command": "curl https://example.com"})
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("MissingExecutable:"))

    def test_failed_attempt_carries_gap_and_only_valid_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "database": "data/a.db", "workspace": "workspace",
                "model": {"provider": "mock"},
                "sandbox": {"backend": "docker", "root": "sandbox"},
                "skills": {"enabled": False, "root": "skills"},
                "evolution": {"enabled": False},
            }), encoding="utf-8")
            settings = Settings.load(config_path)
            settings.ensure_directories()
            source = settings.workspace / "P1593.py"
            source.write_text("print(15)\n", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            store = StateStore(settings.database)
            store.initialize()
            task_id = store.create_task(Task("P1593", "读取 https://luogu.com.cn/problem/P1593"))
            state = {
                "objective": "P1593",
                "verification_gap": {"required_evidence": [{"kind": "source_domain"}]},
                "operational": {"resources": {
                    "P1593.py": {"content_digest": digest},
                    "stale.py": {"content_digest": "wrong"},
                }, "artifacts": [{"path": "uncommitted.py"}]},
                "accessed_resources": ["P1593.py", "stale.py"],
                "available_artifacts": [{"path": "uncommitted.py"}],
                "established_facts": [
                    {"resource": "P1593.py"}, {"resource": "stale.py"},
                ],
                "completed_steps": [
                    {"step": "read:P1593.py"}, {"step": "bash:discarded"},
                ],
                "evidence_ledger": [
                    {"kind": "network_request", "state": "ESTABLISHED"},
                    {"kind": "source_domain", "value": "luogu.com.cn", "state": "ESTABLISHED"},
                    {"kind": "command_success", "state": "ESTABLISHED"},
                ],
            }
            store.add_checkpoint(task_id, "failed_attempt", {"working_state": state})
            restored = AIOSRuntime(settings)._task_working_state(task_id, "P1593")
            self.assertIn("verification_gap", restored)
            self.assertEqual(set(restored["operational"]["resources"]), {"P1593.py"})
            self.assertEqual(restored["available_artifacts"], [])
            self.assertEqual(
                {item["kind"] for item in restored["evidence_ledger"]},
                {"network_request", "source_domain"},
            )


if __name__ == "__main__":
    unittest.main()
