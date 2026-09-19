import hashlib
import json
import os
import subprocess
import unittest

from evoagent.benchmark_governance import (
    BenchmarkManifest,
    case_qualifies_for_primary_gold,
    validate_benchmark_case,
)
from scripts.analyze_protocol_dry_run_v2 import pairings


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRY_RUN = os.path.join(ROOT, "evaluation_data", "protocol_dry_run_v1")
DRY_RUN_V2 = os.path.join(ROOT, "evaluation_data", "protocol_dry_run_v2")


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class ProtocolDryRunAssetTests(unittest.TestCase):
    def test_capture_is_frozen_at_exact_revisions_and_diff_hashes(self):
        capture = load(os.path.join(DRY_RUN, "capture", "capture_manifest.json"))
        self.assertTrue(capture["protocol_dry_run"])
        self.assertFalse(capture["final_claim_eligible"])
        self.assertEqual(8, capture["case_count"])
        self.assertEqual(5, capture["repository_count"])
        for case in capture["cases"]:
            case_root = os.path.join(DRY_RUN, "capture", "cases", case["case_id"])
            with open(os.path.join(case_root, "diff.patch"), "rb") as handle:
                self.assertEqual(case["diff_sha256"], hashlib.sha256(handle.read()).hexdigest())
            bundle = os.path.join(
                DRY_RUN, "capture", "sources",
                case["repository_id"].replace("/", "__") + ".bundle",
            )
            for side in ("base", "head"):
                actual = subprocess.check_output([
                    "git", "bundle", "list-heads", bundle,
                    case[side + "_snapshot_ref"],
                ], text=True).split()[0]
                self.assertEqual(case[side + "_snapshot_commit"], actual)

    def test_first_pass_runs_are_independent_and_blind(self):
        capture = load(os.path.join(DRY_RUN, "capture", "capture_manifest.json"))
        for case in capture["cases"]:
            records = []
            for label in ("A", "B"):
                base = os.path.join(DRY_RUN, "annotations", "runs", case["case_id"], label)
                annotation = load(os.path.join(base, "annotation.json"))
                provenance = load(os.path.join(base, "provenance.json"))
                self.assertEqual("COMPLETE", annotation["annotation_status"])
                self.assertEqual("FIRST_PASS", provenance["role"])
                self.assertEqual("FROZEN_REPOSITORY_READ_ONLY", provenance["repository_access_mode"])
                self.assertFalse(any(provenance["blindness"].values()))
                records.append(provenance)
            self.assertNotEqual(records[0]["run_id"], records[1]["run_id"])
            self.assertNotEqual(records[0]["annotator_id"], records[1]["annotator_id"])

    def test_results_are_non_claim_assets_with_a_valid_manifest(self):
        jsonl = os.path.join(DRY_RUN, "results", "benchmark_cases.jsonl")
        with open(jsonl, encoding="utf-8") as handle:
            cases = [validate_benchmark_case(json.loads(line)) for line in handle if line.strip()]
        self.assertEqual(8, len(cases))
        self.assertTrue(all(case["protocol_dry_run"] for case in cases))
        self.assertTrue(all(case["final_claim_eligible"] is False for case in cases))
        self.assertTrue(all(not case_qualifies_for_primary_gold(case) for case in cases))
        with open(jsonl, "rb") as handle:
            raw_hash = hashlib.sha256(handle.read()).hexdigest()
        manifest = BenchmarkManifest.from_dict(
            load(os.path.join(DRY_RUN, "results", "benchmark_manifest.json"))
        )
        manifest.verify(cases, raw_hash)
        report = load(os.path.join(DRY_RUN, "results", "protocol_report.json"))
        self.assertTrue(report["protocol_dry_run"])
        self.assertFalse(report["final_claim_eligible"])
        self.assertEqual("NEEDS REVISION", report["decision_gates"]["annotation_protocol"])


class ProtocolDryRunV2AssetTests(unittest.TestCase):
    def test_pairing_audit_preserves_taxonomy_and_reason_codes(self):
        a = {"annotation_finding_id": "a", "path": "src/a.py", "start_line": 9,
             "end_line": 11, "category": "error-handling-regression", "cwe": None,
             "issue_exists": True, "should_comment": True, "severity": "low"}
        b = dict(a, annotation_finding_id="b", category="error handling")
        paired = pairings({"findings": [a]}, {"findings": [b]})
        self.assertEqual("DETERMINISTICALLY_PAIRED", paired["pairs"][0]["status"])
        self.assertEqual("error-handling-regression", paired["pairs"][0]["taxonomy_a"]["raw_category"])
        self.assertTrue(paired["pairs"][0]["agreement"]["taxonomy"])
        b["category"] = "authorization-bypass"
        self.assertFalse(pairings({"findings": [a]}, {"findings": [b]})["pairs"][0]["agreement"]["taxonomy"])
        b["path"] = "src/b.py"
        unpaired = pairings({"findings": [a]}, {"findings": [b]})
        self.assertEqual(2, len(unpaired["unpaired"]))
        self.assertEqual("DIFFERENT_LOCATION_OR_ABSTRACTION", unpaired["unpaired"][0]["reason"])

    def test_capture_has_verifiable_source_and_non_claim_flags(self):
        capture = load(os.path.join(DRY_RUN_V2, "capture/capture_manifest.json"))
        self.assertEqual(6, capture["case_count"])
        self.assertEqual(4, len({case["repository_id"] for case in capture["cases"]}))
        for case in capture["cases"]:
            self.assertEqual(2, case["dry_run_iteration"])
            self.assertTrue(case["protocol_dry_run"])
            self.assertFalse(case["final_claim_eligible"])
            with open(os.path.join(DRY_RUN_V2, "capture/cases", case["case_id"], "diff.patch"), "rb") as handle:
                self.assertEqual(case["diff_sha256"], hashlib.sha256(handle.read()).hexdigest())
            bundle = os.path.join(DRY_RUN_V2, "capture/sources", case["repository_id"].replace("/", "__") + ".bundle")
            for side in ("base", "head"):
                actual = subprocess.check_output([
                    "git", "bundle", "list-heads", bundle, case[side + "_snapshot_ref"],
                ], text=True).split()[0]
                self.assertEqual(case[side + "_snapshot_commit"], actual)

    def test_sampling_admission_is_frozen_and_corrects_unproven_multi_finding(self):
        admission = load(os.path.join(DRY_RUN_V2, "admission_manifest.json"))
        self.assertEqual(6, len(admission["admitted_case_ids"]))
        self.assertEqual(2, admission["sampling_category_corrections"])
        for case in admission["cases"]:
            self.assertFalse(case["evoagent_prediction_visible"])
            self.assertEqual("ADMIT", case["decision"])
            if case["requested_sampling_category"] == "multi-finding":
                self.assertEqual("multi-concern-review-stress", case["verified_sampling_category"])

    def test_independent_blind_runs_and_disagreement_only_adjudication(self):
        capture = load(os.path.join(DRY_RUN_V2, "capture/capture_manifest.json"))
        for case in capture["cases"]:
            base = os.path.join(DRY_RUN_V2, "annotations/runs", case["case_id"])
            runs = []
            for side in ("A", "B"):
                annotation = load(os.path.join(base, side, "annotation.json"))
                provenance = load(os.path.join(base, side, "provenance.json"))
                self.assertEqual("COMPLETE", annotation["annotation_status"])
                self.assertEqual("FIRST_PASS", provenance["role"])
                self.assertEqual("FROZEN_REPOSITORY_READ_ONLY", provenance["repository_access_mode"])
                self.assertFalse(any(provenance["blindness"].values()))
                self.assertIsNone(provenance["execution"]["cost_instrumentation"]["input_token_breakdown"]["repository_tool_observations"])
                runs.append(provenance)
            self.assertNotEqual(runs[0]["run_id"], runs[1]["run_id"])
            self.assertNotEqual(runs[0]["annotator_id"], runs[1]["annotator_id"])
            adjudication = os.path.join(DRY_RUN_V2, "annotations/adjudications", case["case_id"], "adjudication.json")
            self.assertEqual(case["case_id"] == "django-21754", os.path.exists(adjudication))

    def test_derived_report_preserves_non_claim_boundary_and_pairing_audit(self):
        report = load(os.path.join(DRY_RUN_V2, "results/protocol_report.json"))
        pairing = load(os.path.join(DRY_RUN_V2, "results/issue_pairing_audit.json"))
        objective = load(os.path.join(DRY_RUN_V2, "results/objective_evidence_scope.json"))
        self.assertTrue(report["protocol_dry_run"])
        self.assertFalse(report["final_claim_eligible"])
        self.assertEqual(6, report["case_count"])
        self.assertEqual(12, report["first_pass_count"])
        self.assertEqual(5, report["agreement"]["technical_agreement"]["numerator"])
        self.assertEqual(1, report["issue_pairing_counts"]["unpaired"])
        self.assertEqual(0, report["issue_pairing_counts"]["paired_during_adjudication"])
        self.assertEqual("NEEDS ANOTHER REVISION", report["decision_gates"]["protocol"])
        self.assertEqual(6, len(pairing))
        self.assertEqual(["FINDING_BEHAVIOR"], objective["records"][0]["scope"])
        self.assertFalse(objective["records"][0]["runtime_executed"])


if __name__ == "__main__":
    unittest.main()
