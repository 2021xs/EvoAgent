"""Compare one-shot Critic and one bounded evidence challenge on controlled cases."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Dict, Optional


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.agentic_core import AgenticReviewer  # noqa: E402
from evoagent.config import Settings  # noqa: E402
from evoagent.diff_parser import parse_unified_diff  # noqa: E402
from evoagent.llm import JsonChatClient  # noqa: E402
from evoagent.store import TaskStore  # noqa: E402


CATEGORIES = {
    "missing_support": 4,
    "counter_evidence": 3,
    "sufficient": 3,
}


class OneShotAgenticReviewer(AgenticReviewer):
    """Experiment-only ablation: preserve Pass 1 and disable its route."""

    def _create_critic_challenge(self, session, candidates):
        return None


def load_cases(path: str) -> list:
    cases = []
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            required = {
                "case_id", "category", "worker", "repository", "diff", "finding",
                "pass1_verdict", "gold_final_verdict", "gold_challenge_useful",
                "evidence_question", "evidence_marker", "evidence_fact",
                "evidence_search_query", "repository_files",
            }
            missing = required.difference(value)
            if missing:
                raise ValueError(
                    "case line %d is missing: %s" % (
                        line_number, ", ".join(sorted(missing)),
                    )
                )
            cases.append(value)
    if len(cases) != 10 or len({item["case_id"] for item in cases}) != 10:
        raise ValueError("bounded evidence pilot requires ten unique cases")
    distribution = {
        name: sum(item["category"] == name for item in cases)
        for name in CATEGORIES
    }
    if distribution != CATEGORIES:
        raise ValueError("unexpected pilot distribution: %r" % distribution)
    for case in cases:
        finding = case["finding"]
        marker = case["evidence_marker"]
        if case["worker"] not in {"security", "correctness-reliability"}:
            raise ValueError("unsupported Worker in %s" % case["case_id"])
        if marker in case["diff"] or marker in json.dumps(finding, sort_keys=True):
            raise ValueError("new-evidence marker leaks into Pass-1 projection")
        repository_text = "\n".join(case["repository_files"].values())
        if marker not in repository_text:
            raise ValueError("repository evidence marker missing in %s" % case["case_id"])
        should_challenge = case["category"] != "sufficient"
        if bool(case["gold_challenge_useful"]) != should_challenge:
            raise ValueError("challenge gold conflicts with category in %s" % case["case_id"])
        if should_challenge and case["pass1_verdict"] == case["gold_final_verdict"]:
            raise ValueError("challenge case must begin with a wrong Pass-1 verdict")
        if not should_challenge and case["pass1_verdict"] != case["gold_final_verdict"]:
            raise ValueError("sufficient case must begin with a correct Pass-1 verdict")
    return cases


class PilotClient:
    """Freeze Lead/Worker inputs; optionally delegate Critic communication to a real model."""

    provider = "scripted-bounded-evidence-pilot"
    model = "protocol-v1"

    def __init__(self, cases: list, real_client: Optional[JsonChatClient] = None):
        self.cases = cases
        self.real_client = real_client
        self.by_rule = {
            str(item["finding"]["rule_id"]): item for item in cases
        }

    def _case(self, system: str, user: str) -> Optional[dict]:
        value = system + "\n" + user
        for case in self.cases:
            if (
                "PILOT_CASE:%s" % case["case_id"] in value
                or str(case["finding"]["rule_id"]) in value
            ):
                return case
        return None

    def _scripted_usage(self, role: str, ledger) -> None:
        if ledger:
            ledger.record_model(
                role, self.provider, self.model,
                {"prompt_tokens": 10, "completion_tokens": 5}, 1,
            )

    @staticmethod
    def _managed_task(user: str) -> tuple:
        managed = json.loads(user)
        return managed, json.loads(managed["task"])

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        managed, task = self._managed_task(user)
        case = self._case(system, user)
        communication = task.get("communication_type") == "REQUEST_EVIDENCE"
        critic_call = role == "critic"
        if self.real_client is not None and (critic_call or communication):
            return self.real_client.complete_json(
                role, system, user, ledger, max_tokens=max_tokens,
            )

        self._scripted_usage(role, ledger)
        if role == "lead":
            phase = task.get("phase")
            if phase == "delegate":
                if not case:
                    raise ValueError("pilot Lead could not resolve its case")
                return {
                    "action": "final", "risk_level": "normal",
                    "delegations": [{
                        "assignment_id": "%s-assignment" % case["case_id"],
                        "worker": case["worker"],
                        "objective": "Review the frozen bounded-evidence pilot change.",
                        "files": [case["finding"]["path"]],
                        "risk_domains": ["development-pilot"],
                        "skills": [],
                    }],
                }
            if phase == "finalize":
                return {
                    "action": "final",
                    "accepted_finding_indices": list(
                        range(len(task.get("candidate_findings") or []))
                    ),
                    "confidence_adjustments": [],
                    "resolution_summary": "Frozen Lead behavior for both pilot arms.",
                }

        if role in {"security", "correctness-reliability"}:
            if not case:
                raise ValueError("pilot Worker could not resolve its case")
            if not communication:
                return {"action": "final", "findings": [dict(case["finding"])]}
            if not managed.get("observations"):
                return {
                    "action": "tool", "tool": "search_repository",
                    "arguments": {"query": case["evidence_search_query"]},
                    "reason": "Retrieve the one frozen repository fact requested by Critic.",
                }
            evidence_ids = []
            for observation in managed.get("observations") or []:
                result = observation.get("result") or {}
                if result.get("evidence_id"):
                    evidence_ids.append(str(result["evidence_id"]))
            return {
                "action": "final", "status": "answered",
                "summary": case["evidence_fact"],
                "evidence_ids": evidence_ids,
            }

        if role == "critic":
            if not case:
                raise ValueError("pilot Critic could not resolve its case")
            if task.get("phase") == "critic-final":
                return {
                    "action": "final", "decisions": [{
                        "finding_index": task["candidate"]["finding_index"],
                        "accepted": bool(case["gold_final_verdict"]),
                        "objections": [], "confidence_adjustment": 0.0,
                        "supporting_evidence_ids": [
                            item["evidence_id"]
                            for item in task["evidence_response"].get(
                                "evidence_refs"
                            ) or []
                        ],
                    }],
                }
            decisions = []
            for candidate in task.get("candidates") or []:
                decision = {
                    "finding_index": candidate["finding_index"],
                    "accepted": bool(case["pass1_verdict"]),
                    "objections": (
                        [] if case["pass1_verdict"]
                        else ["The Candidate lacks one required repository fact."]
                    ),
                    "confidence_adjustment": 0.0,
                    "supporting_evidence_ids": [],
                }
                if case["gold_challenge_useful"]:
                    decision["evidence_request"] = case["evidence_question"]
                decisions.append(decision)
            return {"action": "final", "decisions": decisions}
        raise AssertionError((role, task))


def _real_client() -> Optional[JsonChatClient]:
    resolved = Settings.from_env().resolved_llm()
    if not resolved:
        return None
    return JsonChatClient(
        str(resolved["base_url"]), str(resolved["api_key"]),
        str(resolved["model"]), provider=str(resolved["provider"]),
        timeout=120, extra_headers=dict(resolved.get("headers") or {}),
    )


def _write_repository(root: str, case: dict) -> None:
    for relative, content in case["repository_files"].items():
        target = Path(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _usage(execution: dict, real_model: bool) -> dict:
    calls = execution.get("model_call_log") or []
    if not real_model:
        return {
            "llm_calls": len(calls), "input_tokens": None,
            "output_tokens": None, "total_tokens": None, "latency_ms": None,
        }
    measured = [
        item for item in calls
        if item.get("provider") != PilotClient.provider
    ]
    return {
        "llm_calls": len(calls),
        "input_tokens": sum(int(item.get("input_tokens", 0)) for item in measured),
        "output_tokens": sum(int(item.get("output_tokens", 0)) for item in measured),
        "total_tokens": sum(
            int(item.get("input_tokens", 0)) + int(item.get("output_tokens", 0))
            for item in measured
        ),
        "latency_ms": sum(int(item.get("duration_ms", 0)) for item in measured),
    }


def run_case_arm(case: dict, arm: str, cases: list, real_model: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="evoagent-evidence-pilot-") as temporary:
        repository_root = os.path.join(temporary, "repository")
        os.makedirs(repository_root)
        _write_repository(repository_root, case)
        store = TaskStore(os.path.join(temporary, "pilot.db"))
        task_id = "pilot-%s-%s" % (case["case_id"], arm)
        store.create(task_id, case["repository"], 1, {
            "mode": "agentic",
            "enabled_agents": ["lead", case["worker"], "critic"],
            "repository_root": repository_root,
        })
        client = PilotClient(cases, _real_client() if real_model else None)
        reviewer_type = OneShotAgenticReviewer if arm == "one_shot" else AgenticReviewer
        reviewer = reviewer_type(
            store, client, default_token_budget=8000, default_time_budget=30,
        )
        findings = reviewer.review_with_context(
            task_id, case["diff"], parse_unified_diff(case["diff"]),
            case["repository"],
        )
        checkpoint = store.load_checkpoints(task_id)["agentic-lead-session"]
        session = checkpoint["state"]["session"]
        execution = checkpoint["state"]["execution"]
        pass1 = (session.get("critic_pass1_decisions") or [{}])[0]
        final = (session.get("critic_decisions") or [{}])[0]
        challenge = session.get("critic_challenge") or {}
        response = challenge.get("response") or {}
        evidence_refs = list((response.get("payload") or {}).get("evidence_refs") or [])
        marker = case["evidence_marker"]
        useful = bool(
            challenge and evidence_refs
            and any(marker in str(item.get("output_preview", "")) for item in evidence_refs)
        )
        summary = reviewer.collaboration_summary(task_id)
        return {
            "case_id": case["case_id"], "arm": arm,
            "mode": "real-model" if real_model else "deterministic-protocol-dry-run",
            "category": case["category"],
            "gold_final_verdict": bool(case["gold_final_verdict"]),
            "gold_challenge_useful": bool(case["gold_challenge_useful"]),
            "pass1_verdict": bool(pass1.get("accepted")),
            "final_critic_verdict": bool(final.get("accepted")),
            "final_verdict_correct": (
                bool(final.get("accepted")) == bool(case["gold_final_verdict"])
            ),
            "challenge_triggered": bool(challenge),
            "challenge_status": challenge.get("status"),
            "useful_challenge": useful,
            "verdict_corrected": bool(
                challenge
                and bool(pass1.get("accepted")) != bool(case["gold_final_verdict"])
                and bool(final.get("accepted")) == bool(case["gold_final_verdict"])
            ),
            "response_status": (response.get("payload") or {}).get("status"),
            "new_evidence_refs": len(evidence_refs),
            "published_rule_ids": sorted(item.rule_id for item in findings),
            "downstream": {
                "lead_final": session.get("lead_final"),
                "gates": summary.get("gates"),
                "accepted_findings": len(findings),
            },
            "usage": _usage(execution, real_model),
        }


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def compute_arm_metrics(results: list) -> dict:
    challenged = [item for item in results if item["challenge_triggered"]]
    sufficient = [item for item in results if not item["gold_challenge_useful"]]
    usage = [item["usage"] for item in results]
    return {
        "cases": len(results),
        "final_critic_verdict_accuracy": _rate(
            sum(item["final_verdict_correct"] for item in results), len(results),
        ),
        "challenge_trigger_rate": _rate(len(challenged), len(results)),
        "useful_challenges": sum(item["useful_challenge"] for item in challenged),
        "useful_challenge_rate": _rate(
            sum(item["useful_challenge"] for item in challenged), len(challenged),
        ),
        "verdict_corrections": sum(item["verdict_corrected"] for item in challenged),
        "verdict_correction_rate": _rate(
            sum(item["verdict_corrected"] for item in challenged), len(challenged),
        ),
        "unnecessary_challenges": sum(
            item["challenge_triggered"] for item in sufficient
        ),
        "unnecessary_challenge_rate": _rate(
            sum(item["challenge_triggered"] for item in sufficient), len(sufficient),
        ),
        "llm": {
            "llm_calls": sum(item["llm_calls"] for item in usage),
            **{
                key: (
                    sum(item[key] for item in usage)
                    if usage and all(item[key] is not None for item in usage)
                    else None
                )
                for key in (
                    "input_tokens", "output_tokens", "total_tokens", "latency_ms",
                )
            },
        },
    }


def run_pilot(cases: list, real_model: bool = False) -> tuple:
    results = []
    errors = []
    for case in cases:
        for arm in ("one_shot", "bounded"):
            try:
                results.append(run_case_arm(case, arm, cases, real_model))
            except Exception as exc:
                errors.append({
                    "case_id": case["case_id"], "arm": arm,
                    "error": "%s: %s" % (type(exc).__name__, str(exc)[:500]),
                })
    by_arm = {
        arm: [item for item in results if item["arm"] == arm]
        for arm in ("one_shot", "bounded")
    }
    metrics = {arm: compute_arm_metrics(values) for arm, values in by_arm.items()}
    by_key = {(item["case_id"], item["arm"]): item for item in results}
    downstream_mismatches = []
    for case in cases:
        key = case["case_id"]
        if (key, "one_shot") not in by_key or (key, "bounded") not in by_key:
            continue
        if by_key[(key, "one_shot")]["downstream"] != by_key[(key, "bounded")][
            "downstream"
        ]:
            downstream_mismatches.append(key)
    one_usage = metrics["one_shot"]["llm"]
    bounded_usage = metrics["bounded"]["llm"]
    def extra(name):
        left, right = one_usage[name], bounded_usage[name]
        return right - left if left is not None and right is not None else None
    summary = {
        "experiment": "bounded-evidence-pilot",
        "mode": "real-model" if real_model else "deterministic-protocol-dry-run",
        "interpretation": (
            "REAL_MODEL_DEVELOPMENT_PILOT"
            if real_model else "PROTOCOL_EXPERIMENT_PLUMBING_VALIDATION"
        ),
        "case_count": len(cases),
        "case_distribution": dict(CATEGORIES),
        "arms": metrics,
        "comparison": {
            "extra_llm_calls": bounded_usage["llm_calls"] - one_usage["llm_calls"],
            "extra_input_tokens": extra("input_tokens"),
            "extra_output_tokens": extra("output_tokens"),
            "extra_total_tokens": extra("total_tokens"),
            "extra_latency_ms": extra("latency_ms"),
            "downstream_equivalent": not downstream_mismatches,
            "downstream_mismatches": downstream_mismatches,
        },
        "errors": errors,
        "real_model_results": (
            {"status": "RUN", "note": "One non-retried pass per case and arm."}
            if real_model else {
                "status": "UNKNOWN / NOT RUN",
                "note": "Scripted results are not substituted for real-model evidence.",
            }
        ),
    }
    return summary, results


def render_summary(summary: dict) -> str:
    one = summary["arms"]["one_shot"]
    bounded = summary["arms"]["bounded"]
    lines = [
        "# Bounded Critic Evidence Challenge Pilot", "",
        "> %s" % summary["interpretation"], "",
        "| Metric | One-shot | Bounded |", "| --- | ---: | ---: |",
    ]
    for label, key in (
        ("Final Critic Verdict Accuracy", "final_critic_verdict_accuracy"),
        ("Challenge Trigger Rate", "challenge_trigger_rate"),
        ("Useful Challenge Rate", "useful_challenge_rate"),
        ("Verdict Correction Rate", "verdict_correction_rate"),
        ("Unnecessary Challenge Rate", "unnecessary_challenge_rate"),
    ):
        lines.append("| %s | %s | %s |" % (label, one[key], bounded[key]))
    lines.extend([
        "| LLM Calls | %s | %s |" % (
            one["llm"]["llm_calls"], bounded["llm"]["llm_calls"],
        ),
        "| Total Tokens | %s | %s |" % (
            one["llm"]["total_tokens"], bounded["llm"]["total_tokens"],
        ),
        "| Model Latency ms | %s | %s |" % (
            one["llm"]["latency_ms"], bounded["llm"]["latency_ms"],
        ),
        "", "## Comparison", "",
        "- Extra LLM calls: `%s`" % summary["comparison"]["extra_llm_calls"],
        "- Extra total tokens: `%s`" % summary["comparison"]["extra_total_tokens"],
        "- Downstream Lead/Gate equivalent: `%s`" % (
            summary["comparison"]["downstream_equivalent"]
        ),
        "", "## Interpretation guard", "",
        (
            "The scripted run validates protocol and experiment plumbing only. Frozen scripted "
            "verdicts are not evidence of Critic model quality."
            if summary["mode"] == "deterministic-protocol-dry-run" else
            "This is one non-retried real-model development run, not a final benchmark."
        ),
        "", "Real-model results: `%s`." % summary["real_model_results"]["status"],
        "",
    ])
    return "\n".join(lines)


def write_outputs(output_dir: str, summary: dict, results: list, real_model: bool) -> None:
    os.makedirs(output_dir, exist_ok=True)
    prefix = "real-model-" if real_model else ""
    with open(os.path.join(output_dir, prefix + "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    with open(os.path.join(output_dir, prefix + "summary.md"), "w", encoding="utf-8") as handle:
        handle.write(render_summary(summary))
    with open(
        os.path.join(output_dir, prefix + "case_results.jsonl"), "w", encoding="utf-8"
    ) as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")


def _empty_real_model_summary(cases: list) -> dict:
    empty = compute_arm_metrics([])
    return {
        "experiment": "bounded-evidence-pilot", "mode": "real-model",
        "interpretation": "REAL_MODEL_DEVELOPMENT_PILOT",
        "case_count": len(cases), "case_distribution": dict(CATEGORIES),
        "arms": {"one_shot": empty, "bounded": empty},
        "comparison": {
            "extra_llm_calls": 0, "extra_input_tokens": None,
            "extra_output_tokens": None, "extra_total_tokens": None,
            "extra_latency_ms": None, "downstream_equivalent": False,
            "downstream_mismatches": [],
        },
        "errors": [],
        "real_model_results": {
            "status": "UNKNOWN / NOT RUN",
            "note": "No EvoAgent model is configured; scripted results were not substituted.",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases", default=os.path.join(
            ROOT, "experiments", "bounded-evidence-pilot", "cases.jsonl",
        ),
    )
    parser.add_argument(
        "--output-dir", default=os.path.join(
            ROOT, "output", "bounded-evidence-pilot",
        ),
    )
    parser.add_argument(
        "--real-model", action="store_true",
        help="Use the configured model for Critic and evidence calls, once per case/arm.",
    )
    args = parser.parse_args()
    cases = load_cases(args.cases)
    output_dir = os.path.abspath(args.output_dir)
    if args.real_model and _real_client() is None:
        write_outputs(output_dir, _empty_real_model_summary(cases), [], True)
        print("UNKNOWN / NOT RUN")
        return
    summary, results = run_pilot(cases, real_model=args.real_model)
    write_outputs(output_dir, summary, results, args.real_model)
    print(os.path.join(
        output_dir, "real-model-summary.json" if args.real_model else "summary.json",
    ))
    print(summary["interpretation"])
    if summary["errors"] or not summary["comparison"]["downstream_equivalent"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
