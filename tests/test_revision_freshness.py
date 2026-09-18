import os
import tempfile
import unittest

from agentic_fake import enable_agentic_service
from evoagent.config import Settings
from evoagent.github import GitHubClient
from evoagent.service import GITHUB_REVIEW_STATUS_MARKER, ReviewService


DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
BASE = "1" * 40
HEAD_A = "a" * 40
HEAD_B = "b" * 40


class RecordingQueue:
    backend = "recording"

    def __init__(self):
        self.submissions = []

    def submit(self, payload, message_id=""):
        self.submissions.append((dict(payload), message_id))
        return message_id


class RecordingGitHub:
    def __init__(self):
        self.current_head = HEAD_A
        self.compare_calls = []
        self.moving_diff_calls = []
        self.comment_calls = []
        self.comments = {}

    def ensure_repository_access(self, repository):
        if repository != "org/repo":
            raise PermissionError(repository)

    def fetch_compare_diff(self, repository, base_sha, head_sha):
        self.compare_calls.append((repository, base_sha, head_sha))
        return DIFF

    def fetch_diff(self, url):
        self.moving_diff_calls.append(url)
        raise AssertionError("moving pull-request diff must not be used")

    def get_pull_request(self, repository, number):
        self.ensure_repository_access(repository)
        if number != 7:
            raise AssertionError(number)
        return {"head": {"sha": self.current_head}}

    def upsert_comment(self, api_url, markdown, marker):
        self.comment_calls.append((api_url, markdown, marker))
        self.comments[(api_url, marker)] = markdown


class RevisionFreshnessTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        settings = Settings(
            host="127.0.0.1", port=8080, db_path=self.path,
            max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
            llm_base_url="", llm_api_key="", llm_model="",
            github_webhook_secret="", github_token="",
            auto_post_review=True,
        )
        self.service = enable_agentic_service(ReviewService(settings))
        self.service.queue.close()
        self.queue = RecordingQueue()
        self.github = RecordingGitHub()
        self.service.queue = self.queue
        self.service.github = self.github

    def tearDown(self):
        os.unlink(self.path)

    @staticmethod
    def payload(head=HEAD_A, action="synchronize"):
        return {
            "action": action,
            "number": 7,
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "base": {"sha": BASE},
                "head": {"sha": head},
                "diff_url": "https://github.example/org/repo/pull/7.diff",
                "issue_url": "https://api.github.example/repos/org/repo/issues/7",
            },
        }

    def enqueue(self, head=HEAD_A, delivery="delivery-1"):
        result = self.service.handle_github_pull_request(
            self.payload(head), delivery, "payload-" + delivery,
        )
        queued, message_id = self.queue.submissions[-1]
        self.assertEqual(result["task_id"], message_id)
        return result["task_id"], queued

    def process(self, queued):
        self.service._process_queued(queued)

    def test_compare_client_builds_revision_pinned_url(self):
        client = GitHubClient("")
        urls = []
        client.fetch_diff = lambda url: urls.append(url) or DIFF

        result = client.fetch_compare_diff("org/repo", BASE, HEAD_A)

        self.assertEqual(DIFF, result)
        self.assertEqual([
            "https://api.github.com/repos/org/repo/compare/%s...%s"
            % (BASE, HEAD_A),
        ], urls)

    def test_webhook_persists_revisions_and_fetches_pinned_diff(self):
        task_id, queued = self.enqueue()
        self.github.current_head = HEAD_B

        self.process(queued)

        task = self.service.store.get(task_id)
        self.assertEqual(BASE, task["input"]["review_base_revision"])
        self.assertEqual(HEAD_A, task["input"]["review_head_revision"])
        self.assertEqual(DIFF, self.service.store.get_task_payload(task_id))
        self.assertEqual(
            [("org/repo", BASE, HEAD_A)], self.github.compare_calls,
        )
        self.assertEqual([], self.github.moving_diff_calls)

    def test_current_revision_publishes_report_and_records_outcome(self):
        task_id, queued = self.enqueue()
        self.github.current_head = HEAD_A

        self.process(queued)

        task = self.service.store.get(task_id)
        self.assertEqual({
            "status": "CURRENT", "published": True,
            "reviewed_revision": HEAD_A, "current_revision": HEAD_A,
        }, task["input"]["publication_outcome"])
        markers = [item[2] for item in self.github.comment_calls]
        self.assertIn("<!-- evoagent-review:%s -->" % task_id, markers)
        self.assertIn(GITHUB_REVIEW_STATUS_MARKER, markers)

    def test_stale_revision_skips_report_and_upserts_visible_status(self):
        task_id, queued = self.enqueue()
        self.github.current_head = HEAD_B

        self.process(queued)

        task = self.service.store.get(task_id)
        self.assertEqual("SUCCESS", task["state"])
        self.assertIsNotNone(task["report"])
        self.assertEqual({
            "status": "STALE", "published": False,
            "reviewed_revision": HEAD_A, "current_revision": HEAD_B,
            "reason": "pull_request_updated_during_review",
        }, task["input"]["publication_outcome"])
        markers = [item[2] for item in self.github.comment_calls]
        self.assertNotIn("<!-- evoagent-review:%s -->" % task_id, markers)
        self.assertEqual([GITHUB_REVIEW_STATUS_MARKER], markers)
        status = self.github.comments[(
            "https://api.github.example/repos/org/repo/issues/7",
            GITHUB_REVIEW_STATUS_MARKER,
        )]
        self.assertIn(HEAD_A[:12], status)
        self.assertIn(HEAD_B[:12], status)

    def test_stale_status_is_pr_scoped_and_newer_success_replaces_it(self):
        first_task, first_queued = self.enqueue(HEAD_A, "delivery-a")
        self.github.current_head = HEAD_B
        self.process(first_queued)
        stale_status = self.github.comments[(
            "https://api.github.example/repos/org/repo/issues/7",
            GITHUB_REVIEW_STATUS_MARKER,
        )]
        self.assertIn("did not publish", stale_status)

        second_stale_task, second_stale_queued = self.enqueue(
            HEAD_A, "delivery-a-again",
        )
        self.process(second_stale_queued)
        status_comments = [
            key for key in self.github.comments
            if key[1] == GITHUB_REVIEW_STATUS_MARKER
        ]
        self.assertEqual(1, len(status_comments))
        self.assertFalse(
            self.service.store.get(second_stale_task)["input"]
            ["publication_outcome"]["published"]
        )

        second_task, second_queued = self.enqueue(HEAD_B, "delivery-b")
        self.process(second_queued)

        status_comments = [
            key for key in self.github.comments
            if key[1] == GITHUB_REVIEW_STATUS_MARKER
        ]
        self.assertEqual(1, len(status_comments))
        self.assertIn(
            "reviewed the current pull request revision",
            self.github.comments[status_comments[0]],
        )
        self.assertTrue(
            self.service.store.get(second_task)["input"]
            ["publication_outcome"]["published"]
        )
        self.assertFalse(
            self.service.store.get(first_task)["input"]
            ["publication_outcome"]["published"]
        )

    def test_freshness_guard_does_not_schedule_and_synchronize_still_does(self):
        _task_id, queued = self.enqueue()
        self.assertEqual(1, len(self.queue.submissions))

        self.github.current_head = HEAD_B
        self.process(queued)

        self.assertEqual(1, len(self.queue.submissions))


if __name__ == "__main__":
    unittest.main()
