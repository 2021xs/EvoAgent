import json
import os
import tempfile
import unittest

from evoagent.config import Settings
from evoagent.agentic_core import capture_tool_evidence_artifact
from evoagent.models import Finding, Severity
from evoagent.report import to_markdown
from evoagent.service import ReviewService
from agentic_fake import enable_agentic_service


class EvidenceAttributionClient:
    def __init__(self):
        self.calls = []

    def complete_json(
        self, role, system, user, ledger=None, max_tokens=None,
    ):
        if role != "attribution":
            raise AssertionError(role)
        self.calls.append((role, system, user))
        evidence = json.loads(user)
        runs = evidence["worker_runs"]
        contexts = "\n".join(
            item["final_managed_user_context"] for item in runs
        )
        guidance = "\n".join(item["system_prompt"] for item in runs)
        required = str(evidence["expected_finding"].get("evidence") or "")
        if not required or required not in contexts:
            root_cause = "CONTEXT_EVIDENCE_MISSING"
        elif "Historical CWE-95 guidance" not in guidance:
            root_cause = "SKILL_GUIDANCE_GAP"
        else:
            root_cause = "MODEL_REASONING_FAILURE"
        return {
            "action": "final", "root_cause": root_cause,
            "reason": "Classification based only on the supplied historical snapshot.",
            "evidence_summary": ["bounded historical evidence"],
        }


class ServiceTests(unittest.TestCase):
    DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.services = []
        self.settings = Settings(
            host="127.0.0.1", port=8080, db_path=self.path, max_diff_bytes=10000,
            max_steps=8, timeout_seconds=10, llm_base_url="", llm_api_key="", llm_model="",
            github_webhook_secret="", github_token="", auto_post_review=False,
        )

    def tearDown(self):
        for service in self.services:
            service.queue.close()
        os.unlink(self.path)

    def _completed_task(self):
        service = enable_agentic_service(ReviewService(self.settings))
        self.services.append(service)
        result = service.create_review("org/repo", self.DIFF, 1)
        return service, result["task_id"]

    @staticmethod
    def _finding(candidate_id, rule_id="SEC-EVAL", cwe="CWE-95", source="security"):
        return Finding(
            rule_id=rule_id, cwe=cwe, severity=Severity.CRITICAL,
            title="Finding", explanation="A sufficiently detailed explanation.",
            path="a.py", line=1, evidence="eval(data)", fix="Replace eval.",
            test="Add a regression test.", confidence=0.9, source=source,
            candidate_id=candidate_id,
        ).to_internal_dict()

    @staticmethod
    def _save_lead_session(
        service, task_id, trace, findings=None, delegations=None,
        worker_execution_snapshots=None, worker_results=None,
        revision_results=None, evidence_artifacts=None,
    ):
        service.store.save_checkpoint(
            task_id, "agentic-lead-session", {
                "protocol": "lead-workers-v3",
                "session": {
                    "candidate_trace": trace,
                    "scanner_findings": list(findings or []),
                    "worker_results": dict(worker_results or {}),
                    "revision_results": dict(revision_results or {}),
                    "critic_candidates": [], "accepted_findings": [],
                    "delegations": list(delegations or []),
                    "worker_execution_snapshots": dict(
                        worker_execution_snapshots or {}
                    ),
                    "evidence_artifacts": dict(evidence_artifacts or {}),
                },
            }, "completed", 1,
        )

    @staticmethod
    def _expected():
        return {
            "path": "a.py", "line": 1, "cwe": "CWE-95",
            "title": "Dynamic execution", "evidence": "eval(data)",
        }

    @staticmethod
    def _worker_snapshot(
        context="eval(data)", guidance="Historical CWE-95 guidance",
        skills=("security-review",), run_id="security-1", revision_round=0,
    ):
        return {
            "assignment_id": "security-1", "run_id": run_id,
            "worker": "security", "revision_round": revision_round,
            "system_prompt": guidance,
            "selected_skills": [{
                "name": name, "version": "7", "source": "evolved-db",
                "content_sha256": "sha-" + name,
            } for name in skills],
            "final_managed_user_context": json.dumps({
                "task": json.dumps({"diff": context}),
            }),
            "final_parsed_model_action": {"action": "final", "findings": []},
        }

    def _save_discovery_evidence(
        self, service, task_id, context="eval(data)",
        guidance="Historical CWE-95 guidance", skills=("security-review",),
    ):
        other = self._finding("other", "SEC-PATH-TRAVERSAL", "CWE-22")
        snapshot = self._worker_snapshot(context, guidance, skills)
        self._save_lead_session(
            service, task_id, {
                "candidates": {"other": {"origin": {"producer": "scanner"}}},
                "merge_lineage": [],
            }, [other], [{
                "assignment_id": "security-1", "worker": "security",
                "objective": "Review a.py", "files": ["a.py"],
                "skills": list(skills),
            }], {"security-1": snapshot}, {"security-1": {
                "assignment_id": "security-1", "run_id": "security-1",
                "worker": "security", "revision_round": 0,
                "status": "completed", "findings": [], "error": "",
            }},
        )

    def _record_missed_issue(self, service, task_id, finding=None):
        service.record_feedback(
            task_id, "missed_issue", finding if finding is not None else self._expected(),
            "confirmed missed issue", "default",
        )
        return service.store.list_task_failure_cases(
            task_id, "default"
        )[0]["payload"]["attribution"]

    def test_end_to_end_review(self):
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        service = enable_agentic_service(ReviewService(self.settings))
        result = service.create_review("org/repo", diff, 1)
        task = service.store.get(result["task_id"])
        service.queue.close()
        self.assertEqual("SUCCESS", result["state"])
        self.assertEqual("SEC-EVAL", result["report"]["findings"][0]["rule_id"])
        self.assertNotIn("candidate_id", result["report"]["findings"][0])
        self.assertNotIn("candidate_id", task["report"]["findings"][0])
        self.assertNotIn("candidate_id", json.dumps(result["report"]))
        self.assertNotIn("candidate_id", json.dumps(task["report"]))
        self.assertNotIn("candidate_trace", json.dumps(result["report"]))
        self.assertNotIn("candidate_trace", json.dumps(task["report"]))
        self.assertNotIn("worker_execution_snapshots", json.dumps(result["report"]))
        self.assertNotIn("worker_execution_snapshots", json.dumps(task["report"]))
        self.assertNotIn("execution_profile", json.dumps(result["report"]))
        self.assertNotIn("execution_profile", json.dumps(task))
        self.assertNotIn("execution_profile", to_markdown(result["report"]))
        self.assertEqual("agentic", result["report"]["run_mode"]["effective"])
        self.assertEqual(
            ["lead", "security", "correctness-reliability", "critic"],
            result["report"]["collaboration"]["roles"],
        )
        self.assertEqual(5, result["report"]["execution"]["llm_calls"])
        self.assertEqual("normal", result["report"]["collaboration"]["risk_level"])
        role_calls = {}
        for call in result["report"]["execution"]["model_call_log"]:
            role_calls[call["role"]] = role_calls.get(call["role"], 0) + 1
        self.assertEqual({
            "lead": 2, "security": 1,
            "correctness-reliability": 1, "critic": 1,
        }, role_calls)
        self.assertEqual(0, result["report"]["collaboration"]["revision_rounds"])
        self.assertGreater(result["report"]["execution"]["tool_calls"], 0)
        self.assertEqual([], task["collaboration"])

    def test_rejects_large_diff(self):
        service = enable_agentic_service(ReviewService(self.settings))
        with self.assertRaises(ValueError):
            service.create_review("org/repo", "x" * 10001)

    def test_completed_review_feedback_is_persisted_and_listed_per_task(self):
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        service = enable_agentic_service(ReviewService(self.settings))
        result = service.create_review("org/repo", diff, 1)
        task_id = result["task_id"]

        feedback = service.record_feedback(
            task_id, "false_positive", result["report"]["findings"][0], "不是实际风险",
        )

        self.assertEqual({"recorded": True, "category": "false_positive"}, feedback)
        cases = service.store.list_task_failure_cases(task_id, "default")
        self.assertEqual(1, len(cases))
        self.assertEqual("false_positive", cases[0]["category"])
        self.assertEqual("SEC-EVAL", cases[0]["payload"]["finding"]["rule_id"])
        service.queue.close()

    def test_missed_issue_attribution_reports_discovery_only_with_complete_snapshots(self):
        service, task_id = self._completed_task()
        other = self._finding("other", "SEC-PATH-TRAVERSAL", "CWE-22")
        self._save_lead_session(service, task_id, {
            "candidates": {"other": {"origin": {"producer": "scanner"}}},
            "merge_lineage": [],
        }, [other])

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("INSUFFICIENT_EVIDENCE", attribution["status"])
        self.assertEqual("DISCOVERY", attribution["first_divergence"])
        self.assertEqual("INSUFFICIENT_EVIDENCE", attribution["root_cause"])
        self.assertEqual(
            "NO_SUPPORTED_EVOLUTION", attribution["evolution_surface"],
        )

    def test_attribution_recovers_logical_issue_contributor_snapshots(self):
        service, task_id = self._completed_task()
        winner = self._finding("winner", "SEC-PATH-TRAVERSAL", "CWE-22")
        loser = self._finding("loser", "SEC-PATH-TRAVERSAL", "CWE-22")
        trace = {
            "candidates": {
                "winner": {"origin": {"producer": "scanner"}},
                "loser": {"origin": {"producer": "scanner"}},
            },
            "merge_lineage": [{
                "stage": "scanner_merge", "loser_candidate_id": "loser",
                "winner_candidate_id": "winner", "reason": "lower_confidence",
            }],
        }
        self._save_lead_session(service, task_id, trace, [winner])
        checkpoint = service.store.load_checkpoints(task_id)["agentic-lead-session"]
        checkpoint["state"]["session"]["logical_issues"] = [{
            "schema_version": 1,
            "logical_issue_id": "private-logical-issue",
            "representative_candidate_id": "winner",
            "representative_index": 0,
            "contributors": [
                {"candidate_id": "winner", "finding": winner},
                {"candidate_id": "loser", "finding": loser},
            ],
            "merged_evidence_refs": [],
        }]
        service.store.save_checkpoint(
            task_id, "agentic-lead-session", checkpoint["state"], "completed", 1,
        )

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("DISCOVERY", attribution["first_divergence"])
        self.assertEqual("INSUFFICIENT_EVIDENCE", attribution["root_cause"])

    def test_missed_issue_attribution_does_not_call_orphan_candidate_discovery(self):
        service, task_id = self._completed_task()
        other = self._finding("other", "SEC-PATH-TRAVERSAL", "CWE-22")
        self._save_lead_session(service, task_id, {
            "candidates": {
                "other": {"origin": {"producer": "scanner"}},
                "overwritten": {"origin": {
                    "producer": "worker", "worker": "security",
                    "assignment_id": "security-1", "revision_round": 0,
                }},
            },
            "merge_lineage": [],
        }, [other])

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("UNKNOWN", attribution["status"])
        self.assertEqual("UNKNOWN", attribution["first_divergence"])

    def test_missed_issue_attribution_reports_lead_final(self):
        service, task_id = self._completed_task()
        candidate = self._finding("lead-rejected")
        self._save_lead_session(service, task_id, {
            "candidates": {"lead-rejected": {
                "origin": {"producer": "scanner"},
                "lead_final": {"accepted": False},
            }},
            "merge_lineage": [],
        }, [candidate])

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("LEAD_FINAL", attribution["first_divergence"])

    def test_critic_reject_is_advisory_and_gate_is_first_divergence(self):
        service, task_id = self._completed_task()
        candidate = self._finding("gate-rejected")
        reason = "confidence gate: score below 0.95"
        self._save_lead_session(service, task_id, {
            "candidates": {"gate-rejected": {
                "origin": {"producer": "scanner"},
                "critic": {"accepted": False, "objections": ["unsupported"]},
                "lead_final": {"accepted": True},
                "gate": {"accepted": False, "reasons": [reason]},
            }},
            "merge_lineage": [],
        }, [candidate])

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("GATE", attribution["first_divergence"])
        self.assertEqual([reason], attribution["gate_reasons"])
        self.assertNotIn("CRITIC", json.dumps(attribution))

    def test_published_candidate_missed_issue_is_unknown(self):
        service, task_id = self._completed_task()
        candidate = self._finding("published")
        self._save_lead_session(service, task_id, {
            "candidates": {"published": {
                "origin": {"producer": "scanner"},
                "lead_final": {"accepted": True},
                "gate": {"accepted": True, "reasons": []},
            }},
            "merge_lineage": [],
        }, [candidate])

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("UNKNOWN", attribution["status"])
        self.assertEqual("UNKNOWN", attribution["first_divergence"])

    def test_assignment_skill_requires_exactly_one_associated_skill(self):
        for skills, assignment_skill in (
            (["security-review"], "security-review"),
            ([], None),
            (["security-review", "code-quality"], None),
        ):
            with self.subTest(skills=skills):
                service, task_id = self._completed_task()
                candidate = self._finding("worker-candidate")
                self._save_lead_session(service, task_id, {
                    "candidates": {"worker-candidate": {
                        "origin": {
                            "producer": "worker", "worker": "security",
                            "assignment_id": "security-1",
                        },
                        "lead_final": {"accepted": False},
                    }},
                    "merge_lineage": [],
                }, [candidate], [{
                    "assignment_id": "security-1", "worker": "security",
                    "skills": skills,
                }])

                attribution = self._record_missed_issue(service, task_id)

                self.assertEqual(
                    assignment_skill, attribution.get("assignment_skill"),
                )
                self.assertNotIn("target_surface", attribution)
                self.assertNotIn("target_id", attribution)
                self.assertNotIn("evolution_surface", attribution)
                self.assertNotIn("candidate_id", json.dumps(attribution))
                self.assertNotIn("candidate_trace", json.dumps(attribution))

    def test_discovery_root_cause_context_evidence_missing(self):
        service, task_id = self._completed_task()
        client = EvidenceAttributionClient()
        service.chat_client = client
        self._save_discovery_evidence(service, task_id, context="safe_call(data)")

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("SUPPORTED", attribution["status"])
        self.assertEqual("DISCOVERY", attribution["first_divergence"])
        self.assertEqual("CONTEXT_EVIDENCE_MISSING", attribution["root_cause"])
        self.assertEqual("NO_SUPPORTED_EVOLUTION", attribution["evolution_surface"])
        self.assertNotIn("evolution_target", attribution)
        self.assertEqual(1, len(client.calls))

    def test_discovery_root_cause_skill_guidance_gap_routes_only_historical_skill(self):
        service, task_id = self._completed_task()
        client = EvidenceAttributionClient()
        service.chat_client = client
        self._save_discovery_evidence(
            service, task_id, context="eval(data)", guidance="Generic review guidance",
        )

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("SKILL_GUIDANCE_GAP", attribution["root_cause"])
        self.assertEqual("security-review", attribution["assignment_skill"])
        self.assertEqual("SKILL", attribution["evolution_surface"])
        self.assertEqual("security-review", attribution["evolution_target"])
        persisted = service.store.list_task_failure_cases(task_id, "default")[0][
            "payload"
        ]["attribution"]
        self.assertNotIn("system_prompt", json.dumps(persisted))
        self.assertNotIn("managed_user_context", json.dumps(persisted))
        self.assertNotIn("worker_execution_snapshots", json.dumps(persisted))

    def test_discovery_root_cause_model_reasoning_failure_does_not_route(self):
        service, task_id = self._completed_task()
        client = EvidenceAttributionClient()
        service.chat_client = client
        self._save_discovery_evidence(service, task_id)

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("MODEL_REASONING_FAILURE", attribution["root_cause"])
        self.assertEqual("security-review", attribution["assignment_skill"])
        self.assertEqual("NO_SUPPORTED_EVOLUTION", attribution["evolution_surface"])
        self.assertNotIn("evolution_target", attribution)

    def test_discovery_attribution_materializes_only_relevant_run_artifacts(self):
        service, task_id = self._completed_task()
        client = EvidenceAttributionClient()
        service.chat_client = client
        self._save_discovery_evidence(service, task_id)
        checkpoint = service.store.load_checkpoints(task_id)["agentic-lead-session"]
        session = checkpoint["state"]["session"]
        artifacts = {}
        capture_tool_evidence_artifact(
            task_id, artifacts,
            {"role": "security", "run_id": "security-1", "step": 1,
             "tool": "read_file"},
            {"evidence_id": "relevant-evidence", "tool": "read_file",
             "output": "historical relevant repository fact"},
        )
        capture_tool_evidence_artifact(
            task_id, artifacts,
            {"role": "correctness-reliability", "run_id": "other-run",
             "step": 1, "tool": "read_file"},
            {"evidence_id": "unrelated-evidence", "tool": "read_file",
             "output": "must not enter attribution"},
        )
        session["evidence_artifacts"] = artifacts
        service.store.save_checkpoint(
            task_id, "agentic-lead-session", checkpoint["state"], "completed", 1,
        )

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("SUPPORTED", attribution["status"])
        supplied = json.loads(client.calls[0][2])
        tool_evidence = supplied["worker_runs"][0]["tool_evidence"]
        self.assertEqual(["relevant-evidence"], [
            item["evidence_id"] for item in tool_evidence
        ])
        rendered = json.dumps(supplied)
        self.assertNotIn("unrelated-evidence", rendered)
        self.assertNotIn("artifact_id", rendered)
        self.assertTrue(all(
            "content_sha256" not in item for item in tool_evidence
        ))
        persisted = service.store.list_task_failure_cases(task_id, "default")[0][
            "payload"
        ]
        self.assertNotIn("historical relevant repository fact", json.dumps(persisted))

    def test_corrupt_relevant_artifact_degrades_attribution_to_insufficient(self):
        service, task_id = self._completed_task()
        client = EvidenceAttributionClient()
        service.chat_client = client
        self._save_discovery_evidence(service, task_id)
        checkpoint = service.store.load_checkpoints(task_id)["agentic-lead-session"]
        session = checkpoint["state"]["session"]
        artifacts = {}
        reference = capture_tool_evidence_artifact(
            task_id, artifacts,
            {"role": "security", "run_id": "security-1", "step": 1,
             "tool": "read_file"},
            {"evidence_id": "corrupt", "tool": "read_file", "output": "fact"},
        )
        artifacts[reference["artifact_id"]]["content_sha256"] = "0" * 64
        session["evidence_artifacts"] = artifacts
        service.store.save_checkpoint(
            task_id, "agentic-lead-session", checkpoint["state"], "completed", 1,
        )

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("INSUFFICIENT_EVIDENCE", attribution["root_cause"])
        self.assertEqual([], client.calls)

    def test_skill_gap_without_unique_historical_skill_does_not_route(self):
        service, task_id = self._completed_task()
        client = EvidenceAttributionClient()
        service.chat_client = client
        self._save_discovery_evidence(
            service, task_id, context="eval(data)",
            guidance="Generic review guidance", skills=(),
        )

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("SKILL_GUIDANCE_GAP", attribution["root_cause"])
        self.assertNotIn("assignment_skill", attribution)
        self.assertEqual("NO_SUPPORTED_EVOLUTION", attribution["evolution_surface"])
        self.assertNotIn("evolution_target", attribution)

    def test_discovery_root_cause_ambiguous_assignment_is_insufficient_without_model(self):
        service, task_id = self._completed_task()
        client = EvidenceAttributionClient()
        service.chat_client = client
        self._save_discovery_evidence(service, task_id)
        checkpoint = service.store.load_checkpoints(task_id)["agentic-lead-session"]
        session = checkpoint["state"]["session"]
        session["delegations"].append({
            "assignment_id": "reliability-1", "worker": "correctness-reliability",
            "objective": "Also review a.py", "files": ["a.py"], "skills": [],
        })
        service.store.save_checkpoint(
            task_id, "agentic-lead-session", checkpoint["state"], "completed", 1,
        )

        attribution = self._record_missed_issue(service, task_id)

        self.assertEqual("INSUFFICIENT_EVIDENCE", attribution["root_cause"])
        self.assertEqual("NO_SUPPORTED_EVOLUTION", attribution["evolution_surface"])
        self.assertEqual([], client.calls)

    def test_lead_and_gate_divergence_do_not_invoke_root_cause_model(self):
        for divergence in ("LEAD_FINAL", "GATE"):
            with self.subTest(divergence=divergence):
                service, task_id = self._completed_task()
                client = EvidenceAttributionClient()
                service.chat_client = client
                candidate = self._finding("candidate")
                entry = {
                    "origin": {"producer": "scanner"},
                    "lead_final": {"accepted": divergence != "LEAD_FINAL"},
                }
                if divergence == "GATE":
                    entry["gate"] = {"accepted": False, "reasons": ["gate reason"]}
                self._save_lead_session(service, task_id, {
                    "candidates": {"candidate": entry}, "merge_lineage": [],
                }, [candidate])

                attribution = self._record_missed_issue(service, task_id)

                self.assertEqual(divergence, attribution["first_divergence"])
                self.assertEqual([], client.calls)

    def test_invalid_missed_issue_expected_finding_is_unknown(self):
        invalid_values = (
            {"line": 1, "cwe": "CWE-95"},
            {"path": "a.py", "cwe": "CWE-95"},
            {"path": "a.py", "line": 1},
            {"path": "a.py", "start_line": 2, "end_line": 1, "rule_id": "SEC-EVAL"},
        )
        for invalid in invalid_values:
            with self.subTest(finding=invalid):
                service, task_id = self._completed_task()

                attribution = self._record_missed_issue(
                    service, task_id, invalid,
                )

                self.assertEqual("UNKNOWN", attribution["status"])
                self.assertEqual("UNKNOWN", attribution["first_divergence"])


if __name__ == "__main__":
    unittest.main()
