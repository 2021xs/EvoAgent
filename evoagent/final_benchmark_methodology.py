"""Frozen open-world methodology contracts for the final EvoAgent benchmark."""

import hashlib
import json
import random
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .benchmark_governance import (
    BenchmarkValidationError,
    canonical_json,
    metric_value,
    normalize_taxonomy,
    sha256_json,
)
from .evaluation_harness import one_to_one_match
from .models import Finding, Severity


FINAL_METHODOLOGY_VERSION = "open-world-final-v3"
SEEDED_FINDING_SCHEMA_VERSION = 3
PREDICTION_ADJUDICATION_SCHEMA_VERSION = 1
SEED_QUALIFICATION_STATUSES = {
    "QUALIFIED", "REJECTED", "NEEDS_MORE_CONTEXT", "QUARANTINED",
}
PREDICTION_ADJUDICATION_STATUSES = {
    "ADJUDICATED", "NEEDS_MORE_CONTEXT", "QUARANTINED",
}
ADMISSIBLE_SEED_EVIDENCE_TYPES = {
    "REGRESSION_TEST",
    "REPRODUCER",
    "FIX_COMMIT_WITH_MATCHING_EVIDENCE",
    "ISSUE_PLUS_VERIFIED_FIX",
    "SECURITY_ADVISORY",
    "DETERMINISTIC_RUNTIME_FAILURE",
    "OTHER_EXPLICITLY_VERIFIED_OBJECTIVE_EVIDENCE",
}
PRIMARY_METRIC_NAMES = (
    "seeded_issue_recall",
    "high_risk_seeded_issue_recall",
    "adjudicated_precision",
    "false_positives_per_pr",
    "actionable_findings_per_pr",
    "wrong_surface_evolution_rate",
    "regression_rate",
    "evolution_success_rate",
    "total_tokens",
    "llm_calls",
    "latency",
)
FORBIDDEN_FINAL_METRIC_NAMES = {
    "recall", "overall_pr_recall", "exhaustive_recall", "all_bug_recall",
}
FORBIDDEN_MODEL_CONTEXT_KEYS = {
    "seed", "seed_id", "seeded_finding", "seeded_findings",
    "benchmark_construction_evidence", "future_fix_commit",
    "later_regression_test", "later_issue_report", "security_advisory",
    "evidence_source_revision", "verification_provenance",
}
ARMS = (
    "baseline-no-evolution",
    "naive-global-evolution",
    "evidence-backed-targeted-evolution",
)


def _required(value: Mapping[str, Any], fields: Iterable[str], label: str) -> None:
    missing = sorted(field for field in fields if field not in value)
    if missing:
        raise BenchmarkValidationError("%s is missing: %s" % (label, ", ".join(missing)))


def _nonempty(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BenchmarkValidationError(label + " must be non-empty")
    return text


def _git_revision(value: Any, label: str) -> str:
    text = _nonempty(value, label)
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", text):
        raise BenchmarkValidationError(label + " must be an immutable Git revision")
    return text


def validate_seeded_finding(seed: Mapping[str, Any]) -> Dict[str, Any]:
    _required(seed, (
        "schema_version", "methodology_version", "seed_id", "case_id",
        "repository_id", "base_sha", "head_sha", "issue_identity",
        "location", "severity", "should_comment", "technical_proposition",
        "evidence", "qualification_status", "verification_provenance",
        "rationale",
    ), "seeded_finding")
    if int(seed["schema_version"]) != SEEDED_FINDING_SCHEMA_VERSION:
        raise BenchmarkValidationError("SeededFinding schema_version must be 3")
    if seed["methodology_version"] != FINAL_METHODOLOGY_VERSION:
        raise BenchmarkValidationError("SeededFinding methodology version is not final v3")
    for field in ("seed_id", "case_id", "repository_id", "technical_proposition", "rationale"):
        _nonempty(seed[field], "seeded_finding." + field)
    _git_revision(seed["base_sha"], "seeded_finding.base_sha")
    _git_revision(seed["head_sha"], "seeded_finding.head_sha")
    identity = seed["issue_identity"]
    if not isinstance(identity, Mapping):
        raise BenchmarkValidationError("seeded_finding.issue_identity must be an object")
    _required(identity, ("category", "rule_id", "cwe"), "seeded_finding.issue_identity")
    if not any(str(identity.get(field) or "").strip() for field in ("category", "rule_id", "cwe")):
        raise BenchmarkValidationError("SeededFinding requires category, rule, or CWE identity")
    location = seed["location"]
    if not isinstance(location, Mapping):
        raise BenchmarkValidationError("seeded_finding.location must be an object")
    _required(location, ("path", "start_line", "end_line", "supportable"), "seeded_finding.location")
    if location["supportable"] is True:
        _nonempty(location["path"], "seeded_finding.location.path")
        try:
            start, end = int(location["start_line"]), int(location["end_line"])
        except (TypeError, ValueError) as exc:
            raise BenchmarkValidationError("supportable seed location requires integer range") from exc
        if start < 1 or end < start:
            raise BenchmarkValidationError("supportable seed location has invalid range")
    elif any(location.get(field) is not None for field in ("path", "start_line", "end_line")):
        raise BenchmarkValidationError("unsupported seed location must remain null")
    severity = str(seed["severity"]).lower()
    if severity not in {"low", "medium", "high", "critical"}:
        raise BenchmarkValidationError("SeededFinding severity is invalid")
    if not isinstance(seed["should_comment"], bool):
        raise BenchmarkValidationError("SeededFinding should_comment must be explicit")
    evidence = seed["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise BenchmarkValidationError("SeededFinding requires strong evidence")
    for index, item in enumerate(evidence):
        label = "seeded_finding.evidence[%d]" % index
        if not isinstance(item, Mapping):
            raise BenchmarkValidationError(label + " must be an object")
        _required(item, (
            "evidence_type", "evidence_refs", "source_revision",
            "benchmark_construction_only", "supported_proposition",
        ), label)
        if item["evidence_type"] not in ADMISSIBLE_SEED_EVIDENCE_TYPES:
            raise BenchmarkValidationError(label + " is not admissible strong evidence")
        if not isinstance(item["evidence_refs"], list) or not item["evidence_refs"]:
            raise BenchmarkValidationError(label + " requires evidence refs")
        _nonempty(item["source_revision"], label + ".source_revision")
        if item["benchmark_construction_only"] is not True:
            raise BenchmarkValidationError(label + " must be construction-only")
        if str(item["supported_proposition"]).strip() != str(seed["technical_proposition"]).strip():
            raise BenchmarkValidationError(label + " must support the bounded seed proposition")
    status = seed["qualification_status"]
    if status not in SEED_QUALIFICATION_STATUSES:
        raise BenchmarkValidationError("SeededFinding qualification status is invalid")
    verification = seed["verification_provenance"]
    if not isinstance(verification, Mapping):
        raise BenchmarkValidationError("SeededFinding verification provenance is required")
    _required(verification, (
        "verifier_id", "verifier_kind", "run_id", "outcome",
        "evoagent_prediction_visible", "experiment_arm_visible",
        "repository_access_mode", "rationale",
    ), "seeded_finding.verification_provenance")
    for field in ("verifier_id", "verifier_kind", "run_id", "rationale"):
        _nonempty(verification[field], "verification_provenance." + field)
    if verification["outcome"] != status:
        raise BenchmarkValidationError("seed status must equal independent verifier outcome")
    if verification["evoagent_prediction_visible"] is not False or verification["experiment_arm_visible"] is not False:
        raise BenchmarkValidationError("seed verification must be blind to prediction and arm")
    if verification["repository_access_mode"] != "FROZEN_REPOSITORY_READ_ONLY":
        raise BenchmarkValidationError("seed verifier requires frozen read-only repository access")
    return json.loads(canonical_json(seed))


def qualified_recall_seeds(seeds: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    normalized = [validate_seeded_finding(seed) for seed in seeds]
    identifiers = [seed["seed_id"] for seed in normalized]
    if len(identifiers) != len(set(identifiers)):
        raise BenchmarkValidationError("SeededFinding seed_id values must be unique")
    return [seed for seed in normalized if seed["qualification_status"] == "QUALIFIED"]


def _walk_forbidden_context(value: Any, path: str = "model_visible_context") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in FORBIDDEN_MODEL_CONTEXT_KEYS:
                raise BenchmarkValidationError(path + " exposes benchmark-construction evidence via " + str(key))
            _walk_forbidden_context(item, path + "." + str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_forbidden_context(item, "%s[%d]" % (path, index))


def validate_model_visible_evaluation_context(context: Mapping[str, Any]) -> Dict[str, Any]:
    _required(context, (
        "case_id", "repository_id", "base_sha", "head_sha",
        "frozen_repository", "runtime_context",
    ), "model_visible_context")
    allowed = {"case_id", "repository_id", "base_sha", "head_sha", "frozen_repository", "runtime_context"}
    extras = set(context).difference(allowed)
    if extras:
        raise BenchmarkValidationError("model-visible evaluation context has unapproved fields: %s" % ", ".join(sorted(extras)))
    _git_revision(context["base_sha"], "model_visible_context.base_sha")
    _git_revision(context["head_sha"], "model_visible_context.head_sha")
    repository = context["frozen_repository"]
    if not isinstance(repository, Mapping):
        raise BenchmarkValidationError("frozen_repository must be an object")
    _required(repository, ("snapshot_id", "access_mode"), "frozen_repository")
    if repository["access_mode"] != "FROZEN_REPOSITORY_READ_ONLY":
        raise BenchmarkValidationError("evaluation repository must be frozen and read-only")
    _walk_forbidden_context(context)
    return json.loads(canonical_json(context))


def _prediction_identity(prediction: Mapping[str, Any]) -> Optional[Tuple[Any, ...]]:
    finding = prediction.get("finding") or {}
    path = str(finding.get("path") or "").replace("\\", "/").removeprefix("a/").removeprefix("b/")
    try:
        start = int(finding.get("start_line", finding.get("line")))
        end = int(finding.get("end_line", start))
    except (TypeError, ValueError):
        return None
    taxonomy = normalize_taxonomy(finding.get("cwe"), finding.get("category"))
    identity = taxonomy["canonical_identity"] or str(finding.get("rule_id") or "").strip()
    if not path or not identity or start < 1 or end < start:
        return None
    return str(prediction["case_id"]), path, start, end, identity


def build_arm_blind_prediction_pool(
    predictions: Sequence[Mapping[str, Any]], random_seed: int,
) -> Dict[str, Any]:
    normalized = []
    prediction_ids = set()
    for index, prediction in enumerate(predictions):
        label = "prediction[%d]" % index
        _required(prediction, (
            "prediction_id", "case_id", "repository_id", "arm",
            "release_id", "finding",
        ), label)
        prediction_id = _nonempty(prediction["prediction_id"], label + ".prediction_id")
        if prediction_id in prediction_ids:
            raise BenchmarkValidationError("prediction_id values must be unique")
        prediction_ids.add(prediction_id)
        if prediction["arm"] not in ARMS:
            raise BenchmarkValidationError(label + " has unknown experimental arm")
        if not isinstance(prediction["finding"], Mapping):
            raise BenchmarkValidationError(label + ".finding must be an object")
        normalized.append(dict(prediction))
    groups: Dict[Tuple[Any, ...], List[Mapping[str, Any]]] = {}
    ambiguous = []
    for prediction in normalized:
        identity = _prediction_identity(prediction)
        if identity is None:
            identity = ("AMBIGUOUS", prediction["prediction_id"])
            ambiguous.append(prediction["prediction_id"])
        groups.setdefault(identity, []).append(prediction)
    entries = []
    prediction_to_pool = {}
    for ordinal, (identity, members) in enumerate(sorted(groups.items(), key=lambda item: str(item[0])), 1):
        first = members[0]
        pool_id = "pool-%s-%04d" % (hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:12], ordinal)
        entry = {
            "pool_id": pool_id,
            "case_id": first["case_id"],
            "repository_id": first["repository_id"],
            "finding": json.loads(canonical_json(first["finding"])),
            "source_prediction_ids": [member["prediction_id"] for member in members],
            "source_arms": [member["arm"] for member in members],
            "deterministic_equivalence": identity[0] != "AMBIGUOUS",
        }
        entries.append(entry)
        for member in members:
            prediction_to_pool[member["prediction_id"]] = pool_id
    rng = random.Random(int(random_seed))
    rng.shuffle(entries)
    adjudicator_view = [{
        "pool_id": item["pool_id"],
        "case_id": item["case_id"],
        "repository_id": item["repository_id"],
        "finding": item["finding"],
    } for item in entries]
    counts = {}
    for arm in ARMS:
        arm_predictions = [item for item in normalized if item["arm"] == arm]
        logical = {prediction_to_pool[item["prediction_id"]] for item in arm_predictions}
        counts[arm] = {
            "raw_prediction_count": len(arm_predictions),
            "logical_prediction_count": len(logical),
            "duplicate_prediction_count": len(arm_predictions) - len(logical),
        }
    return {
        "schema_version": 1,
        "methodology_version": FINAL_METHODOLOGY_VERSION,
        "random_seed": int(random_seed),
        "entries": entries,
        "adjudicator_view": adjudicator_view,
        "prediction_to_pool": prediction_to_pool,
        "ambiguous_equivalence_prediction_ids": ambiguous,
        "arm_counts": counts,
    }


def validate_prediction_adjudication(record: Mapping[str, Any]) -> Dict[str, Any]:
    _required(record, (
        "schema_version", "methodology_version", "pool_id", "case_id",
        "repository_id", "status", "issue_exists", "should_comment",
        "severity", "location_supported", "technical_evidence",
        "technical_rationale", "comment_usefulness_rationale", "provenance",
    ), "prediction_adjudication")
    if int(record["schema_version"]) != PREDICTION_ADJUDICATION_SCHEMA_VERSION:
        raise BenchmarkValidationError("prediction adjudication schema_version must be 1")
    if record["methodology_version"] != FINAL_METHODOLOGY_VERSION:
        raise BenchmarkValidationError("prediction adjudication methodology version mismatch")
    status = record["status"]
    if status not in PREDICTION_ADJUDICATION_STATUSES:
        raise BenchmarkValidationError("prediction adjudication status is invalid")
    provenance = record["provenance"]
    if not isinstance(provenance, Mapping):
        raise BenchmarkValidationError("prediction adjudication provenance is required")
    _required(provenance, (
        "adjudicator_id", "run_id", "repository_access_mode",
        "experiment_arm_visible", "release_identity_visible",
        "peer_arm_emissions_visible", "desired_metric_direction_visible",
    ), "prediction_adjudication.provenance")
    if provenance["repository_access_mode"] != "FROZEN_REPOSITORY_READ_ONLY":
        raise BenchmarkValidationError("prediction adjudicator requires frozen read-only repository access")
    blind_fields = (
        "experiment_arm_visible", "release_identity_visible",
        "peer_arm_emissions_visible", "desired_metric_direction_visible",
    )
    if any(provenance[field] is not False for field in blind_fields):
        raise BenchmarkValidationError("prediction adjudicator must be arm and metric blind")
    if status == "ADJUDICATED":
        if not isinstance(record["issue_exists"], bool) or not isinstance(record["should_comment"], bool):
            raise BenchmarkValidationError("adjudicated prediction requires explicit boolean decisions")
        if record["should_comment"] and not record["issue_exists"]:
            raise BenchmarkValidationError("nonexistent issue cannot be commentable")
        if not isinstance(record["location_supported"], bool):
            raise BenchmarkValidationError("adjudicated prediction requires location support decision")
        if str(record["severity"] or "").lower() not in {"low", "medium", "high", "critical"}:
            raise BenchmarkValidationError("adjudicated prediction requires valid severity")
        if record["issue_exists"] and (not isinstance(record["technical_evidence"], list) or not record["technical_evidence"]):
            raise BenchmarkValidationError("valid prediction requires repository-grounded evidence")
        _nonempty(record["technical_rationale"], "prediction_adjudication.technical_rationale")
        _nonempty(record["comment_usefulness_rationale"], "prediction_adjudication.comment_usefulness_rationale")
    elif any(record[field] is not None for field in ("issue_exists", "should_comment", "severity", "location_supported")):
        raise BenchmarkValidationError("unresolved prediction adjudication must not force binary labels")
    return json.loads(canonical_json(record))


def validate_qualified_clean_case(record: Mapping[str, Any]) -> Dict[str, Any]:
    _required(record, (
        "case_id", "repository_id", "qualification_status",
        "independent_reviews", "unresolved_disagreement",
        "known_historical_seed_count", "later_contradictory_evidence",
        "completed_review_rationale",
    ), "qualified_clean_case")
    reviews = record["independent_reviews"]
    if not isinstance(reviews, list) or len(reviews) < 2:
        raise BenchmarkValidationError("clean subset requires independent blind A/B review")
    run_ids = {str(review.get("run_id") or "") for review in reviews}
    if len(run_ids) < 2 or any(
        review.get("experiment_arm_visible") is not False
        or review.get("evoagent_prediction_visible") is not False
        or review.get("completed_review") is not True
        for review in reviews
    ):
        raise BenchmarkValidationError("clean subset reviews must be independent, complete, and blind")
    qualified = record["qualification_status"] == "QUALIFIED_CLEAN"
    if qualified and (
        record["unresolved_disagreement"] is not False
        or int(record["known_historical_seed_count"]) != 0
        or record["later_contradictory_evidence"] is not False
    ):
        raise BenchmarkValidationError("clean subset qualification has unresolved contrary evidence")
    _nonempty(record["completed_review_rationale"], "qualified_clean_case.completed_review_rationale")
    return json.loads(canonical_json(record))


def _as_finding(value: Mapping[str, Any]) -> Finding:
    try:
        severity = Severity(str(value.get("severity") or "medium").lower())
    except ValueError:
        severity = Severity.MEDIUM
    return Finding(
        rule_id=str(value.get("rule_id") or "BENCHMARK-PREDICTION"),
        severity=severity, title=str(value.get("title") or "prediction"),
        explanation=str(value.get("explanation") or "prediction"),
        path=str(value.get("path") or ""),
        line=int(value.get("line", value.get("start_line"))),
        evidence=str(value.get("evidence") or ""), fix=str(value.get("fix") or ""),
        test=str(value.get("test") or ""), confidence=float(value.get("confidence", 1.0)),
        cwe=str(value.get("cwe") or "") or None,
    )


def match_seeded_findings(
    seeds: Sequence[Mapping[str, Any]], pool_entries: Sequence[Mapping[str, Any]],
    line_tolerance: int = 2,
) -> Dict[str, Any]:
    qualified = qualified_recall_seeds(seeds)
    by_case_seeds: Dict[str, List[Mapping[str, Any]]] = {}
    by_case_predictions: Dict[str, List[Mapping[str, Any]]] = {}
    for seed in qualified:
        if seed["location"]["supportable"]:
            by_case_seeds.setdefault(seed["case_id"], []).append(seed)
    for entry in pool_entries:
        by_case_predictions.setdefault(str(entry["case_id"]), []).append(entry)
    matched = []
    ambiguous = []
    for case_id, case_seeds in by_case_seeds.items():
        entries = by_case_predictions.get(case_id, [])
        expected = [{
            "path": seed["location"]["path"],
            "start_line": seed["location"]["start_line"],
            "end_line": seed["location"]["end_line"],
            "rule_id": seed["issue_identity"].get("rule_id"),
            "cwe": seed["issue_identity"].get("cwe"),
        } for seed in case_seeds]
        predictions = [_as_finding(entry["finding"]) for entry in entries]
        edges = []
        for seed_index, truth in enumerate(expected):
            for prediction_index, prediction in enumerate(predictions):
                if one_to_one_match([truth], [prediction], line_tolerance):
                    edges.append((seed_index, prediction_index))
        ambiguous_seed = {index for index in range(len(expected)) if sum(edge[0] == index for edge in edges) > 1}
        ambiguous_prediction = {index for index in range(len(predictions)) if sum(edge[1] == index for edge in edges) > 1}
        for seed_index, prediction_index in edges:
            if seed_index in ambiguous_seed or prediction_index in ambiguous_prediction:
                ambiguous.append({
                    "case_id": case_id, "seed_id": case_seeds[seed_index]["seed_id"],
                    "pool_id": entries[prediction_index]["pool_id"], "status": "AMBIGUOUS",
                })
            else:
                matched.append({
                    "case_id": case_id, "seed_id": case_seeds[seed_index]["seed_id"],
                    "pool_id": entries[prediction_index]["pool_id"], "status": "MATCH",
                })
    return {"matches": matched, "ambiguous": ambiguous}


def score_open_world_arm(
    *, arm: str, cases: Sequence[Mapping[str, Any]], seeds: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]], pool: Mapping[str, Any],
    adjudications: Sequence[Mapping[str, Any]], seed_matches: Optional[Mapping[str, Any]] = None,
    clean_cases: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    if arm not in ARMS:
        raise BenchmarkValidationError("unknown benchmark arm")
    case_map = {str(case["case_id"]): case for case in cases}
    qualified = qualified_recall_seeds(seeds)
    arm_predictions = [prediction for prediction in predictions if prediction["arm"] == arm]
    arm_pool_ids = {pool["prediction_to_pool"][prediction["prediction_id"]] for prediction in arm_predictions}
    matches = seed_matches or match_seeded_findings(qualified, pool["entries"])
    matched_seed_ids = {
        item["seed_id"] for item in matches["matches"] if item["pool_id"] in arm_pool_ids
    }
    seeded_tp = sum(seed["seed_id"] in matched_seed_ids for seed in qualified)
    high_seeds = [seed for seed in qualified if seed["severity"] in {"high", "critical"}]
    high_tp = sum(seed["seed_id"] in matched_seed_ids for seed in high_seeds)
    adjudication_map = {}
    for raw in adjudications:
        adjudication = validate_prediction_adjudication(raw)
        if adjudication["pool_id"] in adjudication_map:
            raise BenchmarkValidationError("one shared pool prediction may be adjudicated only once")
        adjudication_map[adjudication["pool_id"]] = adjudication
    missing = arm_pool_ids.difference(adjudication_map)
    if missing:
        raise BenchmarkValidationError("all logical predictions require shared adjudication records")
    eligible = [adjudication_map[pool_id] for pool_id in arm_pool_ids if adjudication_map[pool_id]["status"] == "ADJUDICATED"]
    valid = [item for item in eligible if item["issue_exists"] and item["should_comment"]]
    false_positives = len(eligible) - len(valid)
    high_valid = [item for item in eligible if item["severity"] in {"high", "critical"}]
    high_valid_tp = sum(item["issue_exists"] and item["should_comment"] for item in high_valid)
    pr_count = len(case_map)
    clean = [validate_qualified_clean_case(item) for item in clean_cases]
    qualified_clean = [item for item in clean if item["qualification_status"] == "QUALIFIED_CLEAN"]
    clean_hits = sum(
        not any(prediction["case_id"] == item["case_id"] for prediction in arm_predictions)
        for item in qualified_clean
    )
    metrics = {
        "seeded_issue_recall": metric_value(
            seeded_tp, len(qualified), seeded_tp / len(qualified) if qualified else None,
            "seeded_finding", len(qualified), "all QUALIFIED evidence-seeded findings",
        ),
        "high_risk_seeded_issue_recall": metric_value(
            high_tp, len(high_seeds), high_tp / len(high_seeds) if high_seeds else None,
            "seeded_finding", len(high_seeds), "eligible high or critical qualified seeds",
        ),
        "adjudicated_precision": metric_value(
            len(valid), len(eligible), len(valid) / len(eligible) if eligible else None,
            "logical_prediction", len(eligible), "blind-adjudicated issue_exists=true and should_comment=true predictions",
        ),
        "false_positives_per_pr": metric_value(
            false_positives, pr_count, false_positives / pr_count if pr_count else None,
            "pull_request", pr_count, "invalid or non-commentable eligible logical predictions per PR",
        ),
        "actionable_findings_per_pr": metric_value(
            len(valid), pr_count, len(valid) / pr_count if pr_count else None,
            "pull_request", pr_count, "valid commentable logical predictions per PR",
        ),
        "high_risk_valid_prediction_rate": metric_value(
            high_valid_tp, len(high_valid), high_valid_tp / len(high_valid) if high_valid else None,
            "logical_prediction", len(high_valid), "adjudicated high or critical predictions",
        ),
        "qualified_clean_pr_accuracy": metric_value(
            clean_hits, len(qualified_clean), clean_hits / len(qualified_clean) if qualified_clean else None,
            "pull_request", len(qualified_clean), "explicitly qualified clean subset only",
        ),
    }
    repository_ids = {str(case["repository_id"]) for case in cases}
    counts = dict(pool["arm_counts"][arm])
    counts.update({
        "eligible_seed_count": len(qualified),
        "repository_count": len(repository_ids),
        "pr_count": pr_count,
        "adjudicated_prediction_count": len(eligible),
        "quarantined_prediction_count": len(arm_pool_ids) - len(eligible),
        "matcher_ambiguity_count": len(matches["ambiguous"]),
    })
    for name in ("seeded_issue_recall", "high_risk_seeded_issue_recall"):
        metrics[name]["eligible_seed_count"] = metrics[name]["denominator"]
        metrics[name]["repository_count"] = len(repository_ids)
    return {
        "contract_version": FINAL_METHODOLOGY_VERSION,
        "arm": arm, "metrics": metrics, "counts": counts,
    }


def validate_supplementary_review_findings(
    findings: Sequence[Mapping[str, Any]], frozen_seed_ids: Sequence[str],
) -> Dict[str, Any]:
    frozen = set(frozen_seed_ids)
    for finding in findings:
        _required(finding, (
            "supplementary_finding_id", "case_id", "repository_id",
            "source_review_run_id", "repository_grounded",
            "evoagent_prediction_visible", "experiment_arm_visible",
        ), "supplementary_finding")
        if finding.get("seed_id") in frozen:
            raise BenchmarkValidationError("supplementary findings remain separate from frozen seeded denominator")
        if finding["repository_grounded"] is not True:
            raise BenchmarkValidationError("supplementary finding must be repository-grounded")
        if finding["evoagent_prediction_visible"] is not False or finding["experiment_arm_visible"] is not False:
            raise BenchmarkValidationError("supplementary review must remain blind")
    return {
        "supplementary_finding_count": len(findings),
        "frozen_seed_count": len(frozen),
        "recall_denominator_changed": False,
    }


def validate_three_arm_fairness_contract(
    manifests: Mapping[str, Mapping[str, Any]], isolated_store_ids: Mapping[str, str],
) -> Dict[str, Any]:
    if set(manifests) != set(ARMS) or set(isolated_store_ids) != set(ARMS):
        raise BenchmarkValidationError("three-arm fairness contract requires exactly A/B/C")
    if len(set(isolated_store_ids.values())) != len(ARMS):
        raise BenchmarkValidationError("each arm requires isolated Store/runtime state")
    comparable_fields = (
        "git_commit", "dataset_manifest_sha256", "starting_release_id",
        "model_identity", "runtime_config", "matcher_identity",
        "metric_identity", "failure_stream_identity", "failure_stream_order",
        "evaluation_case_order", "evaluation_case_order_sha256", "random_seed",
        "experiment_mode",
    )
    reference = manifests[ARMS[0]]
    if reference.get("experiment_mode") != "CONTROLLED_SHARED_FAILURE_STREAM":
        raise BenchmarkValidationError("primary experiment requires controlled shared failure stream")
    for arm in ARMS:
        manifest = manifests[arm]
        if manifest.get("arm") != arm:
            raise BenchmarkValidationError("run manifest arm identity mismatch")
        for field in comparable_fields:
            if manifest.get(field) != reference.get(field):
                raise BenchmarkValidationError("three-arm fairness mismatch: " + field)
    return {
        "experiment_mode": "CONTROLLED_SHARED_FAILURE_STREAM",
        "failure_stream_identity": reference["failure_stream_identity"],
        "failure_stream_order": list(reference["failure_stream_order"]),
        "isolated_store_count": len(set(isolated_store_ids.values())),
        "comparable": True,
    }


def score_evolution_outcomes(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    eligible = [record for record in records if record.get("adjudicated_opportunity") is True]
    candidates = [record for record in eligible if record.get("candidate_generated") is True]
    wrong_surface = sum(record.get("routing_correct") is False for record in eligible)
    regressions = sum(record.get("protected_regression") is True for record in candidates)
    successes = sum(
        record.get("source_failure_improved") is True
        and record.get("protected_regression") is False
        for record in eligible
    )
    promotions = sum(record.get("promoted") is True for record in candidates)
    duplicates = sum(record.get("duplicate_candidate") is True for record in candidates)
    return {
        "wrong_surface_evolution_rate": metric_value(
            wrong_surface, len(eligible), wrong_surface / len(eligible) if eligible else None,
            "evolution_opportunity", len(eligible), "adjudicated attribution/evolution opportunities",
        ),
        "evolution_success_rate": metric_value(
            successes, len(eligible), successes / len(eligible) if eligible else None,
            "evolution_opportunity", len(eligible), "source failure improved without protected regression",
        ),
        "regression_rate": metric_value(
            regressions, len(candidates), regressions / len(candidates) if candidates else None,
            "candidate", len(candidates), "generated candidates evaluated against protected cases",
        ),
        "promotion_rate": metric_value(
            promotions, len(candidates), promotions / len(candidates) if candidates else None,
            "candidate", len(candidates), "generated candidates",
        ),
        "duplicate_candidate_rate": metric_value(
            duplicates, len(candidates), duplicates / len(candidates) if candidates else None,
            "candidate", len(candidates), "generated candidate attempts",
        ),
    }


def aggregate_runtime_cost(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    fields = (
        "input_tokens", "output_tokens", "llm_calls", "wall_latency_ms",
        "provider_cost_usd", "tool_calls", "files_inspected",
    )
    result = {}
    for field in fields:
        values = [record.get(field) for record in records]
        missing = sum(value is None for value in values)
        result[field] = {
            "value": None if missing else sum(values),
            "reported_count": len(values) - missing,
            "missing_count": missing, "aggregation": "sum",
        }
    result["total_tokens"] = {
        "value": None if result["input_tokens"]["value"] is None or result["output_tokens"]["value"] is None
        else result["input_tokens"]["value"] + result["output_tokens"]["value"],
        "aggregation": "sum",
    }
    return result


def create_final_holdout_freeze_manifest(
    *, cases: Sequence[Mapping[str, Any]], seeds: Sequence[Mapping[str, Any]],
    repository_split: Mapping[str, Any], prediction_adjudication_protocol: Mapping[str, Any],
    matcher: Mapping[str, Any], metrics: Mapping[str, Any],
    three_arm_configuration: Mapping[str, Any], failure_stream: Sequence[Mapping[str, Any]],
    run_manifest_template: Mapping[str, Any],
) -> Dict[str, Any]:
    qualified_recall_seeds(seeds)
    components = {
        "case_set_sha256": sha256_json(list(cases)),
        "seed_set_sha256": sha256_json(list(seeds)),
        "repository_split_sha256": sha256_json(dict(repository_split)),
        "prediction_adjudication_protocol_sha256": sha256_json(dict(prediction_adjudication_protocol)),
        "matcher_sha256": sha256_json(dict(matcher)),
        "metrics_sha256": sha256_json(dict(metrics)),
        "three_arm_configuration_sha256": sha256_json(dict(three_arm_configuration)),
        "failure_stream_sha256": sha256_json(list(failure_stream)),
        "run_manifest_template_sha256": sha256_json(dict(run_manifest_template)),
    }
    body = {
        "schema_version": 1,
        "methodology_version": FINAL_METHODOLOGY_VERSION,
        "freeze_components": components,
        "post_open_change_policy": "INVALIDATE_RUN_AND_CREATE_NEW_BENCHMARK_VERSION",
    }
    body["freeze_manifest_sha256"] = sha256_json(body)
    return body


def enforce_frozen_recall_denominator(
    freeze_manifest: Mapping[str, Any], seeds: Sequence[Mapping[str, Any]],
) -> None:
    expected = freeze_manifest.get("freeze_components", {}).get("seed_set_sha256")
    if expected != sha256_json(list(seeds)):
        raise BenchmarkValidationError("post-freeze issue cannot change the frozen Recall denominator")


def verify_final_holdout_freeze_manifest(
    freeze_manifest: Mapping[str, Any], *, cases: Sequence[Mapping[str, Any]],
    seeds: Sequence[Mapping[str, Any]], repository_split: Mapping[str, Any],
    prediction_adjudication_protocol: Mapping[str, Any], matcher: Mapping[str, Any],
    metrics: Mapping[str, Any], three_arm_configuration: Mapping[str, Any],
    failure_stream: Sequence[Mapping[str, Any]], run_manifest_template: Mapping[str, Any],
) -> Dict[str, Any]:
    recreated = create_final_holdout_freeze_manifest(
        cases=cases, seeds=seeds, repository_split=repository_split,
        prediction_adjudication_protocol=prediction_adjudication_protocol,
        matcher=matcher, metrics=metrics,
        three_arm_configuration=three_arm_configuration,
        failure_stream=failure_stream, run_manifest_template=run_manifest_template,
    )
    if json.loads(canonical_json(freeze_manifest)) != recreated:
        raise BenchmarkValidationError(
            "final holdout configuration changed; invalidate run and create a new benchmark version"
        )
    return recreated


def validate_final_report_schema(report: Mapping[str, Any]) -> Dict[str, Any]:
    _required(report, (
        "methodology_version", "primary_metrics", "claim",
        "annotator_disclosure", "statistical_protocol",
    ), "final_report")
    if report["methodology_version"] != FINAL_METHODOLOGY_VERSION:
        raise BenchmarkValidationError("final report methodology version mismatch")
    metrics = report["primary_metrics"]
    if not isinstance(metrics, Mapping):
        raise BenchmarkValidationError("final report primary_metrics must be an object")
    forbidden = FORBIDDEN_FINAL_METRIC_NAMES.intersection(metrics)
    if forbidden:
        raise BenchmarkValidationError("old exhaustive Recall terminology is forbidden: %s" % ", ".join(sorted(forbidden)))
    missing = set(PRIMARY_METRIC_NAMES).difference(metrics)
    if missing:
        raise BenchmarkValidationError("final report is missing frozen primary metrics: %s" % ", ".join(sorted(missing)))
    claim = str(report["claim"]).lower()
    _nonempty(report["annotator_disclosure"], "final_report.annotator_disclosure")
    for forbidden_claim in ("exhaustively labelled", "exhaustive pr recall", "human-labelled benchmark"):
        if forbidden_claim in claim:
            raise BenchmarkValidationError("final claim exceeds open-world evidence")
    protocol = report["statistical_protocol"]
    _required(protocol, ("method", "iterations", "seed", "confidence_interval"), "statistical_protocol")
    if protocol["method"] != "repository-cluster-paired-bootstrap" or int(protocol["iterations"]) != 10000:
        raise BenchmarkValidationError("final report requires 10,000-iteration repository-cluster paired bootstrap")
    return json.loads(canonical_json(report))


def _ratio_from_records(records: Sequence[Mapping[str, Any]], metric: str) -> Optional[float]:
    numerator = sum(float(record.get(metric + "_numerator", 0)) for record in records)
    denominator = sum(float(record.get(metric + "_denominator", 0)) for record in records)
    return numerator / denominator if denominator else None


def repository_cluster_open_world_bootstrap(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]], metric: str,
    iterations: int = 10000, seed: int = 20260819,
) -> Dict[str, Any]:
    left_repositories = {str(item["repository"]) for item in left}
    right_repositories = {str(item["repository"]) for item in right}
    if left_repositories != right_repositories:
        raise BenchmarkValidationError("paired arms require identical repository clusters")
    if iterations < 1:
        raise BenchmarkValidationError("bootstrap iterations must be positive")
    repositories = sorted(left_repositories)
    left_point = _ratio_from_records(left, metric)
    right_point = _ratio_from_records(right, metric)
    rng = random.Random(int(seed))
    deltas = []
    for _ in range(int(iterations)):
        selected = [repositories[rng.randrange(len(repositories))] for _ in repositories] if repositories else []
        sampled_left = [record for repository in selected for record in left if str(record["repository"]) == repository]
        sampled_right = [record for repository in selected for record in right if str(record["repository"]) == repository]
        left_value = _ratio_from_records(sampled_left, metric)
        right_value = _ratio_from_records(sampled_right, metric)
        if left_value is not None and right_value is not None:
            deltas.append(right_value - left_value)
    deltas.sort()
    ci = [None, None] if not deltas else [
        deltas[int((len(deltas) - 1) * .025)],
        deltas[int((len(deltas) - 1) * .975)],
    ]
    return {
        "metric": metric, "left_point_estimate": left_point,
        "right_point_estimate": right_point,
        "paired_delta": None if left_point is None or right_point is None else right_point - left_point,
        "ci95": ci, "repository_count": len(repositories),
        "pr_count": len({(item["repository"], item.get("case_id")) for item in left}),
        "seeded_finding_count": int(sum(item.get("seeded_issue_recall_denominator", 0) for item in left)),
        "prediction_count": {
            "left": int(sum(item.get("adjudicated_precision_denominator", 0) for item in left)),
            "right": int(sum(item.get("adjudicated_precision_denominator", 0) for item in right)),
        },
        "iterations": int(iterations), "seed": int(seed),
        "sampling_unit": "repository-cluster",
    }
