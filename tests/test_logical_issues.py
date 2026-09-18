import copy
import json
import os
import tempfile
import unittest

from evoagent.agentic_core import (
    AgenticReviewer,
    LogicalIssueError,
    MAX_LOGICAL_ISSUE_MODEL_EVIDENCE_REFS,
    capture_tool_evidence_artifact,
)
from evoagent.diff_parser import parse_unified_diff
from evoagent.gates import FindingGate
from evoagent.models import Finding, Severity

from tests.test_lead_worker_collaboration import (
    DIFF,
    DuplicateScanner,
    HierarchicalClient,
    RecordingTaskStore,
)


def finding(
    candidate_id, confidence, *, rule_id="SEC-EVAL", cwe="CWE-95",
    title="Dynamic execution", severity=Severity.CRITICAL, refs=None,
):
    return Finding(
        rule_id=rule_id, cwe=cwe, severity=severity, title=title,
        explanation="Input is executed as code.", path="app.py", line=1,
        evidence="eval(user_input)", fix="Use a constrained parser.",
        test="Reject executable input.", confidence=confidence,
        evidence_refs=list(refs or []), candidate_id=candidate_id,
        source="security",
    )


class DuplicateWorkerClient(HierarchicalClient):
    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        managed = json.loads(user)
        task = json.loads(managed["task"])
        if role == "lead" and task.get("phase") == "delegate":
            self.calls.append((role, "delegate"))
            self.payloads.append((role, task))
            return {
                "action": "final", "risk_level": "normal",
                "delegations": [{
                    "assignment_id": "security-1", "worker": "security",
                    "objective": "Review dynamic execution.",
                }],
            }
        if role == "security":
            self.calls.append((role, "worker"))
            self.payloads.append((role, task))
            return {"action": "final", "findings": [{
                "rule_id": "SEC-EVAL", "cwe": "CWE-95",
                "severity": "critical", "title": "Worker dynamic execution",
                "explanation": "Worker independently found dynamic execution.",
                "path": "app.py", "line": 1, "evidence": "eval(user_input)",
                "fix": "Use a parser.", "test": "Reject code.",
                "confidence": 0.8,
            }]}
        return super().complete_json(role, system, user, ledger, max_tokens)


class RevisionDuplicateClient(HierarchicalClient):
    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        managed = json.loads(user)
        task = json.loads(managed["task"])
        if role == "security":
            self.security_calls += 1
            confidence = 0.8 if self.security_calls == 1 else 0.95
            return {"action": "final", "findings": [{
                "rule_id": "SEC-EVAL", "cwe": "CWE-95",
                "severity": "critical",
                "title": "Initial" if self.security_calls == 1 else "Revision",
                "explanation": "Input is executed as code.",
                "path": "app.py", "line": 1, "evidence": "eval(user_input)",
                "fix": "Use a parser.", "test": "Reject code.",
                "confidence": confidence,
            }]}
        return super().complete_json(role, system, user, ledger, max_tokens)


class LogicalIssueTests(unittest.TestCase):
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
        self.reviewer = AgenticReviewer(self.store, HierarchicalClient())

    def tearDown(self):
        os.unlink(self.path)

    def test_exact_grouping_preserves_winner_tie_and_contributors(self):
        trace = {"candidates": {}, "merge_lineage": []}
        refs = [
            {"artifact_id": "A1", "evidence_id": "e1", "tool": "read_file"},
            {"artifact_id": "A2", "evidence_id": "e2", "tool": "symbol"},
        ]
        first = finding("C17", 0.7, title="first", refs=refs)
        winner = finding("C31", 0.9, title="winner", refs=[
            {"artifact_id": "A3", "evidence_id": "e3", "tool": "search_diff"},
            refs[0],
        ])
        tie = finding("C42", 0.9, title="tie", refs=[{
            "evidence_id": "scanner:e4", "tool": "declarative-scanner",
        }])
        unrelated = finding(
            "C99", 1.0, rule_id="SEC-SQL-CONCAT", cwe="CWE-89",
            title="SQL", severity=Severity.HIGH,
        )

        issues = self.reviewer._aggregate_logical_issues(
            "task", [first, winner, tie, unrelated], trace,
        )

        self.assertEqual(2, len(issues))
        issue = issues[0]
        self.assertEqual("C31", issue.representative_candidate_id)
        self.assertEqual(["C17", "C31", "C42"], [
            item["candidate_id"] for item in issue.contributors
        ])
        self.assertEqual(["A3", "A1", "A2", "scanner:e4"], [
            item.get("artifact_id") or item["evidence_id"]
            for item in issue.merged_evidence_refs
        ])
        projection = self.reviewer._logical_issue_projections([issue])[0]
        self.assertEqual("winner", projection.title)
        self.assertEqual("C31", projection.candidate_id)
        self.assertEqual(4, len(projection.evidence_refs))
        self.assertEqual(
            issue.logical_issue_id,
            trace["candidates"]["C17"]["logical_issue_id"],
        )
        self.assertEqual("tie_kept_existing", trace["merge_lineage"][-1]["reason"])

    def test_duplicate_semantics_remain_exact(self):
        values = [
            finding("base", 0.8),
            finding("nearby", 0.9),
            finding("other-rule", 0.9, rule_id="CUSTOM", cwe=None),
        ]
        values[1].line = 2
        issues = self.reviewer._aggregate_logical_issues(
            "task", values, {"candidates": {}, "merge_lineage": []},
        )
        self.assertEqual(3, len(issues))

    def test_projection_is_bounded_while_private_union_is_complete(self):
        refs = [{
            "evidence_id": "e-%d" % index, "tool": "scanner",
        } for index in range(MAX_LOGICAL_ISSUE_MODEL_EVIDENCE_REFS + 5)]
        issue = self.reviewer._aggregate_logical_issues(
            "task", [finding("C1", 0.9, refs=refs)],
            {"candidates": {}, "merge_lineage": []},
        )[0]
        self.assertEqual(len(refs), len(issue.merged_evidence_refs))
        self.assertEqual(
            MAX_LOGICAL_ISSUE_MODEL_EVIDENCE_REFS,
            len(self.reviewer._logical_issue_projections([issue])[0].evidence_refs),
        )

    def test_loser_strong_evidence_can_improve_existing_gate_outcome(self):
        representative = finding(
            "representative", 0.9, severity=Severity.HIGH, refs=[],
        )
        representative.evidence = "not present on the changed line"
        contributor = finding(
            "contributor", 0.8, severity=Severity.HIGH, refs=[{
                "evidence_id": "ast:e1", "tool": "ast_analyze",
                "output_preview": "validated call structure",
            }],
        )
        contributor.evidence = "not present on the changed line"
        parsed = parse_unified_diff(DIFF)

        self.assertEqual([], FindingGate().apply([representative], parsed).accepted)
        issue = self.reviewer._aggregate_logical_issues(
            "task", [representative, contributor],
            {"candidates": {}, "merge_lineage": []},
        )[0]
        projection = self.reviewer._logical_issue_projections([issue])[0]
        self.assertEqual([projection], FindingGate().apply([projection], parsed).accepted)

    def test_restore_integrity_and_resume_are_stable(self):
        artifacts = {}
        reference = capture_tool_evidence_artifact(
            "task", artifacts,
            {"role": "security", "run_id": "security-1", "step": 1,
             "tool": "read_file"},
            {"evidence_id": "read:e1", "tool": "read_file", "output": "fact"},
        )
        issue = self.reviewer._aggregate_logical_issues(
            "task", [finding("C1", 0.9, refs=[reference])],
            {"candidates": {}, "merge_lineage": []},
        )[0]
        persisted = [issue.to_dict()]
        restored = self.reviewer._restore_logical_issues(
            "task", copy.deepcopy(persisted), artifacts,
        )
        self.assertEqual(persisted, [item.to_dict() for item in restored])

        corrupt = copy.deepcopy(persisted)
        corrupt[0]["contributors"][0]["finding"]["line"] = 2
        with self.assertRaisesRegex(LogicalIssueError, "identity mismatch"):
            self.reviewer._restore_logical_issues("task", corrupt, artifacts)

        corrupt = copy.deepcopy(persisted)
        corrupt[0]["merged_evidence_refs"][0]["content_sha256"] = "0" * 64
        with self.assertRaises(LogicalIssueError):
            self.reviewer._restore_logical_issues("task", corrupt, artifacts)

    def test_scanner_and_worker_duplicates_are_one_private_issue_one_public_finding(self):
        client = DuplicateWorkerClient()
        reviewer = AgenticReviewer(
            self.store, client, scanners=[DuplicateScanner()],
        )
        findings = reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo",
        )
        session = self.store.load_checkpoints("task")[
            "agentic-lead-session"
        ]["state"]["session"]

        self.assertEqual(2, len(session["scanner_candidates"]))
        self.assertEqual(1, len(session["scanner_findings"]))
        self.assertEqual(1, len(session["logical_issues"]))
        issue = session["logical_issues"][0]
        self.assertEqual(3, len(issue["contributors"]))
        self.assertIn("Duplicate dynamic execution", {
            item["finding"]["title"] for item in issue["contributors"]
        })
        self.assertEqual(1, len(findings))
        critic_payload = next(
            payload for role, payload in client.payloads if role == "critic"
        )
        self.assertEqual(1, len(critic_payload["candidates"]))
        rendered = json.dumps(reviewer.collaboration_summary("task"))
        self.assertNotIn("logical_issue", rendered)
        self.assertNotIn("contributors", rendered)

    def test_revision_replaces_initial_active_contributor(self):
        client = RevisionDuplicateClient()
        reviewer = AgenticReviewer(self.store, client)
        reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo",
        )
        session = self.store.load_checkpoints("task")[
            "agentic-lead-session"
        ]["state"]["session"]
        initial_session = next(
            value for value in self.store.lead_sessions
            if value.get("worker_results", {}).get("security-1", {}).get("findings")
            and "1:security-1" not in value.get("worker_results", {})
        )
        initial_id = initial_session["worker_results"]["security-1"][
            "findings"
        ][0]["candidate_id"]
        revision_id = session["worker_results"]["security-1"]["findings"][0][
            "candidate_id"
        ]
        contributor_ids = {
            item["candidate_id"]
            for issue in session["logical_issues"]
            for item in issue["contributors"]
        }
        self.assertNotIn(initial_id, contributor_ids)
        self.assertIn(revision_id, contributor_ids)
        self.assertIn(initial_id, session["candidate_trace"]["candidates"])

    def test_challenge_guard_uses_representative_issue_identity(self):
        worker = finding("worker", 0.95)
        scanner = finding("scanner", 0.9)
        trace = {
            "candidates": {
                "worker": {"origin": {
                    "producer": "worker", "worker": "security",
                    "assignment_id": "security-1", "run_id": "security-1",
                    "revision_round": 0,
                }},
                "scanner": {"origin": {"producer": "scanner"}},
            },
            "merge_lineage": [],
        }
        issues = self.reviewer._aggregate_logical_issues(
            "task", [scanner, worker], trace,
        )
        candidates = self.reviewer._logical_issue_projections(issues)
        session = {
            "logical_issues": [item.to_dict() for item in issues],
            "candidate_trace": trace,
            "delegations": [{
                "assignment_id": "security-1", "worker": "security",
            }],
            "critic_pass1_decisions": [{
                "finding_index": 0, "accepted": False,
                "objections": ["need fact"], "evidence_request": "show fact",
            }],
        }
        challenge = self.reviewer._create_critic_challenge(session, candidates)
        self.assertEqual("security", challenge["request"]["recipient"])
        self.assertEqual(issues[0].logical_issue_id, challenge["logical_issue_id"])

        session["logical_issues"][0]["representative_candidate_id"] = "scanner"
        self.assertIsNone(self.reviewer._create_critic_challenge(session, candidates))

    def test_challenge_evidence_extends_union_without_deleting_contributors(self):
        first = finding("C1", 0.9, refs=[{
            "artifact_id": "A1", "evidence_id": "e1", "tool": "read_file",
        }])
        second = finding("C2", 0.8, refs=[{
            "artifact_id": "A2", "evidence_id": "e2", "tool": "symbol",
        }])
        issue = self.reviewer._aggregate_logical_issues(
            "task", [first, second], {"candidates": {}, "merge_lineage": []},
        )[0]
        projection = self.reviewer._logical_issue_projections([issue])[0]
        projection.evidence_refs.append({
            "artifact_id": "A3", "evidence_id": "e3", "tool": "search_diff",
        })
        self.reviewer._update_logical_issues([issue], [projection])
        self.assertEqual(["A1", "A3", "A2"], [
            item["artifact_id"] for item in issue.merged_evidence_refs
        ])
        self.assertEqual(2, len(issue.contributors))


if __name__ == "__main__":
    unittest.main()
