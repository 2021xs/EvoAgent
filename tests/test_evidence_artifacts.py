import json
import unittest

from evoagent.agentic_core import (
    BoundedRole,
    EvidenceArtifactError,
    MAX_EVIDENCE_ARTIFACT_BYTES,
    MAX_TASK_EVIDENCE_ARTIFACT_BYTES,
    _parse_findings,
    capture_tool_evidence_artifact,
    resolve_evidence_artifact_ref,
)
from evoagent.context_manager import ContextManager
from evoagent.diff_parser import parse_unified_diff
from evoagent.models import Finding, ReviewReport, Severity
from evoagent.report import to_markdown
from evoagent.runtime import AgentTool, ToolRegistry
from evoagent.telemetry import ExecutionLedger


DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(user_input)\n"


class ToolThenFindingClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self):
        self.inputs = []

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        managed = json.loads(user)
        self.inputs.append(managed)
        if not managed.get("observations"):
            return {
                "action": "tool", "tool": "read_file", "arguments": {},
                "reason": "collect exact evidence",
            }
        return {
            "action": "final",
            "findings": [{
                "rule_id": "SEC-EVAL", "cwe": "CWE-95",
                "severity": "critical", "title": "Dynamic execution",
                "explanation": "Input is evaluated as code.",
                "path": "app.py", "line": 1, "evidence": "eval(user_input)",
                "evidence_ids": ["read_file:e1"],
                "fix": "Use a parser.", "test": "Reject executable input.",
                "confidence": 0.9,
            }],
        }


class EvidenceArtifactTests(unittest.TestCase):
    def test_capture_precedes_context_compaction_and_public_projection_is_safe(self):
        private_tail = "PRIVATE-ARTIFACT-TAIL"
        full_output = "x" * 12000 + private_tail
        tools = ToolRegistry()
        tools.register(AgentTool(
            "read_file", "Read one file.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: {
                "evidence_id": "read_file:e1", "tool": "read_file",
                "output": full_output,
            },
        ))
        artifacts = {}

        def sink(role, observation):
            return capture_tool_evidence_artifact(
                "task-1", artifacts, {
                    "role": role, "run_id": "security-1",
                    "step": observation["step"], "tool": observation["tool"],
                }, observation["result"],
            )

        client = ToolThenFindingClient()
        role = BoundedRole(
            "security", "system", client, 8000, 30, max_steps=2,
            context_manager=ContextManager(observation_token_budget=128),
            artifact_sink=sink,
        )
        result = role.run(
            json.dumps({"diff": DIFF}), tools,
            ExecutionLedger("agentic"),
        )

        self.assertEqual(1, len(artifacts))
        artifact = next(iter(artifacts.values()))
        self.assertEqual(full_output, artifact["content"]["output"])
        self.assertNotIn(private_tail, json.dumps(client.inputs[-1]))

        findings = _parse_findings(
            result, parse_unified_diff(DIFF), "security",
        )
        self.assertEqual(1, len(findings))
        reference = findings[0].evidence_refs[0]
        self.assertIn("artifact_id", reference)
        resolved = resolve_evidence_artifact_ref(
            "task-1", artifacts, reference, max_chars=2000,
        )
        self.assertTrue(resolved["content_available"])
        self.assertNotIn(private_tail, resolved["output_preview"])

        public = ReviewReport(
            repository="org/repo", pull_request=1, summary="one finding",
            risk="critical", findings=findings,
        ).to_dict()
        rendered = json.dumps(public)
        self.assertNotIn("artifact_id", rendered)
        self.assertNotIn("content_sha256", rendered)
        self.assertNotIn(private_tail, rendered)
        self.assertNotIn(private_tail, to_markdown(public))

    def test_artifact_is_independent_of_candidate_merge_and_revision_replacement(self):
        artifacts = {}
        reference = capture_tool_evidence_artifact(
            "task-1", artifacts,
            {"role": "security", "run_id": "security-1", "step": 1,
             "tool": "search_diff"},
            {"evidence_id": "search_diff:e1", "tool": "search_diff",
             "output": [{"path": "app.py", "line": 1}]},
        )
        loser = Finding(
            rule_id="SEC-EVAL", cwe="CWE-95", severity=Severity.CRITICAL,
            title="loser", explanation="e", path="app.py", line=1,
            evidence="eval(user_input)", fix="f", test="t", confidence=0.7,
            evidence_refs=[reference], candidate_id="loser",
        )
        winner = Finding(
            rule_id="SEC-EVAL", cwe="CWE-95", severity=Severity.CRITICAL,
            title="winner", explanation="e", path="app.py", line=1,
            evidence="eval(user_input)", fix="f", test="t", confidence=0.9,
            candidate_id="winner",
        )
        from evoagent.agentic_core import AgenticReviewer

        self.assertEqual([winner], AgenticReviewer._merge([loser, winner]))
        worker_results = {"security-1": {"findings": [loser.to_internal_dict()]}}
        worker_results["security-1"] = {"findings": [winner.to_internal_dict()]}
        self.assertIn(reference["artifact_id"], artifacts)
        self.assertTrue(resolve_evidence_artifact_ref(
            "task-1", artifacts, reference,
        )["content_available"])

    def test_oversize_task_budget_and_integrity_guards(self):
        oversized = {}
        oversized_ref = capture_tool_evidence_artifact(
            "task-1", oversized,
            {"role": "security", "run_id": "security-1", "step": 1,
             "tool": "read_file"},
            {"evidence_id": "large", "tool": "read_file",
             "output": "x" * (MAX_EVIDENCE_ARTIFACT_BYTES + 1)},
        )
        record = oversized[oversized_ref["artifact_id"]]
        self.assertFalse(record["metadata"]["content_available"])
        self.assertEqual("artifact_too_large", record["metadata"]["reason"])
        self.assertIsNone(record["content"])
        self.assertEqual("artifact_too_large", resolve_evidence_artifact_ref(
            "task-1", oversized, oversized_ref,
        )["reason"])

        artifacts = {}
        payload_size = MAX_TASK_EVIDENCE_ARTIFACT_BYTES // 4
        refs = []
        for index in range(5):
            refs.append(capture_tool_evidence_artifact(
                "task-1", artifacts,
                {"role": "security", "run_id": "security-1",
                 "step": index + 1, "tool": "read_file"},
                {"evidence_id": "e%d" % index, "tool": "read_file",
                 "output": "x" * (payload_size - 200)},
            ))
        self.assertTrue(any(
            not value["metadata"]["content_available"]
            and value["metadata"]["reason"] == "task_artifact_budget_exceeded"
            for value in artifacts.values()
        ))

        corrupt = json.loads(json.dumps(artifacts))
        corrupt[refs[0]["artifact_id"]]["content_sha256"] = "0" * 64
        with self.assertRaises(EvidenceArtifactError):
            resolve_evidence_artifact_ref("task-1", corrupt, refs[0])


if __name__ == "__main__":
    unittest.main()
