"""Build provisional gold, pairing audit, matcher audit and a non-claim dry-run report."""
import hashlib
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.benchmark_governance import BenchmarkManifest, validate_benchmark_case  # noqa: E402
from evoagent.evaluation_harness import one_to_one_match  # noqa: E402
from evoagent.models import Finding, Severity  # noqa: E402


BASE = os.path.join(ROOT, "evaluation_data", "protocol_dry_run_v1")
CAPTURE = os.path.join(BASE, "capture")
ANNOTATIONS = os.path.join(BASE, "annotations")
OUTPUT = os.path.join(BASE, "results")


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def annotation(case_id, label):
    return load(os.path.join(ANNOTATIONS, "runs", case_id, label, "annotation.json"))


def provenance(case_id, label):
    return load(os.path.join(ANNOTATIONS, "runs", case_id, label, "provenance.json"))


def refresh_inspected_files(case_id, label, value):
    trace_path = os.path.join(ANNOTATIONS, "runs", case_id, label, "trace.jsonl")
    commands = []
    with open(trace_path, encoding="utf-8") as handle:
        for raw in handle:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            command = event.get("item", {}).get("command")
            if command:
                commands.append(str(command))
    command_text = "\n".join(commands)
    checkout = os.path.join(ANNOTATIONS, "checkouts", case_id)
    if not os.path.isdir(checkout):
        return value
    import subprocess
    files = subprocess.check_output(["git", "-C", checkout, "ls-files"], text=True).splitlines()
    value["execution"]["repository_files_inspected"] = sorted(
        path for path in files if path and path in command_text
    )
    write(os.path.join(ANNOTATIONS, "runs", case_id, label, "provenance.json"), value)
    return value


def finding_pair(left, right):
    return left["path"] == right["path"] and not (
        left["end_line"] < right["start_line"] - 2
        or right["end_line"] < left["start_line"] - 2
    )


def pairing_for(case_id, left, right):
    left_findings, right_findings = left["findings"], right["findings"]
    if not left_findings and not right_findings:
        return {
            "case_id": case_id, "pairings": [], "unpaired_a": [], "unpaired_b": [],
            "agreement": {key: True for key in (
                "technical_existence", "logical_issue_identity", "taxonomy", "location",
                "severity", "should_comment", "technical_agreement", "comment_usefulness_agreement",
                "full_agreement",
            )},
        }
    used = set()
    pairs = []
    unpaired_a = []
    for item in left_findings:
        match = next((candidate for candidate in right_findings if candidate["annotation_finding_id"] not in used and finding_pair(item, candidate)), None)
        if match is None:
            unpaired_a.append(item["annotation_finding_id"])
            continue
        used.add(match["annotation_finding_id"])
        agreement = {
            "technical_existence": item["issue_exists"] == match["issue_exists"],
            "logical_issue_identity": True,
            "taxonomy": item["category"] == match["category"],
            "location": item["path"] == match["path"] and abs(item["start_line"] - match["start_line"]) <= 2,
            "severity": item["severity"] == match["severity"],
            "should_comment": item["should_comment"] == match["should_comment"],
        }
        pairs.append({
            "annotator_a_finding_id": item["annotation_finding_id"],
            "annotator_b_finding_id": match["annotation_finding_id"],
            "pairing_status": "EASY_DETERMINISTIC_PAIR",
            "agreement": agreement,
        })
    unpaired_b = [item["annotation_finding_id"] for item in right_findings if item["annotation_finding_id"] not in used]
    dimensions = ("technical_existence", "logical_issue_identity", "taxonomy", "location", "severity", "should_comment")
    dimension_values = {
        name: not unpaired_a and not unpaired_b and all(pair["agreement"][name] for pair in pairs)
        for name in dimensions
    }
    dimension_values["technical_agreement"] = all(
        dimension_values[name] for name in ("technical_existence", "logical_issue_identity", "taxonomy", "location", "severity")
    )
    dimension_values["comment_usefulness_agreement"] = dimension_values["should_comment"]
    dimension_values["full_agreement"] = dimension_values["technical_agreement"] and dimension_values["comment_usefulness_agreement"]
    return {"case_id": case_id, "pairings": pairs, "unpaired_a": unpaired_a, "unpaired_b": unpaired_b, "agreement": dimension_values}


def reviewer_label(raw, provenance_record, logical_issue_id):
    return {
        "annotator_id": provenance_record["annotator_id"], "issue_exists": raw["issue_exists"],
        "should_comment": raw["should_comment"], "logical_issue_id": logical_issue_id,
        "category": raw["category"], "path": raw["path"],
        "start_line": raw["start_line"], "end_line": raw["end_line"],
        "severity": raw["severity"], "evidence_refs": raw["evidence_refs"],
        "rationale": raw["technical_rationale"],
        "duration_seconds": provenance_record["execution"]["wall_clock_seconds"],
        "submitted_at": provenance_record["completed_at"],
    }


def main():
    capture = load(os.path.join(CAPTURE, "capture_manifest.json"))
    objective_context = [
        {
            "case_id": "flask-6096", "scope": "CHANGE_BEHAVIOR_ONLY_NOT_CLEAN_GOLD",
            "evidence_kind": "fix-commit-plus-matching-regression-tests",
            "source_revision": "7203feabf723edae0286ae5dc64fec8ac4c91735",
            "evidence_refs": ["tests/test_basic.py:1913", "tests/test_testing.py:175"],
            "rationale": "Frozen head adds IPv6-specific assertions matching the urlsplit-based host parsing change; this verifies the intended scenario but does not prove the PR is globally issue-free.",
            "runtime_executed": False,
        },
        {
            "case_id": "pytest-15044", "scope": "CHANGE_BEHAVIOR_ONLY_NOT_CLEAN_GOLD",
            "evidence_kind": "fix-commit-plus-matching-regression-test",
            "source_revision": "f36ef6fe1d735aca4c033303c067db037d4aaf22",
            "evidence_refs": ["src/_pytest/cacheprovider.py:243", "testing/test_cacheprovider.py:109-131"],
            "rationale": "Frozen head uses Path.write_text inside the OSError boundary and adds parameterized open/write/close failure assertions; this supports the specific error-handling behavior only.",
            "runtime_executed": False,
        },
    ]
    write(os.path.join(OUTPUT, "objective_evidence_context.json"), {
        "protocol_dry_run": True, "final_claim_eligible": False,
        "records": objective_context,
    })
    audits, cases, first_cost = [], [], []
    for source in capture["cases"]:
        case_id = source["case_id"]
        left, right = annotation(case_id, "A"), annotation(case_id, "B")
        left_p = refresh_inspected_files(case_id, "A", provenance(case_id, "A"))
        right_p = refresh_inspected_files(case_id, "B", provenance(case_id, "B"))
        audit = pairing_for(case_id, left, right)
        audits.append(audit)
        first_cost.extend([left_p["execution"], right_p["execution"]])
        annotators = [
            {key: value for key, value in record.items() if key != "execution"}
            for record in (left_p, right_p)
        ]
        reviewer_records = [{
            "annotator_id": record["annotator_id"], "completed_review": True,
            "duration_seconds": record["execution"]["wall_clock_seconds"],
            "submitted_at": record["completed_at"],
            "tool_calls": record["execution"]["tool_calls"],
            "input_tokens": record["execution"]["input_tokens"],
            "output_tokens": record["execution"]["output_tokens"],
            "repository_files_inspected": record["execution"]["repository_files_inspected"],
        } for record in (left_p, right_p)]
        final_findings = []
        clean_level = "INDEPENDENT_MODEL_AGREEMENT"
        clean_adjudication = None
        if case_id in {"httpx-3690", "requests-6963"}:
            adjudication = load(os.path.join(ANNOTATIONS, "adjudications", case_id, "adjudication.json"))
            adjudicator = load(os.path.join(ANNOTATIONS, "adjudications", case_id, "provenance.json"))
            annotators.append({key: value for key, value in adjudicator.items() if key != "execution"})
            clean_level = "MODEL_ADJUDICATED"
            clean_adjudication = {
                "adjudicator_id": adjudicator["annotator_id"],
                "adjudicated_at": adjudicator["completed_at"],
                "rationale": adjudication["summary"],
                "disagreements": ["technical_existence", "logical_issue_identity", "taxonomy", "location", "severity", "should_comment"],
            }
            for index, resolved in enumerate(adjudication["pairings"]):
                if resolved["resolved_finding"] is None:
                    continue
                value = resolved["resolved_finding"]
                raw_a = next(item for item in left["findings"] if item["annotation_finding_id"] == resolved["annotator_a_finding_id"])
                raw_b = next(item for item in right["findings"] if item["annotation_finding_id"] == resolved["annotator_b_finding_id"])
                logical_id = "%s-issue-%d" % (case_id, index + 1)
                final_findings.append({
                    "issue_id": logical_id, "category": value["category"], "rule_id": None,
                    "cwe": None, "path": value["path"], "start_line": value["start_line"],
                    "end_line": value["end_line"], "severity": value["severity"],
                    "issue_exists": value["issue_exists"], "should_comment": value["should_comment"],
                    "technical_evidence": value["evidence_refs"],
                    "technical_rationale": value["technical_rationale"],
                    "comment_usefulness_rationale": value["comment_usefulness_rationale"],
                    "annotation_status": "ADJUDICATED",
                    "reviewer_labels": [
                        reviewer_label(raw_a, left_p, logical_id),
                        reviewer_label(raw_b, right_p, logical_id),
                    ],
                    "gold_evidence_level": "MODEL_ADJUDICATED", "objective_evidence": None,
                    "adjudication": {
                        "adjudicator_id": adjudicator["annotator_id"],
                        "adjudicated_at": adjudicator["completed_at"],
                        "rationale": resolved["rationale"],
                        "disagreements": [name for name, agrees in audit["pairings"][index]["agreement"].items() if not agrees],
                    },
                    "uncertainty_reason": None,
                    "source_annotation_finding_ids": [resolved["annotator_a_finding_id"], resolved["annotator_b_finding_id"]],
                })
        diff = open(os.path.join(CAPTURE, "cases", case_id, "diff.patch"), encoding="utf-8").read()
        commentable = any(item["should_comment"] for item in final_findings)
        record = {
            "schema_version": 2, "dataset_id": capture["dataset_id"],
            "dataset_version": capture["dataset_version"],
            "annotation_version": "evidence-adjudication-guide-v1",
            "case_id": case_id, "repository_id": source["repository_id"],
            "source": source["source"], "base_sha": source["base_sha"], "head_sha": source["head_sha"],
            "diff_sha256": source["diff_sha256"], "captured_at": source["captured_at"],
            "split": "protocol_dry_run", "protocol_dry_run": True, "related_cases": [],
            "diff": diff, "annotators": annotators, "findings": final_findings,
            "case_annotation_status": "ADJUDICATED", "reviewer_records": reviewer_records,
            "clean_review": {
                "completed": True, "no_commentable_findings_remain": not commentable,
                "rationale": "No adjudicated comment-worthy findings remain." if not commentable else "The adjudicated case contains a comment-worthy finding.",
                "gold_evidence_level": clean_level, "objective_evidence": None,
                "adjudication": clean_adjudication,
            },
            "final_claim_eligible": False,
        }
        cases.append(validate_benchmark_case(record))
    write(os.path.join(OUTPUT, "pairing_audit.json"), {
        "protocol_dry_run": True, "final_claim_eligible": False, "cases": audits,
    })
    os.makedirs(OUTPUT, exist_ok=True)
    jsonl_path = os.path.join(OUTPUT, "benchmark_cases.jsonl")
    rendered = "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases)
    with open(jsonl_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    manifest = BenchmarkManifest.create(
        cases, hashlib.sha256(rendered.encode()).hexdigest(), "purposeful-coverage-v1",
        "evidence-adjudication-guide-v1", "deterministic-v1", "final-metrics-v2",
    )
    write(os.path.join(OUTPUT, "benchmark_manifest.json"), manifest.to_dict())
    matcher_records = []
    for case in cases:
        if case["case_id"] == "httpx-3690":
            gold = case["findings"]
            for label in ("A", "B"):
                raw = annotation(case["case_id"], label)["findings"][0]
                prediction = Finding(
                    rule_id="DRY-RUN", severity=Severity(raw["severity"]), title="dry-run",
                    explanation=raw["technical_rationale"], path=raw["path"], line=raw["start_line"],
                    evidence="", fix="", test="", confidence=1.0,
                )
                matched = bool(one_to_one_match(gold, [prediction]))
                matcher_records.append({"case_id": case["case_id"], "source": "annotator-" + label, "decision": "DEFINITE_MATCH" if matched else "DEFINITE_NO_MATCH"})
        if case["case_id"] == "requests-6963":
            raw = annotation(case["case_id"], "B")["findings"][0]
            matcher_records.append({"case_id": case["case_id"], "source": "annotator-B", "decision": "DEFINITE_NO_MATCH", "reason": "adjudicated gold contains no finding"})
    write(os.path.join(OUTPUT, "matcher_audit.json"), {
        "protocol_dry_run": True, "final_claim_eligible": False,
        "records": matcher_records, "ambiguous_count": 0,
        "ambiguity_rate": 0.0, "note": "Structural audit using frozen annotator outputs after gold freeze; not an EvoAgent performance measurement.",
    })
    adjudication_cost = [
        load(os.path.join(ANNOTATIONS, "adjudications", case_id, "provenance.json"))["execution"]
        for case_id in ("httpx-3690", "requests-6963")
    ]
    def total(records, key):
        values = [item.get(key) for item in records]
        return None if any(value is None for value in values) else sum(values)
    technical_existence = sum(item["agreement"]["technical_existence"] for item in audits)
    logical_identity = sum(item["agreement"]["logical_issue_identity"] for item in audits)
    taxonomy = sum(item["agreement"]["taxonomy"] for item in audits)
    location = sum(item["agreement"]["location"] for item in audits)
    severity = sum(item["agreement"]["severity"] for item in audits)
    technical = sum(item["agreement"]["technical_agreement"] for item in audits)
    comments = sum(item["agreement"]["comment_usefulness_agreement"] for item in audits)
    full = sum(item["agreement"]["full_agreement"] for item in audits)
    issue_units = sum(len(item["pairings"]) + len(item["unpaired_a"]) + len(item["unpaired_b"]) for item in audits)
    unpaired = sum(len(item["unpaired_a"]) + len(item["unpaired_b"]) for item in audits)
    report = {
        "report_type": "EVIDENCE_ADJUDICATED_PROTOCOL_DRY_RUN",
        "protocol_dry_run": True, "final_claim_eligible": False,
        "case_count": len(cases), "repository_count": len({case["repository_id"] for case in cases}),
        "first_pass_cost": {
            "execution_count": len(first_cost), "wall_clock_seconds_sum": total(first_cost, "wall_clock_seconds"),
            "tool_calls": total(first_cost, "tool_calls"), "input_tokens": total(first_cost, "input_tokens"),
            "output_tokens": total(first_cost, "output_tokens"),
        },
        "adjudication_cost": {
            "execution_count": len(adjudication_cost), "wall_clock_seconds_sum": total(adjudication_cost, "wall_clock_seconds"),
            "tool_calls": total(adjudication_cost, "tool_calls"), "input_tokens": total(adjudication_cost, "input_tokens"),
            "output_tokens": total(adjudication_cost, "output_tokens"),
        },
        "agreement": {
            "technical_existence": {"numerator": technical_existence, "denominator": len(audits), "value": technical_existence / len(audits)},
            "logical_issue_identity": {"numerator": logical_identity, "denominator": len(audits), "value": logical_identity / len(audits)},
            "taxonomy": {"numerator": taxonomy, "denominator": len(audits), "value": taxonomy / len(audits)},
            "location": {"numerator": location, "denominator": len(audits), "value": location / len(audits)},
            "severity": {"numerator": severity, "denominator": len(audits), "value": severity / len(audits)},
            "technical_agreement": {"numerator": technical, "denominator": len(audits), "value": technical / len(audits)},
            "should_comment_agreement": {"numerator": comments, "denominator": len(audits), "value": comments / len(audits)},
            "full_agreement": {"numerator": full, "denominator": len(audits), "value": full / len(audits)},
            "unpaired_issue_rate": {"numerator": unpaired, "denominator": issue_units, "value": unpaired / issue_units if issue_units else None},
        },
        "adjudication_rate": {"numerator": 2, "denominator": 8, "value": 0.25},
        "quarantine_rate": {"numerator": 0, "denominator": 8, "value": 0.0},
        "missing_context_rate": {"numerator": 0, "denominator": 16, "value": 0.0},
        "objective_evidence_context_case_count": 2,
        "objective_gold_case_count": 0,
        "model_agreement_case_count": full, "model_adjudicated_count": 2,
        "schema_problems_discovered": [
            "Schema v2 had no protocol_dry_run split and could only misclassify dry-run cases as formal splits; fixed with flag/split equivalence validation.",
            "Independent annotations need local finding IDs plus a post-run pairing artifact; pre-coordinated logical_issue_id is not acceptable.",
            "Strict model-output schemas required explicit type/items declarations for const and array fields.",
        ],
        "annotation_guide_ambiguities": [
            "The guide needed an explicit instruction not to report the bug intentionally fixed by the PR as a newly introduced issue.",
            "Taxonomy synonyms such as error-handling vs error-handling-regression need normalization or documented adjudication semantics.",
            "Objective regression evidence supports a specific change behavior but must not be treated as proof that a case is globally clean.",
        ],
        "repository_context_gaps": [
            "No annotator requested additional repository context; full frozen Git trees were sufficient for these cases.",
            "Read-only sandboxing prevented temporary-file/heredoc experiments in some runs, but source inspection remained possible.",
            "Requests PR 6963 title/body describes a CVE fix while the frozen base already contains hostname parsing; sampling categories must be verified against the actual base/head diff, not PR metadata alone.",
        ],
        "issue_pairing_problems": [
            "Only two issue units exercised pairing: one path/range deterministic pair and one one-sided unpaired claim; evidence is too sparse to validate harder multi-finding pairing.",
        ],
        "decision_gates": {
            "annotation_protocol": "NEEDS REVISION",
            "repository_capture_package": "SUFFICIENT",
            "issue_pairing": "CURRENT STRUCTURE SUFFICIENT",
            "evidence_adjudication": "PRACTICAL",
            "hybrid_matcher": "NOT JUSTIFIED",
        },
        "claim_policy": "Protocol-methodology evidence only. No EvoAgent performance, resume, README, or final benchmark claim is permitted.",
    }
    write(os.path.join(OUTPUT, "protocol_report.json"), report)
    markdown = """# Evidence-Adjudicated Historical PR Protocol Dry Run\n\n**protocol_dry_run = true**  \n**final_claim_eligible = false**\n\nThis report evaluates annotation methodology only. It contains no EvoAgent performance claim.\n\n## Counts\n\n- Cases: {case_count}\n- Repositories: {repository_count}\n- First-pass executions: {first_count}\n- Adjudications: {adj_count}\n\n## Agreement\n\n- Technical existence: {technical_existence:.1%}\n- Full technical dimensions: {technical:.1%}\n- `should_comment`: {comment:.1%}\n- Full structured agreement: {full:.1%}\n- Unpaired issue units: {unpaired:.1%}\n- Adjudication rate: 25.0%\n- Quarantine rate: 0.0%\n- Missing-context rate: 0.0%\n\n## Decision gates\n\n- Annotation protocol: **NEEDS REVISION**\n- Repository capture package: **SUFFICIENT**\n- Issue pairing: **CURRENT STRUCTURE SUFFICIENT**\n- Evidence-adjudication: **PRACTICAL**\n- Hybrid matcher: **NOT JUSTIFIED**\n\nSee `protocol_report.json` for denominators, costs, discovered problems, and limitations.\n""".format(case_count=len(cases), repository_count=report["repository_count"], first_count=len(first_cost), adj_count=len(adjudication_cost), technical_existence=technical_existence / len(audits), technical=technical / len(audits), comment=comments / len(audits), full=full / len(audits), unpaired=unpaired / issue_units if issue_units else 0)
    with open(os.path.join(OUTPUT, "PROTOCOL_REPORT.md"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(markdown)


if __name__ == "__main__":
    main()
