import json
import os
import tempfile
import unittest

from evoagent.agentic_core import AgenticReviewer, BoundedRole, _parse_findings
from evoagent.artifacts import (
    ArtifactPersistFailed,
    ArtifactRuntime,
    ArtifactScope,
    MAX_ARTIFACT_BYTES,
)
from evoagent.context_manager import ContextManager
from evoagent.diff_parser import parse_unified_diff
from evoagent.models import ReviewReport
from evoagent.report import to_markdown
from evoagent.runtime import AgentTool, ToolRegistry
from evoagent.store import TaskStore
from evoagent.telemetry import ExecutionLedger


DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(user_input)\n"


class InjectedCrash(BaseException):
    pass


class ToolThenFindingClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self, crash_after_tool=False):
        self.inputs = []
        self.crash_after_tool = crash_after_tool

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        managed = json.loads(user)
        self.inputs.append(managed)
        if not managed.get("observations"):
            return {
                "action": "tool", "tool": "read_file", "arguments": {},
                "reason": "collect exact evidence",
            }
        if self.crash_after_tool:
            raise InjectedCrash("after artifact write")
        evidence_id = "read_file:e1"
        return {
            "action": "final",
            "findings": [{
                "rule_id": "SEC-EVAL", "cwe": "CWE-95",
                "severity": "critical", "title": "Dynamic execution",
                "explanation": "Input is evaluated as code.",
                "path": "app.py", "line": 1, "evidence": "eval(user_input)",
                "evidence_ids": [evidence_id],
                "fix": "Use a parser.", "test": "Reject executable input.",
                "confidence": 0.9,
            }],
        }


class EvidenceArtifactTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)
        self.store.create("task-1", "org/repo", 1, {}, tenant_id="tenant-a")
        self.scope = ArtifactScope("tenant-a", "org/repo", "task-1")

    def tearDown(self):
        os.unlink(self.path)

    def runtime(self):
        return ArtifactRuntime(
            self.store, self.scope, "revision-1", "security-1", 0,
            "security", "assignment", "security-1",
        )

    @staticmethod
    def registry(handler):
        return ToolRegistry([AgentTool(
            "read_file", "Read one file.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            handler, artifact_replay=True,
        )])

    def test_durable_capture_precedes_context_and_public_projection_is_safe(self):
        private_tail = "PRIVATE-ARTIFACT-TAIL"
        full_output = "x" * 12000 + private_tail
        tools = self.registry(lambda: {
            "evidence_id": "read_file:e1", "tool": "read_file", "output": full_output,
        })
        client = ToolThenFindingClient()
        role = BoundedRole(
            "security", "system", client, 8000, 30, max_steps=2,
            context_manager=ContextManager(observation_token_budget=128),
            artifact_runtime=self.runtime(),
        )
        result = role.run(json.dumps({"diff": DIFF}), tools, ExecutionLedger("agentic"))

        reference = next(iter(result["_evidence_artifact_refs"].values()))
        stored = self.store.get_artifact(
            reference["artifact_id"], "tenant-a", "org/repo", "task-1"
        )
        self.assertEqual(full_output, stored["content"]["output"])
        self.assertNotIn(private_tail, json.dumps(client.inputs[-1]))

        findings = _parse_findings(result, parse_unified_diff(DIFF), "security")
        self.assertEqual(reference["artifact_id"], findings[0].evidence_refs[0]["artifact_id"])
        public = ReviewReport(
            repository="org/repo", pull_request=1, summary="one finding",
            risk="critical", findings=findings,
        ).to_dict()
        rendered = json.dumps(public)
        for private_field in (
            "artifact_id", "content_hash", "logical_execution_key", private_tail,
        ):
            self.assertNotIn(private_field, rendered)
            self.assertNotIn(private_field, to_markdown(public))

    def test_persistence_failure_prevents_observation_from_reaching_reasoning(self):
        class FailingStore:
            def get_artifact_by_logical_execution_key(self, *args, **kwargs):
                return None

            def put_artifact(self, artifact):
                raise OSError("database unavailable")

        calls = {"tool": 0}

        def handler():
            calls["tool"] += 1
            return {"evidence_id": "read_file:e1", "tool": "read_file", "output": "fact"}

        client = ToolThenFindingClient()
        runtime = ArtifactRuntime(
            FailingStore(), self.scope, "revision-1", "security-1", 0,
            "security", "assignment", "security-1",
        )
        role = BoundedRole(
            "security", "system", client, 8000, 30, max_steps=2,
            artifact_runtime=runtime,
        )
        with self.assertRaisesRegex(ArtifactPersistFailed, "ARTIFACT_PERSIST_FAILED"):
            role.run("{}", self.registry(handler), ExecutionLedger("agentic"))
        self.assertEqual(1, calls["tool"])
        self.assertEqual(1, len(client.inputs))

    def test_crash_after_write_replays_artifact_without_tool_reexecution(self):
        calls = {"tool": 0}

        def handler():
            calls["tool"] += 1
            return {"evidence_id": "read_file:e1", "tool": "read_file", "output": "fact"}

        with self.assertRaises(InjectedCrash):
            BoundedRole(
                "security", "system", ToolThenFindingClient(crash_after_tool=True),
                8000, 30, max_steps=2, artifact_runtime=self.runtime(),
            ).run("{}", self.registry(handler), ExecutionLedger("agentic"))
        self.assertEqual(1, calls["tool"])

        result = BoundedRole(
            "security", "system", ToolThenFindingClient(),
            8000, 30, max_steps=2, artifact_runtime=self.runtime(),
        ).run("{}", self.registry(handler), ExecutionLedger("agentic"))
        self.assertEqual(1, calls["tool"])
        self.assertTrue(result["_observations"][0]["artifact_replayed"])

    def test_unsupported_oversize_tool_result_fails_closed(self):
        role = BoundedRole(
            "security", "system", ToolThenFindingClient(), 8000, 30, max_steps=2,
            artifact_runtime=self.runtime(),
        )
        tools = self.registry(lambda: {
            "evidence_id": "large", "tool": "read_file",
            "output": "x" * (MAX_ARTIFACT_BYTES + 1),
        })
        with self.assertRaisesRegex(ArtifactPersistFailed, "supported limit"):
            role.run("{}", tools, ExecutionLedger("agentic"))

    def test_read_artifact_tool_exposes_only_requested_bounded_range(self):
        runtime = self.runtime()
        _view, reference, _replayed = runtime.invoke(
            self.registry(lambda: {
                "evidence_id": "read_file:e1", "tool": "read_file",
                "output": "z" * 20000,
            }), "read_file", {}, 1,
        )
        tools = ToolRegistry()
        AgenticReviewer._register_artifact_read_tool(tools, runtime)
        materialized = tools.invoke("read_artifact", {
            "artifact_id": reference.artifact_id, "offset": 50, "max_chars": 300,
        })
        self.assertEqual(300, len(materialized["content"]))
        self.assertEqual({"start": 50, "end": 350}, materialized["range"])
        self.assertTrue(materialized["truncated"])
        self.assertEqual(reference.content_hash, materialized["content_hash"])


if __name__ == "__main__":
    unittest.main()
