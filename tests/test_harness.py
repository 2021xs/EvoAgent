import json
import os
import tempfile
import unittest

from evoagent.harness import ReviewHarness
from evoagent.models import Finding, Severity
from evoagent.report import to_markdown
from evoagent.reviewer import LocalRuleReviewer
from evoagent.store import TaskStore


class HarnessTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_successful_state_flow_is_persisted(self):
        task_id = "test-task"
        self.store.create(task_id, "demo/repo", 7, {"source": "test"})
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+eval(value)\n"
        report = ReviewHarness(self.store, LocalRuleReviewer()).run(task_id, "demo/repo", 7, diff)
        task = self.store.get(task_id)
        self.assertEqual("SUCCESS", task["state"])
        self.assertEqual(["PLANNING", "EXECUTING", "REVIEWING", "SUCCESS"], [x["state"] for x in task["trace"]])
        self.assertEqual("high", report.risk)

    def test_candidate_identity_survives_internal_checkpoints_only(self):
        class IdentifiedReviewer(LocalRuleReviewer):
            def review(self, diff, parsed):
                findings = super().review(diff, parsed)
                findings[0].candidate_id = "candidate-harness"
                return findings

        task_id = "identity-task"
        self.store.create(task_id, "demo/repo", 7, {"source": "test"})
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+eval(value)\n"
        harness = ReviewHarness(self.store, IdentifiedReviewer())

        report = harness.run(task_id, "demo/repo", 7, diff)
        checkpoints = self.store.load_checkpoints(task_id)

        self.assertEqual(
            "candidate-harness",
            checkpoints["executing"]["state"]["findings"][0]["candidate_id"],
        )
        reviewing_report = checkpoints["reviewing"]["state"]["report"]
        self.assertEqual(
            "candidate-harness", reviewing_report["findings"][0]["candidate_id"]
        )
        restored = harness._report_from_dict(reviewing_report)
        self.assertEqual("candidate-harness", restored.findings[0].candidate_id)

        self.assertNotIn("candidate_id", report.to_dict()["findings"][0])
        self.assertNotIn("candidate_id", self.store.get(task_id)["report"]["findings"][0])
        self.assertNotIn("candidate_id", json.dumps(report.to_dict()))
        self.assertNotIn("candidate_id", json.dumps(self.store.get(task_id)["report"]))
        self.assertNotIn("candidate-harness", to_markdown(report.to_dict()))

    def test_legacy_finding_without_candidate_identity_still_loads(self):
        legacy = Finding(
            rule_id="SEC-EVAL", severity=Severity.CRITICAL,
            title="Dynamic execution", explanation="Input is executed as code.",
            path="x.py", line=1, evidence="eval(value)", fix="Remove eval.",
            test="Reject executable input.",
        ).to_dict()

        restored = ReviewHarness._finding_from_dict(legacy)

        self.assertIsNone(restored.candidate_id)

    def test_invalid_diff_is_recorded_as_failure(self):
        task_id = "bad-task"
        self.store.create(task_id, "demo/repo", None, {"source": "test"})
        with self.assertRaises(ValueError):
            ReviewHarness(self.store, LocalRuleReviewer()).run(task_id, "demo/repo", None, "not a diff")
        self.assertEqual("FAILED", self.store.get(task_id)["state"])


if __name__ == "__main__":
    unittest.main()
