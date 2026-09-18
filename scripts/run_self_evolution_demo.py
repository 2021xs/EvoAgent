"""Run the controlled EvoAgent self-evolution loop against production code paths."""
import argparse
import difflib
import json
import os
import sys
import tempfile
from dataclasses import replace
from typing import Any, Dict


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.evaluation_harness import one_to_one_match  # noqa: E402
from evoagent.service import ReviewService  # noqa: E402
from evoagent.skill_evolution import (  # noqa: E402
    AgentSkillReplayReviewer, SkillPatchGenerator,
)
from evoagent.config import Settings  # noqa: E402


GUIDANCE_MARKER = "CSV formula injection"


class DeterministicDemoClient:
    """Scripted model for a reproducible engineering demo, not an accuracy claim."""

    provider = "scripted-demo"
    model = "csv-formula-gap-v1"

    def __init__(self):
        self.calls = []

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        self.calls.append(role)
        if ledger:
            ledger.record_model(
                role, self.provider, self.model,
                {"prompt_tokens": 10, "completion_tokens": 5}, 1,
            )
        if role == "attribution":
            evidence = json.loads(user)
            runs = evidence.get("worker_runs") or []
            expected = str((evidence.get("expected_finding") or {}).get("evidence") or "")
            contexts = "\n".join(
                str(item.get("final_managed_user_context") or "") for item in runs
            )
            guidance = "\n".join(str(item.get("system_prompt") or "") for item in runs)
            if not expected or expected not in contexts:
                root_cause = "CONTEXT_EVIDENCE_MISSING"
            elif GUIDANCE_MARKER not in guidance:
                root_cause = "SKILL_GUIDANCE_GAP"
            else:
                root_cause = "MODEL_REASONING_FAILURE"
            return {
                "action": "final", "root_cause": root_cause,
                "reason": "Classified from the persisted Worker context and historical guidance.",
                "evidence_summary": [
                    "The changed CSV write was present in Worker context.",
                    "The historical Skill did not name spreadsheet formula injection.",
                ],
            }
        if role == "skill-evolution":
            payload = json.loads(user)
            current = payload["current_skill"]["files"]["SKILL.md"]
            block = """

## CSV formula injection

When untrusted values are exported to CSV or spreadsheet cells, check whether values beginning
with `=`, `+`, `-`, or `@` are neutralized before they reach the writer. Report CWE-1236 only when
the changed code writes attacker-controlled cell content without this protection. Require tests
that open representative dangerous prefixes as inert text while preserving legitimate values.
"""
            return {
                "action": "final",
                "skill_md": current.rstrip() + block,
                "reason": "Add one local CWE-1236 review check for untrusted CSV cells.",
            }

        managed = json.loads(user)
        task = json.loads(managed["task"])
        if role == "lead":
            if task["phase"] == "delegate":
                return {
                    "action": "final", "risk_level": "normal",
                    "delegations": [{
                        "assignment_id": "security-csv-1", "worker": "security",
                        "objective": "Review the changed CSV export trust boundary.",
                        "files": ["export.py"], "risk_domains": ["export-security"],
                        "skills": ["security-review"],
                    }],
                }
            if task["phase"] == "assess-workers":
                return {
                    "action": "final", "revision_requests": [],
                    "critic_objective": "Verify the CSV export finding.",
                }
            if task["phase"] == "finalize":
                return {
                    "action": "final",
                    "accepted_finding_indices": list(
                        range(len(task.get("candidate_findings") or []))
                    ),
                    "confidence_adjustments": [],
                }
        if role == "security":
            rendered = json.dumps(task, ensure_ascii=False)
            if (
                GUIDANCE_MARKER in system
                and "writer.writerow([name, status])" in rendered
            ):
                return {"action": "final", "findings": [{
                    "rule_id": "SEC-CSV-FORMULA-INJECTION", "cwe": "CWE-1236",
                    "severity": "medium",
                    "title": "Untrusted spreadsheet cell permits formula injection",
                    "explanation": (
                        "The raw name can begin with a spreadsheet formula prefix and is written "
                        "without neutralization."
                    ),
                    "path": "export.py", "line": 3,
                    "evidence": "writer.writerow([name, status])",
                    "fix": "Neutralize dangerous spreadsheet prefixes before writing the cell.",
                    "test": "Export =, +, -, and @ prefixes and assert inert cell text.",
                    "confidence": 0.92,
                }]}
            return {"action": "final", "findings": []}
        if role == "correctness-reliability":
            return {"action": "final", "findings": []}
        if role == "critic":
            return {
                "action": "final", "decisions": [{
                    "finding_index": index, "accepted": True, "objections": [],
                    "confidence_adjustment": 0.0,
                } for index, _item in enumerate(task.get("candidates") or [])],
            }
        raise AssertionError("unsupported demo role: %s" % role)


def _settings(db_path: str, real_model: bool) -> Settings:
    if real_model:
        base = Settings.from_env()
        if not base.resolved_llm():
            raise RuntimeError("--real-model requires a configured EvoAgent model")
        return replace(
            base, db_path=db_path, database_url="", redis_url="",
            skills_dir=os.path.join(ROOT, "skills"), eval_min_cases=1,
            eval_max_cases=max(10, base.eval_max_cases), eval_min_holdout_cases=1,
            memory_enabled=False,
        )
    return Settings(
        host="127.0.0.1", port=8080, db_path=db_path,
        max_diff_bytes=1024 * 1024, max_steps=8, timeout_seconds=30,
        llm_base_url="", llm_api_key="", llm_model="",
        github_webhook_secret="", github_token="", auto_post_review=False,
        skills_dir=os.path.join(ROOT, "skills"), eval_min_cases=1,
        eval_max_cases=10, eval_min_improvement=.01, eval_min_holdout_cases=1,
        memory_enabled=False, agent_token_budget=8000, agent_time_budget_seconds=30,
    )


def _wire_deterministic_model(service: ReviewService) -> DeterministicDemoClient:
    client = DeterministicDemoClient()
    service.chat_client = client
    service.reviewer = service._build_agentic_reviewer()
    service.harness.reviewer = service.reviewer
    service.skill_evolution.candidate_generator = SkillPatchGenerator(client)
    service.skill_evolution.reviewer_factory = lambda value: AgentSkillReplayReviewer(
        value, client, service.settings.agent_token_budget,
        service.settings.agent_time_budget_seconds,
    )
    return client


def _normalized_expected(value: dict) -> dict:
    return {
        "path": value["path"], "start_line": int(value["line"]),
        "end_line": int(value["line"]), "cwe": value["cwe"],
    }


def _report_matches(service: ReviewService, report: dict, expected: dict) -> bool:
    findings = [
        service.harness._finding_from_dict(item)
        for item in report.get("findings") or []
    ]
    return bool(one_to_one_match([_normalized_expected(expected)], findings))


def _worker_skill_manifest(service: ReviewService, task_id: str, skill_name: str) -> dict:
    session = service.store.load_checkpoints(task_id)[
        "agentic-lead-session"
    ]["state"]["session"]
    matches = [
        item for snapshot in session["worker_execution_snapshots"].values()
        for item in snapshot.get("selected_skills") or []
        if item.get("name") == skill_name
    ]
    if not matches:
        raise RuntimeError("Worker snapshot did not record the selected Skill")
    identities = {
        (item.get("name"), item.get("version"), item.get("source"), item.get("content_sha256"))
        for item in matches
    }
    if len(identities) != 1:
        raise RuntimeError("Worker runs did not use one stable Skill identity")
    name, version, source, content_sha256 = next(iter(identities))
    return {
        "name": name, "version": version, "source": source,
        "content_sha256": content_sha256,
    }


def _skill_diff(before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True),
        fromfile="bundled/security-review/SKILL.md",
        tofile="candidate/security-review/SKILL.md",
    ))


def _source_result(evolution: dict, side: str) -> str:
    rows = ((evolution.get("source_replay") or {}).get(side) or [])
    if not rows:
        return "not-run"
    row = rows[0]
    return "TP" if row.get("tp") else "FN" if row.get("fn") else "PASS"


def _render_summary(report: dict) -> str:
    stages = report.get("stages") or {}
    baseline = stages.get("baseline") or {}
    attribution = stages.get("attribution") or {}
    evolution = stages.get("evolution") or {}
    promotion = stages.get("promotion") or {}
    after = stages.get("after_promotion") or {}
    rollback = stages.get("rollback") or {}
    return "\n".join([
        "# EvoAgent Controlled Self-Evolution Demo",
        "",
        "> Architecture validation only; this is not an accuracy benchmark.",
        "",
        "## 1. Baseline",
        "",
        f"- Skill: `{(baseline.get('worker_skill') or {}).get('source', 'unknown')} security-review`",
        f"- Expected issue found: `{baseline.get('expected_found')}`",
        "",
        "## 2. Attribution",
        "",
        f"- First divergence: `{attribution.get('first_divergence', 'UNKNOWN')}`",
        f"- Root cause: `{attribution.get('root_cause', 'UNKNOWN')}`",
        f"- Evolution target: `{attribution.get('evolution_target', 'none')}`",
        "",
        "## 3. Evolution and validation",
        "",
        f"- Decision: `{evolution.get('decision', 'not-run')}`",
        "- Generated patch: `security-review.patch`",
        f"- Source baseline result: `{_source_result(evolution, 'baseline')}`",
        f"- Source candidate result: `{_source_result(evolution, 'candidate')}`",
        "",
        "## 4. Promotion and new review",
        "",
        f"- Promoted version: `{promotion.get('version', 'not-run')}`",
        f"- Source failure kept unresolved: `{not promotion.get('source_failure_resolved', True)}`",
        f"- New review found expected issue: `{after.get('expected_found')}`",
        "",
        "## 5. Rollback",
        "",
        f"- Active DB override: `{rollback.get('active_db_override')}`",
        f"- Bundled hash restored: `{rollback.get('bundled_hash_restored')}`",
        "",
        f"Final status: **{report.get('status', 'UNKNOWN')}**",
        "",
    ])


def _seed_demo_evaluation(service: ReviewService, fixture: dict) -> None:
    expected = fixture["expected_finding"]
    service.store.save_evaluation_case(
        fixture["validation"]["name"], "validation", fixture["diff"],
        [{
            "path": expected["path"], "line": expected["line"],
            "rule_id": expected["rule_id"], "cwe": expected["cwe"],
            "min_severity": expected["severity"],
        }],
        "controlled-self-evolution-demo",
    )
    service.store.save_evaluation_case(
        fixture["holdout"]["name"], "holdout", fixture["holdout"]["diff"],
        fixture["holdout"]["expected"], "controlled-self-evolution-demo",
    )


def run_demo(fixture: dict, output_directory: str, real_model: bool = False) -> dict:
    os.makedirs(output_directory, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="evoagent-self-evolution-") as temporary:
        service = ReviewService(_settings(os.path.join(temporary, "demo.db"), real_model))
        client = service.chat_client if real_model else _wire_deterministic_model(service)
        report: Dict[str, Any] = {
            "demo_version": "0.1", "layer": "real-model-smoke" if real_model else "deterministic",
            "claim": "controlled end-to-end architecture validation; not a benchmark",
            "case": fixture["name"], "skill_name": fixture["skill_name"],
            "model": {
                "provider": getattr(client, "provider", ""),
                "model": getattr(client, "model", ""),
                "rerun_policy": "one attempt per review/evolution step",
            },
            "stages": {}, "success": False,
        }
        promoted = False
        try:
            _seed_demo_evaluation(service, fixture)
            skill_name = fixture["skill_name"]
            expected = fixture["expected_finding"]
            baseline_skill = next(
                skill for skill in service._active_agent_skills("default")
                if skill.name == skill_name
            )
            if baseline_skill.source != "disk":
                raise RuntimeError("demo requires the bundled Skill as baseline")

            task_a = service.create_review(
                fixture["repository"], fixture["diff"], tenant_id="default",
                enabled_agents=["lead", "security", "critic"],
                enabled_skills=[skill_name],
            )
            baseline_found = _report_matches(service, task_a["report"], expected)
            report["stages"]["baseline"] = {
                "task_id": task_a["task_id"], "expected_found": baseline_found,
                "worker_skill": _worker_skill_manifest(service, task_a["task_id"], skill_name),
                "findings": task_a["report"].get("findings") or [],
            }
            if baseline_found:
                report.update({"status": "STOPPED", "stopped_at": "BASELINE"})
                return report

            service.record_feedback(
                task_a["task_id"], "missed_issue", expected,
                "Controlled demo expected CWE-1236 finding.", "default",
            )
            failure = service.store.list_failure_cases(True, 10, "default")[0]
            attribution = failure["payload"]["attribution"]
            report["stages"]["attribution"] = attribution
            required = {
                "status": "SUPPORTED", "first_divergence": "DISCOVERY",
                "root_cause": "SKILL_GUIDANCE_GAP", "evolution_surface": "SKILL",
                "evolution_target": skill_name,
            }
            if any(attribution.get(key) != value for key, value in required.items()):
                report.update({"status": "STOPPED", "stopped_at": "ATTRIBUTION"})
                return report

            evolution = service.skill_evolution.auto_propose(skill_name, "default")
            report["stages"]["evolution"] = {
                "decision": evolution["decision"], "reason": evolution["reason"],
                "version": evolution.get("version"), "source_failure_id": evolution.get("source_failure_id"),
                "source_replay": {
                    "baseline": evolution.get("source_baseline", {}).get("case_results", []),
                    "candidate": evolution.get("source_candidate", {}).get("case_results", []),
                },
                "gates": evolution.get("gates") or {},
                "candidate": evolution.get("candidate") or {},
                "baseline": evolution.get("baseline") or {},
                "candidate_holdout": evolution.get("candidate_holdout") or {},
                "baseline_holdout": evolution.get("baseline_holdout") or {},
            }
            if evolution["decision"] != "ready_for_promotion":
                report.update({"status": "STOPPED", "stopped_at": "EVOLUTION"})
                return report

            version = evolution["version"]["version"]
            candidate_row = next(
                item for item in service.store.list_skill_artifact_versions(skill_name, "default")
                if item["version"] == version
            )
            before = baseline_skill.content
            after = candidate_row["artifact"]["files"]["SKILL.md"]
            patch = _skill_diff(before, after)
            report["stages"]["evolution"]["skill_patch"] = patch
            with open(os.path.join(output_directory, "security-review-before.md"), "w", encoding="utf-8") as handle:
                handle.write(before)
            with open(os.path.join(output_directory, "security-review-after.md"), "w", encoding="utf-8") as handle:
                handle.write(after)
            with open(os.path.join(output_directory, "security-review.patch"), "w", encoding="utf-8") as handle:
                handle.write(patch)

            if not service.skill_evolution.rollback(skill_name, version, "default"):
                raise RuntimeError("manual promotion was rejected")
            service.reload_skills()
            promoted = True
            active = service.store.get_active_skill_artifact(skill_name, "default")
            promoted_runtime_skill = next(
                skill for skill in service._active_agent_skills("default")
                if skill.name == skill_name
            )
            report["stages"]["promotion"] = {
                "version": active["version"], "active": active["active"],
                "artifact_sha256": active["artifact_sha256"],
                "runtime_content_sha256": promoted_runtime_skill.content_sha256,
                "source_failure_resolved": service.store.list_failure_cases(
                    False, 10, "default"
                )[0]["resolved"],
            }

            task_b = service.create_review(
                fixture["repository"], fixture["diff"], tenant_id="default",
                enabled_agents=["lead", "security", "critic"], enabled_skills=[skill_name],
            )
            after_found = _report_matches(service, task_b["report"], expected)
            after_manifest = _worker_skill_manifest(service, task_b["task_id"], skill_name)
            report["stages"]["after_promotion"] = {
                "task_id": task_b["task_id"], "expected_found": after_found,
                "worker_skill": after_manifest,
                "findings": task_b["report"].get("findings") or [],
            }
            if (
                not after_found
                or after_manifest["content_sha256"] != promoted_runtime_skill.content_sha256
                or after_manifest["version"] != str(active["version"])
            ):
                report.update({"status": "STOPPED", "stopped_at": "AFTER_PROMOTION"})
                return report

            if not service.fallback_skill_to_bundled(skill_name, "default"):
                raise RuntimeError("bundled fallback was rejected")
            service.reload_skills()
            promoted = False
            fallback_skill = next(
                skill for skill in service._active_agent_skills("default")
                if skill.name == skill_name
            )
            report["stages"]["rollback"] = {
                "active_db_override": service.store.get_active_skill_artifact(
                    skill_name, "default"
                ),
                "runtime_skill": {
                    "name": fallback_skill.name, "version": fallback_skill.version,
                    "source": fallback_skill.source,
                    "content_sha256": fallback_skill.content_sha256,
                },
                "bundled_hash_restored": fallback_skill.content_sha256 == baseline_skill.content_sha256,
            }

            task_c = service.create_review(
                fixture["repository"], fixture["diff"], tenant_id="default",
                enabled_agents=["lead", "security", "critic"], enabled_skills=[skill_name],
            )
            report["stages"]["post_rollback"] = {
                "task_id": task_c["task_id"],
                "expected_found": _report_matches(service, task_c["report"], expected),
                "worker_skill": _worker_skill_manifest(service, task_c["task_id"], skill_name),
            }
            report.update({
                "status": "SELF_EVOLUTION_MVP_END_TO_END_VALIDATED",
                "success": True,
            })
            return report
        finally:
            if promoted:
                service.fallback_skill_to_bundled(fixture["skill_name"], "default")
                service.reload_skills()
            service.queue.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", default=os.path.join(
            ROOT, "demo", "fixtures", "self_evolution_csv_formula.json"
        ),
    )
    parser.add_argument(
        "--output-dir", default=os.path.join(ROOT, "output", "self-evolution-demo"),
    )
    parser.add_argument(
        "--real-model", action="store_true",
        help="Run one non-retried smoke attempt with the currently configured model.",
    )
    args = parser.parse_args()
    with open(args.fixture, "r", encoding="utf-8") as handle:
        fixture = json.load(handle)
    output_directory = os.path.abspath(args.output_dir)
    report = run_demo(fixture, output_directory, args.real_model)
    report_path = os.path.join(
        output_directory, "real-model-report.json" if args.real_model else "report.json"
    )
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    summary_path = os.path.join(
        output_directory,
        "real-model-summary.md" if args.real_model else "summary.md",
    )
    with open(summary_path, "w", encoding="utf-8") as handle:
        handle.write(_render_summary(report))
    print(report_path)
    print(summary_path)
    print(report["status"])
    if not report["success"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
