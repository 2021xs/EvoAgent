"""Validate and freeze an EvoAgent evidence-adjudicated benchmark v2 JSONL manifest."""
import argparse
import hashlib
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.benchmark_governance import (  # noqa: E402
    BenchmarkManifest,
    render_reliability_matrix,
    validate_benchmark_case,
    write_manifest_immutable,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", nargs="?", help="Evidence-adjudicated benchmark v2 JSONL")
    parser.add_argument("--manifest", default="")
    parser.add_argument("--sampling-policy-version", default="sampling-v1")
    parser.add_argument("--annotation-guide-version", default="evidence-adjudication-guide-v1")
    parser.add_argument("--matcher-version", default="deterministic-v1")
    parser.add_argument("--metric-contract-version", default="final-metrics-v2")
    parser.add_argument("--render-reliability", default="")
    args = parser.parse_args()
    if args.render_reliability:
        with open(args.render_reliability, encoding="utf-8") as handle:
            print(render_reliability_matrix(json.load(handle)), end="")
        return
    if not args.dataset or not args.manifest:
        parser.error("dataset and --manifest are required")
    with open(args.dataset, "rb") as handle:
        raw = handle.read()
    cases = [
        validate_benchmark_case(json.loads(line))
        for line in raw.decode("utf-8").splitlines() if line.strip()
    ]
    manifest = BenchmarkManifest.create(
        cases, hashlib.sha256(raw).hexdigest(), args.sampling_policy_version,
        args.annotation_guide_version, args.matcher_version,
        args.metric_contract_version,
    )
    write_manifest_immutable(args.manifest, manifest)
    print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
