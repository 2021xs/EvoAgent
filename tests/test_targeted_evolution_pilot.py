import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "run_targeted_evolution_pilot.py")
CASES = os.path.join(
    ROOT, "experiments", "targeted-evolution-pilot", "cases.jsonl"
)


def run_pilot(output_directory):
    completed = subprocess.run(
        [sys.executable, SCRIPT, "--output-dir", output_directory],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
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


class TargetedEvolutionPilotTests(unittest.TestCase):
    def test_case_distribution_and_gold_controls_are_frozen(self):
        with open(CASES, encoding="utf-8") as handle:
            cases = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(12, len(cases))
        distribution = {}
        for case in cases:
            distribution[case["gold_root_cause"]] = (
                distribution.get(case["gold_root_cause"], 0) + 1
            )
            self.assertEqual("DISCOVERY", case["gold_first_divergence"])
        self.assertEqual({
            "SKILL_GUIDANCE_GAP": 4,
            "CONTEXT_EVIDENCE_MISSING": 3,
            "MODEL_REASONING_FAILURE": 3,
            "INSUFFICIENT_EVIDENCE": 2,
        }, distribution)
        targets = [
            case.get("gold_target_skill") for case in cases
            if case["gold_should_evolve"]
        ]
        self.assertEqual(2, targets.count("security-review"))
        self.assertEqual(2, targets.count("reliability-review"))

    def test_both_arms_are_isolated_equivalent_downstream_and_reproducible(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            summary, results = run_pilot(first)
            second_summary, second_results = run_pilot(second)

            self.assertEqual(summary, second_summary)
            self.assertEqual(results, second_results)
            for name in ("summary.json", "summary.md", "case_results.jsonl"):
                with open(os.path.join(first, name), "rb") as left:
                    with open(os.path.join(second, name), "rb") as right:
                        self.assertEqual(left.read(), right.read())

            self.assertEqual("PIPELINE_VALIDATION_NOT_MODEL_INTELLIGENCE", summary["interpretation"])
            self.assertEqual("UNKNOWN / NOT RUN", summary["real_model_results"]["status"])
            self.assertTrue(summary["development_gates_passed"])
            naive = summary["arms"]["naive"]
            targeted = summary["arms"]["targeted"]
            self.assertEqual(12, naive["evolution_attempts"])
            self.assertEqual(4, targeted["evolution_attempts"])
            self.assertEqual(1.0, naive["wrong_surface_evolution_rate"])
            self.assertEqual(0.0, targeted["wrong_surface_evolution_rate"])
            self.assertEqual(4, naive["source_failures_resolved"])
            self.assertEqual(4, targeted["source_failures_resolved"])
            self.assertEqual(1, naive["regression_rejections"])
            self.assertEqual(1, targeted["regression_rejections"])
            self.assertEqual(3, naive["validated_evolutions"])
            self.assertEqual(3, targeted["validated_evolutions"])

            by_key = {(item["case_id"], item["arm"]): item for item in results}
            self.assertEqual(24, len(by_key))
            for case_id in {item["case_id"] for item in results}:
                naive_case = by_key[(case_id, "naive")]
                targeted_case = by_key[(case_id, "targeted")]
                self.assertTrue(naive_case["evolution_attempted"])
                if naive_case["gold_should_evolve"]:
                    self.assertTrue(targeted_case["evolution_attempted"])
                    self.assertEqual(
                        naive_case["evolution"], targeted_case["evolution"]
                    )
                else:
                    self.assertFalse(targeted_case["evolution_attempted"])
                for item in (naive_case, targeted_case):
                    self.assertEqual("disk", item["baseline_skill_source"])
                    self.assertFalse(item["active_override_after_case"])
                    self.assertTrue(item["attribution_correct"])
                    if item["evolution_attempted"]:
                        self.assertEqual(1, item["evolution"]["version"])
                        self.assertFalse(item["evolution"]["version_active"])
                        self.assertEqual(["SKILL.md"], item["evolution"]["changed_files"])
            contracts = {item["downstream_contract_sha256"] for item in results}
            self.assertEqual({summary["downstream_contract_sha256"]}, contracts)


if __name__ == "__main__":
    unittest.main()
