import hashlib
import json
import os
import tempfile
import unittest

from evoagent.models import Finding, ReviewReport, Severity, TaskState, TraceEvent
from evoagent.patching import VerifiedPatchFixer
from evoagent.service import ReviewService
from evoagent.store import TaskStore, utc_now


SOURCE = "a" * 40
NEW_SOURCE = "b" * 40
PATCH = (
    "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
    "-value = eval(raw)\n+value = parse(raw)\n"
)


class FakeModel:
    def __init__(self, patch=PATCH):
        self.patch = patch
        self.calls = 0

    def complete_json(self, *_args, **_kwargs):
        self.calls += 1
        if not self.patch:
            return {"patch": "", "reason": "insufficient evidence"}
        return {
            "patch": self.patch,
            "behavioral_claims": ["Avoid executable parsing."],
            "related_tests": ["test_rejects_code"],
        }


class FakeVerifier:
    test_command = "pytest"

    def __init__(self, structural=True, comparison=True):
        self.structural = structural
        self.comparison = comparison
        self.calls = 0

    def verify_contents(self, _files):
        self.calls += 1
        return {
            "passed": self.structural,
            "checks": [{"name": "compile:app.py", "passed": self.structural}],
        }

    def verify_archive(self, _archive, _files):
        self.calls += 1
        return {
            "passed": True,
            "checks": [{"name": "repository-tests", "passed": True}],
        }

    def compare(self, _before, _after):
        self.calls += 1
        return {
            "passed": self.comparison,
            "baseline_passed": True,
            "patched_passed": self.comparison,
            "test_evidence_present": True,
            "behavioral_regression_detected": not self.comparison,
        }


class FakeGitHub:
    def __init__(self):
        self.current_head = SOURCE
        self.file_refs = []
        self.commit_creates = 0
        self.branch_creates = 0
        self.pr_creates = 0
        self.commits = {}
        self.branches = {}
        self.pull_requests = {}
        self.branch_ambiguous_once = False
        self.pr_ambiguous_once = False

    def get_pull_request(self, _repository, number):
        if number == 7:
            return {
                "number": 7,
                "head": {
                    "sha": self.current_head, "ref": "feature",
                    "repo": {"full_name": "org/repo"},
                },
                "base": {"ref": "main"},
            }
        return dict(self.pull_requests[number])

    def get_file(self, _repository, path, ref):
        self.file_refs.append((path, ref))
        if ref != SOURCE:
            raise AssertionError("source reads must use the bound SHA")
        return {"decoded_content": "value = eval(raw)\n"}

    def download_archive(self, _repository, ref):
        if ref != SOURCE:
            raise AssertionError("archive must use the bound SHA")
        return b"archive"

    def create_commit_object(self, _repository, parent, files, message, identity):
        self.commit_creates += 1
        encoded = json.dumps(
            [parent, sorted(files.items()), message, identity], sort_keys=True,
        ).encode("utf-8")
        sha = hashlib.sha256(encoded).hexdigest()
        tree_sha = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        commit = {
            "sha": sha, "tree": {"sha": tree_sha},
            "parents": [{"sha": parent}],
        }
        self.commits[sha] = commit
        return dict(commit)

    def get_git_commit(self, _repository, sha):
        return dict(self.commits[sha])

    def get_branch(self, _repository, branch):
        sha = self.branches.get(branch)
        return {"ref": "refs/heads/" + branch, "object": {"sha": sha}} if sha else None

    def create_branch_once(self, _repository, branch, sha):
        self.branch_creates += 1
        self.branches[branch] = sha
        if self.branch_ambiguous_once:
            self.branch_ambiguous_once = False
            raise RuntimeError("ambiguous branch response")

    def find_pull_request(self, _repository, head, base):
        for value in self.pull_requests.values():
            if value["head"]["ref"] == head and value["base"]["ref"] == base:
                return dict(value)
        return None

    def create_draft_pull_request_once(self, _repository, _title, head, base, _body):
        self.pr_creates += 1
        number = 100 + self.pr_creates
        value = {
            "number": number, "html_url": "https://github.example/pr/%d" % number,
            "head": {"ref": head}, "base": {"ref": base},
        }
        self.pull_requests[number] = value
        if self.pr_ambiguous_once:
            self.pr_ambiguous_once = False
            raise RuntimeError("ambiguous pull request response")
        return dict(value)


class AutoFixResumeTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)
        self.model = FakeModel()
        self.verifier = FakeVerifier()
        self.github = FakeGitHub()
        self.service = ReviewService.__new__(ReviewService)
        self.service.store = self.store
        self.service.github = self.github
        self.service.fixer = VerifiedPatchFixer(self.model, self.verifier)
        self.task_id = "11111111-2222-3333-4444-555555555555"
        self._create_task(reviewed_revision=SOURCE)

    def tearDown(self):
        os.unlink(self.path)

    def _create_task(self, reviewed_revision="", tenant="tenant-a", task_id=None, findings=True):
        task_id = task_id or self.task_id
        payload = {"source": "test"}
        if reviewed_revision:
            payload["review_head_revision"] = reviewed_revision
        self.store.create(task_id, "org/repo", 7, payload, tenant)
        finding_values = []
        if findings:
            finding_values.append(Finding(
                rule_id="SEC-EVAL", severity=Severity.HIGH, title="Unsafe eval",
                explanation="Input is executed.", path="app.py", line=1,
                evidence="eval(raw)", fix="Use parse.", test="Reject code.",
                gate={"passed": True},
            ))
        report = ReviewReport(
            repository="org/repo", pull_request=7, summary="review", risk="high",
            findings=finding_values,
        )
        self.store.succeed(
            task_id, report,
            TraceEvent(1, TaskState.SUCCESS, "Review completed", utc_now()),
        )
        self.store.grant_repository(tenant, "org/repo", True)

    def _fix(self, tenant="tenant-a"):
        return self.service.create_fix(self.task_id, tenant_id=tenant)

    def _fail_checkpoint_once(self, phase, after_write):
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

    def test_success_uses_exact_sha_and_completed_retries_do_no_work(self):
        before_report = self.store.get(self.task_id)["report"]
        first = self._fix()
        counters = (
            self.model.calls, self.verifier.calls, self.github.commit_creates,
            self.github.branch_creates, self.github.pr_creates,
        )

        second = self._fix()

        self.assertEqual(first, second)
        self.assertEqual("verified-draft", first["status"])
        self.assertEqual(SOURCE, first["source_sha"])
        self.assertTrue(self.github.file_refs)
        self.assertEqual({SOURCE}, {ref for _path, ref in self.github.file_refs})
        self.assertEqual(counters, (
            self.model.calls, self.verifier.calls, self.github.commit_creates,
            self.github.branch_creates, self.github.pr_creates,
        ))
        task = self.store.get(self.task_id)
        self.assertEqual("SUCCESS", task["state"])
        self.assertEqual(before_report, task["report"])
        checkpoint = self.store.load_checkpoints(self.task_id)["autofix-execution"]
        self.assertEqual("PR_CREATED", checkpoint["state"]["phase"])
        self.assertEqual("completed", checkpoint["status"])

    def test_review_revision_mismatch_persists_stale_without_remote_write(self):
        self.github.current_head = NEW_SOURCE

        first = self._fix()
        second = self._fix()

        self.assertEqual(first, second)
        self.assertEqual("stale-source", first["status"])
        self.assertEqual(SOURCE, first["reviewed_revision"])
        self.assertEqual(NEW_SOURCE, first["current_revision"])
        self.assertFalse(first["remote_write"])
        self.assertEqual(0, self.model.calls)
        self.assertEqual((0, 0, 0), (
            self.github.commit_creates, self.github.branch_creates, self.github.pr_creates,
        ))
        state = self.store.load_checkpoints(self.task_id)["autofix-execution"]["state"]
        self.assertEqual("STALE_SOURCE", state["phase"])

    def test_prewrite_freshness_guard_blocks_after_verified_resume(self):
        original = self._fail_checkpoint_once("VERIFIED", after_write=True)
        with self.assertRaisesRegex(RuntimeError, "VERIFIED"):
            self._fix()
        self.store.save_checkpoint = original
        self.github.current_head = NEW_SOURCE

        result = self._fix()

        self.assertEqual("stale-source", result["status"])
        self.assertEqual("source_pr_updated_before_fix_publication", result["reason"])
        self.assertEqual((0, 0, 0), (
            self.github.commit_creates, self.github.branch_creates, self.github.pr_creates,
        ))

    def test_resume_after_patch_ready_skips_model(self):
        original = self._fail_checkpoint_once("PATCH_READY", after_write=True)
        with self.assertRaisesRegex(RuntimeError, "PATCH_READY"):
            self._fix()
        self.store.save_checkpoint = original

        result = self._fix()

        self.assertEqual("verified-draft", result["status"])
        self.assertEqual(1, self.model.calls)

    def test_resume_after_verified_skips_verification(self):
        original = self._fail_checkpoint_once("VERIFIED", after_write=True)
        with self.assertRaisesRegex(RuntimeError, "VERIFIED"):
            self._fix()
        calls = self.verifier.calls
        self.store.save_checkpoint = original

        self._fix()

        self.assertEqual(calls, self.verifier.calls)

    def test_resume_after_commit_checkpoint_reuses_commit(self):
        original = self._fail_checkpoint_once("COMMIT_CREATED", after_write=True)
        with self.assertRaisesRegex(RuntimeError, "COMMIT_CREATED"):
            self._fix()
        self.store.save_checkpoint = original

        self._fix()

        self.assertEqual(1, self.github.commit_creates)

    def test_lost_commit_checkpoint_recreates_same_logical_commit(self):
        original = self._fail_checkpoint_once("COMMIT_CREATED", after_write=False)
        with self.assertRaisesRegex(RuntimeError, "COMMIT_CREATED"):
            self._fix()
        first_shas = set(self.github.commits)
        self.store.save_checkpoint = original

        result = self._fix()

        self.assertEqual(2, self.github.commit_creates)
        self.assertEqual(1, len(self.github.commits))
        self.assertEqual(first_shas, set(self.github.commits))
        self.assertEqual(next(iter(first_shas)), result["commits"][0]["sha"])

    def test_branch_checkpoint_loss_reconciles_existing_branch(self):
        original = self._fail_checkpoint_once("BRANCH_PUBLISHED", after_write=False)
        with self.assertRaisesRegex(RuntimeError, "BRANCH_PUBLISHED"):
            self._fix()
        self.assertEqual(1, self.github.branch_creates)
        self.store.save_checkpoint = original

        self._fix()

        self.assertEqual(1, self.github.branch_creates)

    def test_existing_branch_with_wrong_commit_fails_closed(self):
        original = self._fail_checkpoint_once("COMMIT_CREATED", after_write=True)
        with self.assertRaisesRegex(RuntimeError, "COMMIT_CREATED"):
            self._fix()
        self.store.save_checkpoint = original
        state = self.store.load_checkpoints(self.task_id)["autofix-execution"]["state"]
        self.github.branches[state["repair_branch"]] = "f" * 64

        with self.assertRaisesRegex(ValueError, "unexpected commit"):
            self._fix()

        self.assertEqual(0, self.github.branch_creates)

    def test_pr_checkpoint_loss_reconciles_existing_pr(self):
        original = self._fail_checkpoint_once("PR_CREATED", after_write=False)
        with self.assertRaisesRegex(RuntimeError, "PR_CREATED"):
            self._fix()
        self.assertEqual(1, self.github.pr_creates)
        self.store.save_checkpoint = original

        result = self._fix()

        self.assertEqual(1, self.github.pr_creates)
        self.assertEqual(101, result["draft_pull_request"]["number"])

    def test_ambiguous_branch_and_pr_writes_reconcile_before_retry(self):
        self.github.branch_ambiguous_once = True
        self.github.pr_ambiguous_once = True

        result = self._fix()

        self.assertEqual("verified-draft", result["status"])
        self.assertEqual(1, self.github.branch_creates)
        self.assertEqual(1, self.github.pr_creates)

    def test_terminal_blocked_and_suggestion_results_are_reused(self):
        self.verifier.structural = False
        blocked = self._fix()
        blocked_counts = (self.model.calls, self.verifier.calls)
        self.assertEqual(blocked, self._fix())
        self.assertEqual(blocked_counts, (self.model.calls, self.verifier.calls))

        other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._create_task(reviewed_revision=SOURCE, task_id=other)
        self.task_id = other
        self.model = FakeModel("")
        self.verifier = FakeVerifier()
        self.service.fixer = VerifiedPatchFixer(self.model, self.verifier)
        suggestion = self._fix()
        calls = self.model.calls
        self.assertEqual(suggestion, self._fix())
        self.assertEqual(calls, self.model.calls)
        self.assertEqual("suggestion-only", suggestion["status"])

    def test_tenant_isolation(self):
        with self.assertRaisesRegex(ValueError, "completed task not found"):
            self._fix("tenant-b")
        self.assertEqual({}, self.store.load_checkpoints(self.task_id))


if __name__ == "__main__":
    unittest.main()
