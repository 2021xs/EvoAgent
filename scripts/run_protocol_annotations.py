"""Run isolated A/B Codex annotations against frozen protocol-dry-run repositories."""
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


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root")
    parser.add_argument("output_root")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--case", default="")
    parser.add_argument("--annotator", choices=("A", "B"), default="")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--protocol-root", default="evaluation_data/protocol_dry_run_v1")
    parser.add_argument("--guide-version", default="evidence-adjudication-guide-v1")
    parser.add_argument("--admission-manifest", default="")
    args = parser.parse_args()
    args.capture_root = os.path.abspath(args.capture_root)
    args.output_root = os.path.abspath(args.output_root)
    root = ROOT
    protocol_root = os.path.abspath(os.path.join(root, args.protocol_root))
    guide_filename = args.guide_version.replace("evidence-adjudication-guide-", "EVIDENCE_ADJUDICATION_GUIDE_").upper() + ".md"
    guide_path = os.path.join(root, "evaluation_data", guide_filename)
    prompt_path = os.path.join(protocol_root, "ANNOTATOR_PROMPT.md")
    schema_path = os.path.join(protocol_root, "annotation_run_output.schema.json")
    with open(guide_path, encoding="utf-8") as handle:
        guide = handle.read()
    with open(prompt_path, encoding="utf-8") as handle:
        prompt_contract = handle.read()
    manifest = load_json(os.path.join(args.capture_root, "capture_manifest.json"))
    admitted = None
    if args.admission_manifest:
        admission = load_json(args.admission_manifest)
        admitted = set(admission["admitted_case_ids"])
        if admission.get("dry_run_iteration") != manifest.get("dry_run_iteration"):
            raise ValueError("sampling admission does not match capture iteration")
    for case in manifest["cases"]:
        if args.case and case["case_id"] != args.case:
            continue
        if admitted is not None and case["case_id"] not in admitted:
            continue
        repository_source = os.path.join(
            args.capture_root, "sources", case["repository_id"].replace("/", "__") + ".bundle",
        )
        checkout = os.path.join(args.output_root, "checkouts", case["case_id"])
        if not os.path.exists(checkout):
            os.makedirs(checkout, exist_ok=True)
            subprocess.check_call(["git", "-C", checkout, "init", "--quiet"])
            subprocess.check_call([
                "git", "-C", checkout, "fetch", "--quiet", repository_source,
                case["base_snapshot_ref"] + ":refs/evoagent/base",
                case["head_snapshot_ref"] + ":refs/evoagent/head",
            ])
            subprocess.check_call(["git", "-C", checkout, "checkout", "--quiet", "--detach", "refs/evoagent/head"])
        if subprocess.check_output(
            ["git", "-C", checkout, "rev-parse", "refs/evoagent/base^{tree}"], text=True,
        ).strip() != case["base_tree_sha"] or subprocess.check_output(
            ["git", "-C", checkout, "rev-parse", "refs/evoagent/head^{tree}"], text=True,
        ).strip() != case["head_tree_sha"]:
            raise RuntimeError("checkout tree does not match frozen capture for " + case["case_id"])
        if args.prepare_only:
            continue
        for label in ("A", "B"):
            if args.annotator and label != args.annotator:
                continue
            destination = os.path.join(args.output_root, "runs", case["case_id"], label)
            if os.path.exists(os.path.join(destination, "annotation.json")):
                continue
            os.makedirs(destination, exist_ok=True)
            run_id = str(uuid.uuid4())
            annotator_id = "%s-%s" % (case["case_id"], label.lower())
            prompt = "\n\n".join([
                prompt_contract, guide,
                "Case identity:\ncase_id=%s\nannotator_id=%s\nbase_sha=%s\nhead_sha=%s\n"
                "Local frozen refs: refs/evoagent/base and refs/evoagent/head. These are synthetic root commits "
                "whose trees exactly match the original revisions; cite the original SHA values in output evidence." % (
                    case["case_id"], annotator_id, case["base_sha"], case["head_sha"],
                ),
                "Return only the schema-conforming annotation. Set case_id and annotator_id exactly as supplied.",
            ])
            output_path = os.path.join(destination, "annotation.json")
            trace_path = os.path.join(destination, "trace.jsonl")
            started_at = utc_now()
            started = time.monotonic()
            command = [
                "codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--enable", "skip_host_skill_discovery",
                "--skip-git-repo-check", "-m", args.model, "-s", "read-only",
                "-C", checkout, "--output-schema", schema_path,
                "--output-last-message", output_path, "--json", prompt,
            ]
            with open(trace_path, "w", encoding="utf-8", newline="\n") as trace:
                completed = subprocess.run(command, stdout=trace, stderr=subprocess.PIPE, text=True)
            completed_at = utc_now()
            trace_events = []
            with open(trace_path, encoding="utf-8") as trace:
                for raw in trace:
                    try:
                        trace_events.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
            usage = next((event.get("usage") for event in reversed(trace_events) if event.get("usage")), {})
            command_text = "\n".join(
                str(event.get("item", {}).get("command", ""))
                for event in trace_events if event.get("type") == "item.completed"
            )
            repository_files = subprocess.check_output(
                ["git", "-C", checkout, "ls-files"], text=True,
            ).splitlines()
            inspected_files = sorted(path for path in repository_files if path and path in command_text)
            cost = instrument_annotation_trace(trace_events, repository_files)
            provenance = {
                "annotator_id": annotator_id, "annotator_kind": "MODEL", "role": "FIRST_PASS",
                "provider": "openai", "model": args.model,
                "model_revision": "unreported-by-codex-cli", "prompt_version": args.guide_version,
                "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                "context_policy_id": "frozen-repository-read-only-" + ("v2" if manifest.get("dry_run_iteration") == 2 else "v1"),
                "context_policy_hash": hashlib.sha256((case["base_sha"] + case["head_sha"]).encode()).hexdigest(),
                "tool_policy_id": "codex-bounded-read-only-v2" if manifest.get("dry_run_iteration") == 2 else "codex-read-only-local-repository-v1",
                "tool_policy_hash": hashlib.sha256(("codex-bounded-read-only-v2" if manifest.get("dry_run_iteration") == 2 else "codex-read-only-local-repository-v1").encode()).hexdigest(),
                "repository_access_mode": "FROZEN_REPOSITORY_READ_ONLY", "run_id": run_id,
                "started_at": started_at, "completed_at": completed_at,
                "blindness": {"evoagent_prediction_visible": False, "experiment_arm_visible": False, "peer_annotation_visible": False, "production_attribution_visible": False},
                "execution": {
                    "exit_code": completed.returncode, "wall_clock_seconds": round(time.monotonic() - started, 3),
                    "tool_calls": sum(event.get("type") == "item.completed" and event.get("item", {}).get("type") in {"command_execution", "mcp_tool_call", "web_search"} for event in trace_events),
                    "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                    "repository_files_inspected": inspected_files, "cost_instrumentation": cost,
                    "stderr": completed.stderr[-2000:],
                },
            }
            write_json(os.path.join(destination, "provenance.json"), provenance)
            if completed.returncode:
                raise RuntimeError("annotation failed for %s %s" % (case["case_id"], label))


if __name__ == "__main__":
    main()
