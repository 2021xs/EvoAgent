"""Compare naive and evidence-targeted Skill evolution routing on controlled cases."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from typing import Any, Dict, Iterable, Optional


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.config import Settings  # noqa: E402
from evoagent.evaluation_harness import one_to_one_match  # noqa: E402
from evoagent.service import ReviewService  # noqa: E402
from evoagent.skill_evolution import (  # noqa: E402
    AgentSkillReplayReviewer,
    SkillPatchGenerator,
    validate_artifact,
)


ROOT_CAUSES = {
    "SKILL_GUIDANCE_GAP",
    "CONTEXT_EVIDENCE_MISSING",
    "MODEL_REASONING_FAILURE",
    "INSUFFICIENT_EVIDENCE",
}
DOWNSTREAM_CONTRACT = {
    "candidate_count": 1,
    "generator": "SkillPatchGenerator",
    "source_replay": "SkillEvolutionEngine._source_failure_evidence",
    "evaluation": "SkillEvolutionEngine._propose",
    "activation_policy": "ready_for_promotion",
}
DOWNSTREAM_CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(DOWNSTREAM_CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def load_cases(path: str) -> list:
    values = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            required = {
                "case_id", "repository", "worker", "assignment_skill", "diff",
                "expected_finding", "gold_first_divergence", "gold_root_cause",
                "gold_should_evolve", "control",
            }
            missing = required.difference(value)
            if missing:
                raise ValueError(
                    "case line %d is missing: %s" % (line_number, ", ".join(sorted(missing)))
                )
            if value["gold_root_cause"] not in ROOT_CAUSES:
                raise ValueError("unsupported gold root cause on line %d" % line_number)
            values.append(value)
    identifiers = [str(item["case_id"]) for item in values]
    if len(values) != 12 or len(set(identifiers)) != 12:
        raise ValueError("pilot requires exactly twelve uniquely identified cases")
    distribution = {
        cause: sum(item["gold_root_cause"] == cause for item in values)
        for cause in ROOT_CAUSES
    }
    expected = {
        "SKILL_GUIDANCE_GAP": 4,
        "CONTEXT_EVIDENCE_MISSING": 3,
        "MODEL_REASONING_FAILURE": 3,
        "INSUFFICIENT_EVIDENCE": 2,
    }
    if distribution != expected:
        raise ValueError("unexpected pilot root-cause distribution: %r" % distribution)
    skill_gaps = [item for item in values if item["gold_should_evolve"]]
    targets = [item.get("gold_target_skill") for item in skill_gaps]
    if targets.count("security-review") < 2 or targets.count("reliability-review") < 2:
        raise ValueError("Skill-gap fixtures require two cases for each bundled Skill")
    return values


def _settings(db_path: str, real_model: bool) -> Settings:
    if real_model:
        base = Settings.from_env()
        if not base.resolved_llm():
            raise RuntimeError("real-model pilot requires a configured EvoAgent model")
        return replace(
            base,
            db_path=db_path,
            database_url="",
            redis_url="",
            skills_dir=os.path.join(ROOT, "skills"),
            eval_min_cases=1,
            eval_max_cases=max(20, base.eval_max_cases),
            eval_min_holdout_cases=1,
            memory_enabled=False,
        )
    return Settings(
        host="127.0.0.1",
        port=8080,
        db_path=db_path,
        max_diff_bytes=1024 * 1024,
        max_steps=8,
        timeout_seconds=30,
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        github_webhook_secret="",
        github_token="",
        auto_post_review=False,
        skills_dir=os.path.join(ROOT, "skills"),
        eval_min_cases=1,
        eval_max_cases=20,
        eval_min_improvement=.01,
        eval_min_holdout_cases=1,
        memory_enabled=False,
        agent_token_budget=8000,
        agent_time_budget_seconds=30,
    )


class PilotProtocolClient:
    """Scripted protocol fixture; its classifications are frozen, not inferred."""

    provider = "scripted-pilot"
    model = "targeted-evolution-protocol-v1"

    def __init__(self, cases: Iterable[dict]):
        self.cases = list(cases)
        self.by_rule = {
            str(item["expected_finding"]["rule_id"]): item for item in self.cases
        }
        self.calls = []

    def _case_from_text(self, value: str) -> Optional[dict]:
        for case in self.cases:
            if (
                "PILOT_CASE:%s" % case["case_id"] in value
                or "PILOT_HOLDOUT:%s" % case["case_id"] in value
                or str(case["expected_finding"]["rule_id"]) in value
            ):
                return case
        return None

    def _record(self, role: str, ledger) -> None:
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        self.calls.append({
            "role": role,
            "input_tokens": 10,
            "output_tokens": 5,
            "latency_ms": 1,
        })
        if ledger:
            ledger.record_model(role, self.provider, self.model, usage, 1)

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        self._record(role, ledger)
        case = self._case_from_text(system + "\n" + user)
        if role == "attribution":
            evidence = json.loads(user)
            rule_id = str((evidence.get("expected_finding") or {}).get("rule_id") or "")
            case = self.by_rule.get(rule_id)
            if not case:
                return {
                    "action": "final",
                    "root_cause": "INSUFFICIENT_EVIDENCE",
                    "reason": "No frozen pilot control matched the supplied evidence.",
                    "evidence_summary": [],
                }
            return {
                "action": "final",
                "root_cause": case["gold_root_cause"],
                "reason": "Deterministic protocol response from the frozen pilot control.",
                "evidence_summary": [str(case["control"]["historical_guidance"])[:300]],
            }
        if role == "skill-evolution":
            payload = json.loads(user)
            expected = payload.get("expected_finding") or {}
            case = self.by_rule.get(str(expected.get("rule_id") or ""))
            if not case:
                raise ValueError("pilot generator could not associate the expected finding")
            current = payload["current_skill"]["files"]["SKILL.md"]
            block = "\n\n".join([
                "## Pilot guidance: %s" % expected["title"],
                "<!-- PILOT_GUIDANCE:%s -->" % case["case_id"],
                str(case["control"]["patch_guidance"]),
            ])
            return {
                "action": "final",
                "skill_md": current.rstrip() + "\n\n" + block + "\n",
                "reason": "Generate one frozen, case-local pilot guidance change.",
            }

        managed = json.loads(user)
        task = json.loads(managed["task"])
        if role == "lead":
            phase = task["phase"]
            if phase == "delegate":
                if case:
                    worker = case["worker"]
                    skill_name = case["assignment_skill"]
                    files = [case["expected_finding"]["path"]]
                    assignment_id = "%s-assignment" % case["case_id"]
                else:
                    available = task.get("available_agent_skills") or []
                    skill_name = str((available[0] if available else {}).get("name") or "")
                    worker = (
                        "security" if skill_name == "security-review"
                        else "correctness-reliability"
                    )
                    files = list(task.get("changed_files") or [])
                    assignment_id = "pilot-regression-assignment"
                return {
                    "action": "final",
                    "risk_level": "normal",
                    "delegations": [{
                        "assignment_id": assignment_id,
                        "worker": worker,
                        "objective": "Review the controlled pilot change.",
                        "files": files,
                        "risk_domains": ["controlled-pilot"],
                        "skills": [skill_name] if skill_name else [],
                    }],
                }
            if phase == "assess-workers":
                return {
                    "action": "final",
                    "revision_requests": [],
                    "critic_objective": "Check every controlled candidate.",
                }
            if phase == "finalize":
                return {
                    "action": "final",
                    "accepted_finding_indices": list(
                        range(len(task.get("candidate_findings") or []))
                    ),
                    "confidence_adjustments": [],
                }

        if role in {"security", "correctness-reliability"}:
            if not case:
                return {"action": "final", "findings": []}
            has_guidance = "PILOT_GUIDANCE:%s" % case["case_id"] in system
            if not has_guidance or case["gold_root_cause"] != "SKILL_GUIDANCE_GAP":
                return {"action": "final", "findings": []}
            if "PILOT_HOLDOUT:%s" % case["case_id"] in user:
                if case["control"]["candidate_outcome"] != "regression":
                    return {"action": "final", "findings": []}
                finding = dict(case["expected_finding"])
                finding.update({
                    "path": "pilot_holdout.py",
                    "line": 1,
                    "evidence": "safe_value = 'PILOT_HOLDOUT:%s'" % case["case_id"],
                    "title": "Pilot-controlled holdout false positive",
                })
            else:
                finding = dict(case["expected_finding"])
            finding["confidence"] = .92
            return {"action": "final", "findings": [finding]}
        if role == "critic":
            return {
                "action": "final",
                "decisions": [{
                    "finding_index": index,
                    "accepted": True,
                    "objections": [],
                    "confidence_adjustment": 0.0,
                } for index, _item in enumerate(task.get("candidates") or [])],
            }
        raise AssertionError("unsupported pilot role: %s" % role)

    def usage(self) -> dict:
        return {
            "llm_calls": len(self.calls),
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "latency_ms": None,
            "measurement": "not measured for scripted protocol calls",
        }


class MeteredClient:
    """Count real calls while leaving the configured transport unchanged."""

    def __init__(self, client):
        self.client = client
        self.provider = client.provider
        self.model = client.model
        self.calls = []

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        before = len(ledger.model_calls) if ledger else 0
        started = time.monotonic()
        try:
            return self.client.complete_json(role, system, user, ledger, max_tokens)
        finally:
            elapsed = int((time.monotonic() - started) * 1000)
            logged = ledger.model_calls[before:] if ledger else []
            self.calls.append({
                "role": role,
                "input_tokens": sum(item.input_tokens for item in logged) if logged else None,
                "output_tokens": sum(item.output_tokens for item in logged) if logged else None,
                "latency_ms": elapsed,
            })

    def usage(self) -> dict:
        known = [item for item in self.calls if item["input_tokens"] is not None]
        return {
            "llm_calls": len(self.calls),
            "input_tokens": sum(item["input_tokens"] for item in known),
            "output_tokens": sum(item["output_tokens"] for item in known),
            "total_tokens": sum(
                item["input_tokens"] + item["output_tokens"] for item in known
            ),
            "calls_with_unavailable_tokens": len(self.calls) - len(known),
            "latency_ms": sum(item["latency_ms"] for item in self.calls),
            "token_measurement": "provider usage where exposed through ExecutionLedger",
        }


def _wire_model(service: ReviewService, cases: list, real_model: bool):
    if real_model:
        client = MeteredClient(service.chat_client)
    else:
        client = PilotProtocolClient(cases)
    service.chat_client = client
    service.reviewer = service._build_agentic_reviewer()
    service.harness.reviewer = service.reviewer
    service.skill_evolution.candidate_generator = SkillPatchGenerator(client)
    service.skill_evolution.reviewer_factory = lambda artifact: AgentSkillReplayReviewer(
        artifact,
        client,
        service.settings.agent_token_budget,
        service.settings.agent_time_budget_seconds,
    )
    return client


def _normalized_expected(value: dict) -> dict:
    return {
        "path": value["path"],
        "start_line": int(value["line"]),
        "end_line": int(value["line"]),
        "rule_id": value.get("rule_id", ""),
        "cwe": value.get("cwe", ""),
    }


def _report_matches(service: ReviewService, report: dict, expected: dict) -> bool:
    findings = [
        service.harness._finding_from_dict(item) for item in report.get("findings") or []
    ]
    return bool(one_to_one_match([_normalized_expected(expected)], findings))


def _seed_evaluation(service: ReviewService, case: dict) -> None:
    expected = case["expected_finding"]
    service.store.save_evaluation_case(
        "pilot-validation-%s" % case["case_id"],
        "validation",
        case["diff"],
        [{
            "path": expected["path"],
            "line": expected["line"],
            "rule_id": expected["rule_id"],
            "cwe": expected.get("cwe", ""),
            "min_severity": expected["severity"],
        }],
        "development-targeted-evolution-pilot",
    )
    service.store.save_evaluation_case(
        "pilot-holdout-%s" % case["case_id"],
        "holdout",
        "--- /dev/null\n+++ b/pilot_holdout.py\n@@ -0,0 +1 @@\n"
        "+safe_value = 'PILOT_HOLDOUT:%s'\n" % case["case_id"],
        [],
        "development-targeted-evolution-pilot",
    )


def _contains_text(value: Any, needle: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_text(item, needle) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_text(item, needle) for item in value)
    if isinstance(value, str):
        if needle in value:
            return True
        try:
            nested = json.loads(value)
        except (TypeError, ValueError):
            return False
        return nested != value and _contains_text(nested, needle)
    return False


def _apply_context_control(service: ReviewService, task_id: str, case: dict) -> bool:
    if case["control"]["context_visibility"] != "omit_expected_line":
        checkpoint = service.store.load_checkpoints(task_id)["agentic-lead-session"]
        session = checkpoint["state"]["session"]
        return any(_contains_text(
            snapshot.get("final_managed_user_context"),
            case["expected_finding"]["evidence"],
        ) for snapshot in session.get("worker_execution_snapshots", {}).values())
    checkpoint = service.store.load_checkpoints(task_id)["agentic-lead-session"]
    state = checkpoint["state"]
    session = state["session"]
    expected_path = case["expected_finding"]["path"]
    assignment_ids = {
        str(item.get("assignment_id") or "")
        for item in session.get("delegations") or []
        if expected_path in item.get("files", [])
    }
    replacement = json.dumps({
        "task": json.dumps({
            "phase": "worker",
            "diff": {"files": [], "hunks": []},
            "instruction": "Controlled pilot context intentionally omits the expected line.",
        }, sort_keys=True),
    }, sort_keys=True)
    changed = False
    for snapshot in session.get("worker_execution_snapshots", {}).values():
        if str(snapshot.get("assignment_id") or "") in assignment_ids:
            snapshot["final_managed_user_context"] = replacement
            changed = True
    if not changed:
        return False
    service.store.save_checkpoint(
        task_id,
        "agentic-lead-session",
        state,
        checkpoint.get("status", "completed"),
        int(checkpoint.get("attempt") or 1),
        str(checkpoint.get("error") or ""),
    )
    return False


def _unique_assignment_skill(service: ReviewService, task_id: str, expected: dict) -> Optional[str]:
    checkpoint = (service.store.load_checkpoints(task_id) or {}).get("agentic-lead-session")
    state = checkpoint.get("state") if isinstance(checkpoint, dict) else None
    session = state.get("session") if isinstance(state, dict) else None
    if not isinstance(session, dict):
        return None
    path = str(expected.get("path") or "").replace("\\", "/")
    matches = []
    for assignment in session.get("delegations") or []:
        files = {str(value).replace("\\", "/") for value in assignment.get("files") or []}
        skills = list(dict.fromkeys(
            str(value) for value in assignment.get("skills") or [] if str(value)
        ))
        if path in files and len(skills) == 1:
            matches.append(skills[0])
    return matches[0] if len(matches) == 1 else None


def _targeted_route(attribution: dict, skill_name: Optional[str]) -> bool:
    return bool(
        skill_name
        and attribution.get("status") == "SUPPORTED"
        and attribution.get("root_cause") == "SKILL_GUIDANCE_GAP"
        and attribution.get("evolution_surface") == "SKILL"
        and attribution.get("evolution_target") == skill_name
    )


def _attempt_evolution(service: ReviewService, failure: dict, skill_name: str) -> dict:
    """One shared downstream implementation used by both experiment arms."""
    engine = service.skill_evolution
    baseline = engine._runtime_baseline(skill_name, "default")
    source_case, execution_evidence = engine._source_failure_evidence(failure, skill_name)
    if source_case is None or execution_evidence is None:
        return {
            "decision": "deferred",
            "reason": "source replay evidence unavailable",
            "gates": {},
            "version": None,
            "candidate_change": {},
        }
    generated = engine.candidate_generator.generate(
        skill_name,
        baseline,
        failure["payload"]["attribution"],
        failure["payload"]["finding"],
        execution_evidence,
    )
    candidate = validate_artifact(generated["artifact"], skill_name)
    engine._require_bounded_skill_patch(baseline, candidate)
    with engine._lock:
        return engine._propose(
            skill_name,
            candidate,
            "default",
            baseline_artifact_override=baseline,
            activation_policy="ready_for_promotion",
            source_case=source_case,
            provenance={
                "experiment": "targeted-evolution-pilot",
                "source_failure_id": failure.get("id"),
            },
        )


def _compact_evolution(result: Optional[dict]) -> dict:
    if result is None:
        return {
            "decision": "not_attempted",
            "source_replay_fixed": False,
            "regression_rejected": False,
            "validated": False,
            "version": None,
            "version_active": None,
            "gates": {},
            "changed_files": [],
        }
    gates = dict(result.get("gates") or {})
    regression_rejected = bool(
        gates.get("source_replay_fixed") is True
        and result.get("decision") == "rejected"
        and any(gates.get(name) is False for name in (
            "evaluation_success",
            "validation_non_regression",
            "holdout_non_regression",
        ))
    )
    version = result.get("version") or {}
    return {
        "decision": result.get("decision"),
        "source_replay_fixed": gates.get("source_replay_fixed") is True,
        "regression_rejected": regression_rejected,
        "validated": result.get("decision") == "ready_for_promotion",
        "version": version.get("version"),
        "version_active": version.get("active"),
        "gates": gates,
        "changed_files": list((result.get("candidate_change") or {}).get("changed_files") or []),
    }


def run_case_arm(case: dict, arm: str, cases: list, real_model: bool = False) -> dict:
    with tempfile.TemporaryDirectory(prefix="evoagent-targeted-pilot-") as temporary:
        service = ReviewService(_settings(os.path.join(temporary, "pilot.db"), real_model))
        client = _wire_model(service, cases, real_model)
        try:
            _seed_evaluation(service, case)
            skill_name = case["assignment_skill"]
            baseline = next(
                skill for skill in service._active_agent_skills("default")
                if skill.name == skill_name
            )
            review = service.create_review(
                case["repository"],
                case["diff"],
                tenant_id="default",
                enabled_agents=["lead", case["worker"], "critic"],
                enabled_skills=[skill_name],
            )
            baseline_found = _report_matches(
                service, review["report"], case["expected_finding"]
            )
            context_evidence_present = _apply_context_control(
                service, review["task_id"], case
            )
            service.record_feedback(
                review["task_id"],
                "missed_issue",
                case["expected_finding"],
                "Controlled targeted-evolution development pilot.",
                "default",
            )
            failure = service.store.list_task_failure_cases(
                review["task_id"], "default"
            )[0]
            attribution = failure["payload"]["attribution"]
            assignment_skill = _unique_assignment_skill(
                service, review["task_id"], case["expected_finding"]
            )
            attempted = bool(assignment_skill) if arm == "naive" else _targeted_route(
                attribution, assignment_skill
            )
            evolution = _attempt_evolution(
                service, failure, assignment_skill
            ) if attempted else None
            active_after = service.store.get_active_skill_artifact(skill_name, "default")
            result = {
                "case_id": case["case_id"],
                "arm": arm,
                "mode": "real-model" if real_model else "deterministic-protocol-dry-run",
                "gold_first_divergence": case["gold_first_divergence"],
                "gold_root_cause": case["gold_root_cause"],
                "gold_should_evolve": bool(case["gold_should_evolve"]),
                "gold_target_skill": case.get("gold_target_skill"),
                "baseline_expected_found": baseline_found,
                "baseline_skill_source": baseline.source,
                "context_evidence_present": context_evidence_present,
                "predicted_first_divergence": attribution.get("first_divergence"),
                "predicted_root_cause": attribution.get("root_cause"),
                "attribution_status": attribution.get("status"),
                "assignment_skill": assignment_skill,
                "predicted_target_skill": attribution.get("evolution_target"),
                "evolution_attempted": attempted,
                "routing_correct": attempted == bool(case["gold_should_evolve"]),
                "attribution_correct": (
                    attribution.get("first_divergence") == case["gold_first_divergence"]
                    and attribution.get("root_cause") == case["gold_root_cause"]
                ),
                "evolution": _compact_evolution(evolution),
                "active_override_after_case": active_after is not None,
                "downstream_contract_sha256": DOWNSTREAM_CONTRACT_SHA256,
                "usage": client.usage(),
            }
            return result
        finally:
            service.queue.close()


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def compute_arm_metrics(results: list) -> dict:
    attempts = [item for item in results if item["evolution_attempted"]]
    non_skill = [item for item in results if not item["gold_should_evolve"]]
    wrong_surface = [item for item in non_skill if item["evolution_attempted"]]
    source_fixed = [item for item in attempts if item["evolution"]["source_replay_fixed"]]
    regression_rejected = [
        item for item in attempts if item["evolution"]["regression_rejected"]
    ]
    validated = [item for item in attempts if item["evolution"]["validated"]]
    usage = [item["usage"] for item in results]
    def available_sum(name: str):
        values = [item.get(name) for item in usage]
        return sum(values) if values and all(value is not None for value in values) else None
    return {
        "cases": len(results),
        "attribution_accuracy": _rate(
            sum(item["attribution_correct"] for item in results), len(results)
        ),
        "routing_accuracy": _rate(
            sum(item["routing_correct"] for item in results), len(results)
        ),
        "wrong_surface_evolutions": len(wrong_surface),
        "wrong_surface_evolution_rate": _rate(len(wrong_surface), len(non_skill)),
        "evolution_attempts": len(attempts),
        "source_failures_resolved": len(source_fixed),
        "source_failure_resolution_rate": _rate(len(source_fixed), len(attempts)),
        "regression_rejections": len(regression_rejected),
        "regression_rejection_rate": _rate(len(regression_rejected), len(attempts)),
        "validated_evolutions": len(validated),
        "validated_evolution_rate": _rate(len(validated), len(attempts)),
        "llm": {
            "calls": sum(item.get("llm_calls", 0) for item in usage),
            "input_tokens": available_sum("input_tokens"),
            "output_tokens": available_sum("output_tokens"),
            "total_tokens": available_sum("total_tokens"),
            "latency_ms": available_sum("latency_ms"),
            "calls_with_unavailable_tokens": sum(
                item.get("calls_with_unavailable_tokens", 0) for item in usage
            ),
        },
    }


def run_pilot(cases: list, real_model: bool = False) -> tuple:
    results = []
    for case in cases:
        for arm in ("naive", "targeted"):
            try:
                results.append(run_case_arm(case, arm, cases, real_model))
            except Exception as exc:
                results.append({
                    "case_id": case["case_id"],
                    "arm": arm,
                    "mode": "real-model" if real_model else "deterministic-protocol-dry-run",
                    "gold_first_divergence": case["gold_first_divergence"],
                    "gold_root_cause": case["gold_root_cause"],
                    "gold_should_evolve": bool(case["gold_should_evolve"]),
                    "gold_target_skill": case.get("gold_target_skill"),
                    "error": "%s: %s" % (type(exc).__name__, str(exc)[:500]),
                    "evolution_attempted": False,
                    "routing_correct": not bool(case["gold_should_evolve"]),
                    "attribution_correct": False,
                    "evolution": _compact_evolution(None),
                    "active_override_after_case": False,
                    "downstream_contract_sha256": DOWNSTREAM_CONTRACT_SHA256,
                    "usage": {
                        "llm_calls": 0, "input_tokens": 0, "output_tokens": 0,
                        "total_tokens": 0, "latency_ms": 0,
                    },
                })
    by_arm = {
        arm: [item for item in results if item["arm"] == arm]
        for arm in ("naive", "targeted")
    }
    metrics = {arm: compute_arm_metrics(values) for arm, values in by_arm.items()}
    targeted = by_arm["targeted"]
    skill_gaps = [item for item in targeted if item["gold_should_evolve"]]
    non_skill = [item for item in targeted if not item["gold_should_evolve"]]
    correctly_attributed_skill_gaps = sum(item["attribution_correct"] for item in skill_gaps)
    targeted_wrong = sum(item["evolution_attempted"] for item in non_skill)
    routed_skill_gaps = [item for item in skill_gaps if item["evolution_attempted"]]
    validated_routed = sum(item["evolution"]["validated"] for item in routed_skill_gaps)
    gates = {
        "skill_gap_attribution_at_least_3_of_4": correctly_attributed_skill_gaps >= 3,
        "targeted_non_skill_wrong_evolution_at_most_1_of_8": targeted_wrong <= 1,
        "validated_at_least_half_of_routed_skill_gaps": bool(routed_skill_gaps)
        and validated_routed * 2 >= len(routed_skill_gaps),
    }
    distribution = {
        cause: sum(item["gold_root_cause"] == cause for item in cases)
        for cause in sorted(ROOT_CAUSES)
    }
    summary = {
        "experiment": "targeted-evolution-pilot",
        "mode": "real-model" if real_model else "deterministic-protocol-dry-run",
        "interpretation": (
            "REAL_MODEL_DEVELOPMENT_PILOT"
            if real_model else "PIPELINE_VALIDATION_NOT_MODEL_INTELLIGENCE"
        ),
        "case_count": len(cases),
        "case_distribution": distribution,
        "downstream_contract": DOWNSTREAM_CONTRACT,
        "downstream_contract_sha256": DOWNSTREAM_CONTRACT_SHA256,
        "arms": metrics,
        "development_gates": gates,
        "development_gates_passed": all(gates.values()),
        "errors": [
            {"case_id": item["case_id"], "arm": item["arm"], "error": item["error"]}
            for item in results if item.get("error")
        ],
        "real_model_results": (
            {"status": "RUN", "note": "One non-retried development-pilot pass."}
            if real_model else
            {"status": "UNKNOWN / NOT RUN", "note": "No scripted result is substituted."}
        ),
    }
    return summary, results


def render_summary(summary: dict) -> str:
    naive = summary["arms"]["naive"]
    targeted = summary["arms"]["targeted"]
    lines = [
        "# Targeted Evolution Pilot",
        "",
        "> %s" % summary["interpretation"],
        "",
        "| Metric | Naive | Targeted |",
        "| --- | ---: | ---: |",
    ]
    for label, key in (
        ("Attribution Accuracy", "attribution_accuracy"),
        ("Routing Accuracy", "routing_accuracy"),
        ("Wrong-Surface Evolution Rate", "wrong_surface_evolution_rate"),
        ("Evolution Attempts", "evolution_attempts"),
        ("Source Failure Resolution Rate", "source_failure_resolution_rate"),
        ("Regression Rejection Rate", "regression_rejection_rate"),
        ("Validated Evolution Rate", "validated_evolution_rate"),
        ("LLM Calls", "llm.calls"),
        ("Total Tokens", "llm.total_tokens"),
        ("Latency ms", "llm.latency_ms"),
    ):
        def get(value, dotted):
            for part in dotted.split("."):
                value = value[part]
            return value
        lines.append("| %s | %s | %s |" % (label, get(naive, key), get(targeted, key)))
    lines.extend(["", "## Development gates", ""])
    for name, passed in summary["development_gates"].items():
        lines.append("- `%s`: **%s**" % (name, "PASS" if passed else "FAIL"))
    lines.extend([
        "",
        "## Interpretation guard",
        "",
        (
            "The scripted mode validates routing and evaluation plumbing. Its frozen attribution "
            "responses do not measure model intelligence."
            if summary["mode"] == "deterministic-protocol-dry-run" else
            "This is a one-pass real-model development pilot, not a final benchmark."
        ),
        "",
        "Real-model results: `%s`." % summary["real_model_results"]["status"],
        "",
    ])
    return "\n".join(lines)


def _write_outputs(output_directory: str, summary: dict, results: list, real_model: bool) -> None:
    os.makedirs(output_directory, exist_ok=True)
    prefix = "real-model-" if real_model else ""
    with open(
        os.path.join(output_directory, prefix + "summary.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    with open(
        os.path.join(output_directory, prefix + "summary.md"), "w", encoding="utf-8"
    ) as handle:
        handle.write(render_summary(summary))
    with open(
        os.path.join(output_directory, prefix + "case_results.jsonl"),
        "w",
        encoding="utf-8",
    ) as handle:
        for item in results:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        default=os.path.join(
            ROOT, "experiments", "targeted-evolution-pilot", "cases.jsonl"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(ROOT, "output", "targeted-evolution-pilot"),
    )
    parser.add_argument(
        "--real-model",
        action="store_true",
        help="Use the configured EvoAgent model for one non-retried pilot pass.",
    )
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if args.real_model and not Settings.from_env().resolved_llm():
        empty_metrics = compute_arm_metrics([])
        summary = {
            "experiment": "targeted-evolution-pilot",
            "mode": "real-model",
            "interpretation": "REAL_MODEL_DEVELOPMENT_PILOT",
            "case_count": len(cases),
            "case_distribution": {
                cause: sum(item["gold_root_cause"] == cause for item in cases)
                for cause in sorted(ROOT_CAUSES)
            },
            "downstream_contract": DOWNSTREAM_CONTRACT,
            "downstream_contract_sha256": DOWNSTREAM_CONTRACT_SHA256,
            "arms": {"naive": empty_metrics, "targeted": empty_metrics},
            "development_gates": {},
            "development_gates_passed": False,
            "errors": [],
            "real_model_results": {
                "status": "UNKNOWN / NOT RUN",
                "note": "No EvoAgent model is configured; scripted results were not substituted.",
            },
        }
        _write_outputs(os.path.abspath(args.output_dir), summary, [], True)
        print("UNKNOWN / NOT RUN")
        return
    summary, results = run_pilot(cases, args.real_model)
    output_directory = os.path.abspath(args.output_dir)
    _write_outputs(output_directory, summary, results, args.real_model)
    print(os.path.join(
        output_directory, "real-model-summary.json" if args.real_model else "summary.json"
    ))
    print(summary["interpretation"])
    if summary["errors"] or not summary["development_gates_passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
