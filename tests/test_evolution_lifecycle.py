import os
import json
import tempfile
import unittest

from agentic_fake import enable_agentic_service
from evoagent.api import ApiHandler
from evoagent.config import Settings
from evoagent.evolution_lifecycle import (
    AttributionResult, CandidateLifecycle, EvolutionCandidate, EvolutionRouter,
)
from evoagent.service import ReviewService
from evoagent.store import TaskStore, utc_now


DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"


class EvolutionLifecycleStoreContract:
    store = None

    def seed_attribution(self, suffix="a", surface="SKILL", cause="SKILL_GUIDANCE_GAP"):
        task_id = "evolution-task-" + suffix
        self.store.create(task_id, "org/repo", 1, {}, "tenant-a")
        failure_id = self.store.record_failure_case(
            task_id, "missed_issue", {"finding": {"path": "a.py", "line": 1}},
        )
        raw = {
            "status": "SUPPORTED", "first_divergence": "DISCOVERY",
            "root_cause": cause, "evolution_surface": surface,
            "evolution_target": "security-review" if surface == "SKILL" else "llm-review",
            "method": "MODEL", "evidence_refs": [{"artifact_id": "artifact:bounded"}],
        }
        attribution = AttributionResult.create(failure_id, task_id, raw, utc_now())
        return failure_id, self.store.save_attribution_result(attribution.to_dict())

    def test_prompt_and_skill_share_candidate_contract_and_exact_dedup(self):
        lifecycle = CandidateLifecycle(self.store)
        for index, (surface, target, change) in enumerate((
            ("GLOBAL_PROMPT", "llm-review", {"prompt": "policy"}),
            ("SKILL", "security-review", {"artifact": {"name": "security-review"}}),
        )):
            failure_id, attribution = self.seed_attribution(
                str(index), surface,
                "SYSTEM_POLICY_GAP" if surface == "GLOBAL_PROMPT" else "SKILL_GUIDANCE_GAP",
            )
            candidate = EvolutionCandidate.create(
                "tenant-a", surface, target, 1, "release-r1",
                attribution["attribution_id"], [failure_id],
                attribution["evidence_refs"], change,
                {"method": "MODEL", "model": "fixture", "config": {}}, utc_now(),
            )
            first = lifecycle.create(candidate)
            duplicate = lifecycle.create(candidate)
            self.assertEqual(first["candidate_id"], duplicate["candidate_id"])
            self.assertEqual("CREATED", first["status"])
            self.assertEqual(attribution["attribution_id"], first["attribution_id"])
            self.assertEqual([failure_id], first["source_failure_ids"])

            calls = {"count": 0}
            def evaluate(_value):
                calls["count"] += 1
                return {"eligible": True, "validation": {"surface": surface}}

            ready = lifecycle.evaluate(first["candidate_id"], evaluate)
            again = lifecycle.evaluate(first["candidate_id"], evaluate)
            self.assertEqual("READY_FOR_PROMOTION", ready["status"])
            self.assertEqual("READY_FOR_PROMOTION", again["status"])
            self.assertEqual(1, calls["count"])
            self.assertTrue(ready["validation_result"]["eligible"])
            self.assertEqual({}, ready["final_evaluation_result"])

    def test_rejected_candidate_cannot_promote(self):
        failure_id, attribution = self.seed_attribution("reject")
        lifecycle = CandidateLifecycle(self.store)
        candidate = lifecycle.create(EvolutionCandidate.create(
            "tenant-a", "SKILL", "security-review", 1, "release-r1",
            attribution["attribution_id"], [failure_id], [],
            {"artifact": {"name": "security-review"}},
            {"method": "MODEL"}, utc_now(),
        ))
        rejected = lifecycle.evaluate(
            candidate["candidate_id"],
            lambda _value: {"eligible": False, "reason": "regression"},
        )
        self.assertEqual("REJECTED", rejected["status"])
        with self.assertRaisesRegex(ValueError, "READY_FOR_PROMOTION"):
            lifecycle.promote(candidate["candidate_id"], lambda _value: {})

    def test_incomplete_validation_can_resume(self):
        failure_id, attribution = self.seed_attribution("resume")
        lifecycle = CandidateLifecycle(self.store)
        candidate = lifecycle.create(EvolutionCandidate.create(
            "tenant-a", "SKILL", "security-review", 1, "release-r1",
            attribution["attribution_id"], [failure_id], [],
            {"artifact": {"name": "security-review", "revision": "resume"}},
            {"method": "MODEL"}, utc_now(),
        ))

        with self.assertRaises(SystemExit):
            lifecycle.evaluate(
                candidate["candidate_id"],
                lambda _value: (_ for _ in ()).throw(SystemExit("crash")),
            )
        self.assertEqual(
            "VALIDATING",
            self.store.get_evolution_candidate(candidate["candidate_id"])["status"],
        )

        resumed = lifecycle.evaluate(
            candidate["candidate_id"], lambda _value: {"eligible": True},
        )
        self.assertEqual("READY_FOR_PROMOTION", resumed["status"])


class SQLiteEvolutionLifecycleTests(EvolutionLifecycleStoreContract, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = TaskStore(self.temp.name)

    def tearDown(self):
        self.temp.close()


@unittest.skipUnless(
    os.environ.get("EVOAGENT_TEST_POSTGRES_URL"),
    "set EVOAGENT_TEST_POSTGRES_URL to run PostgreSQL evolution lifecycle contract",
)
class PostgresEvolutionLifecycleTests(EvolutionLifecycleStoreContract, unittest.TestCase):
    def setUp(self):
        from evoagent.postgres_store import PostgresTaskStore
        self.store = PostgresTaskStore(os.environ["EVOAGENT_TEST_POSTGRES_URL"])
        with self.store._connect() as conn:
            conn.execute("DELETE FROM evolution_candidates WHERE tenant_id='tenant-a'")
            conn.execute("DELETE FROM attribution_results WHERE task_id LIKE 'evolution-task-%'")
            conn.execute("DELETE FROM failure_cases WHERE task_id LIKE 'evolution-task-%'")
            conn.execute("DELETE FROM tasks WHERE id LIKE 'evolution-task-%'")


class EvolutionRoutingTests(unittest.TestCase):
    def test_supported_routes_are_narrow_and_unknown_is_normal_noop(self):
        skill = EvolutionRouter.route({
            "status": "SUPPORTED", "semantic_cause": "SKILL_GUIDANCE_GAP",
            "target_surface": "SKILL", "target_id": "security-review",
        })
        prompt = EvolutionRouter.route({
            "status": "SUPPORTED", "semantic_cause": "SYSTEM_POLICY_GAP",
            "target_surface": "GLOBAL_PROMPT", "target_id": "llm-review",
        })
        local_not_prompt = EvolutionRouter.route({
            "status": "SUPPORTED", "semantic_cause": "SKILL_GUIDANCE_GAP",
            "target_surface": "GLOBAL_PROMPT", "target_id": "llm-review",
        })
        unsupported = EvolutionRouter.route({
            "status": "SUPPORTED", "semantic_cause": "CONTEXT_EVIDENCE_MISSING",
            "target_surface": "GLOBAL_PROMPT", "target_id": "llm-review",
        })
        unknown = EvolutionRouter.route({"status": "UNKNOWN"})
        self.assertEqual({"surface": "SKILL", "target_id": "security-review"}, skill)
        self.assertEqual({"surface": "GLOBAL_PROMPT", "target_id": "llm-review"}, prompt)
        self.assertEqual("NO_SUPPORTED_EVOLUTION", local_not_prompt["surface"])
        self.assertEqual("NO_SUPPORTED_EVOLUTION", unsupported["surface"])
        self.assertEqual("NO_SUPPORTED_EVOLUTION", unknown["surface"])

    def test_automatic_api_does_not_accept_caller_selected_surface(self):
        class Service:
            def generate_evolution_candidate(self, *_args):
                raise AssertionError("caller-selected request must not reach generation")

        class Handler(ApiHandler):
            def _read_body(self):
                return self.body

            def _principal(self, _permission="read"):
                return type("Principal", (), {
                    "tenant_id": "tenant-a", "username": "alice",
                })()

            def _send_json(self, status, value):
                self.response = (status, value)

        def request(path, body):
            handler = object.__new__(Handler)
            handler.path = path
            handler.service = Service()
            handler.body = json.dumps(body).encode("utf-8")
            handler.response = None
            handler.do_POST()
            return handler.response

        status, response = request(
            "/v1/evolution/auto", {"failure_id": 1, "surface": "GLOBAL_PROMPT"},
        )
        self.assertEqual(400, status)
        self.assertIn("AttributionResult", response["error"])
        self.assertEqual(
            (409, {
                "error": "use /v1/evolution/auto with failure_id; callers cannot select the automatic surface"
            }),
            request("/v1/skill-evolution/auto", {"skill_name": "security-review"}),
        )


class EvolutionReleaseIntegrationTests(unittest.TestCase):
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

    def tearDown(self):
        self.service.queue.close()
        os.unlink(self.path)

    def test_generation_and_evaluation_do_not_promote_but_promotion_creates_release(self):
        task_a = self.service._create_task("org/repo", DIFF, 1, "test", "tenant-a")
        stored_a = self.service.store.get(task_a, "tenant-a")
        release_r1 = self.service.releases.active("tenant-a")
        failure_id = self.service.store.record_failure_case(
            task_a, "missed_issue", {"finding": {"path": "a.py", "line": 1}},
        )
        attribution = AttributionResult.create(failure_id, task_a, {
            "status": "SUPPORTED", "first_divergence": "DISCOVERY",
            "root_cause": "SYSTEM_POLICY_GAP",
            "evolution_surface": "GLOBAL_PROMPT", "evolution_target": "llm-review",
            "method": "DETERMINISTIC",
        }, utc_now())
        persisted = self.service.store.save_attribution_result(attribution.to_dict())
        candidate = self.service.candidate_lifecycle.create(EvolutionCandidate.create(
            "tenant-a", "GLOBAL_PROMPT", "llm-review", None,
            release_r1["release_id"], persisted["attribution_id"], [failure_id], [],
            {"prompt": "Review diffs. Return JSON with severity, fix, and test."},
            {"method": "MANUAL_TEST"}, utc_now(),
        ))
        self.assertEqual(release_r1["release_id"], self.service.releases.active("tenant-a")["release_id"])

        ready = self.service.candidate_lifecycle.evaluate(
            candidate["candidate_id"],
            lambda _value: {"eligible": True, "validation": {"candidate": {"score": 1.0}}},
        )
        self.assertEqual("READY_FOR_PROMOTION", ready["status"])
        self.assertEqual(release_r1["release_id"], self.service.releases.active("tenant-a")["release_id"])

        promoted = self.service.promote_evolution_candidate(
            candidate["candidate_id"], "tenant-a",
        )
        release_r2 = self.service.releases.active("tenant-a")
        self.assertEqual("PROMOTED", promoted["status"])
        self.assertEqual({}, promoted["final_evaluation_result"])
        self.assertEqual(release_r2["release_id"], promoted["surface_version"]["release_id"])
        self.assertNotEqual(release_r1["release_id"], release_r2["release_id"])
        self.assertEqual(stored_a["release_id"], self.service.store.get(task_a)["release_id"])
        task_b = self.service._create_task("org/repo", DIFF, 2, "test", "tenant-a")
        self.assertEqual(release_r2["release_id"], self.service.store.get(task_b)["release_id"])

    def test_insufficient_evidence_creates_no_candidate(self):
        task_id = self.service._create_task("org/repo", DIFF, 1, "test", "tenant-a")
        failure_id = self.service.store.record_failure_case(
            task_id, "missed_issue", {"finding": {"path": "a.py", "line": 1}},
        )
        attribution = AttributionResult.create(failure_id, task_id, {
            "status": "INSUFFICIENT_EVIDENCE",
            "first_divergence": "DISCOVERY",
            "root_cause": "INSUFFICIENT_EVIDENCE",
            "evolution_surface": "NO_SUPPORTED_EVOLUTION",
            "method": "MODEL",
        }, utc_now())
        self.service.store.save_attribution_result(attribution.to_dict())

        result = self.service.generate_evolution_candidate(failure_id, "tenant-a")

        self.assertEqual("NO_SUPPORTED_EVOLUTION", result["route"]["surface"])
        self.assertIsNone(result["candidate"])
        self.assertEqual([], self.service.store.list_evolution_candidates("tenant-a"))


if __name__ == "__main__":
    unittest.main()
