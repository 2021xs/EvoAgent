import copy
import unittest

from evoagent.benchmark_governance import BenchmarkValidationError
from evoagent.final_benchmark_methodology import (
    FINAL_METHODOLOGY_VERSION,
    build_arm_blind_prediction_pool,
    create_final_holdout_freeze_manifest,
    enforce_frozen_recall_denominator,
    match_seeded_findings,
    repository_cluster_open_world_bootstrap,
    score_open_world_arm,
    score_evolution_outcomes,
    validate_three_arm_fairness_contract,
    validate_final_report_schema,
    validate_model_visible_evaluation_context,
    validate_prediction_adjudication,
    validate_qualified_clean_case,
    validate_seeded_finding,
    validate_supplementary_review_findings,
    verify_final_holdout_freeze_manifest,
)


def seed(seed_id="seed-1", case_id="case-1", evidence_type="REGRESSION_TEST", status="QUALIFIED"):
    proposition = "The new call executes attacker-controlled input."
    return {
        "schema_version": 3, "methodology_version": FINAL_METHODOLOGY_VERSION,
        "seed_id": seed_id, "case_id": case_id, "repository_id": "repo-1",
        "base_sha": "a" * 40, "head_sha": "b" * 40,
        "issue_identity": {"category": "injection", "rule_id": "SEC-EVAL", "cwe": "CWE-95"},
        "location": {"path": "app.py", "start_line": 1, "end_line": 1, "supportable": True},
        "severity": "high", "should_comment": True, "technical_proposition": proposition,
        "evidence": [{
            "evidence_type": evidence_type, "evidence_refs": ["tests/test_app.py::test_injection"],
            "source_revision": "c" * 40, "benchmark_construction_only": True,
            "supported_proposition": proposition,
        }],
        "qualification_status": status,
        "verification_provenance": {
            "verifier_id": "verifier-1", "verifier_kind": "OBJECTIVE_EVIDENCE",
            "run_id": "verify-1", "outcome": status,
            "evoagent_prediction_visible": False, "experiment_arm_visible": False,
            "repository_access_mode": "FROZEN_REPOSITORY_READ_ONLY",
            "rationale": "The regression test isolates the bounded proposition.",
        },
        "rationale": "The objective failure is independently reproducible.",
    }


def prediction(prediction_id, arm, case_id="case-1", line=1, category="injection"):
    return {
        "prediction_id": prediction_id, "case_id": case_id, "repository_id": "repo-1",
        "arm": arm, "release_id": "secret-release-" + arm,
        "finding": {
            "category": category, "rule_id": "SEC-EVAL", "cwe": "CWE-95",
            "path": "app.py", "start_line": line, "end_line": line,
            "severity": "high", "title": "Unsafe evaluation",
        },
    }


def adjudication(pool_id, issue_exists=True, should_comment=True, status="ADJUDICATED"):
    unresolved = status != "ADJUDICATED"
    return {
        "schema_version": 1, "methodology_version": FINAL_METHODOLOGY_VERSION,
        "pool_id": pool_id, "case_id": "case-1", "repository_id": "repo-1",
        "status": status, "issue_exists": None if unresolved else issue_exists,
        "should_comment": None if unresolved else should_comment,
        "severity": None if unresolved else "high",
        "location_supported": None if unresolved else True,
        "technical_evidence": [] if unresolved or not issue_exists else ["app.py:1"],
        "technical_rationale": "" if unresolved else "Repository evidence supports the decision.",
        "comment_usefulness_rationale": "" if unresolved else "The developer can act on it.",
        "provenance": {
            "adjudicator_id": "judge-1", "run_id": "judge-run-1",
            "repository_access_mode": "FROZEN_REPOSITORY_READ_ONLY",
            "experiment_arm_visible": False, "release_identity_visible": False,
            "peer_arm_emissions_visible": False, "desired_metric_direction_visible": False,
        },
    }


def clean_case():
    reviews = [{
        "run_id": run_id, "completed_review": True,
        "experiment_arm_visible": False, "evoagent_prediction_visible": False,
    } for run_id in ("review-a", "review-b")]
    return {
        "case_id": "case-clean", "repository_id": "repo-1",
        "qualification_status": "QUALIFIED_CLEAN", "independent_reviews": reviews,
        "unresolved_disagreement": False, "known_historical_seed_count": 0,
        "later_contradictory_evidence": False,
        "completed_review_rationale": "Both reviewers completed repository-wide review.",
    }


class SeedAndLeakageTests(unittest.TestCase):
    def test_seeded_finding_without_strong_evidence_is_rejected(self):
        weak = seed(evidence_type="PR_TITLE")
        with self.assertRaisesRegex(BenchmarkValidationError, "admissible strong evidence"):
            validate_seeded_finding(weak)

    def test_future_evidence_never_exposed_to_evoagent_input(self):
        context = {
            "case_id": "case-1", "repository_id": "repo-1",
            "base_sha": "a" * 40, "head_sha": "b" * 40,
            "frozen_repository": {"snapshot_id": "snapshot-1", "access_mode": "FROZEN_REPOSITORY_READ_ONLY"},
            "runtime_context": {"diff": "+danger(value)"},
        }
        validate_model_visible_evaluation_context(context)
        context["runtime_context"]["future_fix_commit"] = "c" * 40
        with self.assertRaisesRegex(BenchmarkValidationError, "construction evidence"):
            validate_model_visible_evaluation_context(context)

    def test_post_freeze_issue_cannot_change_recall_denominator(self):
        original = [seed()]
        freeze = create_final_holdout_freeze_manifest(
            cases=[{"case_id": "case-1"}], seeds=original, repository_split={"repo-1": "final_holdout"},
            prediction_adjudication_protocol={"version": 1}, matcher={"version": 1},
            metrics={"version": 3}, three_arm_configuration={"version": 1},
            failure_stream=[{"failure_id": "f1"}], run_manifest_template={"version": 1},
        )
        enforce_frozen_recall_denominator(freeze, original)
        with self.assertRaisesRegex(BenchmarkValidationError, "frozen Recall denominator"):
            enforce_frozen_recall_denominator(freeze, original + [seed("seed-2")])
        components = {
            "cases": [{"case_id": "case-1"}], "seeds": original,
            "repository_split": {"repo-1": "final_holdout"},
            "prediction_adjudication_protocol": {"version": 1},
            "matcher": {"version": 1}, "metrics": {"version": 3},
            "three_arm_configuration": {"version": 1},
            "failure_stream": [{"failure_id": "f1"}],
            "run_manifest_template": {"version": 1},
        }
        verify_final_holdout_freeze_manifest(freeze, **components)
        components["matcher"] = {"version": 2}
        with self.assertRaisesRegex(BenchmarkValidationError, "invalidate run"):
            verify_final_holdout_freeze_manifest(freeze, **components)


class PredictionPoolAndMetricTests(unittest.TestCase):
    def setUp(self):
        self.predictions = [
            prediction("p-a", "baseline-no-evolution"),
            prediction("p-b", "naive-global-evolution"),
            prediction("p-c", "evidence-backed-targeted-evolution", line=4),
        ]
        self.pool = build_arm_blind_prediction_pool(self.predictions, random_seed=17)

    def test_same_logical_prediction_shares_adjudication_and_arm_is_hidden(self):
        self.assertEqual(self.pool["prediction_to_pool"]["p-a"], self.pool["prediction_to_pool"]["p-b"])
        shared = self.pool["prediction_to_pool"]["p-a"]
        self.assertFalse(any("arm" in item or "release" in item for item in self.pool["adjudicator_view"]))
        self.assertEqual(1, sum(entry["pool_id"] == shared for entry in self.pool["entries"]))

    def test_unmatched_valid_prediction_is_not_automatically_fp(self):
        target_pool_id = self.pool["prediction_to_pool"]["p-c"]
        report = score_open_world_arm(
            arm="evidence-backed-targeted-evolution", cases=[{"case_id": "case-1", "repository_id": "repo-1"}],
            seeds=[seed()], predictions=self.predictions, pool=self.pool,
            adjudications=[adjudication(target_pool_id, True, True)],
        )
        self.assertEqual(1.0, report["metrics"]["adjudicated_precision"]["value"])
        self.assertEqual(0.0, report["metrics"]["false_positives_per_pr"]["value"])

    def test_unmatched_invalid_prediction_becomes_fp_after_adjudication(self):
        target_pool_id = self.pool["prediction_to_pool"]["p-c"]
        report = score_open_world_arm(
            arm="evidence-backed-targeted-evolution", cases=[{"case_id": "case-1", "repository_id": "repo-1"}],
            seeds=[seed()], predictions=self.predictions, pool=self.pool,
            adjudications=[adjudication(target_pool_id, False, False)],
        )
        self.assertEqual(0.0, report["metrics"]["adjudicated_precision"]["value"])
        self.assertEqual(1.0, report["metrics"]["false_positives_per_pr"]["value"])

    def test_qualified_seed_enters_seeded_recall_denominator(self):
        shared = self.pool["prediction_to_pool"]["p-a"]
        matches = match_seeded_findings([seed()], self.pool["entries"])
        report = score_open_world_arm(
            arm="baseline-no-evolution", cases=[{"case_id": "case-1", "repository_id": "repo-1"}],
            seeds=[seed()], predictions=self.predictions, pool=self.pool,
            adjudications=[adjudication(shared)], seed_matches=matches,
        )
        recall = report["metrics"]["seeded_issue_recall"]
        self.assertEqual(1, recall["denominator"])
        self.assertEqual(1.0, recall["value"])

    def test_quarantine_is_excluded_under_prediction_policy(self):
        target_pool_id = self.pool["prediction_to_pool"]["p-c"]
        validate_prediction_adjudication(adjudication(target_pool_id, status="QUARANTINED"))
        report = score_open_world_arm(
            arm="evidence-backed-targeted-evolution", cases=[{"case_id": "case-1", "repository_id": "repo-1"}],
            seeds=[seed()], predictions=self.predictions, pool=self.pool,
            adjudications=[adjudication(target_pool_id, status="QUARANTINED")],
        )
        self.assertIsNone(report["metrics"]["adjudicated_precision"]["value"])
        self.assertEqual(1, report["counts"]["quarantined_prediction_count"])


class CleanReportAndSupplementaryTests(unittest.TestCase):
    def test_absence_of_seeds_does_not_imply_clean(self):
        pool = build_arm_blind_prediction_pool([], random_seed=1)
        report = score_open_world_arm(
            arm="baseline-no-evolution", cases=[{"case_id": "case-1", "repository_id": "repo-1"}],
            seeds=[], predictions=[], pool=pool, adjudications=[],
        )
        self.assertIsNone(report["metrics"]["qualified_clean_pr_accuracy"]["value"])

    def test_clean_subset_requires_explicit_qualification(self):
        valid = clean_case()
        validate_qualified_clean_case(valid)
        invalid = copy.deepcopy(valid)
        invalid["independent_reviews"] = invalid["independent_reviews"][:1]
        with self.assertRaisesRegex(BenchmarkValidationError, "independent blind A/B"):
            validate_qualified_clean_case(invalid)

    def test_supplementary_findings_do_not_change_seeded_denominator(self):
        result = validate_supplementary_review_findings([{
            "supplementary_finding_id": "supp-1", "case_id": "case-1",
            "repository_id": "repo-1", "source_review_run_id": "review-a",
            "repository_grounded": True, "evoagent_prediction_visible": False,
            "experiment_arm_visible": False,
        }], ["seed-1"])
        self.assertFalse(result["recall_denominator_changed"])

    def test_final_report_requires_seeded_recall_and_rejects_old_recall(self):
        metrics = {name: {} for name in (
            "seeded_issue_recall", "high_risk_seeded_issue_recall",
            "adjudicated_precision", "false_positives_per_pr",
            "actionable_findings_per_pr", "wrong_surface_evolution_rate",
            "regression_rate", "evolution_success_rate", "total_tokens",
            "llm_calls", "latency",
        )}
        report = {
            "methodology_version": FINAL_METHODOLOGY_VERSION, "primary_metrics": metrics,
            "claim": "Detection used evidence-seeded issues and blind repository-grounded adjudication.",
            "annotator_disclosure": "Model-based annotators are explicitly disclosed.",
            "statistical_protocol": {
                "method": "repository-cluster-paired-bootstrap", "iterations": 10000,
                "seed": 7, "confidence_interval": "95-percentile",
            },
        }
        validate_final_report_schema(report)
        report["primary_metrics"]["recall"] = {}
        with self.assertRaisesRegex(BenchmarkValidationError, "old exhaustive Recall terminology"):
            validate_final_report_schema(report)


class StatisticalProtocolTests(unittest.TestCase):
    def test_cluster_bootstrap_handles_multiple_seeds_and_unequal_predictions(self):
        left = [
            {"repository": "a", "case_id": "a1", "seeded_issue_recall_numerator": 1, "seeded_issue_recall_denominator": 2, "adjudicated_precision_numerator": 1, "adjudicated_precision_denominator": 1},
            {"repository": "a", "case_id": "a2", "seeded_issue_recall_numerator": 2, "seeded_issue_recall_denominator": 3, "adjudicated_precision_numerator": 1, "adjudicated_precision_denominator": 3},
            {"repository": "b", "case_id": "b1", "seeded_issue_recall_numerator": 1, "seeded_issue_recall_denominator": 1, "adjudicated_precision_numerator": 0, "adjudicated_precision_denominator": 0},
        ]
        right = copy.deepcopy(left)
        right[0]["adjudicated_precision_denominator"] = 4
        report = repository_cluster_open_world_bootstrap(left, right, "seeded_issue_recall", iterations=100, seed=5)
        self.assertEqual(6, report["seeded_finding_count"])
        self.assertEqual({"left": 4, "right": 7}, report["prediction_count"])
        self.assertEqual(2, report["repository_count"])

    def test_controlled_failure_stream_is_identical_across_arms(self):
        common = {
            "git_commit": "a" * 40, "dataset_manifest_sha256": "b" * 64,
            "starting_release_id": "release-r0", "ending_release_id": "release-r0",
            "model_identity": {"provider": "p", "model": "m", "model_revision": "r", "config_hash": "h"},
            "runtime_config": {"all": "frozen"}, "matcher_identity": {"version": "1"},
            "metric_identity": {"version": "3"}, "failure_stream_identity": "stream-hash",
            "failure_stream_order": ["f1", "f2"], "evaluation_case_order": ["c1"],
            "evaluation_case_order_sha256": "case-hash", "random_seed": 7,
            "experiment_mode": "CONTROLLED_SHARED_FAILURE_STREAM",
        }
        manifests = {arm: dict(common, arm=arm) for arm in (
            "baseline-no-evolution", "naive-global-evolution",
            "evidence-backed-targeted-evolution",
        )}
        result = validate_three_arm_fairness_contract(manifests, {
            "baseline-no-evolution": "store-a", "naive-global-evolution": "store-b",
            "evidence-backed-targeted-evolution": "store-c",
        })
        self.assertTrue(result["comparable"])
        manifests["naive-global-evolution"]["failure_stream_order"] = ["f2", "f1"]
        with self.assertRaisesRegex(BenchmarkValidationError, "failure_stream_order"):
            validate_three_arm_fairness_contract(manifests, {
                "baseline-no-evolution": "store-a", "naive-global-evolution": "store-b",
                "evidence-backed-targeted-evolution": "store-c",
            })

    def test_evolution_metrics_use_adjudicated_opportunities(self):
        metrics = score_evolution_outcomes([
            {"adjudicated_opportunity": True, "routing_correct": False, "candidate_generated": True, "source_failure_improved": False, "protected_regression": True, "promoted": False, "duplicate_candidate": False},
            {"adjudicated_opportunity": True, "routing_correct": True, "candidate_generated": True, "source_failure_improved": True, "protected_regression": False, "promoted": True, "duplicate_candidate": True},
            {"adjudicated_opportunity": False, "routing_correct": False, "candidate_generated": True, "source_failure_improved": True, "protected_regression": False},
        ])
        self.assertEqual(0.5, metrics["wrong_surface_evolution_rate"]["value"])
        self.assertEqual(0.5, metrics["evolution_success_rate"]["value"])
        self.assertEqual(0.5, metrics["regression_rate"]["value"])


if __name__ == "__main__":
    unittest.main()
