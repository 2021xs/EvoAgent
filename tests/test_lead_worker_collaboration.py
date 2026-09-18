import copy
import json
import os
import tempfile
import unittest

from evoagent.agentic_core import (
    AgenticReviewer,
    ExecutionConfigurationError,
    ensure_candidate_identity,
)
from evoagent.context_manager import ContextManager
from evoagent.diff_parser import parse_unified_diff
from evoagent.gates import FindingGate
from evoagent.memory import MemoryManager
from evoagent.models import Finding, Severity
from evoagent.skills import AgentSkill
from evoagent.store import TaskStore


DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(user_input)\n"


def agent_skill(version, guidance, allowed_tool="search_diff", resource=""):
    return AgentSkill.from_markdown(
        """---
name: security-review
description: Versioned security review guidance.
allowed-tools:
  - %s
---

%s
""" % (allowed_tool, guidance),
        source="evolved-db", version=str(version),
        resources={"guidance.txt": resource or ("resource-" + str(version))},
    )


class RecordingTaskStore(TaskStore):
    def __init__(self, path):
        super().__init__(path)
        self.lead_sessions = []

    def save_checkpoint(
        self, task_id, node, state, status="completed", attempt=1, error="",
    ):
        if node == "agentic-lead-session":
            self.lead_sessions.append(copy.deepcopy(state["session"]))
        return super().save_checkpoint(task_id, node, state, status, attempt, error)


class HierarchicalClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self):
        self.calls = []
        self.payloads = []
        self.security_calls = 0

    def complete_json(self, role, _system, user, ledger=None, max_tokens=None):
        managed = json.loads(user)
        task = json.loads(managed["task"])
        self.calls.append((role, task.get("phase", "worker")))
        self.payloads.append((role, task))
        if ledger:
            ledger.record_model(
                role, self.provider, self.model,
                {"prompt_tokens": 10, "completion_tokens": 5}, 1,
            )
        if role == "lead":
            if task["phase"] == "delegate":
                return {
                    "action": "final", "delegations": [
                        {
                            "assignment_id": "security-1", "worker": "security",
                            "objective": "Trace the changed input into dangerous calls.",
                        },
                        {
                            "assignment_id": "reliability-1",
                            "worker": "correctness-reliability",
                            "objective": "Review failure and resource behavior.",
                        },
                    ], "risk_level": "high",
                }
            if task["phase"] == "assess-workers" and task["revision_round"] == 0:
                return {
                    "action": "final", "revision_requests": [{
                        "assignment_id": "security-1", "worker": "security",
                        "guidance": "Add an exact changed-line finding for dynamic execution.",
                        "required_evidence": ["changed-line evidence"],
                    }], "critic_objective": "Challenge every proposed finding.",
                }
            if task["phase"] == "assess-workers":
                return {
                    "action": "final", "revision_requests": [],
                    "critic_objective": "Challenge every proposed finding.",
                }
            if task["phase"] == "finalize":
                return {
                    "action": "final",
                    "accepted_finding_indices": list(
                        range(len(task["candidate_findings"]))
                    ),
                    "confidence_adjustments": [],
                    "resolution_summary": "Workers supplied evidence and Critic approved.",
                }
        if role == "security":
            self.security_calls += 1
            if self.security_calls == 1:
                return {"action": "final", "findings": []}
            return {"action": "final", "findings": [{
                "rule_id": "SEC-LEAD-REVISION", "severity": "medium",
                "title": "Dynamic execution", "explanation": "Input is executed as code.",
                "path": "app.py", "line": 1, "evidence": "eval(user_input)",
                "fix": "Use a constrained parser.",
                "test": "Prove expressions are treated as data.", "confidence": 0.9,
            }]}
        if role == "correctness-reliability":
            return {"action": "final", "findings": []}
        if role == "critic":
            return {
                "action": "final", "decisions": [
                    {
                        "finding_index": index, "accepted": True,
                        "objections": [], "confidence_adjustment": 0.0,
                    }
                    for index, _item in enumerate(task["candidates"])
                ],
            }
        raise AssertionError((role, task))


class IdentityHierarchicalClient(HierarchicalClient):
    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        result = super().complete_json(role, system, user, ledger, max_tokens)
        if role == "security" and self.security_calls == 1:
            return {"action": "final", "findings": [{
                "rule_id": "SEC-EVAL", "cwe": "CWE-95",
                "severity": "critical", "title": "Dynamic execution",
                "explanation": "Input is executed as code.",
                "path": "app.py", "line": 1, "evidence": "eval(user_input)",
                "fix": "Use a constrained parser.",
                "test": "Prove expressions are treated as data.", "confidence": 0.8,
            }]}
        return result


class DuplicateScanner:
    name = "duplicate-agent"

    def review(self, _diff, _parsed):
        return [Finding(
            rule_id="SEC-EVAL", cwe="CWE-95", severity=Severity.CRITICAL,
            title="Duplicate dynamic execution",
            explanation="A second scanner found the same issue.",
            path="app.py", line=1, evidence="eval(user_input)",
            fix="Use a constrained parser.", test="Reject executable input.",
            confidence=0.7,
        )]


class DecisionTraceClient(HierarchicalClient):
    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        task = json.loads(json.loads(user)["task"])
        result = super().complete_json(role, system, user, ledger, max_tokens)
        if role == "critic":
            for index, decision in enumerate(result["decisions"]):
                decision["accepted"] = index != 0
                decision["objections"] = ["critic objection"] if index == 0 else []
        if role == "lead" and task.get("phase") == "finalize":
            result["accepted_finding_indices"] = [0]
        return result


class SnapshotHierarchicalClient(HierarchicalClient):
    def __init__(self):
        super().__init__()
        self.worker_calls = {}

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        result = super().complete_json(role, system, user, ledger, max_tokens)
        if role == "lead":
            task = json.loads(json.loads(user)["task"])
            if task.get("phase") == "delegate":
                result["delegations"][0]["skills"] = ["security-review"]
        elif role in {"security", "correctness-reliability"}:
            self.worker_calls.setdefault(role, []).append({
                "system_prompt": system,
                "managed_user_context": user,
                "parsed_action": copy.deepcopy(result),
            })
        return result


class EvidenceChallengeClient(HierarchicalClient):
    def __init__(
        self, request_indices=(1,), final_accepted=True,
        use_tool=True, request_again_in_final=False,
    ):
        super().__init__()
        self.request_indices = tuple(request_indices)
        self.final_accepted = final_accepted
        self.use_tool = use_tool
        self.request_again_in_final = request_again_in_final
        self.critic_pass1_calls = 0
        self.critic_final_calls = 0
        self.evidence_model_calls = 0
        self.evidence_runs = 0

    def _record(self, role, task, ledger):
        self.calls.append((role, task.get("phase", "worker")))
        self.payloads.append((role, task))
        if ledger:
            ledger.record_model(
                role, self.provider, self.model,
                {"prompt_tokens": 10, "completion_tokens": 5}, 1,
            )

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        managed = json.loads(user)
        task = json.loads(managed["task"])
        if role in {"security", "correctness-reliability"} and task.get(
            "communication_type"
        ) == "REQUEST_EVIDENCE":
            self._record(role, task, ledger)
            self.evidence_model_calls += 1
            if self.evidence_model_calls == 1:
                self.evidence_runs += 1
            if self.use_tool and not managed.get("observations"):
                return {
                    "action": "tool", "tool": "search_diff",
                    "arguments": {"query": "eval"},
                    "reason": "Verify the challenged changed line.",
                }
            evidence_ids = []
            for observation in managed.get("observations") or []:
                result = observation.get("result") or {}
                if result.get("evidence_id"):
                    evidence_ids.append(result["evidence_id"])
            return {
                "action": "final", "status": "answered",
                "summary": "The changed line directly evaluates untrusted input.",
                "evidence_ids": evidence_ids,
            }
        if role == "critic" and task.get("phase") == "critic-final":
            self._record(role, task, ledger)
            self.critic_final_calls += 1
            decision = {
                "finding_index": task["candidate"]["finding_index"],
                "accepted": self.final_accepted,
                "objections": [] if self.final_accepted else ["Evidence remained weak."],
                "confidence_adjustment": 0.0,
                "supporting_evidence_ids": [
                    item["evidence_id"]
                    for item in task["evidence_response"].get("evidence_refs") or []
                ],
            }
            if self.request_again_in_final:
                decision["evidence_request"] = "Ask again."
            return {"action": "final", "decisions": [decision]}
        result = super().complete_json(role, system, user, ledger, max_tokens)
        if role == "critic":
            self.critic_pass1_calls += 1
            for index in self.request_indices:
                if 0 <= index < len(result["decisions"]):
                    result["decisions"][index]["accepted"] = False
                    result["decisions"][index]["objections"] = [
                        "Authorization evidence is incomplete."
                    ]
                    result["decisions"][index]["evidence_request"] = (
                        "Show whether the challenged callee validates ownership."
                    )
        return result


class PinnedEvidenceClient(EvidenceChallengeClient):
    def __init__(self):
        super().__init__(request_indices=(1,), use_tool=False)
        self.system_prompts = []
        self.evidence_contexts = []

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        managed = json.loads(user)
        task = json.loads(managed["task"])
        self.system_prompts.append((role, task.get("phase", "worker"), system))
        if role in {"security", "correctness-reliability"} and task.get(
            "communication_type"
        ) == "REQUEST_EVIDENCE":
            self._record(role, task, ledger)
            self.evidence_model_calls += 1
            self.evidence_contexts.append(copy.deepcopy(managed))
            if not managed.get("observations"):
                self.evidence_runs += 1
                return {
                    "action": "tool", "tool": "read_skill_resource",
                    "arguments": {
                        "skill": "security-review", "path": "guidance.txt",
                    },
                    "reason": "Read the pinned guidance resource.",
                }
            evidence_ids = [
                (item.get("result") or {}).get("evidence_id")
                for item in managed.get("observations") or []
                if (item.get("result") or {}).get("evidence_id")
            ]
            return {
                "action": "final", "status": "answered",
                "summary": "Pinned resource inspected.",
                "evidence_ids": evidence_ids,
            }
        result = super().complete_json(role, system, user, ledger, max_tokens)
        if role == "lead" and task.get("phase") == "delegate":
            result["delegations"][0]["skills"] = ["security-review"]
        return result


class NormalWorkerToolClient(HierarchicalClient):
    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        managed = json.loads(user)
        task = json.loads(managed["task"])
        self.calls.append((role, task.get("phase", "worker")))
        self.payloads.append((role, task))
        if ledger:
            ledger.record_model(
                role, self.provider, self.model,
                {"prompt_tokens": 10, "completion_tokens": 5}, 1,
            )
        if role == "lead":
            if task["phase"] == "delegate":
                return {
                    "action": "final", "risk_level": "high",
                    "delegations": [{
                        "assignment_id": "security-1", "worker": "security",
                        "objective": "Inspect dynamic execution.",
                    }],
                }
            if task["phase"] == "assess-workers":
                return {
                    "action": "final", "revision_requests": [],
                    "critic_objective": "Check the evidence.",
                }
            if task["phase"] == "finalize":
                return {
                    "action": "final",
                    "accepted_finding_indices": list(range(len(task["candidate_findings"]))),
                    "confidence_adjustments": [],
                }
        if role == "security":
            if not managed.get("observations"):
                return {
                    "action": "tool", "tool": "search_diff",
                    "arguments": {"query": "eval"}, "reason": "collect evidence",
                }
            evidence_id = managed["observations"][0]["result"]["evidence_id"]
            return {"action": "final", "findings": [{
                "rule_id": "SEC-EVAL", "cwe": "CWE-95", "severity": "critical",
                "title": "Dynamic execution", "explanation": "Input is executed.",
                "path": "app.py", "line": 1, "evidence": "eval(user_input)",
                "evidence_ids": [evidence_id], "fix": "Use a parser.",
                "test": "Reject executable input.", "confidence": 0.8,
            }]}
        if role == "correctness-reliability":
            return {"action": "final", "findings": []}
        if role == "critic":
            return {"action": "final", "decisions": [{
                "finding_index": index, "accepted": True, "objections": [],
                "confidence_adjustment": 0.0,
            } for index, _item in enumerate(task["candidates"])]}
        raise AssertionError((role, task))


class LeadWorkerCollaborationTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = RecordingTaskStore(self.path)
        self.store.create("task", "org/repo", 1, {
            "mode": "agentic",
            "enabled_agents": [
                "lead", "security", "correctness-reliability", "critic",
            ],
        })

    def tearDown(self):
        os.unlink(self.path)

    def assertNoCandidateIdentity(self, value):
        if isinstance(value, dict):
            self.assertNotIn("candidate_id", value)
            self.assertNotIn("candidate_trace", value)
            for item in value.values():
                self.assertNoCandidateIdentity(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.assertNoCandidateIdentity(item)

    def restore_lead_session(self, session):
        final = self.store.load_checkpoints("task")["agentic-lead-session"]
        self.store.save_checkpoint("task", "agentic-lead-session", {
            "protocol": "lead-workers-v3",
            "session": copy.deepcopy(session),
            "execution": final["state"]["execution"],
        }, "in_progress", 1)

    def session_snapshot(self, predicate):
        return next(copy.deepcopy(value) for value in self.store.lead_sessions if predicate(value))

    def test_lead_delegates_requests_revision_and_synthesizes(self):
        client = HierarchicalClient()
        reviewer = AgenticReviewer(self.store, client)

        findings = reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo"
        )
        summary = reviewer.collaboration_summary("task")

        self.assertIn("SEC-LEAD-REVISION", {item.rule_id for item in findings})
        self.assertEqual("lead-workers", summary["collaboration"]["protocol"])
        self.assertEqual(2, client.security_calls)
        self.assertEqual(1, len(summary["collaboration"]["revision_results"]))
        self.assertEqual(
            "high-risk-one-revision-round",
            summary["collaboration"]["stop_reason"],
        )
        self.assertEqual(1, summary["collaboration"]["revision_rounds"])
        self.assertEqual(
            1, client.calls.count(("lead", "assess-workers"))
        )
        self.assertEqual(7, summary["execution"]["llm_calls"])
        session_events = {
            item["event"]
            for item in summary["execution"]["agent_traces"]["lead-session"]
        }
        self.assertTrue({
            "assignment_created", "worker_reported", "revision_completed",
            "lead_activated", "lead_completed",
        }.issubset(session_events))
        checkpoint = self.store.load_checkpoints("task")["agentic-lead-session"]
        self.assertEqual("completed", checkpoint["status"])
        session = checkpoint["state"]["session"]
        self.assertEqual("completed", session["phase"])
        self.assertIsNone(session["critic_challenge"])
        self.assertTrue(all(
            item.get("candidate_id") for item in session["scanner_findings"]
        ))
        self.assertTrue(all(
            item.get("candidate_id") for item in session["critic_candidates"]
        ))
        self.assertTrue(all(
            item.get("candidate_id") for item in session["accepted_findings"]
        ))

        phases = {(role, task.get("phase", "worker")) for role, task in client.payloads}
        self.assertTrue({
            ("lead", "delegate"), ("lead", "assess-workers"),
            ("lead", "finalize"), ("security", "worker"),
            ("correctness-reliability", "worker"), ("critic", "worker"),
        }.issubset(phases))
        self.assertEqual(2, sum(role == "security" for role, _task in client.payloads))
        self.assertEqual(
            {0, 1}, {
                int(task["lead_assignment"].get("revision_round", 0))
                for role, task in client.payloads if role == "security"
            },
        )
        for _role, payload in client.payloads:
            self.assertNoCandidateIdentity(payload)

    def test_worker_execution_snapshots_preserve_each_run_and_stay_internal(self):
        client = SnapshotHierarchicalClient()
        skill = AgentSkill.from_markdown(
            """---
name: security-review
description: Historical security guidance.
---

Historical CWE-95 guidance.
""",
            source="evolved-db", version="7",
        )
        reviewer = AgenticReviewer(
            self.store, client, skill_provider=lambda _tenant: [skill],
        )

        reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo",
        )

        session = self.store.load_checkpoints("task")[
            "agentic-lead-session"
        ]["state"]["session"]
        snapshots = session["worker_execution_snapshots"]
        self.assertIn("security-1", snapshots)
        self.assertIn("1:security-1", snapshots)
        initial = snapshots["security-1"]
        revision = snapshots["1:security-1"]
        self.assertEqual(0, initial["revision_round"])
        self.assertEqual(1, revision["revision_round"])
        self.assertEqual(
            client.worker_calls["security"][0]["system_prompt"],
            initial["system_prompt"],
        )
        self.assertEqual(
            client.worker_calls["security"][0]["managed_user_context"],
            initial["final_managed_user_context"],
        )
        self.assertEqual(
            client.worker_calls["security"][0]["parsed_action"],
            initial["final_parsed_model_action"],
        )
        self.assertEqual({
            "name": "security-review", "version": "7",
            "source": "evolved-db", "content_sha256": skill.content_sha256,
        }, initial["selected_skills"][0])
        self.assertEqual(
            client.worker_calls["security"][1]["parsed_action"],
            revision["final_parsed_model_action"],
        )
        self.assertNotIn(
            "worker_execution_snapshots",
            json.dumps(reviewer.collaboration_summary("task")),
        )
        self.assertNotIn(
            "Historical CWE-95 guidance",
            json.dumps(reviewer.collaboration_summary("task")),
        )
        for _role, payload in client.payloads:
            self.assertNotIn("worker_execution_snapshots", json.dumps(payload))

    def test_execution_profile_is_created_once_at_first_agent_execution(self):
        calls = []
        skill = agent_skill("1", "PINNED-V1")
        reviewer = AgenticReviewer(
            self.store, SnapshotHierarchicalClient(),
            skill_provider=lambda tenant: calls.append(tenant) or [skill],
        )

        reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo",
        )

        profiles = [
            value["execution_profile"] for value in self.store.lead_sessions
            if isinstance(value.get("execution_profile"), dict)
        ]
        self.assertTrue(profiles)
        self.assertEqual(1, len({item["profile_sha256"] for item in profiles}))
        self.assertEqual(["default"], calls)
        first = profiles[0]
        self.assertEqual("1", first["skills"][0]["version"])
        self.assertEqual(skill.content_sha256, first["skills"][0]["content_sha256"])
        self.assertEqual("PINNED-V1", AgentSkill.from_artifact(
            first["skills"][0]["artifact"], "1",
        ).instructions)

        self.store.create("queued-only", "org/repo", 2, {"mode": "agentic"})
        self.assertNotIn(
            "agentic-lead-session", self.store.load_checkpoints("queued-only"),
        )

    def test_pending_initial_and_revision_workers_use_pinned_skill_after_promotion(self):
        v1 = agent_skill("1", "PINNED-V1")
        v2 = agent_skill("2", "CURRENT-V2", allowed_tool="read_file")
        first = SnapshotHierarchicalClient()
        AgenticReviewer(
            self.store, first, skill_provider=lambda _tenant: [v1],
        ).review_with_context("task", DIFF, parse_unified_diff(DIFF), "org/repo")

        pending_initial = self.session_snapshot(
            lambda value: bool(value.get("delegations"))
            and not value.get("worker_results")
        )
        self.restore_lead_session(pending_initial)
        provider_calls = []
        resumed_initial = SnapshotHierarchicalClient()
        AgenticReviewer(
            self.store, resumed_initial,
            skill_provider=lambda tenant: provider_calls.append(tenant) or [v2],
        ).review_with_context("task", DIFF, parse_unified_diff(DIFF), "org/repo")
        session = self.store.load_checkpoints("task")["agentic-lead-session"][
            "state"
        ]["session"]
        self.assertEqual([], provider_calls)
        self.assertEqual("1", session["worker_execution_snapshots"][
            "security-1"
        ]["selected_skills"][0]["version"])
        self.assertIn("PINNED-V1", session["worker_execution_snapshots"][
            "security-1"
        ]["system_prompt"])

        pending_revision = self.session_snapshot(
            lambda value: bool(value.get("lead_assessments"))
            and "1:security-1" not in value.get("worker_execution_snapshots", {})
        )
        self.restore_lead_session(pending_revision)
        resumed_revision = SnapshotHierarchicalClient()
        AgenticReviewer(
            self.store, resumed_revision, skill_provider=lambda _tenant: [v2],
        ).review_with_context("task", DIFF, parse_unified_diff(DIFF), "org/repo")
        session = self.store.load_checkpoints("task")["agentic-lead-session"][
            "state"
        ]["session"]
        self.assertEqual("1", session["worker_execution_snapshots"][
            "1:security-1"
        ]["selected_skills"][0]["version"])

        self.store.create("task-v2", "org/repo", 2, {
            "mode": "agentic",
            "enabled_agents": [
                "lead", "security", "correctness-reliability", "critic",
            ],
        })
        AgenticReviewer(
            self.store, SnapshotHierarchicalClient(),
            skill_provider=lambda _tenant: [v2],
        ).review_with_context(
            "task-v2", DIFF, parse_unified_diff(DIFF), "org/repo",
        )
        task_v2 = self.store.load_checkpoints("task-v2")["agentic-lead-session"][
            "state"
        ]["session"]
        self.assertEqual("2", task_v2["worker_execution_snapshots"][
            "security-1"
        ]["selected_skills"][0]["version"])

    def test_evidence_resume_replays_pinned_skill_resources_tools_and_overlay(self):
        v1 = agent_skill(
            "1", "PINNED-V1-GUIDANCE", allowed_tool="search_diff",
            resource="PINNED-V1-RESOURCE",
        )
        v2 = agent_skill(
            "2", "CURRENT-V2-GUIDANCE", allowed_tool="read_file",
            resource="CURRENT-V2-RESOURCE",
        )
        AgenticReviewer(
            self.store, PinnedEvidenceClient(), prompt_overlay="PINNED-OVERLAY",
            structured_config={"budget_parameters": {"security": 1234, "critic": 1000}},
            skill_provider=lambda _tenant: [v1],
        ).review_with_context("task", DIFF, parse_unified_diff(DIFF), "org/repo")
        requested = self.session_snapshot(
            lambda value: (value.get("critic_challenge") or {}).get("status")
            == "requested"
        )
        self.restore_lead_session(requested)

        provider_calls = []
        resumed = PinnedEvidenceClient()
        AgenticReviewer(
            self.store, resumed, prompt_overlay="CURRENT-OVERLAY",
            structured_config={"budget_parameters": {"security": 3000, "critic": 3000}},
            skill_provider=lambda tenant: provider_calls.append(tenant) or [v2],
        ).review_with_context("task", DIFF, parse_unified_diff(DIFF), "org/repo")

        self.assertEqual([], provider_calls)
        self.assertEqual(1, resumed.evidence_runs)
        evidence_systems = [
            system for role, _phase, system in resumed.system_prompts
            if role == "security" and "bounded evidence" in system
        ]
        self.assertEqual(2, len(evidence_systems))
        self.assertTrue(all("PINNED-V1-GUIDANCE" in item for item in evidence_systems))
        self.assertTrue(all("PINNED-OVERLAY" in item for item in evidence_systems))
        self.assertTrue(all('"security": 1234' in item for item in evidence_systems))
        self.assertTrue(all("CURRENT-V2-GUIDANCE" not in item for item in evidence_systems))
        self.assertTrue(all("CURRENT-OVERLAY" not in item for item in evidence_systems))
        self.assertTrue(all('"security": 3000' not in item for item in evidence_systems))
        tool_names = {
            item["name"] for item in resumed.evidence_contexts[0]["available_tools"]
        }
        self.assertIn("search_diff", tool_names)
        self.assertIn("read_skill_resource", tool_names)
        self.assertNotIn("read_file", tool_names)
        self.assertIn(
            "PINNED-V1-RESOURCE", json.dumps(resumed.evidence_contexts[-1]),
        )
        critic_final_system = next(
            system for role, phase, system in resumed.system_prompts
            if role == "critic" and phase == "critic-final"
        )
        self.assertIn("PINNED-OVERLAY", critic_final_system)
        self.assertNotIn("CURRENT-OVERLAY", critic_final_system)

    def test_execution_profile_runtime_mismatch_fails_closed(self):
        AgenticReviewer(self.store, HierarchicalClient()).review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo",
        )

        changed_model = HierarchicalClient()
        changed_model.model = "different-model"
        with self.assertRaisesRegex(
            ExecutionConfigurationError, "configuration changed.*model",
        ):
            AgenticReviewer(self.store, changed_model).review_with_context(
                "task", DIFF, parse_unified_diff(DIFF), "org/repo",
            )
        with self.assertRaisesRegex(
            ExecutionConfigurationError, "configuration changed.*context_manager",
        ):
            AgenticReviewer(
                self.store, HierarchicalClient(),
                context_manager=ContextManager(diff_token_budget=2048),
            ).review_with_context(
                "task", DIFF, parse_unified_diff(DIFF), "org/repo",
            )
        with self.assertRaisesRegex(
            ExecutionConfigurationError, "configuration changed.*default_token_budget",
        ):
            AgenticReviewer(
                self.store, HierarchicalClient(), default_token_budget=9000,
            ).review_with_context(
                "task", DIFF, parse_unified_diff(DIFF), "org/repo",
            )

        class ChangedPolicyReviewer(AgenticReviewer):
            @classmethod
            def _code_policy_sha256(cls):
                return "changed-policy"

        with self.assertRaisesRegex(
            ExecutionConfigurationError, "configuration changed.*code_policy_sha256",
        ):
            ChangedPolicyReviewer(self.store, HierarchicalClient()).review_with_context(
                "task", DIFF, parse_unified_diff(DIFF), "org/repo",
            )

    def test_execution_profile_integrity_size_and_privacy(self):
        skill = agent_skill("1", "PINNED-PRIVATE-GUIDANCE")
        client = SnapshotHierarchicalClient()
        reviewer = AgenticReviewer(
            self.store, client, skill_provider=lambda _tenant: [skill],
        )
        reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo",
        )
        checkpoint = self.store.load_checkpoints("task")["agentic-lead-session"]
        session = checkpoint["state"]["session"]
        self.assertIn("execution_profile", session)
        self.assertNotIn("execution_profile", json.dumps(
            reviewer.collaboration_summary("task"),
        ))
        self.assertNotIn("execution_profile", json.dumps(self.store.get("task")))
        for _role, payload in client.payloads:
            self.assertNotIn("execution_profile", json.dumps(payload))

        corrupt = copy.deepcopy(session)
        profile = corrupt["execution_profile"]
        profile["skills"][0]["artifact"]["files"]["SKILL.md"] += "\ntampered\n"
        unsigned = dict(profile)
        unsigned.pop("profile_sha256", None)
        profile["profile_sha256"] = AgenticReviewer._value_sha256(unsigned)
        self.restore_lead_session(corrupt)
        with self.assertRaisesRegex(
            ExecutionConfigurationError, "Skill hash mismatch",
        ):
            AgenticReviewer(
                self.store, HierarchicalClient(), skill_provider=lambda _tenant: [],
            ).review_with_context(
                "task", DIFF, parse_unified_diff(DIFF), "org/repo",
            )

        self.store.create("oversized", "org/repo", 3, {
            "mode": "agentic",
            "enabled_agents": ["lead", "security", "critic"],
        })
        huge = AgentSkill.from_markdown(
            """---
name: huge-skill
description: Deliberately oversized profile fixture.
---

Bounded guidance.
""",
            resources={
                "one.txt": "a" * 750000,
                "two.txt": "b" * 750000,
                "three.txt": "c" * 750000,
            },
        )
        with self.assertRaisesRegex(
            ExecutionConfigurationError, "checkpoint limit",
        ):
            AgenticReviewer(
                self.store, HierarchicalClient(),
                skill_provider=lambda _tenant: [huge],
            ).review_with_context(
                "oversized", DIFF, parse_unified_diff(DIFF), "org/repo",
            )
        self.assertNotIn(
            "agentic-lead-session", self.store.load_checkpoints("oversized"),
        )

    def test_bounded_evidence_protocol_routes_and_stays_internal(self):
        client = EvidenceChallengeClient(request_again_in_final=True)
        memory = MemoryManager(self.store)
        reviewer = AgenticReviewer(self.store, client, memory_manager=memory)

        reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo",
        )

        checkpoint = self.store.load_checkpoints("task")["agentic-lead-session"]
        session = checkpoint["state"]["session"]
        challenge = session["critic_challenge"]
        request = challenge["request"]
        response = challenge["response"]
        requested_session = next(
            value for value in self.store.lead_sessions
            if (value.get("critic_challenge") or {}).get("status") == "requested"
        )

        self.assertEqual("complete", challenge["status"])
        self.assertEqual("REQUEST_EVIDENCE", request["message_type"])
        self.assertEqual("critic", request["sender"])
        self.assertEqual("security", request["recipient"])
        self.assertTrue(request["subject_id"].startswith("candidate:"))
        self.assertEqual("EVIDENCE_RESPONSE", response["message_type"])
        self.assertEqual("security", response["sender"])
        self.assertEqual("critic", response["recipient"])
        self.assertEqual(request["message_id"], response["correlation_id"])
        self.assertEqual(request["subject_id"], response["subject_id"])
        self.assertEqual("answered", response["payload"]["status"])
        self.assertTrue(response["payload"]["evidence_refs"])
        self.assertEqual(1, client.evidence_runs)
        self.assertEqual(1, client.critic_pass1_calls)
        self.assertEqual(1, client.critic_final_calls)
        self.assertEqual(
            requested_session["worker_results"], session["worker_results"],
        )
        self.assertEqual(
            set(requested_session["candidate_trace"]["candidates"]),
            set(session["candidate_trace"]["candidates"]),
        )
        evidence_payloads = [
            payload for role, payload in client.payloads
            if role in {"security", "correctness-reliability"}
            and payload.get("communication_type") == "REQUEST_EVIDENCE"
        ]
        self.assertEqual(2, len(evidence_payloads))
        self.assertEqual({"security-1"}, {
            payload["lead_assignment"]["assignment_id"]
            for payload in evidence_payloads
        })
        final_payload = next(
            payload for role, payload in client.payloads
            if role == "critic" and payload.get("phase") == "critic-final"
        )
        self.assertEqual(
            request["payload"]["question"], final_payload["evidence_request"],
        )
        self.assertEqual(
            response["payload"]["summary"],
            final_payload["evidence_response"]["summary"],
        )
        internal_ref = response["payload"]["evidence_refs"][0]
        final_ref = final_payload["evidence_response"]["evidence_refs"][0]
        self.assertIn("artifact_id", internal_ref)
        self.assertIn("content_sha256", internal_ref)
        self.assertNotIn("artifact_id", final_ref)
        self.assertNotIn("content_sha256", final_ref)
        self.assertTrue(final_ref["content_available"])
        artifacts = session["evidence_artifacts"]
        self.assertIn(internal_ref["artifact_id"], artifacts)
        self.assertEqual(
            internal_ref["content_sha256"],
            artifacts[internal_ref["artifact_id"]]["content_sha256"],
        )
        self.assertNotIn("evidence_request", challenge["final_decision"])
        self.assertTrue(any(
            item["role"] == "security" and item["tool"] == "search_diff"
            for item in checkpoint["state"]["execution"]["tool_call_log"]
        ))

        public_values = [
            reviewer.collaboration_summary("task"), self.store.get("task"),
        ]
        for value in public_values:
            rendered = json.dumps(value)
            self.assertNotIn("REQUEST_EVIDENCE", rendered)
            self.assertNotIn("EVIDENCE_RESPONSE", rendered)
            self.assertNotIn(request["message_id"], rendered)
            self.assertNotIn(request["subject_id"], rendered)
            self.assertNotIn("evidence_artifacts", rendered)
        semantic_memory = json.dumps(memory.recall(
            "default", "org/repo", "SEC-LEAD-REVISION",
        ))
        self.assertNotIn("REQUEST_EVIDENCE", semantic_memory)
        self.assertNotIn("EVIDENCE_RESPONSE", semantic_memory)
        self.assertNotIn(request["message_id"], semantic_memory)
        self.assertNotIn("artifact:", semantic_memory)
        self.assertNotIn("evidence_artifacts", semantic_memory)
        for _role, payload in client.payloads:
            self.assertNotIn("candidate_id", json.dumps(payload))
            self.assertNotIn("critic_challenge", json.dumps(payload))
            self.assertNotIn(internal_ref["artifact_id"], json.dumps(payload))
        unrelated = [
            payload for role, payload in client.payloads
            if role != "critic" and payload.get("communication_type") is None
        ]
        self.assertTrue(unrelated)
        self.assertTrue(all(
            request["payload"]["question"] not in json.dumps(payload)
            for payload in unrelated
        ))

    def test_normal_worker_artifact_persists_and_resumes_without_tool_rerun(self):
        client = NormalWorkerToolClient()
        reviewer = AgenticReviewer(self.store, client)
        reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo",
        )
        session = self.store.load_checkpoints("task")[
            "agentic-lead-session"
        ]["state"]["session"]
        artifacts = session["evidence_artifacts"]
        self.assertEqual(1, len(artifacts))
        finding = session["worker_results"]["security-1"]["findings"][0]
        reference = finding["evidence_refs"][0]
        self.assertIn(reference["artifact_id"], artifacts)
        self.assertEqual(
            "search_diff", artifacts[reference["artifact_id"]]["producer"]["tool"],
        )

        resumed = NormalWorkerToolClient()
        AgenticReviewer(self.store, resumed).review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo",
        )
        self.assertNotIn(("security", "worker"), resumed.calls)
        resumed_session = self.store.load_checkpoints("task")[
            "agentic-lead-session"
        ]["state"]["session"]
        self.assertEqual(artifacts, resumed_session["evidence_artifacts"])

    def test_challenge_selects_one_lowest_routable_request_and_rejects_unroutable(self):
        client = EvidenceChallengeClient(request_indices=(0, 1), use_tool=False)
        reviewer = AgenticReviewer(self.store, client)

        reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo",
        )

        session = self.store.load_checkpoints("task")[
            "agentic-lead-session"
        ]["state"]["session"]
        self.assertEqual(1, client.evidence_runs)
        self.assertEqual(1, session["critic_challenge"]["finding_index"])
        self.assertEqual("security", session["critic_challenge"]["request"][
            "recipient"
        ])

        candidate = Finding(
            rule_id="SCANNER", severity=Severity.MEDIUM,
            title="Scanner candidate", explanation="Scanner-only evidence.",
            path="app.py", line=1, evidence="eval(user_input)",
            fix="Avoid eval.", test="Reject code.", candidate_id="scanner-id",
        )
        unroutable = {
            "delegations": session["delegations"],
            "candidate_trace": {
                "candidates": {"scanner-id": {"origin": {"producer": "scanner"}}},
                "merge_lineage": [],
            },
            "critic_pass1_decisions": [{
                "finding_index": 0, "accepted": False, "objections": [],
                "evidence_request": "Need more evidence.",
            }],
        }
        self.assertIsNone(reviewer._create_critic_challenge(unroutable, [candidate]))
        candidate.candidate_id = None
        self.assertIsNone(reviewer._create_critic_challenge(unroutable, [candidate]))

    def test_critic_challenge_resume_states_do_not_duplicate_completed_steps(self):
        initial = EvidenceChallengeClient(use_tool=True)
        AgenticReviewer(self.store, initial).review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo",
        )
        final_checkpoint = self.store.load_checkpoints("task")[
            "agentic-lead-session"
        ]
        snapshots = {
            status: next(
                copy.deepcopy(value) for value in self.store.lead_sessions
                if (value.get("critic_challenge") or {}).get("status") == status
                and not value.get("critic_complete")
            )
            for status in ("requested", "evidence_received", "complete")
        }

        def restore_and_run(status):
            self.store.save_checkpoint("task", "agentic-lead-session", {
                "protocol": "lead-workers-v3",
                "session": snapshots[status],
                "execution": final_checkpoint["state"]["execution"],
            }, "in_progress", 1)
            client = EvidenceChallengeClient(use_tool=False)
            AgenticReviewer(self.store, client).review_with_context(
                "task", DIFF, parse_unified_diff(DIFF), "org/repo",
            )
            return client

        requested = restore_and_run("requested")
        self.assertEqual(1, requested.evidence_runs)
        self.assertEqual(1, requested.critic_final_calls)
        self.assertEqual(0, requested.critic_pass1_calls)

        evidence_received = restore_and_run("evidence_received")
        self.assertEqual(0, evidence_received.evidence_runs)
        self.assertEqual(1, evidence_received.critic_final_calls)
        self.assertEqual(0, evidence_received.critic_pass1_calls)
        final_payload = next(
            payload for role, payload in evidence_received.payloads
            if role == "critic" and payload.get("phase") == "critic-final"
        )
        self.assertTrue(final_payload["evidence_response"]["evidence_refs"])
        self.assertNotIn(
            "artifact_id",
            final_payload["evidence_response"]["evidence_refs"][0],
        )

        complete = restore_and_run("complete")
        self.assertEqual(0, complete.evidence_runs)
        self.assertEqual(0, complete.critic_final_calls)
        self.assertEqual(0, complete.critic_pass1_calls)

    def test_candidate_identity_is_unique_per_producer_and_revision_occurrence(self):
        reviewer = AgenticReviewer(self.store, IdentityHierarchicalClient())

        reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo"
        )

        session = self.store.load_checkpoints("task")[
            "agentic-lead-session"
        ]["state"]["session"]
        initial_session = next(
            value for value in self.store.lead_sessions
            if value.get("worker_results", {}).get("security-1", {}).get("findings")
            and "1:security-1" not in value.get("worker_results", {})
        )
        scanner = session["scanner_findings"][0]
        initial_worker = initial_session["worker_results"]["security-1"]["findings"][0]
        revision_worker = session["worker_results"]["security-1"]["findings"][0]

        self.assertTrue(scanner["candidate_id"])
        self.assertTrue(initial_worker["candidate_id"])
        self.assertTrue(revision_worker["candidate_id"])
        self.assertEqual("SEC-EVAL", scanner["rule_id"])
        self.assertEqual("SEC-EVAL", initial_worker["rule_id"])
        self.assertNotEqual(scanner["candidate_id"], initial_worker["candidate_id"])
        self.assertNotEqual(initial_worker["candidate_id"], revision_worker["candidate_id"])

        trace = session["candidate_trace"]
        lineage = next(
            item for item in trace["merge_lineage"]
            if item["stage"] == "session_candidate_merge"
            and item["loser_candidate_id"] == initial_worker["candidate_id"]
        )
        self.assertEqual(scanner["candidate_id"], lineage["winner_candidate_id"])
        self.assertEqual("lower_confidence", lineage["reason"])
        self.assertEqual({
            "producer": "worker", "worker": "security", "run_id": "security-1",
            "assignment_id": "security-1", "revision_round": 0,
            "source": "security",
        }, trace["candidates"][initial_worker["candidate_id"]]["origin"])
        self.assertEqual(
            1,
            trace["candidates"][revision_worker["candidate_id"]]["origin"][
                "revision_round"
            ],
        )

    def test_scanner_merge_records_destructive_lineage(self):
        reviewer = AgenticReviewer(
            self.store, HierarchicalClient(), scanners=[DuplicateScanner()],
        )

        reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo"
        )

        session = self.store.load_checkpoints("task")[
            "agentic-lead-session"
        ]["state"]["session"]
        trace = session["candidate_trace"]
        lineage = [
            item for item in trace["merge_lineage"]
            if item["stage"] == "scanner_merge"
        ]
        self.assertEqual(1, len(lineage))
        self.assertEqual("lower_confidence", lineage[0]["reason"])
        loser = trace["candidates"][lineage[0]["loser_candidate_id"]]
        winner = trace["candidates"][lineage[0]["winner_candidate_id"]]
        self.assertEqual("scanner", loser["origin"]["producer"])
        self.assertEqual(
            "declarative-scanner:duplicate-scanner", loser["origin"]["source"]
        )
        self.assertEqual("local-rule-scanner", winner["origin"]["source"])

    def test_merge_lineage_reasons_preserve_current_winner_rules(self):
        def finding(candidate_id, confidence):
            return Finding(
                rule_id="SEC-EVAL", cwe="CWE-95", severity=Severity.CRITICAL,
                title="Dynamic execution", explanation="Input is executed as code.",
                path="app.py", line=1, evidence="eval(user_input)",
                fix="Use a constrained parser.", test="Reject executable input.",
                confidence=confidence, candidate_id=candidate_id,
            )

        trace = {"candidates": {}, "merge_lineage": []}
        merged = AgenticReviewer._merge([
            finding("lower", 0.7), finding("winner", 0.9), finding("tie", 0.9),
        ], trace, stage="session_candidate_merge")

        self.assertEqual(["winner"], [item.candidate_id for item in merged])
        self.assertEqual([
            {
                "stage": "session_candidate_merge",
                "loser_candidate_id": "lower",
                "winner_candidate_id": "winner",
                "reason": "replaced_by_higher_confidence",
            },
            {
                "stage": "session_candidate_merge",
                "loser_candidate_id": "tie",
                "winner_candidate_id": "winner",
                "reason": "tie_kept_existing",
            },
        ], trace["merge_lineage"])

    def test_critic_lead_and_gate_decisions_are_candidate_linked(self):
        reviewer = AgenticReviewer(self.store, DecisionTraceClient())
        reviewer.gate = FindingGate(minimum_confidence=0.95)

        findings = reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo"
        )

        self.assertEqual([], findings)
        session = self.store.load_checkpoints("task")[
            "agentic-lead-session"
        ]["state"]["session"]
        self.assertIsNone(session["critic_challenge"])
        trace = session["candidate_trace"]["candidates"]
        scanner_id = session["scanner_findings"][0]["candidate_id"]
        revision_id = session["worker_results"]["security-1"]["findings"][0][
            "candidate_id"
        ]

        self.assertEqual({
            "accepted": False, "objections": ["critic objection"],
        }, trace[scanner_id]["critic"])
        self.assertEqual({"accepted": True}, trace[scanner_id]["lead_final"])
        self.assertFalse(trace[scanner_id]["gate"]["accepted"])
        self.assertIn(
            "confidence gate: score below 0.95",
            trace[scanner_id]["gate"]["reasons"],
        )
        self.assertEqual({"accepted": True, "objections": []}, trace[revision_id]["critic"])
        self.assertEqual({"accepted": False}, trace[revision_id]["lead_final"])
        self.assertNotIn("gate", trace[revision_id])

    def test_completed_session_resumes_without_repeating_agent_calls(self):
        first = HierarchicalClient()
        AgenticReviewer(self.store, first).review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo"
        )
        before = self.store.load_checkpoints("task")["agentic-lead-session"]
        expected_ids = {
            item["candidate_id"]
            for item in before["state"]["session"]["accepted_findings"]
        }
        resumed_client = HierarchicalClient()
        resumed = AgenticReviewer(self.store, resumed_client)

        findings = resumed.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo"
        )

        self.assertIn("SEC-LEAD-REVISION", {item.rule_id for item in findings})
        self.assertEqual(expected_ids, {item.candidate_id for item in findings})
        self.assertEqual([], resumed_client.calls)
        self.assertGreater(
            resumed.collaboration_summary("task")["execution"]["llm_calls"], 0
        )

    def test_gate_decisions_are_archived_for_future_agent_recall(self):
        memory = MemoryManager(self.store)
        reviewer = AgenticReviewer(self.store, HierarchicalClient(), memory_manager=memory)

        reviewer.review_with_context("task", DIFF, parse_unified_diff(DIFF), "org/repo")

        episodes = memory.recall("default", "org/repo", "SEC-LEAD-REVISION")
        self.assertTrue(any(item["kind"] == "finding_approved" for item in episodes))
        self.assertTrue(any(item["kind"] == "task_summary" for item in episodes))
        for item in episodes:
            self.assertNoCandidateIdentity(item.get("metadata", {}))

    def test_candidate_identity_does_not_change_memory_fingerprint(self):
        memory = MemoryManager(self.store)
        values = []
        for candidate_id in ("candidate-one", "candidate-two"):
            finding = Finding(
                rule_id="SEC-EVAL", severity=Severity.CRITICAL,
                title="Dynamic execution", explanation="Input is executed as code.",
                path="app.py", line=1, evidence="eval(user_input)",
                fix="Use a constrained parser.", test="Reject executable input.",
                candidate_id=candidate_id,
            )
            values.append(memory.remember_finding(
                "default", "org/repo", "task", finding.to_dict(), True, (),
            ))

        self.assertEqual(values[0]["id"], values[1]["id"])
        self.assertNoCandidateIdentity(values[0]["metadata"])

    def test_identity_helper_is_idempotent_and_legacy_restore_stays_unidentified(self):
        finding = Finding(
            rule_id="SEC-EVAL", severity=Severity.CRITICAL,
            title="Dynamic execution", explanation="Input is executed as code.",
            path="app.py", line=1, evidence="eval(user_input)",
            fix="Use a constrained parser.", test="Reject executable input.",
            candidate_id="existing-candidate",
        )

        ensure_candidate_identity([finding])
        legacy = AgenticReviewer._restore_findings([finding.to_dict()])[0]

        self.assertEqual("existing-candidate", finding.candidate_id)
        self.assertIsNone(legacy.candidate_id)


if __name__ == "__main__":
    unittest.main()
