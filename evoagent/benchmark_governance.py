"""Evidence-adjudicated benchmark contracts and final-holdout governance.

This module is deliberately separate from the development-regression harness.
It never relaxes provenance, annotation or final-holdout requirements for the
production evolution path.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import random
import re
import subprocess
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .evaluation_harness import one_to_one_match
from .models import Finding, Severity


BENCHMARK_SCHEMA_VERSION = 2
BENCHMARK_SPLITS = {
    "evolution_feedback", "validation", "operational_gating", "final_holdout",
}
BENCHMARK_PROVENANCE = {"public-github-pr", "private-historical-pr"}
ANNOTATION_FINAL_STATUSES = {"ADJUDICATED", "QUARANTINED", "NEEDS_MORE_CONTEXT"}
FINDING_ANNOTATION_STATUSES = ANNOTATION_FINAL_STATUSES
RELATION_TYPES = {"backport", "cherry-pick", "revert", "forked-duplicate", "patch-series"}
ATTRIBUTION_STATUSES = {"SUPPORTED", "INSUFFICIENT_EVIDENCE", "UNKNOWN"}
ATTRIBUTION_SURFACES = {"GLOBAL_PROMPT", "SKILL", "NO_SUPPORTED_EVOLUTION"}
SEVERITIES = {"low", "medium", "high", "critical"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
ANNOTATOR_KINDS = {"MODEL", "HUMAN", "OBJECTIVE_EVIDENCE"}
ANNOTATOR_ROLES = {"FIRST_PASS", "ADJUDICATOR", "OBJECTIVE_EVIDENCE"}
REPOSITORY_ACCESS_MODES = {
    "FROZEN_DIFF_ONLY", "FROZEN_REPOSITORY_READ_ONLY", "OBJECTIVE_RUNTIME_EVIDENCE",
}
GOLD_EVIDENCE_LEVELS = {
    "OBJECTIVE", "INDEPENDENT_MODEL_AGREEMENT", "MODEL_ADJUDICATED",
    "HUMAN_ADJUDICATED", "QUARANTINED",
}
AGREEMENT_DIMENSIONS = {
    "technical_existence": "issue_exists",
    "logical_issue_identity": "logical_issue_id",
    "taxonomy": "category",
    "location": ("path", "start_line", "end_line"),
    "severity": "severity",
    "should_comment": "should_comment",
}

class BenchmarkValidationError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _required(value: Mapping[str, Any], fields: Iterable[str], label: str) -> None:
    missing = sorted(field for field in fields if field not in value)
    if missing:
        raise BenchmarkValidationError("%s is missing: %s" % (label, ", ".join(missing)))


def _nonempty(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BenchmarkValidationError("%s must be non-empty" % label)
    return text


def _validate_timestamp(value: Any, path: str) -> None:
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BenchmarkValidationError(path + " must be ISO-8601") from exc


def _validate_annotator(record: Mapping[str, Any], path: str, formal: bool) -> None:
    _required(record, (
        "annotator_id", "annotator_kind", "role", "provider", "model", "model_revision",
        "prompt_version", "prompt_hash", "context_policy_id", "context_policy_hash",
        "tool_policy_id", "tool_policy_hash", "repository_access_mode", "run_id",
        "started_at", "completed_at", "blindness",
    ), path)
    _nonempty(record["annotator_id"], path + ".annotator_id")
    if record["annotator_kind"] not in ANNOTATOR_KINDS:
        raise BenchmarkValidationError(path + " has invalid annotator_kind")
    if record["role"] not in ANNOTATOR_ROLES:
        raise BenchmarkValidationError(path + " has invalid role")
    if record["repository_access_mode"] not in REPOSITORY_ACCESS_MODES:
        raise BenchmarkValidationError(path + " has invalid repository_access_mode")
    _nonempty(record["run_id"], path + ".run_id")
    _validate_timestamp(record["started_at"], path + ".started_at")
    _validate_timestamp(record["completed_at"], path + ".completed_at")
    blindness = record["blindness"]
    if not isinstance(blindness, Mapping):
        raise BenchmarkValidationError(path + ".blindness must be an object")
    _required(blindness, (
        "evoagent_prediction_visible", "experiment_arm_visible",
        "peer_annotation_visible", "production_attribution_visible",
    ), path + ".blindness")
    if any(not isinstance(blindness[field], bool) for field in blindness):
        raise BenchmarkValidationError(path + ".blindness values must be booleans")
    if record["annotator_kind"] == "MODEL":
        for field in (
            "provider", "model", "model_revision", "prompt_version", "prompt_hash",
            "context_policy_id", "context_policy_hash", "tool_policy_id", "tool_policy_hash",
        ):
            _nonempty(record[field], path + "." + field)
    if formal and record["role"] == "FIRST_PASS" and record["annotator_kind"] in {"MODEL", "HUMAN"}:
        forbidden = (
            "evoagent_prediction_visible", "experiment_arm_visible", "peer_annotation_visible",
        )
        if any(blindness[field] for field in forbidden):
            raise BenchmarkValidationError(path + " formal first-pass annotation must be blind")
    if formal and record["role"] == "ADJUDICATOR" and (
        blindness["evoagent_prediction_visible"] or blindness["experiment_arm_visible"]
    ):
        raise BenchmarkValidationError(path + " formal adjudicator must be blind to prediction and arm")


def _validate_objective_evidence(value: Any, path: str, source_revision: str) -> None:
    if not isinstance(value, list) or not value:
        raise BenchmarkValidationError(path + " requires objective evidence")
    for index, item in enumerate(value):
        item_path = "%s[%d]" % (path, index)
        if not isinstance(item, Mapping):
            raise BenchmarkValidationError(item_path + " must be an object")
        _required(item, ("evidence_kind", "source_revision", "evidence_refs", "rationale"), item_path)
        _nonempty(item["evidence_kind"], item_path + ".evidence_kind")
        if str(item["source_revision"]) != str(source_revision):
            raise BenchmarkValidationError(item_path + " must bind the frozen source revision")
        if not isinstance(item["evidence_refs"], list) or not item["evidence_refs"]:
            raise BenchmarkValidationError(item_path + ".evidence_refs must be non-empty")
        _nonempty(item["rationale"], item_path + ".rationale")


def _validate_reviewer_label(label: Mapping[str, Any], path: str) -> None:
    _required(label, (
        "annotator_id", "issue_exists", "should_comment", "logical_issue_id", "category",
        "path", "start_line", "end_line", "severity", "evidence_refs", "rationale",
        "duration_seconds", "submitted_at",
    ), path)
    _nonempty(label["annotator_id"], path + ".annotator_id")
    if not isinstance(label["issue_exists"], bool) or not isinstance(label["should_comment"], bool):
        raise BenchmarkValidationError(path + " boolean labels must be explicit")
    if not isinstance(label["duration_seconds"], (int, float)) or label["duration_seconds"] < 0:
        raise BenchmarkValidationError(path + ".duration_seconds must be non-negative")
    if not isinstance(label["evidence_refs"], list):
        raise BenchmarkValidationError(path + ".evidence_refs must be an array")
    _nonempty(label["rationale"], path + ".rationale")
    _validate_timestamp(label["submitted_at"], path + ".submitted_at")


def compare_annotation_labels(left: Mapping[str, Any], right: Mapping[str, Any]) -> Dict[str, bool]:
    """Compare independently authored labels by semantic dimension."""
    comparison = {}
    for dimension, fields in AGREEMENT_DIMENSIONS.items():
        names = fields if isinstance(fields, tuple) else (fields,)
        comparison[dimension] = all(left.get(name) == right.get(name) for name in names)
    comparison["technical_agreement"] = all(
        comparison[name] for name in (
            "technical_existence", "logical_issue_identity", "taxonomy", "location", "severity",
        )
    )
    comparison["comment_usefulness_agreement"] = comparison["should_comment"]
    comparison["full_agreement"] = comparison["technical_agreement"] and comparison["comment_usefulness_agreement"]
    return comparison


def _validate_finding(
    finding: Mapping[str, Any], path: str, annotators: Mapping[str, Mapping[str, Any]],
    formal: bool, source_revision: str,
) -> None:
    _required(finding, (
        "issue_id", "category", "path", "start_line", "end_line", "severity",
        "issue_exists", "should_comment", "technical_evidence", "technical_rationale",
        "comment_usefulness_rationale", "annotation_status", "reviewer_labels",
        "gold_evidence_level", "objective_evidence",
    ), path)
    _nonempty(finding["issue_id"], path + ".issue_id")
    _nonempty(finding["category"], path + ".category")
    _nonempty(finding["path"], path + ".path")
    if not isinstance(finding["issue_exists"], bool):
        raise BenchmarkValidationError(path + ".issue_exists must be explicit boolean")
    if not isinstance(finding["should_comment"], bool):
        raise BenchmarkValidationError(path + ".should_comment must be explicit boolean")
    if finding["should_comment"] and not finding["issue_exists"]:
        raise BenchmarkValidationError(path + " cannot comment on an adjudicated non-issue")
    try:
        start, end = int(finding["start_line"]), int(finding["end_line"])
    except (TypeError, ValueError) as exc:
        raise BenchmarkValidationError(path + " line range must be integers") from exc
    if start < 1 or end < start:
        raise BenchmarkValidationError(path + " has invalid line range")
    if str(finding["severity"]).lower() not in SEVERITIES:
        raise BenchmarkValidationError(path + " has invalid severity")
    if not isinstance(finding["technical_evidence"], list):
        raise BenchmarkValidationError(path + ".technical_evidence must be an array")
    if finding["issue_exists"] and not finding["technical_evidence"]:
        raise BenchmarkValidationError(path + " positive finding requires repository-grounded evidence")
    _nonempty(finding["technical_rationale"], path + ".technical_rationale")
    _nonempty(finding["comment_usefulness_rationale"], path + ".comment_usefulness_rationale")
    status = str(finding["annotation_status"])
    if status not in FINDING_ANNOTATION_STATUSES:
        raise BenchmarkValidationError(path + " has invalid annotation_status")
    labels = finding["reviewer_labels"]
    level = finding["gold_evidence_level"]
    if not isinstance(labels, list) or (not labels and level != "OBJECTIVE"):
        raise BenchmarkValidationError(path + ".reviewer_labels must retain original labels")
    reviewers = set()
    for index, label in enumerate(labels):
        if not isinstance(label, Mapping):
            raise BenchmarkValidationError(path + ".reviewer_labels must contain objects")
        _validate_reviewer_label(label, "%s.reviewer_labels[%d]" % (path, index))
        reviewer = str(label["annotator_id"])
        if reviewer not in annotators:
            raise BenchmarkValidationError(path + " references unknown annotator")
        if annotators[reviewer]["role"] != "FIRST_PASS":
            raise BenchmarkValidationError(path + " labels must reference FIRST_PASS annotators")
        if annotators[reviewer]["annotator_kind"] == "MODEL" and label["issue_exists"]:
            if not label["evidence_refs"] or not str(label["rationale"]).strip():
                raise BenchmarkValidationError(path + " positive MODEL label requires evidence and rationale")
        if reviewer in reviewers:
            raise BenchmarkValidationError(path + " contains duplicate reviewer labels")
        reviewers.add(reviewer)
    if level not in GOLD_EVIDENCE_LEVELS:
        raise BenchmarkValidationError(path + " has invalid gold_evidence_level")
    if level == "OBJECTIVE":
        _validate_objective_evidence(finding["objective_evidence"], path + ".objective_evidence", source_revision)
        if not any(item["annotator_kind"] == "OBJECTIVE_EVIDENCE" for item in annotators.values()):
            raise BenchmarkValidationError(path + " OBJECTIVE gold requires objective-evidence annotator provenance")
    elif finding["objective_evidence"] not in (None, []):
        _validate_objective_evidence(finding["objective_evidence"], path + ".objective_evidence", source_revision)
    if status == "ADJUDICATED":
        adjudication = finding.get("adjudication")
        if level in {"MODEL_ADJUDICATED", "HUMAN_ADJUDICATED"}:
            if not isinstance(adjudication, Mapping):
                raise BenchmarkValidationError(path + " adjudicated finding requires adjudication")
            _required(adjudication, ("adjudicator_id", "adjudicated_at", "rationale", "disagreements"), path + ".adjudication")
            adjudicator_id = str(adjudication["adjudicator_id"])
            if adjudicator_id not in annotators or annotators[adjudicator_id]["role"] != "ADJUDICATOR":
                raise BenchmarkValidationError(path + " references invalid adjudicator")
            expected_kind = "MODEL" if level == "MODEL_ADJUDICATED" else "HUMAN"
            if annotators[adjudicator_id]["annotator_kind"] != expected_kind:
                raise BenchmarkValidationError(path + " adjudicator kind does not match gold evidence level")
            allowed = set(AGREEMENT_DIMENSIONS)
            disagreements = adjudication["disagreements"]
            if not isinstance(disagreements, list) or not set(disagreements).issubset(allowed):
                raise BenchmarkValidationError(path + " has invalid disagreement categories")
        elif adjudication not in (None, {}):
            raise BenchmarkValidationError(path + " agreement/objective gold must not claim adjudication")
    else:
        _nonempty(finding.get("uncertainty_reason"), path + ".uncertainty_reason")
        if level != "QUARANTINED":
            raise BenchmarkValidationError(path + " unresolved finding must be QUARANTINED")


def _independent_blind_labels(
    labels: Sequence[Mapping[str, Any]], annotators: Mapping[str, Mapping[str, Any]],
) -> bool:
    if len(labels) < 2:
        return False
    left, right = labels[0], labels[1]
    left_annotator = annotators[str(left["annotator_id"])]
    right_annotator = annotators[str(right["annotator_id"])]
    return (
        left_annotator["run_id"] != right_annotator["run_id"]
        and left_annotator["annotator_id"] != right_annotator["annotator_id"]
        and not any(left_annotator["blindness"][name] for name in (
            "evoagent_prediction_visible", "experiment_arm_visible", "peer_annotation_visible",
        ))
        and not any(right_annotator["blindness"][name] for name in (
            "evoagent_prediction_visible", "experiment_arm_visible", "peer_annotation_visible",
        ))
    )


def _independent_first_pass_labels(
    labels: Sequence[Mapping[str, Any]], annotators: Mapping[str, Mapping[str, Any]],
) -> bool:
    return _independent_blind_labels(labels, annotators) and all(
        annotators[str(label["annotator_id"])]["annotator_kind"] == "MODEL"
        for label in labels[:2]
    )


def finding_qualifies_for_primary_gold(
    finding: Mapping[str, Any], annotators: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Return whether evidence and adjudication, not status alone, qualify a finding."""
    if finding.get("annotation_status") != "ADJUDICATED":
        return False
    level = finding.get("gold_evidence_level")
    if level == "OBJECTIVE":
        return bool(
            finding.get("objective_evidence")
            and any(item["annotator_kind"] == "OBJECTIVE_EVIDENCE" for item in annotators.values())
        )
    labels = finding.get("reviewer_labels") or []
    if level == "INDEPENDENT_MODEL_AGREEMENT":
        return (
            _independent_first_pass_labels(labels, annotators)
            and compare_annotation_labels(labels[0], labels[1])["full_agreement"]
            and all(
                all(
                    finding.get("issue_id" if name == "logical_issue_id" else name) == label.get(name)
                    for name in (fields if isinstance(fields, tuple) else (fields,))
                )
                for label in labels[:2]
                for fields in AGREEMENT_DIMENSIONS.values()
            )
        )
    if level in {"MODEL_ADJUDICATED", "HUMAN_ADJUDICATED"}:
        adjudication = finding.get("adjudication") or {}
        adjudicator = annotators.get(str(adjudication.get("adjudicator_id")))
        expected_kind = "MODEL" if level == "MODEL_ADJUDICATED" else "HUMAN"
        return bool(
            _independent_blind_labels(labels, annotators) and adjudicator
            and adjudicator["annotator_kind"] == expected_kind
            and adjudicator["role"] == "ADJUDICATOR"
            and not adjudicator["blindness"]["evoagent_prediction_visible"]
            and not adjudicator["blindness"]["experiment_arm_visible"]
            and not compare_annotation_labels(labels[0], labels[1])["full_agreement"]
        )
    return False


def case_qualifies_for_primary_gold(case: Mapping[str, Any]) -> bool:
    """Return whether a validated case may contribute to primary metrics."""
    if case.get("case_annotation_status") != "ADJUDICATED" or case.get("protocol_dry_run"):
        return False
    annotators = {str(item["annotator_id"]): item for item in case.get("annotators") or []}
    findings = case.get("findings") or []
    if findings:
        return all(finding_qualifies_for_primary_gold(item, annotators) for item in findings)
    clean = case.get("clean_review") or {}
    level = clean.get("gold_evidence_level")
    if not clean.get("completed") or not clean.get("no_commentable_findings_remain"):
        return False
    if level == "OBJECTIVE":
        return bool(
            clean.get("objective_evidence")
            and any(item["annotator_kind"] == "OBJECTIVE_EVIDENCE" for item in annotators.values())
        )
    reviews = case.get("reviewer_records") or []
    if level == "INDEPENDENT_MODEL_AGREEMENT" and len(reviews) >= 2:
        left = annotators.get(str(reviews[0].get("annotator_id")))
        right = annotators.get(str(reviews[1].get("annotator_id")))
        return bool(
            left and right and left["annotator_kind"] == right["annotator_kind"] == "MODEL"
            and left["run_id"] != right["run_id"]
            and left["annotator_id"] != right["annotator_id"]
        )
    if level in {"MODEL_ADJUDICATED", "HUMAN_ADJUDICATED"}:
        adjudication = clean.get("adjudication") or {}
        adjudicator = annotators.get(str(adjudication.get("adjudicator_id")))
        expected = "MODEL" if level == "MODEL_ADJUDICATED" else "HUMAN"
        if len(reviews) < 2:
            return False
        left = annotators.get(str(reviews[0].get("annotator_id")))
        right = annotators.get(str(reviews[1].get("annotator_id")))
        return bool(
            left and right and left["run_id"] != right["run_id"]
            and left["annotator_id"] != right["annotator_id"]
            and adjudicator and adjudicator["role"] == "ADJUDICATOR"
            and adjudicator["annotator_kind"] == expected
        )
    return False


def validate_benchmark_case(case: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate one schema-v2 evidence-adjudicated case without defaults."""
    _required(case, (
        "schema_version", "dataset_id", "dataset_version", "annotation_version",
        "case_id", "repository_id", "source", "base_sha", "head_sha",
        "diff_sha256", "captured_at", "split", "diff", "findings",
        "case_annotation_status", "annotators", "reviewer_records", "clean_review",
    ), "case")
    if int(case["schema_version"]) != BENCHMARK_SCHEMA_VERSION:
        raise BenchmarkValidationError("evidence-adjudicated benchmark schema_version must be 2")
    for field in ("dataset_id", "dataset_version", "annotation_version", "case_id", "repository_id"):
        _nonempty(case[field], "case." + field)
    source = case["source"]
    if not isinstance(source, Mapping) or source.get("kind") not in BENCHMARK_PROVENANCE:
        raise BenchmarkValidationError("benchmark provenance must be public or private historical PR")
    _required(source, ("kind", "reference_id"), "case.source")
    _nonempty(source["reference_id"], "case.source.reference_id")
    if not GIT_SHA_RE.match(str(case["base_sha"])) or not GIT_SHA_RE.match(str(case["head_sha"])):
        raise BenchmarkValidationError("case requires immutable base_sha and head_sha")
    if not SHA256_RE.match(str(case["diff_sha256"])):
        raise BenchmarkValidationError("case.diff_sha256 must be a lowercase SHA-256")
    actual_diff_hash = hashlib.sha256(str(case["diff"]).encode("utf-8")).hexdigest()
    if actual_diff_hash != case["diff_sha256"]:
        raise BenchmarkValidationError("case diff does not match diff_sha256")
    try:
        datetime.fromisoformat(str(case["captured_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BenchmarkValidationError("case.captured_at must be ISO-8601") from exc
    if case["split"] not in BENCHMARK_SPLITS:
        raise BenchmarkValidationError("case has invalid benchmark split")
    status = str(case["case_annotation_status"])
    if status not in ANNOTATION_FINAL_STATUSES:
        raise BenchmarkValidationError("case has invalid annotation status")
    formal = not bool(case.get("protocol_dry_run"))
    annotator_records = case["annotators"]
    if not isinstance(annotator_records, list) or not annotator_records:
        raise BenchmarkValidationError("case must retain annotator provenance")
    annotators = {}
    for index, record in enumerate(annotator_records):
        if not isinstance(record, Mapping):
            raise BenchmarkValidationError("case.annotators must contain objects")
        _validate_annotator(record, "case.annotators[%d]" % index, formal)
        annotator_id = str(record["annotator_id"])
        if annotator_id in annotators:
            raise BenchmarkValidationError("case contains duplicate annotator_id")
        annotators[annotator_id] = record
    findings = case["findings"]
    if not isinstance(findings, list):
        raise BenchmarkValidationError("case.findings must be an array")
    issue_ids = set()
    for index, finding in enumerate(findings):
        if not isinstance(finding, Mapping):
            raise BenchmarkValidationError("case.findings must contain objects")
        _validate_finding(
            finding, "case.findings[%d]" % index, annotators, formal, str(case["head_sha"]),
        )
        if finding["issue_id"] in issue_ids:
            raise BenchmarkValidationError("case contains duplicate issue_id")
        issue_ids.add(finding["issue_id"])
    reviewers = case["reviewer_records"]
    if not isinstance(reviewers, list) or not reviewers:
        raise BenchmarkValidationError("case must retain reviewer_records")
    for index, record in enumerate(reviewers):
        if not isinstance(record, Mapping):
            raise BenchmarkValidationError("reviewer_records must contain objects")
        _required(record, ("annotator_id", "completed_review", "duration_seconds", "submitted_at"), "case.reviewer_records[%d]" % index)
        if str(record["annotator_id"]) not in annotators:
            raise BenchmarkValidationError("reviewer record references unknown annotator")
        if record["completed_review"] is not True:
            raise BenchmarkValidationError("reviewer record must explicitly complete review")
    commentable = [
        item for item in findings
        if item["issue_exists"] and item["should_comment"]
        and finding_qualifies_for_primary_gold(item, annotators)
    ]
    clean = case["clean_review"]
    if not isinstance(clean, Mapping):
        raise BenchmarkValidationError("case.clean_review must be an object")
    _required(clean, (
        "completed", "no_commentable_findings_remain", "rationale",
        "gold_evidence_level", "objective_evidence",
    ), "case.clean_review")
    if not isinstance(clean["completed"], bool) or not isinstance(clean["no_commentable_findings_remain"], bool):
        raise BenchmarkValidationError("clean_review decisions must be explicit booleans")
    if status == "ADJUDICATED" and not commentable:
        if clean["completed"] is not True or clean["no_commentable_findings_remain"] is not True:
            raise BenchmarkValidationError("clean case requires explicit completed no-commentable-findings audit")
        _nonempty(clean["rationale"], "case.clean_review.rationale")
        if clean["gold_evidence_level"] == "OBJECTIVE":
            _validate_objective_evidence(
                clean["objective_evidence"], "case.clean_review.objective_evidence", str(case["head_sha"]),
            )
            if not any(item["annotator_kind"] == "OBJECTIVE_EVIDENCE" for item in annotators.values()):
                raise BenchmarkValidationError("OBJECTIVE clean gold requires objective-evidence annotator provenance")
        elif clean["gold_evidence_level"] == "INDEPENDENT_MODEL_AGREEMENT":
            review_annotators = [annotators[str(record["annotator_id"])] for record in reviewers]
            if len(review_annotators) < 2 or any(item["annotator_kind"] != "MODEL" for item in review_annotators[:2]):
                raise BenchmarkValidationError("clean gold requires two independent MODEL reviews")
            if review_annotators[0]["run_id"] == review_annotators[1]["run_id"]:
                raise BenchmarkValidationError("clean gold requires separate annotation runs")
        elif clean["gold_evidence_level"] in {"MODEL_ADJUDICATED", "HUMAN_ADJUDICATED"}:
            adjudication = clean.get("adjudication")
            if not isinstance(adjudication, Mapping):
                raise BenchmarkValidationError("adjudicated clean gold requires adjudication")
            adjudicator = annotators.get(str(adjudication.get("adjudicator_id")))
            expected = "MODEL" if clean["gold_evidence_level"] == "MODEL_ADJUDICATED" else "HUMAN"
            if not adjudicator or adjudicator["role"] != "ADJUDICATOR" or adjudicator["annotator_kind"] != expected:
                raise BenchmarkValidationError("clean adjudicator kind does not match gold evidence level")
            if len(reviewers) < 2:
                raise BenchmarkValidationError("adjudicated clean gold requires two independent reviews")
            left = annotators[str(reviewers[0]["annotator_id"])]
            right = annotators[str(reviewers[1]["annotator_id"])]
            if left["run_id"] == right["run_id"] or left["annotator_id"] == right["annotator_id"]:
                raise BenchmarkValidationError("adjudicated clean gold requires independent reviews")
        else:
            raise BenchmarkValidationError("clean case has unsupported gold evidence level")
    elif status == "ADJUDICATED" and clean["no_commentable_findings_remain"]:
        raise BenchmarkValidationError("case with commentable gold cannot be declared clean")
    if status == "ADJUDICATED" and any(
        not finding_qualifies_for_primary_gold(item, annotators) for item in findings
    ):
        raise BenchmarkValidationError("ADJUDICATED case cannot contain unqualified findings")
    relations = case.get("related_cases") or []
    if not isinstance(relations, list):
        raise BenchmarkValidationError("case.related_cases must be an array")
    for relation in relations:
        if not isinstance(relation, Mapping) or relation.get("relationship") not in RELATION_TYPES:
            raise BenchmarkValidationError("case has invalid related-case metadata")
        _nonempty(relation.get("case_id"), "related case_id")
    if case.get("protocol_dry_run") not in (None, False, True):
        raise BenchmarkValidationError("protocol_dry_run must be boolean")
    return json.loads(canonical_json(case))


def validate_repository_splits(cases: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    repositories: Dict[str, str] = {}
    by_case = {str(case["case_id"]): case for case in cases}
    for case in cases:
        repository, split = str(case["repository_id"]), str(case["split"])
        previous = repositories.setdefault(repository, split)
        if previous != split:
            raise BenchmarkValidationError("repository split leakage: %s" % repository)
    violations = []
    for case in cases:
        for relation in case.get("related_cases") or []:
            related = by_case.get(str(relation["case_id"]))
            if related is None:
                raise BenchmarkValidationError(
                    "related patch metadata references unknown case: %s" % relation["case_id"]
                )
            if related["split"] != case["split"]:
                violations.append({
                    "left": case["case_id"], "right": related["case_id"],
                    "relationship": relation["relationship"],
                    "left_split": case["split"], "right_split": related["split"],
                })
    if violations:
        raise BenchmarkValidationError("related patch split violation: %s" % canonical_json(violations))
    return {
        "repositories": len(repositories),
        "split_repositories": {
            split: sorted(repo for repo, value in repositories.items() if value == split)
            for split in sorted(BENCHMARK_SPLITS)
        },
    }


def canonical_cases_fingerprint(cases: Sequence[Mapping[str, Any]]) -> str:
    return sha256_json(sorted((dict(case) for case in cases), key=lambda item: str(item["case_id"])))


@dataclass(frozen=True)
class BenchmarkManifest:
    dataset_id: str
    dataset_version: str
    schema_version: int
    annotation_version: str
    provenance_policy: Dict[str, Any]
    case_ids: List[str]
    repository_ids: List[str]
    split_assignment: Dict[str, str]
    raw_content_sha256: str
    canonical_content_sha256: str
    sampling_policy_version: str
    annotation_guide_version: str
    matcher_version: str
    metric_contract_version: str
    manifest_sha256: str

    @classmethod
    def create(
        cls, cases: Sequence[Mapping[str, Any]], raw_content_sha256: str,
        sampling_policy_version: str, annotation_guide_version: str,
        matcher_version: str, metric_contract_version: str,
    ) -> "BenchmarkManifest":
        if not cases:
            raise BenchmarkValidationError("benchmark manifest requires at least one case")
        normalized = [validate_benchmark_case(case) for case in cases]
        validate_repository_splits(normalized)
        identities = {(c["dataset_id"], c["dataset_version"], c["annotation_version"]) for c in normalized}
        if len(identities) != 1:
            raise BenchmarkValidationError("all cases must share dataset and annotation versions")
        if not SHA256_RE.match(str(raw_content_sha256)):
            raise BenchmarkValidationError("raw_content_sha256 must be a lowercase SHA-256")
        dataset_id, dataset_version, annotation_version = next(iter(identities))
        body = {
            "dataset_id": dataset_id, "dataset_version": dataset_version,
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "annotation_version": annotation_version,
            "provenance_policy": {"allowed": sorted(BENCHMARK_PROVENANCE), "immutable_revision_required": True},
            "case_ids": sorted(str(c["case_id"]) for c in normalized),
            "repository_ids": sorted({str(c["repository_id"]) for c in normalized}),
            "split_assignment": {str(c["case_id"]): str(c["split"]) for c in sorted(normalized, key=lambda item: str(item["case_id"]))},
            "raw_content_sha256": raw_content_sha256,
            "canonical_content_sha256": canonical_cases_fingerprint(normalized),
            "sampling_policy_version": _nonempty(sampling_policy_version, "sampling_policy_version"),
            "annotation_guide_version": _nonempty(annotation_guide_version, "annotation_guide_version"),
            "matcher_version": _nonempty(matcher_version, "matcher_version"),
            "metric_contract_version": _nonempty(metric_contract_version, "metric_contract_version"),
        }
        return cls(**body, manifest_sha256=sha256_json(body))

    def to_dict(self) -> Dict[str, Any]:
        return json.loads(canonical_json(self.__dict__))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BenchmarkManifest":
        manifest = cls(**dict(value))
        body = manifest.to_dict()
        claimed = body.pop("manifest_sha256")
        if sha256_json(body) != claimed:
            raise BenchmarkValidationError("benchmark manifest fingerprint is invalid")
        return manifest

    def verify(self, cases: Sequence[Mapping[str, Any]], raw_content_sha256: str) -> None:
        recreated = BenchmarkManifest.create(
            cases, raw_content_sha256, self.sampling_policy_version,
            self.annotation_guide_version, self.matcher_version,
            self.metric_contract_version,
        )
        if recreated.to_dict() != self.to_dict():
            raise BenchmarkValidationError("dataset contents, labels or splits do not match immutable manifest")


def write_manifest_immutable(path: str, manifest: BenchmarkManifest) -> None:
    rendered = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            if handle.read() != rendered:
                raise BenchmarkValidationError("dataset version manifest is immutable")
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)


def _prediction_finding(value: Mapping[str, Any]) -> Finding:
    try:
        severity = Severity(str(value.get("severity", "medium")).lower())
    except ValueError:
        severity = Severity.MEDIUM
    return Finding(
        rule_id=str(value.get("rule_id") or "BENCHMARK-PREDICTION"),
        severity=severity, title=str(value.get("title") or "prediction"),
        explanation=str(value.get("explanation") or "prediction"),
        path=str(value["path"]), line=int(value.get("line", value.get("start_line"))),
        evidence=str(value.get("evidence") or ""), fix=str(value.get("fix") or ""),
        test=str(value.get("test") or ""), confidence=float(value.get("confidence", 1.0)),
        cwe=str(value.get("cwe") or "") or None,
    )


def metric_value(
    numerator: Optional[float], denominator: Optional[float], value: Optional[float],
    unit: str, eligible_cases: int, definition: str,
) -> Dict[str, Any]:
    return {
        "numerator": numerator, "denominator": denominator,
        "value": None if value is None else round(float(value), 6),
        "aggregation_unit": unit, "eligible_case_count": int(eligible_cases),
        "eligible_definition": definition,
    }


def _metrics_from_contributions(contributions: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    count = len(contributions)
    tp = sum(int(c.get("tp", 0)) for c in contributions)
    fp = sum(int(c.get("fp", 0)) for c in contributions)
    fn = sum(int(c.get("fn", 0)) for c in contributions)
    high_tp = sum(int(c.get("high_tp", 0)) for c in contributions)
    high_fn = sum(int(c.get("high_fn", 0)) for c in contributions)
    clean = [c for c in contributions if bool(c.get("is_clean"))]
    clean_hits = sum(bool(c.get("clean_hit")) for c in clean)
    high_case_count = sum(
        bool(int(c.get("high_tp", 0)) + int(c.get("high_fn", 0)))
        for c in contributions
    )
    positive_gold = tp + fn
    if not count:
        precision = recall = f1 = high_recall = fp_per_pr = clean_accuracy = None
    else:
        precision = tp / (tp + fp) if tp + fp else (0.0 if positive_gold else None)
        recall = tp / positive_gold if positive_gold else None
        f1 = (2 * precision * recall / (precision + recall)) if (
            precision is not None and recall is not None and precision + recall
        ) else (0.0 if precision == 0 and recall == 0 else None)
        high_recall = high_tp / (high_tp + high_fn) if high_tp + high_fn else None
        fp_per_pr = fp / count
        clean_accuracy = clean_hits / len(clean) if clean else None
    return {
        "precision": metric_value(tp, tp + fp, precision, "comment", count, "qualified evidence-adjudicated should_comment=true gold and all predictions"),
        "recall": metric_value(tp, positive_gold, recall, "comment", count, "qualified issue_exists=true and should_comment=true gold"),
        "f1": metric_value(2 * tp, 2 * tp + fp + fn, f1, "comment", count, "2TP / (2TP + FP + FN), undefined when recall has no eligible gold"),
        "high_risk_recall": metric_value(high_tp, high_tp + high_fn, high_recall, "comment", high_case_count, "high or critical qualified commentable gold"),
        "false_positives_per_pr": metric_value(fp, count, fp_per_pr, "pull_request", count, "unmatched predictions across qualified PRs"),
        "clean_pr_accuracy": metric_value(clean_hits, len(clean), clean_accuracy, "pull_request", len(clean), "qualified PRs with no commentable gold"),
    }


def score_final_benchmark(
    cases: Sequence[Mapping[str, Any]], predictions: Mapping[str, Sequence[Mapping[str, Any]]],
    line_tolerance: int = 2,
) -> Dict[str, Any]:
    contributions = []
    excluded = []
    for raw in cases:
        case = validate_benchmark_case(raw)
        if case.get("protocol_dry_run"):
            excluded.append({"case_id": case["case_id"], "reason": "protocol-dry-run"})
            continue
        if case["case_annotation_status"] != "ADJUDICATED":
            excluded.append({"case_id": case["case_id"], "reason": case["case_annotation_status"]})
            continue
        if not case_qualifies_for_primary_gold(case):
            excluded.append({"case_id": case["case_id"], "reason": "unqualified-gold"})
            continue
        annotators = {str(item["annotator_id"]): item for item in case["annotators"]}
        expected = [
            item for item in case["findings"]
            if finding_qualifies_for_primary_gold(item, annotators)
            and item["issue_exists"] and item["should_comment"]
        ]
        predicted = [_prediction_finding(item) for item in predictions.get(case["case_id"], [])]
        matches = one_to_one_match(expected, predicted, line_tolerance)
        high_indices = {index for index, item in enumerate(expected) if item["severity"] in {"high", "critical"}}
        matched_expected = {item.expected_index for item in matches}
        contributions.append({
            "case_id": case["case_id"], "repository": case["repository_id"],
            "tp": len(matches), "fp": len(predicted) - len(matches),
            "fn": len(expected) - len(matches),
            "high_tp": len(high_indices & matched_expected),
            "high_fn": len(high_indices - matched_expected),
            "is_clean": not expected, "clean_hit": not expected and not predicted,
        })
    return {
        "contract_version": "final-metrics-v2",
        "metrics": _metrics_from_contributions(contributions),
        "case_contributions": contributions, "excluded": excluded,
    }


def aggregate_optional_cost(executions: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    fields = ("input_tokens", "output_tokens", "total_tokens", "provider_cost_usd", "model_latency_ms", "end_to_end_latency_ms")
    result = {}
    for field in fields:
        values = [item.get(field) for item in executions]
        missing = sum(value is None for value in values)
        result[field] = {
            "value": None if missing else sum(values),
            "reported_count": len(values) - missing,
            "missing_count": missing,
            "aggregation": "sum",
        }
    return result


def _metric_scalar(contributions: Sequence[Mapping[str, Any]], metric: str) -> Optional[float]:
    value = _metrics_from_contributions(contributions).get(metric)
    if value is None:
        raise BenchmarkValidationError("unsupported bootstrap metric: %s" % metric)
    return value["value"]


def expand_repository_sample(
    records: Sequence[Mapping[str, Any]], selected_repositories: Sequence[str],
) -> List[Mapping[str, Any]]:
    """Expand sampled clusters without sampling individual PRs inside them."""
    by_repository = {
        repository: [item for item in records if str(item["repository"]) == repository]
        for repository in {str(item["repository"]) for item in records}
    }
    unknown = set(selected_repositories).difference(by_repository)
    if unknown:
        raise BenchmarkValidationError("unknown repository cluster: %s" % ", ".join(sorted(unknown)))
    return [item for repository in selected_repositories for item in by_repository[repository]]


def repository_cluster_paired_bootstrap(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]], metric: str,
    iterations: Optional[int] = None, seed: int = 20260819, final_report: bool = False,
) -> Dict[str, Any]:
    left_keys = [(str(c["case_id"]), str(c["repository"])) for c in left]
    right_keys = [(str(c["case_id"]), str(c["repository"])) for c in right]
    if left_keys != right_keys:
        raise BenchmarkValidationError("paired arms require identical ordered case and repository identities")
    repositories = sorted({repository for _, repository in left_keys})
    chosen_iterations = int(iterations if iterations is not None else (10000 if final_report else 2000))
    if chosen_iterations < 1:
        raise BenchmarkValidationError("bootstrap iterations must be positive")
    left_point, right_point = _metric_scalar(left, metric), _metric_scalar(right, metric)
    point_delta = None if left_point is None or right_point is None else right_point - left_point
    deltas = []
    rng = random.Random(int(seed))
    for _ in range(chosen_iterations):
        selected = [repositories[rng.randrange(len(repositories))] for _ in repositories] if repositories else []
        sampled_left = expand_repository_sample(left, selected)
        sampled_right = expand_repository_sample(right, selected)
        left_value, right_value = _metric_scalar(sampled_left, metric), _metric_scalar(sampled_right, metric)
        if left_value is not None and right_value is not None:
            deltas.append(right_value - left_value)
    deltas.sort()
    if not deltas:
        ci = [None, None]
    else:
        ci = [deltas[int((len(deltas) - 1) * .025)], deltas[int((len(deltas) - 1) * .975)]]
    return {
        "metric": metric, "left_point_estimate": left_point,
        "right_point_estimate": right_point,
        "paired_delta": None if point_delta is None else round(point_delta, 6),
        "ci95": [None if value is None else round(value, 6) for value in ci],
        "repository_count": len(repositories), "pr_count": len(left),
        "iterations": chosen_iterations, "seed": int(seed),
        "sampling_unit": "repository-cluster",
    }


def validate_attribution_gold(record: Mapping[str, Any]) -> Dict[str, Any]:
    _required(record, (
        "schema_version", "failure_id", "task_id", "release_id", "review_revision",
        "observable_failure_layer", "status", "supported_surface", "target_skill",
        "evidence_refs", "rationale", "annotation_status", "annotators", "reviewer_labels",
        "gold_evidence_level", "objective_evidence",
    ), "attribution_gold")
    if int(record["schema_version"]) != 1:
        raise BenchmarkValidationError("attribution gold schema_version must be 1")
    for field in ("failure_id", "task_id", "release_id", "review_revision", "observable_failure_layer"):
        _nonempty(record[field], "attribution_gold." + field)
    if record["status"] not in ATTRIBUTION_STATUSES:
        raise BenchmarkValidationError("invalid attribution gold status")
    if record["supported_surface"] not in ATTRIBUTION_SURFACES:
        raise BenchmarkValidationError("invalid attribution gold surface")
    if record["supported_surface"] == "SKILL" and not str(record["target_skill"] or "").strip():
        raise BenchmarkValidationError("SKILL attribution gold requires target_skill")
    if record["supported_surface"] != "SKILL" and record["target_skill"] not in (None, ""):
        raise BenchmarkValidationError("non-Skill attribution gold cannot name target_skill")
    if record["status"] != "SUPPORTED" and record["supported_surface"] != "NO_SUPPORTED_EVOLUTION":
        raise BenchmarkValidationError("unknown/insufficient gold cannot support evolution")
    if record["annotation_status"] not in ANNOTATION_FINAL_STATUSES:
        raise BenchmarkValidationError("invalid attribution annotation status")
    if not isinstance(record["evidence_refs"], list) or not isinstance(record["reviewer_labels"], list):
        raise BenchmarkValidationError("attribution evidence and reviewer labels must be arrays")
    annotator_values = record["annotators"]
    if not isinstance(annotator_values, list) or not annotator_values:
        raise BenchmarkValidationError("attribution gold must retain annotator provenance")
    annotators = {}
    for index, annotator in enumerate(annotator_values):
        if not isinstance(annotator, Mapping):
            raise BenchmarkValidationError("attribution annotators must be objects")
        _validate_annotator(annotator, "attribution_gold.annotators[%d]" % index, True)
        if annotator["role"] in {"FIRST_PASS", "ADJUDICATOR"} and annotator["blindness"]["production_attribution_visible"]:
            raise BenchmarkValidationError("attribution annotator cannot see production AttributionResult")
        annotator_id = str(annotator["annotator_id"])
        if annotator_id in annotators:
            raise BenchmarkValidationError("duplicate attribution annotator_id")
        annotators[annotator_id] = annotator
    for index, label in enumerate(record["reviewer_labels"]):
        path = "attribution_gold.reviewer_labels[%d]" % index
        _required(label, (
            "annotator_id", "observable_failure_layer", "status", "supported_surface",
            "target_skill", "evidence_refs", "rationale", "submitted_at",
        ), path)
        if str(label["annotator_id"]) not in annotators:
            raise BenchmarkValidationError(path + " references unknown annotator")
        if annotators[str(label["annotator_id"])]["role"] != "FIRST_PASS":
            raise BenchmarkValidationError(path + " must reference FIRST_PASS annotator")
        if not isinstance(label["evidence_refs"], list) or not label["evidence_refs"]:
            raise BenchmarkValidationError(path + " requires execution evidence refs")
        _nonempty(label["rationale"], path + ".rationale")
        _validate_timestamp(label["submitted_at"], path + ".submitted_at")
    level = record["gold_evidence_level"]
    if level not in GOLD_EVIDENCE_LEVELS:
        raise BenchmarkValidationError("invalid attribution gold_evidence_level")
    if level == "OBJECTIVE":
        _validate_objective_evidence(
            record["objective_evidence"], "attribution_gold.objective_evidence",
            str(record["review_revision"]),
        )
        supported = {
            field for item in record["objective_evidence"]
            for field in item.get("supports_fields", [])
        }
        required_supported = {
            "observable_failure_layer", "status", "supported_surface", "target_skill",
        }
        if not required_supported.issubset(supported):
            raise BenchmarkValidationError(
                "objective attribution evidence must explicitly support layer and evolution route"
            )
        if not any(item["annotator_kind"] == "OBJECTIVE_EVIDENCE" for item in annotators.values()):
            raise BenchmarkValidationError("OBJECTIVE attribution gold requires objective-evidence annotator provenance")
    if record["annotation_status"] == "ADJUDICATED" and level in {"MODEL_ADJUDICATED", "HUMAN_ADJUDICATED"}:
        adjudication = record.get("adjudication")
        if not isinstance(adjudication, Mapping):
            raise BenchmarkValidationError("adjudicated attribution gold requires adjudication")
        _required(adjudication, ("adjudicator_id", "adjudicated_at", "rationale", "disagreements"), "attribution_gold.adjudication")
        adjudicator = annotators.get(str(adjudication["adjudicator_id"]))
        expected = "MODEL" if level == "MODEL_ADJUDICATED" else "HUMAN"
        if not adjudicator or adjudicator["role"] != "ADJUDICATOR" or adjudicator["annotator_kind"] != expected:
            raise BenchmarkValidationError("invalid attribution adjudicator provenance")
    else:
        if record["annotation_status"] != "ADJUDICATED":
            _nonempty(record.get("uncertainty_reason"), "attribution_gold.uncertainty_reason")
            if level != "QUARANTINED":
                raise BenchmarkValidationError("unresolved attribution must be QUARANTINED")
    _nonempty(record["rationale"], "attribution_gold.rationale")
    return json.loads(canonical_json(record))


def attribution_qualifies_for_primary_gold(record: Mapping[str, Any]) -> bool:
    if record.get("annotation_status") != "ADJUDICATED":
        return False
    level = record.get("gold_evidence_level")
    if level == "OBJECTIVE":
        supported = {
            field for item in record.get("objective_evidence") or []
            for field in item.get("supports_fields", [])
        }
        return (
            {"observable_failure_layer", "status", "supported_surface", "target_skill"}.issubset(supported)
            and any(
                item["annotator_kind"] == "OBJECTIVE_EVIDENCE"
                for item in record.get("annotators") or []
            )
        )
    annotators = {str(item["annotator_id"]): item for item in record.get("annotators") or []}
    labels = record.get("reviewer_labels") or []
    if level == "INDEPENDENT_MODEL_AGREEMENT" and len(labels) >= 2:
        left, right = labels[0], labels[1]
        return bool(
            _independent_first_pass_labels(labels, annotators)
            and all(left.get(field) == right.get(field) == record.get(field) for field in (
                "observable_failure_layer", "status", "supported_surface", "target_skill",
            ))
        )
    if level in {"MODEL_ADJUDICATED", "HUMAN_ADJUDICATED"}:
        adjudication = record.get("adjudication") or {}
        adjudicator = annotators.get(str(adjudication.get("adjudicator_id")))
        expected = "MODEL" if level == "MODEL_ADJUDICATED" else "HUMAN"
        fields = ("observable_failure_layer", "status", "supported_surface", "target_skill")
        disagreement = len(labels) >= 2 and any(labels[0].get(field) != labels[1].get(field) for field in fields)
        return bool(
            _independent_blind_labels(labels, annotators) and disagreement
            and adjudicator and adjudicator["annotator_kind"] == expected
            and adjudicator["role"] == "ADJUDICATOR"
        )
    return False


def score_attribution(
    gold_records: Sequence[Mapping[str, Any]], predictions: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    gold = [validate_attribution_gold(item) for item in gold_records]
    eligible = [item for item in gold if attribution_qualifies_for_primary_gold(item)]
    counts = {name: 0 for name in (
        "layer", "routing", "target", "combined", "deterministic", "unknown",
        "wrong_surface", "over", "under",
    )}
    skill_total = over_total = under_total = 0
    for item in eligible:
        prediction = predictions.get(str(item["failure_id"]), {})
        predicted_status = str(prediction.get("status") or "UNKNOWN")
        predicted_surface = str(prediction.get("supported_surface") or prediction.get("target_surface") or "NO_SUPPORTED_EVOLUTION")
        predicted_target = str(prediction.get("target_skill") or prediction.get("target_id") or "")
        gold_target = str(item.get("target_skill") or "")
        layer_ok = str(prediction.get("observable_failure_layer") or prediction.get("failure_layer") or "") == item["observable_failure_layer"]
        route_ok = predicted_surface == item["supported_surface"]
        target_ok = predicted_target == gold_target
        counts["layer"] += layer_ok
        counts["routing"] += route_ok and (item["supported_surface"] != "SKILL" or target_ok)
        if item["supported_surface"] == "SKILL":
            skill_total += 1
            counts["target"] += predicted_surface == "SKILL" and target_ok
        status_ok = predicted_status == item["status"]
        counts["combined"] += layer_ok and route_ok and target_ok and status_ok
        counts["deterministic"] += str(prediction.get("method") or "").upper() == "DETERMINISTIC"
        counts["unknown"] += predicted_status in {"UNKNOWN", "INSUFFICIENT_EVIDENCE"}
        counts["wrong_surface"] += predicted_surface != item["supported_surface"]
        gold_evolvable = item["supported_surface"] in {"GLOBAL_PROMPT", "SKILL"}
        predicted_evolvable = predicted_surface in {"GLOBAL_PROMPT", "SKILL"} and predicted_status == "SUPPORTED"
        if not gold_evolvable:
            over_total += 1
            counts["over"] += predicted_evolvable
        else:
            under_total += 1
            counts["under"] += not predicted_evolvable
    total = len(eligible)
    def rate(name: str, denominator: int, definition: str) -> Dict[str, Any]:
        return metric_value(counts[name], denominator, counts[name] / denominator if denominator else None, "failure", denominator, definition)
    return {
        "observable_layer_accuracy": rate("layer", total, "qualified evidence-adjudicated attribution failures"),
        "routing_accuracy": rate("routing", total, "surface plus Skill target where applicable"),
        "target_skill_accuracy": rate("target", skill_total, "gold SKILL routes"),
        "combined_attribution_accuracy": rate("combined", total, "exact status, layer, surface and target"),
        "deterministic_attribution_coverage": rate("deterministic", total, "predictions using deterministic method"),
        "unknown_insufficient_rate": rate("unknown", total, "UNKNOWN or INSUFFICIENT_EVIDENCE predictions"),
        "wrong_surface_rate": rate("wrong_surface", total, "predicted surface differs from gold"),
        "over_evolution_rate": rate("over", over_total, "gold non-evolvable failures"),
        "under_evolution_rate": rate("under", under_total, "gold evolvable failures"),
    }


def audit_matcher(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    allowed_det = {"MATCH", "NO_MATCH"}
    allowed_audit = {"SAME_ISSUE", "DIFFERENT_ISSUE", "UNCERTAIN"}
    for item in records:
        if item.get("deterministic_decision") not in allowed_det or item.get("adjudicated_decision") not in allowed_audit:
            raise BenchmarkValidationError("invalid matcher audit decision")
    determinate = [item for item in records if item["adjudicated_decision"] != "UNCERTAIN"]
    det_matches = [item for item in determinate if item["deterministic_decision"] == "MATCH"]
    det_nonmatches = [item for item in determinate if item["deterministic_decision"] == "NO_MATCH"]
    false_match = sum(item["adjudicated_decision"] == "DIFFERENT_ISSUE" for item in det_matches)
    false_nonmatch = sum(item["adjudicated_decision"] == "SAME_ISSUE" for item in det_nonmatches)
    uncertain = sum(item["adjudicated_decision"] == "UNCERTAIN" for item in records)
    deterministic_tp = sum(item["deterministic_decision"] == "MATCH" for item in records)
    adjudicated_tp = sum(item["adjudicated_decision"] == "SAME_ISSUE" for item in determinate)
    return {
        "false_match_rate": metric_value(false_match, len(det_matches), false_match / len(det_matches) if det_matches else None, "match-decision", len(det_matches), "deterministic MATCH with determinate evidence adjudication"),
        "false_non_match_rate": metric_value(false_nonmatch, len(det_nonmatches), false_nonmatch / len(det_nonmatches) if det_nonmatches else None, "match-decision", len(det_nonmatches), "deterministic NO_MATCH with determinate evidence adjudication"),
        "ambiguity_rate": metric_value(uncertain, len(records), uncertain / len(records) if records else None, "match-decision", len(records), "all matcher audit records"),
        "metric_sensitivity": {
            "deterministic_tp": deterministic_tp, "evidence_adjudicated_tp": adjudicated_tp,
            "tp_delta": adjudicated_tp - deterministic_tp,
            "note": "TP sensitivity only; recompute full benchmark metrics for final impact.",
        },
    }


def summarize_protocol_dry_run(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Aggregate usability signals only; these are never final benchmark metrics."""
    required = (
        "annotation_seconds", "tool_calls", "input_tokens", "output_tokens",
        "technical_agreement", "should_comment_agreement", "quarantined", "missing_context",
    )
    for record in records:
        _required(record, required, "protocol dry-run record")
    count = len(records)
    def ratio(field: str) -> Optional[float]:
        return None if not count else round(sum(bool(item[field]) for item in records) / count, 6)
    return {
        "protocol_dry_run": True,
        "case_count": count,
        "annotation_seconds": sum(float(item["annotation_seconds"]) for item in records),
        "tool_calls": sum(int(item["tool_calls"]) for item in records),
        "input_tokens": None if any(item["input_tokens"] is None for item in records) else sum(int(item["input_tokens"]) for item in records),
        "output_tokens": None if any(item["output_tokens"] is None for item in records) else sum(int(item["output_tokens"]) for item in records),
        "agreement_rate": ratio("technical_agreement"),
        "technical_disagreement_rate": None if not count else round(1 - ratio("technical_agreement"), 6),
        "should_comment_disagreement_rate": None if not count else round(1 - ratio("should_comment_agreement"), 6),
        "quarantine_rate": ratio("quarantined"),
        "missing_context_rate": ratio("missing_context"),
        "final_claim_eligible": False,
    }


@dataclass(frozen=True)
class FinalRunIntent:
    requester: str
    reason: str
    run_id: str

    @classmethod
    def create(cls, requester: str, reason: str, run_id: Optional[str] = None) -> "FinalRunIntent":
        return cls(_nonempty(requester, "requester"), _nonempty(reason, "reason"), run_id or str(uuid.uuid4()))


class FinalEvaluationRunner:
    """Explicit one-way boundary for final-holdout access with append-only audit."""

    def __init__(self, audit_path: str):
        self.audit_path = os.path.abspath(audit_path)

    def run(
        self, intent: FinalRunIntent, manifest: BenchmarkManifest,
        cases: Sequence[Mapping[str, Any]], release_ids: Sequence[str],
        model_identity: Mapping[str, Any], matcher_identity: Mapping[str, Any],
        metric_identity: Mapping[str, Any], evaluator: Callable[[Sequence[Mapping[str, Any]]], Any],
        git_commit: Optional[str] = None,
    ) -> Any:
        manifest.verify(cases, manifest.raw_content_sha256)
        selected = [validate_benchmark_case(case) for case in cases if case.get("split") == "final_holdout"]
        if not selected:
            raise BenchmarkValidationError("explicit final run requires final_holdout cases")
        if any(case.get("protocol_dry_run") for case in selected):
            raise BenchmarkValidationError("protocol dry-run cases cannot enter final evaluation")
        commit = git_commit or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        audit = {
            "schema_version": 1, "event": "FINAL_HOLDOUT_ACCESS",
            "dataset_manifest_sha256": manifest.manifest_sha256,
            "git_commit": commit, "release_ids": list(release_ids),
            "model_identity": dict(model_identity),
            "matcher_identity": dict(matcher_identity),
            "metric_identity": dict(metric_identity),
            "requester": intent.requester, "run_id": intent.run_id,
            "timestamp": utc_now(), "reason": intent.reason,
            "case_count": len(selected),
        }
        os.makedirs(os.path.dirname(self.audit_path), exist_ok=True)
        with open(self.audit_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(audit) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return evaluator(selected)


def build_run_manifest(
    *, git_commit: str, dataset_manifest_sha256: str, arm: str,
    starting_release_id: str, ending_release_id: str,
    model_identity: Mapping[str, Any], runtime_config: Mapping[str, Any],
    matcher_identity: Mapping[str, Any], metric_identity: Mapping[str, Any],
    failure_stream: Sequence[str], evaluation_case_order: Sequence[str], random_seed: int,
) -> Dict[str, Any]:
    body = {
        "schema_version": 1, "git_commit": _nonempty(git_commit, "git_commit"),
        "dataset_manifest_sha256": _nonempty(dataset_manifest_sha256, "dataset_manifest_sha256"),
        "arm": _nonempty(arm, "arm"),
        "starting_release_id": _nonempty(starting_release_id, "starting_release_id"),
        "ending_release_id": _nonempty(ending_release_id, "ending_release_id"),
        "model_identity": dict(model_identity), "runtime_config": dict(runtime_config),
        "matcher_identity": dict(matcher_identity), "metric_identity": dict(metric_identity),
        "failure_stream_identity": sha256_json(list(failure_stream)),
        "failure_stream_order": list(failure_stream), "random_seed": int(random_seed),
        "evaluation_case_order": list(evaluation_case_order),
        "evaluation_case_order_sha256": sha256_json(list(evaluation_case_order)),
    }
    required_runtime = {
        "context_policy", "tool_policy", "token_budget", "time_budget",
        "operational_gate",
    }
    if not required_runtime.issubset(body["runtime_config"]):
        raise BenchmarkValidationError("run manifest is missing frozen runtime budgets/policies")
    if not {"provider", "model", "config_hash"}.issubset(body["model_identity"]):
        raise BenchmarkValidationError("run manifest is missing model/provider/config identity")
    for name in ("matcher_identity", "metric_identity"):
        if not {"version", "sha256"}.issubset(body[name]):
            raise BenchmarkValidationError("run manifest is missing %s version/hash" % name)
    body["run_manifest_sha256"] = sha256_json(body)
    return body


def verify_run_manifest(value: Mapping[str, Any]) -> Dict[str, Any]:
    body = json.loads(canonical_json(value))
    claimed = str(body.pop("run_manifest_sha256", ""))
    if not claimed or sha256_json(body) != claimed:
        raise BenchmarkValidationError("run manifest fingerprint is invalid")
    return json.loads(canonical_json(value))


def write_run_manifest_immutable(path: str, value: Mapping[str, Any]) -> None:
    verified = verify_run_manifest(value)
    rendered = json.dumps(verified, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            if handle.read() != rendered:
                raise BenchmarkValidationError("run manifest is immutable")
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)


def render_reliability_matrix(entries: Sequence[Mapping[str, Any]]) -> str:
    required = ("failure_mode", "mechanism", "test", "fault_point", "observed_invariant", "git_commit", "backend")
    lines = [
        "| Failure Mode | Mechanism | Test | Fault Point | Observed Invariant | Git Commit / Backend |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        _required(entry, required, "reliability entry")
        values = [str(entry[field]).replace("|", "\\|") for field in required]
        lines.append("| %s | %s | `%s` | %s | %s | `%s` / %s |" % tuple(values))
    return "\n".join(lines) + "\n"
