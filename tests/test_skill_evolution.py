import json
import os
import tempfile
import unittest
import uuid

from evoagent.api import ApiHandler
from evoagent.config import Settings
from evoagent.diff_parser import parse_unified_diff
from evoagent.service import ReviewService
from evoagent.skill_evolution import (
    AgentSkillReplayReviewer,
    SkillEvolutionEngine,
    validate_artifact,
)
from evoagent.skills import AgentSkill, SkillRegistry
from evoagent.store import TaskStore, utc_now
from agentic_fake import enable_agentic_service


RISK_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+dangerous_call(data)\n"
CLEAN_DIFF = "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-old\n+safe_call(data)\n"
EXISTING_DIFF = "--- a/c.py\n+++ b/c.py\n@@ -1 +1 @@\n-old\n+existing_call(data)\n"


def skill_markdown(name="review-dangerous-calls", include_rule=True):
    guidance = (
        "\n## Confirmed SEC-DANGEROUS-CALL guidance\n\n"
        "Inspect added behavior equivalent to `dangerous_call(data)`.\n"
        "Report `SEC-DANGEROUS-CALL` at `high` severity when context confirms it.\n"
        if include_rule else ""
    )
    return """---
name: %s
description: Review added code for confirmed project-specific dangerous calls.
---

# Review dangerous calls

Use changed-line evidence and report only actionable defects.%s
""" % (name, guidance)


def artifact(name="review-dangerous-calls", include_rule=True):
    return validate_artifact(skill_markdown(name, include_rule), name)


class SkillAwareClient:
    provider = "fake"
    model = "skill-aware"

    def complete_json(self, role, _system, user, ledger=None, max_tokens=None):
        if ledger:
            ledger.record_model(role, self.provider, self.model, {
                "prompt_tokens": 10, "completion_tokens": 5,
            }, 1)
        managed = json.loads(user)
        task = json.loads(managed["task"])
        if role == "lead":
            if task["phase"] == "delegate":
                requested = task.get("requested_agent_skills") or []
                selected = requested or [
                    item["name"] for item in task.get("available_agent_skills") or []
                ]
                return {"action": "final", "delegations": [{
                    "assignment_id": "security-1", "worker": "security",
                    "objective": "Review project-specific dangerous calls",
                    "skills": selected,
                }], "risk_level": "normal"}
            if task["phase"] == "assess-workers":
                return {"action": "final", "revision_requests": [], "critic_objective": "Verify"}
            if task["phase"] == "finalize":
                return {
                    "action": "final",
                    "accepted_finding_indices": list(range(len(task["candidate_findings"]))),
                    "confidence_adjustments": [],
                }
        if role == "security":
            instructions = "\n".join(
                item.get("instructions", "") for item in task.get("active_agent_skills") or []
            )
            rendered_diff = json.dumps(task["diff"], ensure_ascii=False)
            if "dangerous_call(data)" in instructions and "dangerous_call(data)" in rendered_diff:
                return {"action": "final", "findings": [{
                    "rule_id": "SEC-DANGEROUS-CALL", "severity": "high",
                    "title": "Dangerous call", "explanation": "Confirmed unsafe API was added.",
                    "path": "a.py", "line": 1, "evidence": "dangerous_call(data)",
                    "fix": "Use safe_call instead.", "test": "Add a regression test.",
                    "confidence": .9,
                    "call_chain": [{"path": "a.py", "line": 1, "symbol": "dangerous_call"}],
                }]}
            if "existing_call(data)" in instructions and "existing_call(data)" in rendered_diff:
                return {"action": "final", "findings": [{
                    "rule_id": "SEC-EXISTING", "severity": "high",
                    "title": "Existing issue", "explanation": "Existing guidance matched.",
                    "path": "c.py", "line": 1, "evidence": "existing_call(data)",
                    "fix": "Use the safe API.", "test": "Add a regression test.",
                    "confidence": .9,
                    "call_chain": [{"path": "c.py", "line": 1, "symbol": "existing_call"}],
                }]}
            if "REPORT_SAFE_CALL" in instructions and "safe_call(data)" in rendered_diff:
                return {"action": "final", "findings": [{
                    "rule_id": "SEC-FALSE-POSITIVE", "severity": "medium",
                    "title": "False positive", "explanation": "Deliberate test regression.",
                    "path": "b.py", "line": 1, "evidence": "safe_call(data)",
                    "fix": "No fix.", "test": "No test.", "confidence": .9,
                }]}
            return {"action": "final", "findings": []}
        if role == "correctness-reliability":
            return {"action": "final", "findings": []}
        if role == "critic":
            return {"action": "final", "decisions": [{
                "finding_index": index, "accepted": True, "objections": [],
                "confidence_adjustment": 0.0,
            } for index, _item in enumerate(task["candidates"])]}
        raise AssertionError(role)


class FakeSkillPatchGenerator:
    def __init__(self, builder=None):
        self.builder = builder or (lambda _name, _baseline, _attribution, _expected, _evidence: artifact())
        self.calls = []

    def generate(self, skill_name, baseline, attribution, expected, evidence):
        self.calls.append({
            "skill_name": skill_name, "baseline": baseline,
            "attribution": attribution, "expected": expected, "evidence": evidence,
        })
        return {
            "artifact": self.builder(skill_name, baseline, attribution, expected, evidence),
            "reason": "focused guidance patch", "generation": {"calls": 1},
            "generator": {"provider": "fake", "model": "patch"},
        }


class SkillEvolutionTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)
        self.client = SkillAwareClient()

    def tearDown(self):
        os.unlink(self.path)

    def seed_cases(self):
        self.store.save_evaluation_case(
            "danger-validation", "validation", RISK_DIFF,
            [{"path": "a.py", "line": 1, "rule_id": "SEC-DANGEROUS-CALL", "min_severity": "high"}],
            "test",
        )
        self.store.save_evaluation_case("clean-holdout", "holdout", CLEAN_DIFF, [], "test")

    def engine(self, candidate_generator=None, runtime_skill_provider=None):
        return SkillEvolutionEngine(
            self.store,
            reviewer_factory=lambda value: AgentSkillReplayReviewer(value, self.client),
            min_cases=1, max_cases=10, min_improvement=.01, min_holdout_cases=1,
            candidate_generator=candidate_generator,
            runtime_skill_provider=runtime_skill_provider,
        )

    def save_artifact_with_run(
        self, decision=None, active=False, tenant_id="default", value=None,
    ):
        version = self.store.save_skill_artifact(
            "review-dangerous-calls", value or artifact(), 1.0, active, tenant_id,
        )
        if decision:
            self.store.save_skill_evolution_run({
                "id": str(uuid.uuid4()), "tenant_id": tenant_id,
                "skill_name": "review-dangerous-calls",
                "candidate_version": version["version"], "baseline_version": None,
                "decision": decision, "candidate_score": 1.0,
                "baseline_score": 0.0, "metrics": {}, "created_at": utc_now(),
            })
        return version

    def seed_targeted_failure(
        self, attribution=None, diff=RISK_DIFF, snapshots=True, save_diff=True,
        category="missed_issue",
    ):
        task_id = "task-%d" % len(self.store.list_failure_cases())
        self.store.create(task_id, "org/repo", 1, {"source": "test"})
        if save_diff:
            self.store.save_task_payload(task_id, diff)
        if snapshots:
            self.store.save_checkpoint(task_id, "agentic-lead-session", {
                "protocol": "lead-workers-v3",
                "session": {
                    "delegations": [{
                        "assignment_id": "security-1", "worker": "security",
                        "objective": "Review dangerous calls", "files": ["a.py"],
                        "skills": ["review-dangerous-calls"],
                    }],
                    "worker_execution_snapshots": {
                        "security-1": {
                            "assignment_id": "security-1", "run_id": "security-1",
                            "worker": "security", "revision_round": 0,
                            "system_prompt": "historical system prompt",
                            "selected_skills": [{
                                "name": "review-dangerous-calls", "version": "1",
                                "source": "disk", "content_sha256": "abc",
                            }],
                            "final_managed_user_context": json.dumps({"task": "historical"}),
                            "final_parsed_model_action": {"action": "final", "findings": []},
                        },
                    },
                },
            })
        value = attribution or {
            "status": "SUPPORTED", "first_divergence": "DISCOVERY",
            "root_cause": "SKILL_GUIDANCE_GAP", "evolution_surface": "SKILL",
            "evolution_target": "review-dangerous-calls",
            "reason": "Historical guidance omitted this domain check.",
            "evidence_summary": ["The relevant code was present."],
        }
        self.store.record_failure_case(task_id, category, {
            "finding": {
                "rule_id": "SEC-DANGEROUS-CALL", "severity": "high",
                "path": "a.py", "line": 1, "title": "Dangerous call",
                "evidence": "dangerous_call(data)",
            },
            "attribution": value,
        })
        return task_id, self.store.list_failure_cases()[0]

    def test_registry_loads_standard_skill_and_resources_without_python(self):
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "review-dangerous-calls")
            os.makedirs(os.path.join(directory, "references"))
            with open(os.path.join(directory, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write(skill_markdown())
            with open(os.path.join(directory, "references", "policy.md"), "w", encoding="utf-8") as handle:
                handle.write("Use safe_call.\n")
            with open(os.path.join(directory, "skill.py"), "w", encoding="utf-8") as handle:
                handle.write("raise RuntimeError('must never be imported')\n")
            registry = SkillRegistry(root)
            registry.reload()
            loaded = registry.get_agent_skill("review-dangerous-calls")
            self.assertIsNotNone(loaded)
            self.assertEqual("Use safe_call.\n", loaded.read_resource("references/policy.md"))
            self.assertEqual("agent-skill", registry.list()[0]["kind"])
            os.unlink(os.path.join(directory, "SKILL.md"))
            registry.reload()
            self.assertIsNone(registry.get_agent_skill("review-dangerous-calls"))

    def test_bundled_agent_skills_are_discoverable(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills"))
        registry = SkillRegistry(root)
        registry.reload()
        self.assertEqual({
            "api-compatibility", "code-quality", "correctness-review",
            "database-review", "observability-review", "performance-review",
            "reliability-review", "security-review", "test-quality",
        }, {item["name"] for item in registry.catalog()})
        for item in registry.list():
            self.assertEqual("agent-skill", item["kind"])
            self.assertTrue(item["description"])

    def test_skill_md_requires_frontmatter_and_matching_directory_name(self):
        with self.assertRaisesRegex(ValueError, "frontmatter"):
            AgentSkill.from_markdown("# Missing metadata")
        with self.assertRaisesRegex(ValueError, "match"):
            AgentSkill.from_markdown(
                skill_markdown("review-dangerous-calls"),
                expected_name="different-name",
            )

    def test_agent_skill_body_is_selected_and_runs_through_worker_graph(self):
        reviewer = AgentSkillReplayReviewer(artifact(), self.client)
        findings = reviewer.review(RISK_DIFF, parse_unified_diff(RISK_DIFF))
        self.assertEqual(["SEC-DANGEROUS-CALL"], [item.rule_id for item in findings])
        summary = reviewer.agentic.collaboration_summary("skill-replay:review-dangerous-calls:1")
        self.assertEqual(["review-dangerous-calls"], summary["collaboration"]["agent_skills"])

    def test_candidate_skill_md_replay_activates_and_persists(self):
        self.seed_cases()
        result = self.engine().propose("review-dangerous-calls", artifact())
        self.assertEqual("activated", result["decision"])
        active = self.store.get_active_skill_artifact("review-dangerous-calls")
        self.assertEqual("agent-skill", active["artifact"]["format"])
        self.assertIn("SKILL.md", active["artifact"]["files"])
        self.assertEqual(2, active["artifact"]["schema_version"])
        self.assertEqual(["SKILL.md"], result["candidate_change"]["changed_files"])

    def test_pure_feedback_mutation_remains_available(self):
        built = self.engine().build_candidate_artifact(
            "review-dangerous-calls", artifact(include_rule=False), [{
                "id": 1, "category": "missed_issue", "payload": {"finding": {
                    "rule_id": "SEC-DANGEROUS-CALL", "severity": "high",
                    "evidence": "dangerous_call(data)",
                }},
            }],
        )
        self.assertIn(
            "Confirmed SEC-DANGEROUS-CALL guidance",
            built["artifact"]["files"]["SKILL.md"],
        )

    def test_targeted_auto_propose_saves_inactive_candidate_without_resolving_failure(self):
        self.seed_cases()
        _task_id, failure = self.seed_targeted_failure()
        baseline_skill = AgentSkill.from_artifact(artifact(include_rule=False))
        generator = FakeSkillPatchGenerator()
        result = self.engine(
            generator, lambda _tenant: [baseline_skill],
        ).auto_propose("review-dangerous-calls")
        self.assertEqual("ready_for_promotion", result["decision"])
        self.assertFalse(result["version"]["active"])
        self.assertIsNone(self.store.get_active_skill_artifact("review-dangerous-calls"))
        self.assertFalse(self.store.list_failure_cases()[0]["resolved"])
        self.assertEqual(failure["id"], result["source_failure_id"])
        run = self.store.list_skill_evolution_runs()[0]
        self.assertEqual("ready_for_promotion", run["decision"])
        self.assertEqual(failure["id"], run["metrics"]["provenance"]["source_failure_id"])
        self.assertTrue(result["gates"]["source_replay_fixed"])

    def test_targeted_gate_requires_exact_supported_skill_gap_and_target(self):
        variants = [
            {"status": "INSUFFICIENT_EVIDENCE", "root_cause": "SKILL_GUIDANCE_GAP",
             "evolution_surface": "SKILL", "evolution_target": "review-dangerous-calls"},
            {"status": "SUPPORTED", "root_cause": "CONTEXT_EVIDENCE_MISSING",
             "evolution_surface": "SKILL", "evolution_target": "review-dangerous-calls"},
            {"status": "SUPPORTED", "root_cause": "MODEL_REASONING_FAILURE",
             "evolution_surface": "SKILL", "evolution_target": "review-dangerous-calls"},
            {"status": "SUPPORTED", "root_cause": "SKILL_GUIDANCE_GAP",
             "evolution_surface": "NO_SUPPORTED_EVOLUTION",
             "evolution_target": "review-dangerous-calls"},
            {"status": "SUPPORTED", "root_cause": "SKILL_GUIDANCE_GAP",
             "evolution_surface": "SKILL", "evolution_target": "other-skill"},
        ]
        generator = FakeSkillPatchGenerator()
        for value in variants:
            self.seed_targeted_failure(value)
        self.seed_targeted_failure(category="false_positive")
        result = self.engine(generator).auto_propose("review-dangerous-calls")
        self.assertEqual("deferred", result["decision"])
        self.assertEqual([], generator.calls)
        self.assertEqual([], self.store.list_skill_artifact_versions("review-dangerous-calls"))

    def test_targeted_auto_propose_selects_only_newest_eligible_failure(self):
        self.seed_cases()
        self.seed_targeted_failure()
        _task_id, newest = self.seed_targeted_failure()
        generator = FakeSkillPatchGenerator()
        baseline_skill = AgentSkill.from_artifact(artifact(include_rule=False))
        result = self.engine(generator, lambda _tenant: [baseline_skill]).auto_propose(
            "review-dangerous-calls"
        )
        self.assertEqual(newest["id"], result["source_failure_id"])
        self.assertEqual(1, len(generator.calls))

    def test_runtime_baseline_uses_bundled_skill_and_db_override_wins(self):
        self.seed_cases()
        self.seed_targeted_failure()
        bundled_artifact = artifact(include_rule=False)
        bundled_artifact["files"]["references/policy.md"] = "bundled policy\n"
        bundled_skill = AgentSkill.from_artifact(bundled_artifact)
        generator = FakeSkillPatchGenerator(lambda _name, baseline, *_args: {
            **baseline,
            "files": {**baseline["files"], "SKILL.md": skill_markdown()},
        })
        self.engine(generator, lambda _tenant: [bundled_skill]).auto_propose(
            "review-dangerous-calls"
        )
        self.assertEqual(
            "bundled policy\n",
            generator.calls[0]["baseline"]["files"]["references/policy.md"],
        )

        db_artifact = artifact(include_rule=False)
        db_artifact["files"]["references/policy.md"] = "tenant override\n"
        self.store.save_skill_artifact(
            "review-dangerous-calls", db_artifact, 0.2, True,
        )
        self.seed_targeted_failure()
        generator.calls.clear()
        self.engine(generator, lambda _tenant: [bundled_skill]).auto_propose(
            "review-dangerous-calls"
        )
        self.assertEqual(
            "tenant override\n",
            generator.calls[0]["baseline"]["files"]["references/policy.md"],
        )

    def test_generator_boundary_rejects_name_resource_and_noop_changes(self):
        cases = {
            "name": lambda _name, baseline, *_args: {
                **baseline, "name": "other-skill",
                "files": {**baseline["files"], "SKILL.md": skill_markdown("other-skill")},
            },
            "added-resource": lambda _name, baseline, *_args: {
                **baseline,
                "files": {
                    **baseline["files"], "SKILL.md": skill_markdown(),
                    "references/new.md": "not allowed\n",
                },
            },
            "changed-resource": lambda _name, baseline, *_args: {
                **baseline,
                "files": {
                    **baseline["files"], "SKILL.md": skill_markdown(),
                    "references/policy.md": "changed\n",
                },
            },
            "noop": lambda _name, baseline, *_args: baseline,
        }
        for label, builder in cases.items():
            with self.subTest(label=label):
                handle, path = tempfile.mkstemp(suffix=".db")
                os.close(handle)
                store = TaskStore(path)
                try:
                    original_store, self.store = self.store, store
                    self.seed_cases()
                    self.seed_targeted_failure()
                    baseline = artifact(include_rule=False)
                    baseline["files"]["references/policy.md"] = "original\n"
                    provider = lambda _tenant: [AgentSkill.from_artifact(baseline)]
                    result = self.engine(
                        FakeSkillPatchGenerator(builder), provider,
                    ).auto_propose("review-dangerous-calls")
                    self.assertEqual("rejected", result["decision"])
                    self.assertIsNone(result["version"])
                    self.assertEqual(
                        [], store.list_skill_artifact_versions("review-dangerous-calls"),
                    )
                finally:
                    self.store = original_store
                    os.unlink(path)

    def test_missing_source_diff_defers_without_generation(self):
        self.seed_cases()
        self.seed_targeted_failure(save_diff=False)
        generator = FakeSkillPatchGenerator()
        result = self.engine(generator).auto_propose("review-dangerous-calls")
        self.assertEqual("deferred", result["decision"])
        self.assertEqual([], generator.calls)
        self.assertEqual([], self.store.list_skill_artifact_versions("review-dangerous-calls"))

    def test_candidate_that_does_not_fix_source_failure_is_rejected_before_regression(self):
        self.seed_cases()
        self.seed_targeted_failure()
        generator = FakeSkillPatchGenerator(
            lambda _name, baseline, *_args: {
                **baseline,
                "files": {
                    **baseline["files"],
                    "SKILL.md": baseline["files"]["SKILL.md"].rstrip()
                    + "\n\nCheck an unrelated local convention.\n",
                },
            }
        )
        baseline = AgentSkill.from_artifact(artifact(include_rule=False))
        result = self.engine(generator, lambda _tenant: [baseline]).auto_propose(
            "review-dangerous-calls"
        )
        self.assertEqual("rejected", result["decision"])
        self.assertFalse(result["gates"]["source_replay_fixed"])
        self.assertIsNone(result["gates"]["evaluation_success"])
        self.assertEqual([], result["candidate"]["case_results"])

    def test_source_fix_is_rejected_when_protected_validation_regresses(self):
        self.store.save_evaluation_case(
            "existing-validation", "validation", EXISTING_DIFF,
            [{"path": "c.py", "line": 1, "rule_id": "SEC-EXISTING", "min_severity": "high"}],
            "test",
        )
        self.store.save_evaluation_case("clean-holdout", "holdout", CLEAN_DIFF, [], "test")
        self.seed_targeted_failure()
        baseline_md = skill_markdown(include_rule=False).rstrip() + (
            "\n\nInspect `existing_call(data)` and report `SEC-EXISTING`.\n"
        )
        baseline = validate_artifact(baseline_md, "review-dangerous-calls")
        generator = FakeSkillPatchGenerator()
        result = self.engine(
            generator, lambda _tenant: [AgentSkill.from_artifact(baseline)],
        ).auto_propose("review-dangerous-calls")
        self.assertEqual("rejected", result["decision"])
        self.assertTrue(result["gates"]["source_replay_fixed"])
        self.assertFalse(result["gates"]["validation_non_regression"])
        self.assertFalse(result["version"]["active"])

    def test_source_fix_is_rejected_when_holdout_regresses(self):
        self.seed_cases()
        self.seed_targeted_failure()
        regressing_md = skill_markdown().rstrip() + "\n\nREPORT_SAFE_CALL\n"
        generator = FakeSkillPatchGenerator(
            lambda _name, _baseline, *_args: validate_artifact(
                regressing_md, "review-dangerous-calls"
            )
        )
        baseline = AgentSkill.from_artifact(artifact(include_rule=False))
        result = self.engine(generator, lambda _tenant: [baseline]).auto_propose(
            "review-dangerous-calls"
        )
        self.assertEqual("rejected", result["decision"])
        self.assertTrue(result["gates"]["source_replay_fixed"])
        self.assertFalse(result["gates"]["holdout_non_regression"])

    def test_ready_candidate_promotes_and_keeps_source_failure_unresolved(self):
        self.store.create("source-task", "org/repo", 1, {})
        self.store.record_failure_case("source-task", "missed_issue", {"finding": {}})
        previous = self.save_artifact_with_run("activated", active=True)
        ready = self.save_artifact_with_run(
            "ready_for_promotion", value=artifact(include_rule=False),
        )

        self.assertTrue(self.engine().rollback(
            "review-dangerous-calls", ready["version"], "default"
        ))

        versions = self.store.list_skill_artifact_versions("review-dangerous-calls")
        self.assertEqual([ready["version"]], [item["version"] for item in versions if item["active"]])
        self.assertNotEqual(previous["version"], ready["version"])
        self.assertFalse(self.store.list_failure_cases()[0]["resolved"])

    def test_rejected_and_arbitrary_artifacts_cannot_replace_active_version(self):
        active = self.save_artifact_with_run("activated", active=True)
        rejected = self.save_artifact_with_run(
            "rejected", value=artifact(include_rule=False),
        )
        arbitrary = self.save_artifact_with_run(
            value=validate_artifact(
                skill_markdown().rstrip() + "\n\nArbitrary guidance.\n",
                "review-dangerous-calls",
            ),
        )

        for candidate in (rejected, arbitrary):
            self.assertFalse(self.engine().rollback(
                "review-dangerous-calls", candidate["version"], "default"
            ))
            self.assertEqual(
                active["version"],
                self.store.get_active_skill_artifact("review-dangerous-calls")["version"],
            )

    def test_explicit_db_rollback_reactivates_prior_successful_version(self):
        first = self.save_artifact_with_run("activated", active=True)
        second = self.save_artifact_with_run(
            "ready_for_promotion", value=artifact(include_rule=False),
        )
        engine = self.engine()
        self.assertTrue(engine.rollback(
            "review-dangerous-calls", second["version"], "default"
        ))
        self.assertTrue(engine.rollback(
            "review-dangerous-calls", first["version"], "default"
        ))
        versions = self.store.list_skill_artifact_versions("review-dangerous-calls")
        self.assertEqual([first["version"]], [item["version"] for item in versions if item["active"]])
        with tempfile.TemporaryDirectory() as skills_dir:
            settings = Settings(
                host="127.0.0.1", port=8080, db_path=self.path,
                max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
                llm_base_url="", llm_api_key="", llm_model="",
                github_webhook_secret="", github_token="", auto_post_review=False,
                skills_dir=skills_dir, eval_min_holdout_cases=0,
            )
            service = ReviewService(settings)
            try:
                resolved = {
                    skill.name: skill for skill in service._active_agent_skills("default")
                }["review-dangerous-calls"]
                self.assertEqual(str(first["version"]), resolved.version)
                self.assertIn("SEC-DANGEROUS-CALL", resolved.instructions)
            finally:
                service.queue.close()

    def test_promotion_and_bundled_fallback_are_tenant_isolated(self):
        self.save_artifact_with_run("activated", active=True, tenant_id="tenant-a")
        a = self.save_artifact_with_run("ready_for_promotion", tenant_id="tenant-a")
        b = self.save_artifact_with_run("activated", active=True, tenant_id="tenant-b")
        engine = self.engine()

        self.assertTrue(engine.rollback(
            "review-dangerous-calls", a["version"], "tenant-a"
        ))
        self.assertFalse(engine.rollback(
            "review-dangerous-calls", a["version"], "tenant-b"
        ))
        self.assertTrue(engine.fallback_to_bundled(
            "review-dangerous-calls", "tenant-a"
        ))
        self.assertIsNone(self.store.get_active_skill_artifact(
            "review-dangerous-calls", "tenant-a"
        ))
        self.assertEqual(
            b["version"], self.store.get_active_skill_artifact(
                "review-dangerous-calls", "tenant-b"
            )["version"],
        )
        self.assertEqual(2, len(self.store.list_skill_artifact_versions(
            "review-dangerous-calls", "tenant-a"
        )))

    def test_runtime_uses_promoted_db_skill_then_falls_back_to_bundled(self):
        bundled_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills"))
        settings = Settings(
            host="127.0.0.1", port=8080, db_path=self.path,
            max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
            llm_base_url="", llm_api_key="", llm_model="",
            github_webhook_secret="", github_token="", auto_post_review=False,
            skills_dir=bundled_root, eval_min_holdout_cases=0,
        )
        service = enable_agentic_service(ReviewService(settings))
        try:
            bundled = next(
                skill for skill in service.registry.agent_skills()
                if skill.name == "security-review"
            )
            override = bundled.to_artifact()
            override["files"]["SKILL.md"] = (
                override["files"]["SKILL.md"].rstrip()
                + "\n\n## Tenant override marker\n\nCheck the tenant-specific policy.\n"
            )
            promoted = service.store.save_skill_artifact(
                "security-review", validate_artifact(override, "security-review"),
                1.0, True, "tenant-a",
            )
            resolved = {
                skill.name: skill for skill in service._active_agent_skills("tenant-a")
            }["security-review"]
            self.assertEqual("evolved-db", resolved.source)
            self.assertEqual(str(promoted["version"]), resolved.version)
            self.assertNotEqual(bundled.content_sha256, resolved.content_sha256)

            review = service.create_review(
                "org/repo", RISK_DIFF, tenant_id="tenant-a",
                enabled_skills=["security-review"],
            )
            checkpoint = service.store.load_checkpoints(
                review["task_id"]
            )["agentic-lead-session"]["state"]["session"]
            manifests = [
                item for snapshot in checkpoint["worker_execution_snapshots"].values()
                for item in snapshot["selected_skills"]
                if item["name"] == "security-review"
            ]
            self.assertTrue(manifests)
            self.assertTrue(all(item["source"] == "evolved-db" for item in manifests))
            self.assertTrue(all(item["content_sha256"] == resolved.content_sha256 for item in manifests))

            # Leave the original Task with a pinned profile but pending Workers.
            # A later fallback must not change that Task's execution authority.
            state = service.store.load_checkpoints(
                review["task_id"]
            )["agentic-lead-session"]["state"]
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
            service.store.save_checkpoint(review["task_id"], "agentic-lead-session", {
                "protocol": "lead-workers-v3", "session": pending,
                "execution": state["execution"],
            }, "in_progress", 1)

            self.assertTrue(service.fallback_skill_to_bundled(
                "security-review", "tenant-a"
            ))
            service.reload_skills()
            fallback = {
                skill.name: skill for skill in service._active_agent_skills("tenant-a")
            }["security-review"]
            self.assertEqual("disk", fallback.source)
            self.assertEqual(bundled.content_sha256, fallback.content_sha256)
            self.assertIsNone(service.store.get_active_skill_artifact(
                "security-review", "tenant-a"
            ))
            self.assertEqual(1, len(service.store.list_skill_artifact_versions(
                "security-review", "tenant-a"
            )))

            service.reviewer.review_with_context(
                review["task_id"], RISK_DIFF, parse_unified_diff(RISK_DIFF),
                "org/repo", "tenant-a",
            )
            resumed = service.store.load_checkpoints(
                review["task_id"]
            )["agentic-lead-session"]["state"]["session"]
            resumed_security = [
                item for snapshot in resumed["worker_execution_snapshots"].values()
                for item in snapshot["selected_skills"]
                if item["name"] == "security-review"
            ]
            self.assertTrue(resumed_security)
            self.assertTrue(all(
                item["source"] == "evolved-db"
                and item["content_sha256"] == resolved.content_sha256
                for item in resumed_security
            ))

            new_review = service.create_review(
                "org/repo", RISK_DIFF, tenant_id="tenant-a",
                enabled_skills=["security-review"],
            )
            new_session = service.store.load_checkpoints(
                new_review["task_id"]
            )["agentic-lead-session"]["state"]["session"]
            new_security = [
                item for snapshot in new_session["worker_execution_snapshots"].values()
                for item in snapshot["selected_skills"]
                if item["name"] == "security-review"
            ]
            self.assertTrue(new_security)
            self.assertTrue(all(
                item["source"] == "disk"
                and item["content_sha256"] == bundled.content_sha256
                for item in new_security
            ))
        finally:
            service.queue.close()

    def test_activation_and_fallback_api_reload_audit_and_require_manage(self):
        class FakeStore:
            def __init__(self):
                self.audits = []

            def audit(self, *args):
                self.audits.append(args)

        class FakeEvolution:
            activation = True

            def rollback(self, *_args):
                return self.activation

        class FakeService:
            def __init__(self):
                self.skill_evolution = FakeEvolution()
                self.store = FakeStore()
                self.reloads = 0
                self.fallback = True

            def reload_skills(self):
                self.reloads += 1

            def fallback_skill_to_bundled(self, *_args):
                return self.fallback

        class Handler(ApiHandler):
            def _read_body(self):
                return b""

            def _principal(self, permission="read"):
                self.permissions.append(permission)
                if self.deny:
                    raise PermissionError("permission denied")
                return type("Principal", (), {
                    "tenant_id": "tenant-a", "username": "alice",
                })()

            def _send_json(self, status, value):
                self.response = (status, value)

        def request(path, service, deny=False):
            handler = object.__new__(Handler)
            handler.path = path
            handler.service = service
            handler.permissions = []
            handler.deny = deny
            handler.response = None
            handler.do_POST()
            return handler

        service = FakeService()
        promoted = request(
            "/v1/skill-evolution/security-review/versions/2/activate", service
        )
        self.assertEqual(["manage"], promoted.permissions)
        self.assertEqual((200, {"activated": True}), promoted.response)
        self.assertEqual(1, service.reloads)
        self.assertEqual("skill.evolution.activate", service.store.audits[-1][2])

        service.skill_evolution.activation = False
        rejected = request(
            "/v1/skill-evolution/security-review/versions/3/activate", service
        )
        self.assertEqual((404, {"activated": False}), rejected.response)
        self.assertEqual(1, service.reloads)

        fallback = request(
            "/v1/skill-evolution/security-review/fallback", service
        )
        self.assertEqual((200, {"fallback_to_bundled": True}), fallback.response)
        self.assertEqual(2, service.reloads)
        self.assertEqual("skill.evolution.fallback", service.store.audits[-1][2])

        denied = request(
            "/v1/skill-evolution/security-review/fallback", service, deny=True
        )
        self.assertEqual((403, {"error": "permission denied"}), denied.response)
        self.assertEqual(2, service.reloads)

    def test_active_agent_skills_are_tenant_isolated_and_override_disk(self):
        self.store.save_skill_artifact(
            "review-dangerous-calls", artifact(), 1.0, True, "tenant-a",
        )
        with tempfile.TemporaryDirectory() as skills_dir:
            settings = Settings(
                host="127.0.0.1", port=8080, db_path=self.path,
                max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
                llm_base_url="", llm_api_key="", llm_model="",
                github_webhook_secret="", github_token="", auto_post_review=False,
                skills_dir=skills_dir, eval_min_holdout_cases=0,
            )
            service = ReviewService(settings)
            service.chat_client = self.client
            service.reviewer = service._build_agentic_reviewer()
            service.harness.reviewer = service.reviewer
            try:
                self.assertIn(
                    "review-dangerous-calls", {item["name"] for item in service.list_skills("tenant-a")}
                )
                self.assertNotIn(
                    "review-dangerous-calls", {item["name"] for item in service.list_skills("tenant-b")}
                )
                report = service.create_review(
                    "org/repo", RISK_DIFF, tenant_id="tenant-a",
                    enabled_skills=["review-dangerous-calls"],
                )["report"]
                self.assertIn(
                    "SEC-DANGEROUS-CALL", [item["rule_id"] for item in report["findings"]]
                )
            finally:
                service.queue.close()


if __name__ == "__main__":
    unittest.main()
