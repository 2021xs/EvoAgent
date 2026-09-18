import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "run_bounded_evidence_pilot.py")
CASES = os.path.join(
    ROOT, "experiments", "bounded-evidence-pilot", "cases.jsonl",
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location("bounded_evidence_pilot", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_pilot(output_directory):
    completed = subprocess.run(
        [sys.executable, SCRIPT, "--output-dir", output_directory],
        cwd=ROOT, check=False, capture_output=True, text=True, timeout=45,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr or completed.stdout)
    with open(os.path.join(output_directory, "summary.json"), encoding="utf-8") as handle:
        summary = json.load(handle)
    with open(
        os.path.join(output_directory, "case_results.jsonl"), encoding="utf-8"
    ) as handle:
        results = [json.loads(line) for line in handle if line.strip()]
    return summary, results


class BoundedEvidencePilotTests(unittest.TestCase):
    def test_cases_have_frozen_distribution_and_hidden_repository_evidence(self):
        module = _load_script_module()
        cases = module.load_cases(CASES)

        self.assertEqual(10, len(cases))
        self.assertEqual({
            "missing_support": 4, "counter_evidence": 3, "sufficient": 3,
        }, {
            category: sum(item["category"] == category for item in cases)
            for category in module.CATEGORIES
        })
        for case in cases:
            marker = case["evidence_marker"]
            self.assertNotIn(marker, case["diff"])
            self.assertNotIn(marker, json.dumps(case["finding"], sort_keys=True))
            self.assertIn(marker, "\n".join(case["repository_files"].values()))
            if case["category"] == "sufficient":
                self.assertEqual(
                    case["pass1_verdict"], case["gold_final_verdict"],
                )
                self.assertFalse(case["gold_challenge_useful"])
            else:
                self.assertNotEqual(
                    case["pass1_verdict"], case["gold_final_verdict"],
                )
                self.assertTrue(case["gold_challenge_useful"])

    def test_both_arms_metrics_isolation_and_outputs_are_reproducible(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            summary, results = run_pilot(first)
            second_summary, second_results = run_pilot(second)

            self.assertEqual(summary, second_summary)
            self.assertEqual(results, second_results)
            for name in ("summary.json", "summary.md", "case_results.jsonl"):
                with open(os.path.join(first, name), "rb") as left:
                    with open(os.path.join(second, name), "rb") as right:
                        self.assertEqual(left.read(), right.read())

        self.assertEqual(
            "PROTOCOL_EXPERIMENT_PLUMBING_VALIDATION", summary["interpretation"],
        )
        self.assertEqual("UNKNOWN / NOT RUN", summary["real_model_results"]["status"])
        self.assertEqual([], summary["errors"])
        self.assertTrue(summary["comparison"]["downstream_equivalent"])
        self.assertEqual([], summary["comparison"]["downstream_mismatches"])
        self.assertEqual(21, summary["comparison"]["extra_llm_calls"])

        one = summary["arms"]["one_shot"]
        bounded = summary["arms"]["bounded"]
        self.assertEqual(0.3, one["final_critic_verdict_accuracy"])
        self.assertEqual(1.0, bounded["final_critic_verdict_accuracy"])
        self.assertEqual(0.0, one["challenge_trigger_rate"])
        self.assertEqual(0.7, bounded["challenge_trigger_rate"])
        self.assertEqual(1.0, bounded["useful_challenge_rate"])
        self.assertEqual(1.0, bounded["verdict_correction_rate"])
        self.assertEqual(0.0, bounded["unnecessary_challenge_rate"])

        by_key = {(item["case_id"], item["arm"]): item for item in results}
        self.assertEqual(20, len(by_key))
        case_ids = {item["case_id"] for item in results}
        for case_id in case_ids:
            one_case = by_key[(case_id, "one_shot")]
            bounded_case = by_key[(case_id, "bounded")]
            self.assertFalse(one_case["challenge_triggered"])
            self.assertEqual(one_case["published_rule_ids"], bounded_case[
                "published_rule_ids"
            ])
            self.assertEqual(one_case["downstream"], bounded_case["downstream"])
            if bounded_case["gold_challenge_useful"]:
                self.assertTrue(bounded_case["challenge_triggered"])
                self.assertTrue(bounded_case["useful_challenge"])
                self.assertTrue(bounded_case["verdict_corrected"])
                self.assertEqual(1, bounded_case["new_evidence_refs"])
            else:
                self.assertFalse(bounded_case["challenge_triggered"])
                self.assertFalse(bounded_case["useful_challenge"])

    def test_metric_denominators_are_challenge_scoped(self):
        module = _load_script_module()
        results = [
            {
                "challenge_triggered": True, "gold_challenge_useful": True,
                "final_verdict_correct": True, "useful_challenge": True,
                "verdict_corrected": True,
                "usage": {key: 1 for key in (
                    "llm_calls", "input_tokens", "output_tokens", "total_tokens",
                    "latency_ms",
                )},
            },
            {
                "challenge_triggered": True, "gold_challenge_useful": False,
                "final_verdict_correct": False, "useful_challenge": False,
                "verdict_corrected": False,
                "usage": {key: 1 for key in (
                    "llm_calls", "input_tokens", "output_tokens", "total_tokens",
                    "latency_ms",
                )},
            },
            {
                "challenge_triggered": False, "gold_challenge_useful": False,
                "final_verdict_correct": True, "useful_challenge": False,
                "verdict_corrected": False,
                "usage": {key: 1 for key in (
                    "llm_calls", "input_tokens", "output_tokens", "total_tokens",
                    "latency_ms",
                )},
            },
        ]

        metrics = module.compute_arm_metrics(results)

        self.assertEqual(0.6667, metrics["final_critic_verdict_accuracy"])
        self.assertEqual(0.6667, metrics["challenge_trigger_rate"])
        self.assertEqual(0.5, metrics["useful_challenge_rate"])
        self.assertEqual(0.5, metrics["verdict_correction_rate"])
        self.assertEqual(0.5, metrics["unnecessary_challenge_rate"])
        self.assertEqual(3, metrics["llm"]["llm_calls"])


if __name__ == "__main__":
    unittest.main()
