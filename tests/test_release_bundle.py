import copy
import json
import os
import tempfile
import unittest

from agentic_fake import enable_agentic_service
from evoagent.agentic_core import ExecutionConfigurationError
from evoagent.config import Settings
from evoagent.diff_parser import parse_unified_diff
from evoagent.gates import FindingGate
from evoagent.releases import (
    RELEASE_SPEC_SCHEMA_VERSION,
    ReleaseIntegrityError,
    ReleaseNotFound,
)
from evoagent.service import ReviewService
from evoagent.skill_evolution import validate_artifact
from evoagent.store import TaskStore


REVIEW_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
LOW_RISK_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+print(value)\n"


def contract_spec(marker="r1"):
    return {
        "schema_version": RELEASE_SPEC_SCHEMA_VERSION,
        "marker": marker,
    }


class ReleaseStoreContract:
    store = None

    def test_identical_spec_is_deduplicated(self):
        first = self.store.put_release("tenant-a", contract_spec())
        second = self.store.put_release("tenant-a", copy.deepcopy(contract_spec()))
        self.assertEqual(first["release_id"], second["release_id"])
        self.assertEqual(1, len(self.store.list_releases("tenant-a")))

    def test_changed_spec_creates_new_release_without_mutating_original(self):
        first = self.store.put_release("tenant-a", contract_spec("r1"))
        second = self.store.put_release(
            "tenant-a", contract_spec("r2"), first["release_id"],
        )
        self.assertNotEqual(first["release_id"], second["release_id"])
        self.assertEqual(
            "r1", self.store.get_release(
                first["release_id"], "tenant-a",
            )["spec"]["marker"],
        )

    def test_active_pointer_moves_without_mutating_releases_or_tasks(self):
        r1 = self.store.put_release("tenant-a", contract_spec("r1"))
        r2 = self.store.put_release(
            "tenant-a", contract_spec("r2"), r1["release_id"],
        )
        self.store.activate_release("tenant-a", r1["release_id"])
        self.store.create(
            "release-task-a", "org/repo", 1, {}, "tenant-a", r1["release_id"],
        )
        self.store.activate_release("tenant-a", r2["release_id"])
        self.assertEqual(r2["release_id"], self.store.get_active_release(
            "tenant-a"
        )["release_id"])
        self.assertEqual(r1["release_id"], self.store.get(
            "release-task-a", "tenant-a"
        )["release_id"])
        self.assertEqual("r1", self.store.get_release(
            r1["release_id"], "tenant-a"
        )["spec"]["marker"])

    def test_release_scope_is_enforced_for_task_creation(self):
        release = self.store.put_release("tenant-a", contract_spec())
        with self.assertRaises(ReleaseNotFound):
            self.store.create(
                "wrong-tenant-task", "org/repo", 1, {},
                "tenant-b", release["release_id"],
            )


class SQLiteReleaseStoreTests(ReleaseStoreContract, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = TaskStore(self.temp.name)

    def tearDown(self):
        self.temp.close()

    def test_corrupt_immutable_release_fails_integrity_validation(self):
        release = self.store.put_release("tenant-a", contract_spec())
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE releases SET spec_json=? WHERE release_id=?",
                (json.dumps(contract_spec("tampered")), release["release_id"]),
            )
        with self.assertRaises(ReleaseIntegrityError):
            self.store.get_release(release["release_id"], "tenant-a")


@unittest.skipUnless(
    os.environ.get("EVOAGENT_TEST_POSTGRES_URL"),
    "set EVOAGENT_TEST_POSTGRES_URL to run the PostgreSQL release contract",
)
class PostgresReleaseStoreTests(ReleaseStoreContract, unittest.TestCase):
    def setUp(self):
        from evoagent.postgres_store import PostgresTaskStore
        self.store = PostgresTaskStore(os.environ["EVOAGENT_TEST_POSTGRES_URL"])
        with self.store._connect() as conn:
            conn.execute(
                "DELETE FROM release_activations WHERE tenant_id IN ('tenant-a','tenant-b')"
            )
            conn.execute(
                "DELETE FROM active_releases WHERE tenant_id IN ('tenant-a','tenant-b')"
            )
            conn.execute(
                "DELETE FROM tasks WHERE id IN ('release-task-a','wrong-tenant-task')"
            )
            conn.execute(
                "DELETE FROM releases WHERE tenant_id IN ('tenant-a','tenant-b')"
            )


class ReleaseRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        settings = Settings(
            host="127.0.0.1", port=8080, db_path=self.path,
            max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
            llm_base_url="", llm_api_key="", llm_model="",
            github_webhook_secret="", github_token="", auto_post_review=False,
            skills_dir=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills")),
        )
        self.service = enable_agentic_service(ReviewService(settings))
        self.service.queue.close()

    def tearDown(self):
        os.unlink(self.path)

    def create_pending(self, diff=REVIEW_DIFF, tenant="tenant-a"):
        task_id = self.service._create_task(
            "org/repo", diff, 1, "test", tenant,
        )
        return task_id, self.service.store.get(task_id, tenant)

    @staticmethod
    def skill_entry(release, name="security-review"):
        return next(
            item for item in release["spec"]["skills"] if item["name"] == name
        )

    def promote_prompt_and_skill(self, tenant="tenant-a"):
        bundled = {
            item.name: item for item in self.service._active_agent_skills(tenant)
        }["security-review"]
        artifact = bundled.to_artifact()
        artifact["files"]["SKILL.md"] += "\n\nRelease R2 marker.\n"
        self.service.store.save_skill_artifact(
            "security-review", validate_artifact(artifact, "security-review"),
            1.0, True, tenant,
        )
        self.service.store.save_skill_version(
            "llm-review", "PROMPT-R2", 1.0, activate=True,
        )
        self.service.reload_skills(tenant)

    def make_worker_phase_incomplete(self, task_id):
        state = self.service.store.load_checkpoints(task_id)[
            "agentic-lead-session"
        ]["state"]
        pending = json.loads(json.dumps(state["session"]))
        pending.update({
            "phase": "delegated", "worker_results": {},
            "worker_execution_snapshots": {}, "lead_assessments": [],
            "revision_results": {}, "critic_pass1_complete": False,
            "critic_pass1_decisions": [], "critic_challenge": None,
            "critic_decisions": [], "critic_candidates": [],
            "critic_complete": False, "lead_final": {},
            "accepted_findings": [], "revision_rounds": 0,
        })
        self.service.store.save_checkpoint(task_id, "agentic-lead-session", {
            "protocol": "lead-workers-v4", "session": pending,
            "execution": state["execution"],
        }, "in_progress", 1)

    def test_queue_time_pin_prompt_skill_promotion_and_release_rollback(self):
        task_a, stored_a = self.create_pending()
        r1 = self.service.store.get_release(stored_a["release_id"], "tenant-a")

        self.promote_prompt_and_skill()
        r2 = self.service.releases.active("tenant-a")
        self.assertNotEqual(r1["release_id"], r2["release_id"])
        task_b, stored_b = self.create_pending()
        self.assertEqual(r2["release_id"], stored_b["release_id"])
        self.assertEqual("PROMPT-R2", r2["spec"]["prompt_policy"]["overlay"])
        self.assertNotEqual(
            self.skill_entry(r1)["content_sha256"],
            self.skill_entry(r2)["content_sha256"],
        )

        # Neither Task has entered agent execution before the promotion.
        self.assertNotIn("agentic-lead-session", self.service.store.load_checkpoints(task_a))
        self.service._run_review(task_a, "org/repo", 1, REVIEW_DIFF, "tenant-a")
        session_a = self.service.store.load_checkpoints(task_a)[
            "agentic-lead-session"
        ]["state"]["session"]
        self.assertEqual(r1["release_id"], session_a["execution_profile"]["release_id"])
        self.assertNotIn("PROMPT-R2", json.dumps(session_a["worker_execution_snapshots"]))
        self.assertTrue(all(
            item["content_sha256"] == self.skill_entry(r1)["content_sha256"]
            for snapshot in session_a["worker_execution_snapshots"].values()
            for item in snapshot["selected_skills"]
            if item["name"] == "security-review"
        ))

        self.service.releases.rollback("tenant-a", r1["release_id"])
        task_c, stored_c = self.create_pending()
        self.assertEqual(r1["release_id"], stored_c["release_id"])
        self.assertEqual(r1["release_id"], self.service.store.get(task_a)["release_id"])
        self.assertEqual(r2["release_id"], self.service.store.get(task_b)["release_id"])

        # B was queued under R2 and still executes R2 after the active pointer rolls back.
        self.service._run_review(task_b, "org/repo", 1, REVIEW_DIFF, "tenant-a")
        session_b = self.service.store.load_checkpoints(task_b)[
            "agentic-lead-session"
        ]["state"]["session"]
        self.assertEqual(r2["release_id"], session_b["execution_profile"]["release_id"])

        # A completed/persisted session still resolves the original Release after rollback.
        self.service.reviewer.review_with_context(
            task_a, REVIEW_DIFF, self.service.harness._deserialize_parsed(
                self.service.store.load_checkpoints(task_a)["planning"]["state"]["parsed"]
            ), "org/repo", "tenant-a",
        )
        resumed = self.service.store.load_checkpoints(task_a)[
            "agentic-lead-session"
        ]["state"]["session"]
        self.assertEqual(r1["release_id"], resumed["execution_profile"]["release_id"])

    def test_in_progress_task_resumes_original_release_after_promotion(self):
        task_a, stored_a = self.create_pending()
        r1 = self.service.store.get_release(stored_a["release_id"], "tenant-a")
        self.service._run_review(task_a, "org/repo", 1, REVIEW_DIFF, "tenant-a")
        self.make_worker_phase_incomplete(task_a)

        self.promote_prompt_and_skill()
        r2 = self.service.releases.active("tenant-a")
        self.assertNotEqual(r1["release_id"], r2["release_id"])

        self.service.reviewer.review_with_context(
            task_a, REVIEW_DIFF, parse_unified_diff(REVIEW_DIFF),
            "org/repo", "tenant-a",
        )
        resumed = self.service.store.load_checkpoints(task_a)[
            "agentic-lead-session"
        ]["state"]["session"]
        self.assertEqual(r1["release_id"], resumed["execution_profile"]["release_id"])
        selected = [
            item for snapshot in resumed["worker_execution_snapshots"].values()
            for item in snapshot["selected_skills"]
            if item["name"] == "security-review"
        ]
        self.assertTrue(selected)
        self.assertTrue(all(
            item["content_sha256"] == self.skill_entry(r1)["content_sha256"]
            for item in selected
        ))

    def test_deferred_task_is_pinned_before_diff_fetch(self):
        task_id = self.service._create_deferred_task(
            "org/repo", 7, "github", "tenant-a", {
                "enabled_agents": ["lead", "security", "critic"],
                "enabled_skills": [], "review_revision": "abc123",
            },
        )
        task = self.service.store.get(task_id, "tenant-a")
        self.assertTrue(task["release_id"])
        release = self.service.store.get_release(task["release_id"], "tenant-a")
        self.assertEqual(
            ["critic", "lead", "security"],
            release["spec"]["runtime_identity"]["effective_enabled_roles"],
        )
        self.assertNotIn("agentic-lead-session", self.service.store.load_checkpoints(task_id))

    def test_finding_gate_policy_is_materialized_from_pinned_release(self):
        task_g1, stored_g1 = self.create_pending(LOW_RISK_DIFF)
        g1 = self.service.store.get_release(stored_g1["release_id"], "tenant-a")
        self.assertEqual(0.55, g1["spec"]["gate_policy"]["minimum_confidence"])

        self.service.reviewer.gate = FindingGate(minimum_confidence=0.95)
        g2 = self.service.releases.publish(
            "tenant-a", self.service._current_release_spec("tenant-a"), g1["release_id"],
        )
        task_g2, _stored_g2 = self.create_pending(LOW_RISK_DIFF)

        report_g1 = self.service._run_review(
            task_g1, "org/repo", 1, LOW_RISK_DIFF, "tenant-a",
        )
        report_g2 = self.service._run_review(
            task_g2, "org/repo", 1, LOW_RISK_DIFF, "tenant-a",
        )
        self.assertEqual(g1["release_id"], self.service.store.get(task_g1)["release_id"])
        self.assertEqual(g2["release_id"], self.service.store.get(task_g2)["release_id"])
        self.assertEqual(1, len(report_g1.findings))
        self.assertEqual(0, len(report_g2.findings))

    def test_missing_and_corrupt_task_release_fail_closed(self):
        self.service.store.create(
            "legacy-task", "org/repo", 1,
            {"mode": "agentic", "enabled_agents": sorted(self.service.reviewer.enabled_roles)},
            "tenant-a",
        )
        with self.assertRaisesRegex(ExecutionConfigurationError, "no pinned Release"):
            self.service.reviewer.review_with_context(
                "legacy-task", REVIEW_DIFF,
                parse_unified_diff(REVIEW_DIFF),
                "org/repo", "tenant-a",
            )

        task_id, task = self.create_pending()
        with self.service.store._connect() as conn:
            conn.execute(
                "UPDATE releases SET spec_json=? WHERE release_id=?",
                (json.dumps({"schema_version": 1, "tampered": True}), task["release_id"]),
            )
        with self.assertRaisesRegex(ExecutionConfigurationError, "pinned Release cannot be loaded"):
            self.service._run_review(task_id, "org/repo", 1, REVIEW_DIFF, "tenant-a")


if __name__ == "__main__":
    unittest.main()
