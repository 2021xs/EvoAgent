import copy
import hashlib
import json
import os
import tempfile
import unittest

from evoagent.benchmark_experiments import (
    ArmRuntime,
    ThreeArmBenchmarkRunner,
)
from evoagent.benchmark_governance import (
    BenchmarkManifest,
    BenchmarkValidationError,
    FinalEvaluationRunner,
    FinalRunIntent,
    aggregate_optional_cost,
    attribution_qualifies_for_primary_gold,
    audit_matcher,
    build_run_manifest,
    case_qualifies_for_primary_gold,
    compare_annotation_labels,
    expand_repository_sample,
    finding_qualifies_for_primary_gold,
    repository_cluster_paired_bootstrap,
    render_reliability_matrix,
    score_attribution,
    score_final_benchmark,
    summarize_protocol_dry_run,
    validate_attribution_gold,
    validate_benchmark_case,
    validate_repository_splits,
    verify_run_manifest,
    write_manifest_immutable,
    write_run_manifest_immutable,
)
from evoagent.evolution import EvolutionEngine
from evoagent.evolution_lifecycle import CandidateLifecycle, EvolutionCandidate
from evoagent.store import TaskStore


DIFF = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+danger(value)\n"


def annotator(name="annotator-a", run_id="run-a", kind="MODEL", role="FIRST_PASS"):
    model_value = "fixed" if kind == "MODEL" else None
    return {
        "annotator_id": name, "annotator_kind": kind, "role": role,
        "provider": model_value, "model": model_value, "model_revision": model_value,
        "prompt_version": model_value, "prompt_hash": model_value,
        "context_policy_id": model_value, "context_policy_hash": model_value,
        "tool_policy_id": model_value, "tool_policy_hash": model_value,
        "repository_access_mode": (
            "FROZEN_REPOSITORY_READ_ONLY" if kind != "OBJECTIVE_EVIDENCE"
            else "OBJECTIVE_RUNTIME_EVIDENCE"
        ),
        "run_id": run_id, "started_at": "2026-09-19T00:00:00Z",
        "completed_at": "2026-09-19T00:01:00Z",
        "blindness": {
            "evoagent_prediction_visible": False, "experiment_arm_visible": False,
            "peer_annotation_visible": role == "ADJUDICATOR",
            "production_attribution_visible": False,
        },
    }


def reviewer_label(name="annotator-a", issue_exists=True, should_comment=True):
    return {
        "annotator_id": name, "issue_exists": issue_exists,
        "should_comment": should_comment, "logical_issue_id": "issue-1",
        "category": "security", "path": "app.py", "start_line": 1, "end_line": 1,
        "severity": "high", "evidence_refs": ["app.py:1"],
        "rationale": "The frozen repository line supports this label.", "duration_seconds": 90,
        "submitted_at": "2026-09-19T00:00:00Z",
    }


def finding(should_comment=True, status="ADJUDICATED"):
    value = {
        "issue_id": "issue-1", "category": "security", "rule_id": "SEC-EVAL",
        "cwe": "CWE-95", "path": "app.py", "start_line": 1, "end_line": 1,
        "severity": "high", "issue_exists": True,
        "should_comment": should_comment,
        "technical_evidence": ["danger(value)"],
        "technical_rationale": "The frozen line invokes a dangerous operation.",
        "comment_usefulness_rationale": (
            "The developer should change it." if should_comment
            else "The issue is technically present but intentionally accepted here."
        ),
        "annotation_status": status,
        "reviewer_labels": [
            reviewer_label("annotator-a", True, should_comment),
            reviewer_label("annotator-b", True, should_comment),
        ],
        "gold_evidence_level": (
            "INDEPENDENT_MODEL_AGREEMENT" if status == "ADJUDICATED" else "QUARANTINED"
        ),
        "objective_evidence": None,
    }
    if status == "ADJUDICATED":
        value["adjudication"] = None
        value["uncertainty_reason"] = None
    else:
        value["adjudication"] = None
        value["uncertainty_reason"] = "Repository context is incomplete."
    return value


def benchmark_case(
    case_id="case-1", repository="repo-1", split="validation",
    findings=None, status="ADJUDICATED", protocol_dry_run=False,
):
    values = [finding()] if findings is None else findings
    commentable = any(
        item["issue_exists"] and item["should_comment"]
        and item["annotation_status"] == "ADJUDICATED" for item in values
    )
    return {
        "schema_version": 2, "dataset_id": "evidence-adjudicated-benchmark",
        "dataset_version": "1.0.0", "annotation_version": "evidence-adjudication-guide-v1",
        "case_id": case_id, "repository_id": repository,
        "source": {"kind": "public-github-pr", "reference_id": repository + "#1", "url": None},
        "base_sha": "a" * 40, "head_sha": "b" * 40,
        "diff_sha256": hashlib.sha256(DIFF.encode()).hexdigest(),
        "captured_at": "2026-09-19T00:00:00Z", "split": split,
        "protocol_dry_run": protocol_dry_run, "related_cases": [], "diff": DIFF,
        "annotators": [annotator("annotator-a", "run-a"), annotator("annotator-b", "run-b")],
        "findings": values, "case_annotation_status": status,
        "reviewer_records": [{
            "annotator_id": "annotator-a", "completed_review": True,
            "duration_seconds": 120, "submitted_at": "2026-09-19T00:00:00Z",
        }, {
            "annotator_id": "annotator-b", "completed_review": True,
            "duration_seconds": 120, "submitted_at": "2026-09-19T00:00:00Z",
        }],
        "clean_review": {
            "completed": status == "ADJUDICATED" and not commentable,
            "no_commentable_findings_remain": status == "ADJUDICATED" and not commentable,
            "rationale": "No adjudicated comment-worthy issues remain." if not commentable else "Positive case.",
            "gold_evidence_level": "INDEPENDENT_MODEL_AGREEMENT",
            "objective_evidence": None,
        },
    }


class BenchmarkSchemaTests(unittest.TestCase):
    def test_schema_rejects_missing_revision_and_should_comment(self):
        case = benchmark_case()
        del case["head_sha"]
        with self.assertRaisesRegex(BenchmarkValidationError, "head_sha"):
            validate_benchmark_case(case)
        case = benchmark_case()
        del case["findings"][0]["should_comment"]
        with self.assertRaisesRegex(BenchmarkValidationError, "should_comment"):
            validate_benchmark_case(case)
        case = benchmark_case()
        case["source"]["kind"] = "synthetic-controlled"
        with self.assertRaisesRegex(BenchmarkValidationError, "provenance"):
            validate_benchmark_case(case)

    def test_clean_case_requires_explicit_completed_audit(self):
        case = benchmark_case(findings=[])
        case["clean_review"]["completed"] = False
        with self.assertRaisesRegex(BenchmarkValidationError, "explicit completed"):
            validate_benchmark_case(case)

    def test_model_provenance_and_formal_blindness_are_enforced(self):
        case = benchmark_case()
        case["annotators"][0]["model_revision"] = None
        with self.assertRaisesRegex(BenchmarkValidationError, "model_revision"):
            validate_benchmark_case(case)
        for field in ("evoagent_prediction_visible", "peer_annotation_visible"):
            case = benchmark_case()
            case["annotators"][0]["blindness"][field] = True
            with self.assertRaisesRegex(BenchmarkValidationError, "must be blind"):
                validate_benchmark_case(case)

    def test_single_model_opinion_is_valid_record_but_not_primary_gold(self):
        case = benchmark_case()
        case["findings"][0]["reviewer_labels"] = case["findings"][0]["reviewer_labels"][:1]
        case["case_annotation_status"] = "NEEDS_MORE_CONTEXT"
        normalized = validate_benchmark_case(case)
        annotators = {item["annotator_id"]: item for item in normalized["annotators"]}
        self.assertFalse(finding_qualifies_for_primary_gold(normalized["findings"][0], annotators))
        self.assertFalse(case_qualifies_for_primary_gold(normalized))

    def test_two_independent_model_runs_agree_and_qualify(self):
        case = validate_benchmark_case(benchmark_case())
        labels = case["findings"][0]["reviewer_labels"]
        self.assertTrue(compare_annotation_labels(labels[0], labels[1])["full_agreement"])
        self.assertTrue(case_qualifies_for_primary_gold(case))

    def test_disagreement_requires_valid_independent_adjudication(self):
        case = benchmark_case()
        case["findings"][0]["reviewer_labels"][1]["severity"] = "critical"
        case["case_annotation_status"] = "NEEDS_MORE_CONTEXT"
        normalized = validate_benchmark_case(case)
        self.assertFalse(case_qualifies_for_primary_gold(normalized))
        case["case_annotation_status"] = "ADJUDICATED"
        case["annotators"].append(annotator("adjudicator", "run-c", role="ADJUDICATOR"))
        item = case["findings"][0]
        item["gold_evidence_level"] = "MODEL_ADJUDICATED"
        item["adjudication"] = {
            "adjudicator_id": "adjudicator", "adjudicated_at": "2026-09-19T01:00:00Z",
            "rationale": "Repository evidence resolves the severity.", "disagreements": ["severity"],
        }
        self.assertTrue(case_qualifies_for_primary_gold(validate_benchmark_case(case)))

    def test_objective_evidence_qualifies_without_model_voting(self):
        case = benchmark_case()
        case["annotators"] = [annotator("objective", "objective-run", "OBJECTIVE_EVIDENCE", "OBJECTIVE_EVIDENCE")]
        case["reviewer_records"] = [{
            "annotator_id": "objective", "completed_review": True,
            "duration_seconds": 0, "submitted_at": "2026-09-19T00:00:00Z",
        }]
        item = case["findings"][0]
        item["reviewer_labels"] = []
        item["gold_evidence_level"] = "OBJECTIVE"
        item["objective_evidence"] = [{
            "evidence_kind": "regression-test", "source_revision": "b" * 40,
            "evidence_refs": ["tests/test_app.py::test_danger"],
            "rationale": "The deterministic regression test isolates this finding.",
        }]
        normalized = validate_benchmark_case(case)
        self.assertTrue(case_qualifies_for_primary_gold(normalized))

    def test_positive_model_label_without_evidence_is_rejected(self):
        case = benchmark_case()
        case["findings"][0]["reviewer_labels"][0]["evidence_refs"] = []
        with self.assertRaisesRegex(BenchmarkValidationError, "positive MODEL"):
            validate_benchmark_case(case)

    def test_non_commentable_technical_issue_is_not_false_negative(self):
        case = benchmark_case(findings=[finding(should_comment=False)])
        report = score_final_benchmark([case], {})
        self.assertIsNone(report["metrics"]["recall"]["value"])
        self.assertEqual(1.0, report["metrics"]["clean_pr_accuracy"]["value"])
        self.assertEqual(0, report["case_contributions"][0]["fn"])

    def test_quarantined_case_and_protocol_dry_run_are_excluded(self):
        quarantined = benchmark_case("q", status="QUARANTINED", findings=[finding(status="QUARANTINED")])
        dry = benchmark_case("dry", protocol_dry_run=True)
        report = score_final_benchmark([quarantined, dry], {})
        self.assertEqual([], report["case_contributions"])
        self.assertEqual({"QUARANTINED", "protocol-dry-run"}, {item["reason"] for item in report["excluded"]})

    def test_quarantined_finding_never_enters_primary_metrics(self):
        case = benchmark_case(status="QUARANTINED", findings=[finding(status="QUARANTINED")])
        report = score_final_benchmark([case], {})
        self.assertEqual([], report["case_contributions"])

    def test_repository_and_related_patch_split_leakage_rejected(self):
        left = benchmark_case("a", "repo", "validation")
        right = benchmark_case("b", "repo", "final_holdout")
        with self.assertRaisesRegex(BenchmarkValidationError, "repository split leakage"):
            validate_repository_splits([left, right])
        right["repository_id"] = "repo-2"
        left["related_cases"] = [{"case_id": "b", "relationship": "backport"}]
        with self.assertRaisesRegex(BenchmarkValidationError, "related patch split violation"):
            validate_repository_splits([left, right])

    def test_manifest_detects_label_or_split_change_and_is_immutable(self):
        cases = [benchmark_case()]
        raw_hash = "c" * 64
        manifest = BenchmarkManifest.create(
            cases, raw_hash, "sampling-v1", "annotation-guide-v1",
            "deterministic-v1", "final-metrics-v2",
        )
        changed = copy.deepcopy(cases)
        changed[0]["findings"][0]["severity"] = "critical"
        for label in changed[0]["findings"][0]["reviewer_labels"]:
            label["severity"] = "critical"
        with self.assertRaisesRegex(BenchmarkValidationError, "immutable manifest"):
            manifest.verify(changed, raw_hash)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "manifest.json")
            write_manifest_immutable(path, manifest)
            write_manifest_immutable(path, manifest)
            other = BenchmarkManifest.create(
                changed, raw_hash, "sampling-v1", "annotation-guide-v1",
                "deterministic-v1", "final-metrics-v2",
            )
            self.assertNotEqual(manifest.manifest_sha256, other.manifest_sha256)
            with self.assertRaisesRegex(BenchmarkValidationError, "immutable"):
                write_manifest_immutable(path, other)


class FinalMetricTests(unittest.TestCase):
    def test_empty_denominators_are_null_not_one(self):
        empty = score_final_benchmark([], {})["metrics"]
        self.assertTrue(all(value["value"] is None for value in empty.values()))
        positive = benchmark_case()
        metrics = score_final_benchmark([positive], {})["metrics"]
        self.assertEqual(0.0, metrics["precision"]["value"])
        self.assertEqual(0.0, metrics["recall"]["value"])
        self.assertEqual(0.0, metrics["f1"]["value"])
        self.assertEqual(0, metrics["f1"]["numerator"])
        self.assertEqual(1, metrics["f1"]["denominator"])
        clean = score_final_benchmark([positive], {})["metrics"]["clean_pr_accuracy"]
        self.assertIsNone(clean["value"])

    def test_unknown_cost_remains_null_but_real_zero_remains_zero(self):
        unknown = aggregate_optional_cost([{
            "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
            "provider_cost_usd": None, "model_latency_ms": None,
            "end_to_end_latency_ms": 20,
        }])
        self.assertIsNone(unknown["provider_cost_usd"]["value"])
        known = aggregate_optional_cost([{
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "provider_cost_usd": 0.0, "model_latency_ms": 0,
            "end_to_end_latency_ms": 0,
        }])
        self.assertEqual(0.0, known["provider_cost_usd"]["value"])

    def test_cluster_bootstrap_expands_complete_repositories_and_checks_alignment(self):
        records = [
            {"case_id": "a1", "repository": "a", "tp": 1, "fp": 0, "fn": 0},
            {"case_id": "a2", "repository": "a", "tp": 0, "fp": 1, "fn": 1},
            {"case_id": "b1", "repository": "b", "tp": 1, "fp": 0, "fn": 0},
        ]
        expanded = expand_repository_sample(records, ["a", "a"])
        self.assertEqual(["a1", "a2", "a1", "a2"], [item["case_id"] for item in expanded])
        report = repository_cluster_paired_bootstrap(
            records, copy.deepcopy(records), "f1", iterations=100, seed=7,
        )
        self.assertEqual(0.0, report["paired_delta"])
        self.assertEqual(2, report["repository_count"])
        self.assertEqual(3, report["pr_count"])
        misaligned = copy.deepcopy(records)
        misaligned.reverse()
        with self.assertRaisesRegex(BenchmarkValidationError, "identical ordered"):
            repository_cluster_paired_bootstrap(records, misaligned, "f1", iterations=10)


class AttributionGoldTests(unittest.TestCase):
    @staticmethod
    def gold(status="SUPPORTED", surface="SKILL", target="security-review"):
        labels = [{
            "annotator_id": name, "observable_failure_layer": "DISCOVERY",
            "status": status, "supported_surface": surface, "target_skill": target,
            "evidence_refs": [{"artifact_id": "artifact-1"}],
            "rationale": "The frozen trace supports this route.",
            "submitted_at": "2026-09-19T00:00:00Z",
        } for name in ("annotator-a", "annotator-b")]
        return {
            "schema_version": 1, "failure_id": "failure-1", "task_id": "task-1",
            "release_id": "release-1", "review_revision": "a" * 40,
            "observable_failure_layer": "DISCOVERY", "status": status,
            "supported_surface": surface, "target_skill": target,
            "evidence_refs": [{"artifact_id": "artifact-1"}],
            "rationale": "The trace supports this route.", "annotation_status": "ADJUDICATED",
            "annotators": [annotator("annotator-a", "run-a"), annotator("annotator-b", "run-b")],
            "reviewer_labels": labels, "gold_evidence_level": "INDEPENDENT_MODEL_AGREEMENT",
            "objective_evidence": None, "adjudication": None,
        }

    def test_no_supported_evolution_and_unknown_are_scored_as_valid_gold(self):
        no_route = self.gold("INSUFFICIENT_EVIDENCE", "NO_SUPPORTED_EVOLUTION", None)
        validate_attribution_gold(no_route)
        prediction = {
            "failure-1": {
                "status": "INSUFFICIENT_EVIDENCE", "failure_layer": "DISCOVERY",
                "target_surface": "NO_SUPPORTED_EVOLUTION", "target_id": "",
                "method": "MODEL",
            }
        }
        metrics = score_attribution([no_route], prediction)
        self.assertEqual(1.0, metrics["combined_attribution_accuracy"]["value"])
        self.assertEqual(1.0, metrics["unknown_insufficient_rate"]["value"])
        self.assertIsNone(metrics["under_evolution_rate"]["value"])

    def test_attribution_denominators_separate_skill_over_and_under_routes(self):
        skill = self.gold()
        no_route = self.gold("UNKNOWN", "NO_SUPPORTED_EVOLUTION", None)
        no_route["failure_id"] = "failure-2"
        predictions = {
            "failure-1": {"status": "UNKNOWN", "failure_layer": "DISCOVERY", "target_surface": "NO_SUPPORTED_EVOLUTION", "method": "DETERMINISTIC"},
            "failure-2": {"status": "SUPPORTED", "failure_layer": "DISCOVERY", "target_surface": "GLOBAL_PROMPT", "method": "MODEL"},
        }
        metrics = score_attribution([skill, no_route], predictions)
        self.assertEqual(1.0, metrics["under_evolution_rate"]["value"])
        self.assertEqual(1.0, metrics["over_evolution_rate"]["value"])
        self.assertEqual(0.5, metrics["deterministic_attribution_coverage"]["value"])

    def test_attribution_annotator_cannot_see_production_result(self):
        gold = self.gold()
        gold["annotators"][0]["blindness"]["production_attribution_visible"] = True
        with self.assertRaisesRegex(BenchmarkValidationError, "cannot see production"):
            validate_attribution_gold(gold)

    def test_attribution_objective_layer_does_not_implicitly_prove_route(self):
        gold = self.gold()
        gold["gold_evidence_level"] = "OBJECTIVE"
        gold["reviewer_labels"] = []
        gold["annotators"] = [
            annotator("objective", "objective-run", "OBJECTIVE_EVIDENCE", "OBJECTIVE_EVIDENCE")
        ]
        gold["objective_evidence"] = [{
            "evidence_kind": "deterministic-trace", "source_revision": "a" * 40,
            "evidence_refs": [{"checkpoint": "lead-final"}],
            "rationale": "The trace establishes the observable layer.",
            "supports_fields": ["observable_failure_layer"],
        }]
        with self.assertRaisesRegex(BenchmarkValidationError, "layer and evolution route"):
            validate_attribution_gold(gold)
        gold["objective_evidence"][0]["supports_fields"] = [
            "observable_failure_layer", "status", "supported_surface", "target_skill",
        ]
        normalized = validate_attribution_gold(gold)
        self.assertTrue(attribution_qualifies_for_primary_gold(normalized))

    def test_attribution_quarantine_is_not_scored(self):
        gold = self.gold()
        gold["annotation_status"] = "QUARANTINED"
        gold["gold_evidence_level"] = "QUARANTINED"
        gold["uncertainty_reason"] = "The execution evidence is incomplete."
        metrics = score_attribution([gold], {})
        self.assertIsNone(metrics["combined_attribution_accuracy"]["value"])


class HoldoutAndAuditTests(unittest.TestCase):
    def test_normal_candidate_dataset_rejects_final_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = EvolutionEngine(TaskStore(os.path.join(directory, "db.sqlite")), seed_defaults=False)
            with self.assertRaisesRegex(ValueError, "split"):
                engine.add_evaluation_case("final", DIFF, [], split="final_holdout")

    def test_explicit_final_runner_writes_audit_before_evaluation(self):
        case = benchmark_case(split="final_holdout")
        manifest = BenchmarkManifest.create(
            [case], "d" * 64, "sampling-v1", "annotation-guide-v1",
            "deterministic-v1", "final-metrics-v2",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "access.jsonl")
            runner = FinalEvaluationRunner(path)
            result = runner.run(
                FinalRunIntent.create("evaluator", "frozen final comparison", "run-1"),
                manifest, [case], ["release-a", "release-b"],
                {"provider": "test", "model": "fixed"},
                {"version": "deterministic-v1"}, {"version": "final-metrics-v2"},
                lambda selected: len(selected), git_commit="f" * 40,
            )
            self.assertEqual(1, result)
            with open(path, encoding="utf-8") as handle:
                audit = json.load(handle)
            self.assertEqual("FINAL_HOLDOUT_ACCESS", audit["event"])
            self.assertEqual(manifest.manifest_sha256, audit["dataset_manifest_sha256"])
            self.assertEqual("run-1", audit["run_id"])

    def test_matcher_audit_reports_disagreement_without_judge(self):
        report = audit_matcher([
            {"deterministic_decision": "MATCH", "adjudicated_decision": "DIFFERENT_ISSUE"},
            {"deterministic_decision": "NO_MATCH", "adjudicated_decision": "SAME_ISSUE"},
            {"deterministic_decision": "NO_MATCH", "adjudicated_decision": "UNCERTAIN"},
        ])
        self.assertEqual(1.0, report["false_match_rate"]["value"])
        self.assertEqual(1.0, report["false_non_match_rate"]["value"])
        self.assertAlmostEqual(1 / 3, report["ambiguity_rate"]["value"], places=6)

    def test_reliability_matrix_is_generated_from_structured_test_evidence(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "evaluation_data", "reliability_matrix.json",
        )
        with open(path, encoding="utf-8") as handle:
            entries = json.load(handle)
        rendered = render_reliability_matrix(entries)
        self.assertIn("stale PR revision", rendered)
        self.assertIn("test_event_before_wait_is_reconciled", rendered)
        self.assertNotIn("99.9%", rendered)

    def test_protocol_dry_run_summary_is_never_final_claim_evidence(self):
        report = summarize_protocol_dry_run([{
            "annotation_seconds": 120, "tool_calls": 4, "input_tokens": None,
            "output_tokens": None, "technical_agreement": False,
            "should_comment_agreement": True, "quarantined": True,
            "missing_context": False,
        }])
        self.assertEqual(1.0, report["technical_disagreement_rate"])
        self.assertEqual(1.0, report["quarantine_rate"])
        self.assertIsNone(report["input_tokens"])
        self.assertFalse(report["final_claim_eligible"])

    def test_generated_user_facing_metadata_has_no_human_only_claim(self):
        root = os.path.dirname(os.path.dirname(__file__))
        paths = [
            os.path.join(root, "evaluation_data", "EVIDENCE_ADJUDICATION_GUIDE_V1.md"),
            os.path.join(root, "evaluation_data", "schemas", "evidence_adjudicated_benchmark_case_v2.schema.json"),
            os.path.join(root, "scripts", "validate_evidence_benchmark.py"),
        ]
        contents = []
        for path in paths:
            with open(path, encoding="utf-8") as handle:
                contents.append(handle.read())
        rendered = "\n".join(contents)
        self.assertNotIn("Human Benchmark", rendered)
        self.assertNotIn("Human-Labeled Benchmark", rendered)
        self.assertIn("Evidence-Adjudicated", rendered)


class MemoryCandidateStore:
    def __init__(self):
        self.candidates = {}
        self.transitions = []

    def put_evolution_candidate(self, value):
        self.candidates.setdefault(value["candidate_id"], copy.deepcopy(value))
        return copy.deepcopy(self.candidates[value["candidate_id"]])

    def get_evolution_candidate(self, candidate_id):
        value = self.candidates.get(candidate_id)
        return copy.deepcopy(value) if value else None

    def transition_evolution_candidate(self, candidate_id, status, fields):
        self.transitions.append((candidate_id, status))
        self.candidates[candidate_id].update(copy.deepcopy(fields))
        self.candidates[candidate_id]["status"] = status
        return copy.deepcopy(self.candidates[candidate_id])


class ThreeArmRunnerTests(unittest.TestCase):
    def test_naive_global_uses_candidate_lifecycle_and_manifests_are_frozen(self):
        stores = {}
        active = {}

        def factory(arm):
            store = MemoryCandidateStore()
            stores[arm] = store
            active[arm] = "release-r0"
            lifecycle = CandidateLifecycle(store)

            def builder(failure, route):
                return EvolutionCandidate.create(
                    "default", route["surface"], route["target_id"], 1,
                    "release-r0", "attribution-" + failure["failure_id"],
                    [int(failure["sequence"])], [],
                    {"prompt": "change-" + failure["failure_id"]},
                    {"method": "BENCHMARK", "model": "fixed", "config": {}},
                    "2026-09-19T00:00:00Z",
                )

            def evaluator(_candidate):
                return {"eligible": True, "decision": "ready_for_promotion"}

            def promoter(candidate_id):
                active[arm] = "release-" + candidate_id[-8:]
                return {"candidate_id": candidate_id, "release_id": active[arm]}

            return ArmRuntime(
                arm, "release-r0", "release-r0", lifecycle, builder,
                evaluator, promoter, lambda: active[arm], "operational-gate-v1",
            )

        runner = ThreeArmBenchmarkRunner(
            factory, git_commit="f" * 40, dataset_manifest_sha256="d" * 64,
            model_identity={"provider": "test", "model": "fixed", "config_hash": "m"},
            runtime_config={
                "context_policy": "c", "tool_policy": "t",
                "token_budget": 1000, "time_budget": 30,
                "operational_gate": "operational-gate-v1",
            },
            matcher_identity={"version": "deterministic-v1", "sha256": "x"},
            metric_identity={"version": "final-metrics-v2", "sha256": "y"},
            evaluation_case_order=["case-a", "case-b"],
        )
        result = runner.run([{
            "failure_id": "failure-1", "sequence": "1",
            "attribution": {
                "status": "SUPPORTED", "semantic_cause": "SKILL_GUIDANCE_GAP",
                "target_surface": "SKILL", "target_id": "security-review",
            },
        }])
        self.assertEqual("NO_CANDIDATE", result["arms"]["baseline-no-evolution"]["events"][0]["decision"])
        naive_event = result["arms"]["naive-global-evolution"]["events"][0]
        self.assertEqual("GLOBAL_PROMPT", naive_event["route"]["surface"])
        statuses = [status for _candidate, status in stores["naive-global-evolution"].transitions]
        self.assertEqual(["VALIDATING", "READY_FOR_PROMOTION"], statuses)
        targeted = result["arms"]["evidence-backed-targeted-evolution"]["events"][0]
        self.assertEqual("SKILL", targeted["route"]["surface"])
        for arm in result["arms"].values():
            manifest = arm["run_manifest"]
            self.assertEqual(["failure-1"], manifest["failure_stream_order"])
            self.assertEqual(["case-a", "case-b"], manifest["evaluation_case_order"])
            self.assertEqual("release-r0", manifest["starting_release_id"])
            self.assertIn("run_manifest_sha256", manifest)
            verify_run_manifest(manifest)
        self.assertEqual("operational-gate-v1", result["evaluation_policy_id"])

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "run.json")
            manifest = result["arms"]["baseline-no-evolution"]["run_manifest"]
            write_run_manifest_immutable(path, manifest)
            write_run_manifest_immutable(path, manifest)
            tampered = dict(manifest)
            tampered["ending_release_id"] = "different"
            with self.assertRaisesRegex(BenchmarkValidationError, "fingerprint"):
                write_run_manifest_immutable(path, tampered)

    def test_run_manifest_requires_runtime_policy_and_budget_identity(self):
        with self.assertRaisesRegex(BenchmarkValidationError, "runtime"):
            build_run_manifest(
                git_commit="f" * 40, dataset_manifest_sha256="d" * 64,
                arm="baseline", starting_release_id="r0", ending_release_id="r0",
                model_identity={}, runtime_config={}, matcher_identity={},
                metric_identity={}, failure_stream=[], evaluation_case_order=[],
                random_seed=1,
            )


if __name__ == "__main__":
    unittest.main()
