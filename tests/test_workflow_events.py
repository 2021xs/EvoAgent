import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
import tempfile
import unittest

from evoagent.api import ApiHandler
from evoagent.models import Finding, ReviewReport, Severity, TaskState, TraceEvent
from evoagent.patching import VerifiedPatchFixer
from evoagent.service import ReviewService
from evoagent.store import TaskStore, utc_now
from evoagent.workflow_events import WorkflowEvent
from test_autofix_resume import FakeGitHub, FakeModel, FakeVerifier, SOURCE


TASK_ID = "31111111-2222-3333-4444-555555555555"
CI_AUTHORITY = {"github_app_id": "42", "github_app_slug": "ci"}


def check_suite_payload(
    commit_sha, conclusion="success", status="completed", suite_id=9001,
    app_id=42, app_slug="ci",
):
    return {
        "action": "completed" if status == "completed" else "requested",
        "check_suite": {
            "id": suite_id, "head_sha": commit_sha, "status": status,
            "conclusion": conclusion if status == "completed" else None,
            "updated_at": "2026-09-18T12:00:00Z",
            "app": {"id": app_id, "slug": app_slug},
            "pull_requests": [{"number": 101}],
        },
        "repository": {"full_name": "org/repo"},
    }


def normalized(payload, tenant="tenant-a"):
    return WorkflowEvent.from_github_check_suite(
        payload, tenant, "2026-09-18T12:00:01+00:00",
    )


class WorkflowEventStoreContract:
    store = None

    def prepare_waiting(self, task_id=TASK_ID, commit_sha="c" * 40):
        self.store.create(task_id, "org/repo", 7, {}, "tenant-a")
        state = {
            "phase": "PR_CREATED", "workflow_revision": 6,
            "task_id": task_id, "tenant": "tenant-a", "repository": "org/repo",
            "commit_sha": commit_sha, "repair_branch": "evoagent/fix-contract",
            "pr_number": 101, "result": {"status": "verified-draft"},
        }
        self.store.save_checkpoint(
            task_id, "autofix-execution", state, "in_progress", 1,
        )
        return self.store.suspend_autofix_for_ci(
            task_id, "tenant-a", "org/repo", commit_sha,
            "evoagent/fix-contract", 101, CI_AUTHORITY,
            {"status": "waiting-for-ci", "commit_sha": commit_sha}, 2,
        )

    def test_success_transition_and_logical_dedup(self):
        commit_sha = "c" * 40
        waiting = self.prepare_waiting(commit_sha=commit_sha)
        self.assertEqual("WAITING_FOR_CI", waiting["phase"])
        event = normalized(check_suite_payload(commit_sha)).to_dict()

        first = self.store.record_and_apply_workflow_event(event)
        second = self.store.record_and_apply_workflow_event(dict(event))

        self.assertEqual("APPLIED", first["processing_status"])
        self.assertEqual(first["event_id"], second["event_id"])
        checkpoint = self.store.load_checkpoints(TASK_ID)["autofix-execution"]
        self.assertEqual("CI_PASSED", checkpoint["state"]["phase"])
        self.assertEqual(8, checkpoint["state"]["workflow_revision"])

    def test_nonterminal_and_unknown_conclusion_do_not_wake(self):
        commit_sha = "d" * 40
        self.prepare_waiting(commit_sha=commit_sha)
        nonterminal = normalized(
            check_suite_payload(commit_sha, status="in_progress", suite_id=1)
        )
        unknown = normalized(
            check_suite_payload(commit_sha, conclusion="mystery", suite_id=2)
        )

        first = self.store.record_and_apply_workflow_event(nonterminal.to_dict())
        second = self.store.record_and_apply_workflow_event(unknown.to_dict())

        self.assertEqual("IGNORED", first["processing_status"])
        self.assertEqual("WORKFLOW_EVENT_NON_TERMINAL", first["processing_result"])
        self.assertEqual("IGNORED", second["processing_status"])
        self.assertEqual("CI_CONCLUSION_UNSUPPORTED", second["processing_result"])
        state = self.store.load_checkpoints(TASK_ID)["autofix-execution"]["state"]
        self.assertEqual("WAITING_FOR_CI", state["phase"])

    def test_non_authoritative_suite_is_audited_without_transition(self):
        commit_sha = "e" * 40
        self.prepare_waiting(commit_sha=commit_sha)
        event = normalized(check_suite_payload(
            commit_sha, suite_id=88, app_id=99, app_slug="other-ci",
        )).to_dict()

        result = self.store.record_and_apply_workflow_event(event)

        self.assertEqual("IGNORED", result["processing_status"])
        self.assertEqual(
            "WORKFLOW_EVENT_NON_AUTHORITATIVE", result["processing_result"],
        )
        state = self.store.load_checkpoints(TASK_ID)["autofix-execution"]["state"]
        self.assertEqual("WAITING_FOR_CI", state["phase"])


class SQLiteWorkflowEventStoreTests(WorkflowEventStoreContract, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = TaskStore(self.temp.name)

    def tearDown(self):
        self.temp.close()


@unittest.skipUnless(
    os.environ.get("EVOAGENT_TEST_POSTGRES_URL"),
    "set EVOAGENT_TEST_POSTGRES_URL to run the PostgreSQL workflow-event contract",
)
class PostgresWorkflowEventStoreTests(WorkflowEventStoreContract, unittest.TestCase):
    def setUp(self):
        from evoagent.postgres_store import PostgresTaskStore
        self.store = PostgresTaskStore(os.environ["EVOAGENT_TEST_POSTGRES_URL"])
        with self.store._connect() as conn:
            conn.execute("DELETE FROM workflow_events WHERE tenant_id='tenant-a'")
            conn.execute("DELETE FROM autofix_correlations WHERE tenant_id='tenant-a'")
            conn.execute("DELETE FROM checkpoints WHERE task_id=%s", (TASK_ID,))
            conn.execute("DELETE FROM tasks WHERE id=%s", (TASK_ID,))


class AutoFixWorkflowEventTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)
        self.github = FakeGitHub()
        self.service = self.make_service(self.store)
        self.create_task(TASK_ID)

    def tearDown(self):
        os.unlink(self.path)

    def make_service(self, store):
        service = ReviewService.__new__(ReviewService)
        service.store = store
        service.github = self.github
        service.fixer = VerifiedPatchFixer(FakeModel(), FakeVerifier())
        service.settings = SimpleNamespace(
            default_tenant_id="tenant-a",
            github_ci_app_id="42", github_ci_app_slug="ci",
        )
        return service

    def create_task(self, task_id):
        self.store.create(
            task_id, "org/repo", 7,
            {"source": "test", "review_head_revision": SOURCE}, "tenant-a",
        )
        report = ReviewReport(
            repository="org/repo", pull_request=7, summary="review", risk="high",
            findings=[Finding(
                rule_id="SEC-EVAL", severity=Severity.HIGH, title="Unsafe eval",
                explanation="Input is executed.", path="app.py", line=1,
                evidence="eval(raw)", fix="Use parse.", test="Reject code.",
                gate={"passed": True},
            )],
        )
        self.store.succeed(
            task_id, report,
            TraceEvent(1, TaskState.SUCCESS, "Review completed", utc_now()),
        )
        self.store.grant_repository("tenant-a", "org/repo", True)

    def checkpoint(self, task_id=TASK_ID):
        return self.store.load_checkpoints(task_id)["autofix-execution"]

    def suspend(self):
        result = self.service.create_fix(TASK_ID, tenant_id="tenant-a")
        self.assertEqual("waiting-for-ci", result["status"])
        self.assertEqual("WAITING_FOR_CI", self.checkpoint()["state"]["phase"])
        return result

    def deliver(
        self, conclusion="success", status="completed", suite_id=9001,
        delivery="delivery-ci-1", commit_sha=None, service=None,
    ):
        commit_sha = commit_sha or self.checkpoint()["state"]["commit_sha"]
        payload = check_suite_payload(commit_sha, conclusion, status, suite_id)
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return (service or self.service).handle_github_check_suite(
            payload, delivery, digest, "tenant-a",
        )

    def fail_checkpoint_once(self, phase, after_write=True):
        original = self.store.save_checkpoint
        fired = {"value": False}

        def failing(task_id, node, state, status="completed", attempt=1, error=""):
            if state.get("phase") == phase and not fired["value"]:
                fired["value"] = True
                if after_write:
                    original(task_id, node, state, status, attempt, error)
                raise RuntimeError("injected checkpoint failure at " + phase)
            return original(task_id, node, state, status, attempt, error)

        self.store.save_checkpoint = failing
        return original

    def test_normal_suspend_returns_without_polling(self):
        result = self.suspend()
        state = self.checkpoint()["state"]
        self.assertTrue(result["published"])
        self.assertEqual(state["commit_sha"], result["commit_sha"])
        correlations = self.store.list_autofix_correlations(
            "tenant-a", "org/repo", state["commit_sha"],
        )
        self.assertEqual([TASK_ID], [item["task_id"] for item in correlations])
        self.assertEqual(CI_AUTHORITY, state["ci_authority"])
        self.assertEqual("42", correlations[0]["ci_authority_app_id"])
        self.assertEqual("ci", correlations[0]["ci_authority_app_slug"])

    def test_success_failure_and_process_restart(self):
        self.suspend()
        restarted = self.make_service(TaskStore(self.path))

        applied = self.deliver(service=restarted)

        self.assertEqual("APPLIED", applied["processing_status"])
        self.assertEqual("CI_PASSED", self.checkpoint()["state"]["phase"])

        other = "32222222-2222-3333-4444-555555555555"
        self.create_task(other)
        # Use the same fixture service with a second task to prove failure mapping.
        failure_result = self.service.create_fix(other, tenant_id="tenant-a")
        failure_state = self.checkpoint(other)["state"]
        payload = check_suite_payload(
            failure_state["commit_sha"], "timed_out", "completed", 9002,
        )
        event = normalized(payload).to_dict()
        applied_failure = self.store.record_and_apply_workflow_event(event)
        self.assertEqual("APPLIED", applied_failure["processing_status"])
        self.assertEqual("CI_FAILED", self.checkpoint(other)["state"]["phase"])
        self.assertEqual("waiting-for-ci", failure_result["status"])

    def test_transport_and_logical_duplicates_transition_once(self):
        self.suspend()
        first = self.deliver(delivery="delivery-a")
        revision = self.checkpoint()["state"]["workflow_revision"]
        transport_duplicate = self.deliver(delivery="delivery-a")
        logical_duplicate = self.deliver(delivery="delivery-b")

        self.assertFalse(first["duplicate_delivery"])
        self.assertTrue(transport_duplicate["duplicate_delivery"])
        self.assertFalse(logical_duplicate["duplicate_delivery"])
        self.assertEqual(revision, self.checkpoint()["state"]["workflow_revision"])
        self.assertEqual(first["event_id"], logical_duplicate["event_id"])

    def test_event_before_wait_is_reconciled(self):
        original = self.fail_checkpoint_once("PR_CREATED", after_write=True)
        with self.assertRaisesRegex(RuntimeError, "PR_CREATED"):
            self.service.create_fix(TASK_ID, tenant_id="tenant-a")
        self.store.save_checkpoint = original
        state = self.checkpoint()["state"]
        self.assertEqual("PR_CREATED", state["phase"])

        early = self.deliver(commit_sha=state["commit_sha"])
        self.assertEqual("PENDING", early["processing_status"])
        self.assertEqual("WORKFLOW_EVENT_UNMATCHED", early["processing_result"])

        resumed = self.service.create_fix(TASK_ID, tenant_id="tenant-a")
        self.assertEqual("ci-passed", resumed["status"])
        self.assertEqual("CI_PASSED", self.checkpoint()["state"]["phase"])

    def test_crash_during_pending_event_application_rolls_back_and_reconciles(self):
        original = self.fail_checkpoint_once("PR_CREATED", after_write=True)
        with self.assertRaises(RuntimeError):
            self.service.create_fix(TASK_ID, tenant_id="tenant-a")
        self.store.save_checkpoint = original
        state = self.checkpoint()["state"]
        event_result = self.deliver(commit_sha=state["commit_sha"])
        self.assertEqual("PENDING", event_result["processing_status"])
        waiting_result = dict(state["result"])
        waiting_result.update({
            "status": "waiting-for-ci", "published": True,
            "commit_sha": state["commit_sha"],
        })

        def crash(stage):
            if stage == "after_workflow_transition":
                raise RuntimeError("injected event-application crash")

        with self.assertRaisesRegex(RuntimeError, "event-application"):
            self.store.suspend_autofix_for_ci(
                TASK_ID, "tenant-a", "org/repo", state["commit_sha"],
                state["repair_branch"], state["pr_number"], CI_AUTHORITY,
                waiting_result, 2,
                fault_injector=crash,
            )
        self.assertEqual("PR_CREATED", self.checkpoint()["state"]["phase"])
        self.assertEqual([], self.store.list_autofix_correlations(
            "tenant-a", "org/repo", state["commit_sha"],
        ))

        result = self.service.create_fix(TASK_ID, tenant_id="tenant-a")
        self.assertEqual("ci-passed", result["status"])
        self.assertEqual("CI_PASSED", self.checkpoint()["state"]["phase"])

    def test_wrong_sha_ambiguous_and_nonterminal_do_not_wake(self):
        self.suspend()
        wrong = self.deliver(commit_sha="f" * 40, suite_id=2, delivery="wrong")
        self.assertEqual("PENDING", wrong["processing_status"])
        nonterminal = self.deliver(
            status="in_progress", suite_id=3, delivery="nonterminal",
        )
        self.assertEqual("IGNORED", nonterminal["processing_status"])
        self.assertEqual("WAITING_FOR_CI", self.checkpoint()["state"]["phase"])

        other = "33333333-2222-3333-4444-555555555555"
        self.create_task(other)
        state = self.checkpoint()["state"]
        with self.store._connect() as conn:
            conn.execute(
                "INSERT INTO autofix_correlations(task_id,tenant_id,repository,commit_sha,"
                "repair_branch,pr_number,ci_authority_app_id,ci_authority_app_slug,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (other, "tenant-a", "org/repo", state["commit_sha"],
                 "pathological", 999, "42", "ci", utc_now()),
            )
        ambiguous = self.deliver(suite_id=4, delivery="ambiguous")
        self.assertEqual("AMBIGUOUS", ambiguous["processing_status"])
        self.assertEqual("WAITING_FOR_CI", self.checkpoint()["state"]["phase"])

    def test_contradictory_terminal_result_is_recorded_without_rewrite(self):
        self.suspend()
        self.deliver(conclusion="success", delivery="success")
        passed = self.checkpoint()["state"]
        conflict = self.deliver(conclusion="failure", delivery="failure")

        self.assertEqual("CONFLICT", conflict["processing_status"])
        self.assertEqual("WORKFLOW_EVENT_CONFLICT", conflict["processing_result"])
        current = self.checkpoint()["state"]
        self.assertEqual("CI_PASSED", current["phase"])
        self.assertEqual(passed["workflow_revision"], current["workflow_revision"])

    def test_non_authoritative_suites_never_wake_canonical_waiter(self):
        self.suspend()
        state = self.checkpoint()["state"]
        commit_sha = state["commit_sha"]

        for suite_id, conclusion in ((7001, "success"), (7002, "failure")):
            payload = check_suite_payload(
                commit_sha, conclusion=conclusion, suite_id=suite_id,
                app_id=99, app_slug="other-ci",
            )
            event = normalized(payload).to_dict()
            ignored = self.store.record_and_apply_workflow_event(event)
            self.assertEqual("IGNORED", ignored["processing_status"])
            self.assertEqual(
                "WORKFLOW_EVENT_NON_AUTHORITATIVE", ignored["processing_result"],
            )
            self.assertEqual("WAITING_FOR_CI", self.checkpoint()["state"]["phase"])

        restarted = self.make_service(TaskStore(self.path))
        restarted.settings.github_ci_app_id = "777"
        restarted.settings.github_ci_app_slug = "changed-after-suspend"
        applied = self.deliver(
            conclusion="success", suite_id=7003, delivery="canonical-after-restart",
            service=restarted,
        )
        self.assertEqual("APPLIED", applied["processing_status"])
        self.assertEqual("CI_PASSED", self.checkpoint()["state"]["phase"])

    def test_verified_autofix_requires_explicit_canonical_ci_authority(self):
        self.service.settings.github_ci_app_id = ""
        self.service.settings.github_ci_app_slug = ""

        with self.assertRaisesRegex(ValueError, "canonical CI authority"):
            self.service.create_fix(TASK_ID, tenant_id="tenant-a")

        self.assertEqual(0, self.github.commit_creates)
        self.assertEqual(0, self.github.branch_creates)
        self.assertEqual(0, self.github.pr_creates)

    def test_verified_check_suite_webhook_dispatches_through_api_boundary(self):
        self.suspend()
        commit_sha = self.checkpoint()["state"]["commit_sha"]
        payload = check_suite_payload(commit_sha)
        payload["check_suite"]["updated_at"] = datetime.now(timezone.utc).isoformat()
        body = json.dumps(payload).encode("utf-8")
        secret = "webhook-test-secret"
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256,
        ).hexdigest()

        class Handler(ApiHandler):
            def _read_body(self):
                return body

            def _send_json(self, status, value):
                self.response = (status, value)

        handler = object.__new__(Handler)
        handler.path = "/webhooks/github"
        handler.service = self.service
        handler.settings = SimpleNamespace(
            max_diff_bytes=1024,
            github_webhook_secret=secret,
            webhook_max_age_seconds=600,
        )
        handler.headers = {
            "X-GitHub-Event": "check_suite",
            "X-GitHub-Delivery": "api-delivery",
            "X-Hub-Signature-256": signature,
        }
        handler.response = None

        handler.do_POST()

        self.assertEqual(202, handler.response[0])
        self.assertEqual("APPLIED", handler.response[1]["processing_status"])
        self.assertEqual("CI_PASSED", self.checkpoint()["state"]["phase"])


if __name__ == "__main__":
    unittest.main()
