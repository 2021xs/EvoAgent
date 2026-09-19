"""Derived v2 protocol analysis; never modifies frozen captures or annotator records."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evoagent.benchmark_governance import normalize_taxonomy


ROOT = Path(__file__).resolve().parents[1] / "evaluation_data"
V1 = ROOT / "protocol_dry_run_v1"
V2 = ROOT / "protocol_dry_run_v2"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def pairings(left, right, adjudication=None):
    """Only exact path/near-line pairing; no semantic or model pairing guesses."""
    used = set()
    pairs = []
    unpaired = []
    for a in left["findings"]:
        b = next((f for f in right["findings"] if f["annotation_finding_id"] not in used
                  and f["path"] == a["path"]
                  and f["start_line"] <= a["end_line"] + 2
                  and a["start_line"] <= f["end_line"] + 2), None)
        if b is None:
            unpaired.append({"side": "A", "finding_id": a["annotation_finding_id"],
                             "status": "UNPAIRED", "reason": "ONE_SIDED_ISSUE" if not right["findings"] else "DIFFERENT_LOCATION_OR_ABSTRACTION"})
            continue
        used.add(b["annotation_finding_id"])
        taxonomy_a = normalize_taxonomy(a.get("cwe"), a.get("category"))
        taxonomy_b = normalize_taxonomy(b.get("cwe"), b.get("category"))
        pairs.append({"a": a["annotation_finding_id"], "b": b["annotation_finding_id"],
                      "status": "DETERMINISTICALLY_PAIRED",
                      "reason": "SAME_PATH_OVERLAPPING_OR_NEAR_RANGE",
                      "taxonomy_a": taxonomy_a, "taxonomy_b": taxonomy_b,
                      "agreement": {
                          "technical_existence": a["issue_exists"] == b["issue_exists"],
                          "logical_issue_identity": True,
                          "taxonomy": taxonomy_a["canonical_identity"] == taxonomy_b["canonical_identity"],
                          "location": a["path"] == b["path"] and abs(a["start_line"] - b["start_line"]) <= 2,
                          "severity": a["severity"] == b["severity"],
                          "should_comment": a["should_comment"] == b["should_comment"],
                      }})
    for b in right["findings"]:
        if b["annotation_finding_id"] not in used:
            unpaired.append({"side": "B", "finding_id": b["annotation_finding_id"],
                             "status": "UNPAIRED", "reason": "ONE_SIDED_ISSUE" if not left["findings"] else "DIFFERENT_LOCATION_OR_ABSTRACTION"})
    if adjudication:
        for resolution in adjudication["pairings"]:
            a_id, b_id = resolution.get("annotator_a_finding_id"), resolution.get("annotator_b_finding_id")
            if a_id and b_id and not any(p["a"] == a_id and p["b"] == b_id for p in pairs):
                pairs.append({"a": a_id, "b": b_id, "status": "PAIRED_DURING_ADJUDICATION",
                              "reason": resolution.get("pairing_reason"), "agreement": resolution.get("agreement")})
                unpaired = [p for p in unpaired if p["finding_id"] not in (a_id, b_id)]
            for entry in unpaired:
                if entry["finding_id"] in (a_id, b_id):
                    entry["adjudication_outcome"] = adjudication["adjudication_status"]
                    entry["adjudication_reason"] = resolution.get("pairing_reason")
    return {"pairs": pairs, "unpaired": unpaired}


def summarize():
    capture = read(V2 / "capture/capture_manifest.json")
    admission = read(V2 / "admission_manifest.json")
    audits, executions = [], []
    disagreements = missing = quarantine = model_agreement = adjudicated = 0
    for case in capture["cases"]:
        case_id = case["case_id"]
        base = V2 / "annotations/runs" / case_id
        a, b = [read(base / side / "annotation.json") for side in ("A", "B")]
        pa, pb = [read(base / side / "provenance.json") for side in ("A", "B")]
        executions.extend([pa["execution"], pb["execution"]])
        missing += sum(bool(x["missing_context_requests"]) for x in (a, b))
        adjudication_file = V2 / "annotations/adjudications" / case_id / "adjudication.json"
        adjudication = read(adjudication_file) if adjudication_file.exists() else None
        pairing = pairings(a, b, adjudication)
        technical = not pairing["unpaired"] and all(
            all(pair["agreement"][key] for key in
                ("technical_existence", "logical_issue_identity", "taxonomy", "location", "severity"))
            for pair in pairing["pairs"]
        )
        usefulness = not pairing["unpaired"] and all(
            pair["agreement"]["should_comment"] for pair in pairing["pairs"]
        )
        agreed = technical and usefulness
        if adjudication:
            disagreements += 1
            adjudicated += adjudication["adjudication_status"] == "MODEL_ADJUDICATED"
            quarantine += adjudication["adjudication_status"] in {"QUARANTINED", "NEEDS_MORE_CONTEXT"}
        else:
            model_agreement += agreed
        audits.append({"case_id": case_id, "pairing": pairing, "technical_agreement": technical,
                       "should_comment_agreement": usefulness, "full_structured_agreement": agreed,
                       "adjudication_status": adjudication["adjudication_status"] if adjudication else None})
    n = len(audits)
    cost = [execution["cost_instrumentation"] for execution in executions]
    token_total = sum(execution["input_tokens"] for execution in executions)
    tool_total = sum(execution["tool_calls"] for execution in executions)
    unique_total = sum(item["unique_files_inspected"] for item in cost)
    repeat_total = sum(item["repeat_file_reads"] for item in cost)
    control = V2 / "control_legacy_case"
    old = [read(V1 / "annotations/runs/flask-6096" / side / "provenance.json")["execution"] for side in ("A", "B")]
    new = [read(control / "runs/flask-6096" / side / "provenance.json")["execution"] for side in ("A", "B")]
    old_tokens, new_tokens = sum(x["input_tokens"] for x in old), sum(x["input_tokens"] for x in new)
    old_labels = [read(V1 / "annotations/runs/flask-6096" / side / "annotation.json") for side in ("A", "B")]
    new_labels = [read(control / "runs/flask-6096" / side / "annotation.json") for side in ("A", "B")]
    v1_report = read(V1 / "results/protocol_report.json")
    report = {
        "report_type": "EVIDENCE_ADJUDICATED_PROTOCOL_DRY_RUN_V2", "protocol_dry_run": True,
        "final_claim_eligible": False, "dry_run_iteration": 2,
        "case_count": n, "repository_count": len({c["repository_id"] for c in capture["cases"]}),
        "first_pass_count": len(executions),
        "agreement": {key: ratio(sum(bool(a[key]) for a in audits), n) for key in
                      ("technical_agreement", "should_comment_agreement", "full_structured_agreement")},
        "adjudication_rate": ratio(disagreements, n), "quarantine_rate": ratio(quarantine, n),
        "missing_context_rate": ratio(missing, len(executions)),
        "issue_pairing_counts": {"deterministically_paired": sum(p["status"] == "DETERMINISTICALLY_PAIRED" for a in audits for p in a["pairing"]["pairs"]),
                                 "paired_during_adjudication": sum(p["status"] == "PAIRED_DURING_ADJUDICATION" for a in audits for p in a["pairing"]["pairs"]),
                                 "unpaired": sum(len(a["pairing"]["unpaired"]) for a in audits)},
        "first_pass_cost": {"input_tokens_total": token_total, "input_tokens_mean": token_total / len(executions),
                            "tool_calls_total": tool_total, "tool_calls_mean": tool_total / len(executions),
                            "unique_files_inspected_total": unique_total, "unique_files_inspected_mean": unique_total / len(executions),
                            "repeat_file_reads_total": repeat_total, "repeat_file_reads_mean": repeat_total / len(executions),
                            "wall_clock_seconds_total": sum(x["wall_clock_seconds"] for x in executions),
                            "tool_result_chars_total": sum(x["tool_result_chars"] for x in cost),
                            "source_token_breakdown": None},
        "adjudication_cost": {"executions": adjudicated, "input_tokens": sum(read(V2 / "annotations/adjudications" / a["case_id"] / "provenance.json")["execution"]["input_tokens"] for a in audits if a["adjudication_status"]),
                              "wall_clock_seconds": sum(read(V2 / "annotations/adjudications" / a["case_id"] / "provenance.json")["execution"]["wall_clock_seconds"] for a in audits if a["adjudication_status"])},
        "objective_evidence": {"case_count": 1, "objective_gold_case_count": 0,
                               "model_agreement_case_count": model_agreement, "model_adjudicated_case_count": adjudicated},
        "sampling_category_corrections": admission["sampling_category_corrections"],
        "controlled_same_case_flask_6096": {
            "old_input_tokens": old_tokens, "new_input_tokens": new_tokens,
            "input_token_delta": new_tokens - old_tokens,
            "input_token_delta_fraction": (new_tokens - old_tokens) / old_tokens,
            "old_tool_calls": sum(x["tool_calls"] for x in old), "new_tool_calls": sum(x["tool_calls"] for x in new),
            "old_first_pass_finding_counts": [len(x["findings"]) for x in old_labels],
            "new_first_pass_finding_counts": [len(x["findings"]) for x in new_labels],
            "old_missing_context_count": sum(bool(x["missing_context_requests"]) for x in old_labels),
            "new_missing_context_count": sum(bool(x["missing_context_requests"]) for x in new_labels),
            "limitation": "Two stochastic runs per protocol on one frozen PR; the new one-sided issue changes agreement and cannot establish non-inferiority."},
        "v1_comparison": {"case_count": v1_report["case_count"], "first_pass_mean_input_tokens": v1_report["first_pass_cost"]["input_tokens"] / 16,
                          "first_pass_mean_tool_calls": v1_report["first_pass_cost"]["tool_calls"] / 16,
                          "technical_agreement": v1_report["agreement"]["technical_agreement"],
                          "should_comment_agreement": v1_report["agreement"]["should_comment_agreement"],
                          "full_agreement": v1_report["agreement"]["full_agreement"]},
        "limitations": ["Six v2 cases are different from v1; only the one-case control is paired.",
                        "No paired two-sided positive findings in v2: alias-heavy and multi-concern cases did not empirically exercise pairing or taxonomy normalization.",
                        "Frozen urllib3 test cannot execute in this checkout: the source archive lacks generated urllib3._version.",
                        "The Django adjudication did not independently inspect base-side objects; preserve this qualification caveat.",
                        "Provider-level input token source breakdown is unavailable; repeat-read and tool-output volume are trace-derived proxies."],
        "decision_gates": {"protocol": "NEEDS ANOTHER REVISION", "annotation_cost": "TOO EXPENSIVE",
                           "taxonomy": "NORMALIZATION SUFFICIENT (DETERMINISTIC CONTRACT ONLY; EMPIRICAL STRESS UNTESTED)",
                           "issue_pairing": "CURRENT APPROACH SUFFICIENT (NO REDESIGN EVIDENCE; MULTI-ISSUE UNTESTED)",
                           "objective_evidence": "SCOPE CONTRACT SUFFICIENT (RUNTIME OBJECTIVE PROOF NOT YET AVAILABLE)",
                           "hybrid_matcher": "NOT JUSTIFIED"},
    }
    output = V2 / "results"
    output.mkdir(exist_ok=True)
    for name, data in (("issue_pairing_audit.json", audits), ("protocol_report.json", report)):
        (output / name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(summarize(), indent=2))
