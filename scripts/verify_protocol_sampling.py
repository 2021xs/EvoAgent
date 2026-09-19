"""Validate post-capture sampling classifications without consulting EvoAgent output."""
import argparse
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.benchmark_governance import validate_sampling_admission  # noqa: E402


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_manifest")
    parser.add_argument("verification")
    parser.add_argument("output")
    args = parser.parse_args()
    capture = load(args.capture_manifest)
    proposed = load(args.verification)
    captured = {case["case_id"]: case for case in capture["cases"]}
    if set(captured) != {item["case_id"] for item in proposed["cases"]}:
        raise ValueError("sampling verification must cover every captured case exactly once")
    cases = [validate_sampling_admission(item, captured[item["case_id"]]) for item in proposed["cases"]]
    result = {
        "protocol_dry_run": True, "final_claim_eligible": False,
        "dry_run_iteration": capture["dry_run_iteration"],
        "capture_dataset_id": capture["dataset_id"],
        "cases": cases,
        "admitted_case_ids": sorted(item["case_id"] for item in cases if item["decision"] == "ADMIT"),
        "rejected_case_ids": sorted(item["case_id"] for item in cases if item["decision"] == "REJECT"),
        "sampling_category_corrections": sum(
            item["requested_sampling_category"] != item["verified_sampling_category"] for item in cases
        ),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    main()
