"""Run a blind independent adjudication for one A/B disagreement case."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.benchmark_governance import instrument_annotation_trace  # noqa: E402


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root")
    parser.add_argument("annotations_root")
    parser.add_argument("case_id")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--protocol-root", default="evaluation_data/protocol_dry_run_v1")
    parser.add_argument("--guide-version", default="evidence-adjudication-guide-v1")
    parser.add_argument("--adjudication-dir", default="adjudications")
    args = parser.parse_args()
    root = ROOT
    protocol_root = os.path.abspath(os.path.join(root, args.protocol_root))
    capture_root, annotations_root = os.path.abspath(args.capture_root), os.path.abspath(args.annotations_root)
    case = next(item for item in load(os.path.join(capture_root, "capture_manifest.json"))["cases"] if item["case_id"] == args.case_id)
    left = load(os.path.join(annotations_root, "runs", args.case_id, "A", "annotation.json"))
    right = load(os.path.join(annotations_root, "runs", args.case_id, "B", "annotation.json"))
    guide_filename = args.guide_version.replace("evidence-adjudication-guide-", "EVIDENCE_ADJUDICATION_GUIDE_").upper() + ".md"
    with open(os.path.join(root, "evaluation_data", guide_filename), encoding="utf-8") as handle:
        guide = handle.read()
    prompt = """You are the independent adjudicator for an evidence-adjudicated protocol dry run.
Use only the frozen repository in the current directory plus the two first-pass records below.
You may inspect their claims, evidence, and rationale. You must not seek or inspect EvoAgent output,
experiment arms, or a production AttributionResult. Do not use network access or edit files.
Resolve only what repository evidence supports. Return QUARANTINED or NEEDS_MORE_CONTEXT instead of
forcing an answer. For each proposed pair, report all agreement dimensions separately. A finding
that is rejected as technically nonexistent should have resolved_finding=null.

CASE: %s
BASE: %s
HEAD: %s
The local refs/evoagent/base and refs/evoagent/head are synthetic commits with exact original
base/head file trees. Use those refs for base-to-head inspection; original GitHub commit objects
may not be present in the portable snapshot. Cite the original SHA values in evidence.

ANNOTATOR A:\n%s

ANNOTATOR B:\n%s

GOVERNANCE GUIDE:\n%s
""" % (args.case_id, case["base_sha"], case["head_sha"], json.dumps(left), json.dumps(right), guide)
    destination = os.path.join(annotations_root, args.adjudication_dir, args.case_id)
    os.makedirs(destination, exist_ok=True)
    output = os.path.join(destination, "adjudication.json")
    if os.path.exists(output):
        return
    trace = os.path.join(destination, "trace.jsonl")
    schema = os.path.join(protocol_root, "adjudication_output.schema.json")
    checkout = os.path.join(annotations_root, "checkouts", args.case_id)
    started_at, started = now(), time.monotonic()
    command = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--enable", "skip_host_skill_discovery", "--skip-git-repo-check", "-m", args.model, "-s", "read-only", "-C", checkout, "--output-schema", schema, "--output-last-message", output, "--json", prompt]
    with open(trace, "w", encoding="utf-8", newline="\n") as handle:
        result = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, text=True)
    events = []
    with open(trace, encoding="utf-8") as handle:
        for raw in handle:
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    usage = next((event.get("usage") for event in reversed(events) if event.get("usage")), {})
    repository_files = subprocess.check_output(["git", "-C", checkout, "ls-files"], text=True).splitlines()
    cost = instrument_annotation_trace(events, repository_files)
    provenance = {
        "annotator_id": args.case_id + "-adjudicator", "annotator_kind": "MODEL", "role": "ADJUDICATOR",
        "provider": "openai", "model": args.model, "model_revision": "unreported-by-codex-cli",
        "prompt_version": args.guide_version, "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
        "context_policy_id": "frozen-repository-plus-a-b-evidence-v1", "context_policy_hash": hashlib.sha256((case["base_sha"] + case["head_sha"]).encode()).hexdigest(),
        "tool_policy_id": "codex-read-only-local-repository-v1", "tool_policy_hash": hashlib.sha256(b"codex-read-only-local-repository-v1").hexdigest(),
        "repository_access_mode": "FROZEN_REPOSITORY_READ_ONLY", "run_id": str(uuid.uuid4()),
        "started_at": started_at, "completed_at": now(),
        "blindness": {"evoagent_prediction_visible": False, "experiment_arm_visible": False, "peer_annotation_visible": True, "production_attribution_visible": False},
        "execution": {"exit_code": result.returncode, "wall_clock_seconds": round(time.monotonic() - started, 3), "tool_calls": sum(event.get("type") == "item.completed" and event.get("item", {}).get("type") == "command_execution" for event in events), "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"), "cost_instrumentation": cost, "stderr": result.stderr[-2000:]},
    }
    with open(os.path.join(destination, "provenance.json"), "w", encoding="utf-8") as handle:
        json.dump(provenance, handle, ensure_ascii=False, indent=2, sort_keys=True); handle.write("\n")
    if result.returncode:
        raise RuntimeError("adjudication failed")


if __name__ == "__main__":
    main()
