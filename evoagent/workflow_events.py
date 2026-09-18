"""Narrow durable workflow events for AutoFix CI suspension and wakeup."""

import copy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Dict, Optional


AUTOFIX_CHECKPOINT = "autofix-execution"

AUTOFIX_TRANSITIONS = {
    "": {"SOURCE_BOUND", "SUGGESTION_ONLY", "STALE_SOURCE"},
    "SOURCE_BOUND": {"PATCH_READY", "SUGGESTION_ONLY", "STALE_SOURCE"},
    "PATCH_READY": {"VERIFIED", "BLOCKED", "SUGGESTION_ONLY"},
    "VERIFIED": {"COMMIT_CREATED", "STALE_SOURCE"},
    "COMMIT_CREATED": {"BRANCH_PUBLISHED"},
    "BRANCH_PUBLISHED": {"PR_CREATED"},
    "PR_CREATED": {"WAITING_FOR_CI"},
    "WAITING_FOR_CI": {"CI_PASSED", "CI_FAILED"},
    "BLOCKED": set(),
    "SUGGESTION_ONLY": set(),
    "STALE_SOURCE": set(),
    "CI_PASSED": set(),
    "CI_FAILED": set(),
}

CI_TERMINAL_PHASES = {"CI_PASSED", "CI_FAILED"}
AUTOFIX_TERMINAL_PHASES = {
    "BLOCKED", "SUGGESTION_ONLY", "STALE_SOURCE", *CI_TERMINAL_PHASES,
}

CI_CONCLUSIONS = {
    "success": "CI_PASSED",
    "failure": "CI_FAILED",
    "timed_out": "CI_FAILED",
    "action_required": "CI_FAILED",
    "startup_failure": "CI_FAILED",
    "cancelled": "CI_FAILED",
}
AMBIGUOUS_CI_CONCLUSIONS = {"neutral", "skipped", "stale"}
SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class WorkflowEventError(RuntimeError):
    code = "WORKFLOW_EVENT_ERROR"


class WorkflowTransitionInvalid(WorkflowEventError):
    code = "WORKFLOW_TRANSITION_INVALID"


class CIConclusionUnsupported(WorkflowEventError):
    code = "CI_CONCLUSION_UNSUPPORTED"


def canonical_ci_authority(app_id: Any = "", app_slug: Any = "") -> Dict[str, str]:
    """Normalize the configured GitHub App selector for one canonical check suite."""
    normalized_id = str(app_id or "").strip()
    normalized_slug = str(app_slug or "").strip().lower()
    if normalized_id and not normalized_id.isdigit():
        raise ValueError("canonical CI GitHub App ID must be numeric")
    if not normalized_id and not normalized_slug:
        raise ValueError(
            "canonical CI authority is not configured; set "
            "EVOAGENT_GITHUB_CI_APP_ID and/or EVOAGENT_GITHUB_CI_APP_SLUG"
        )
    return {"github_app_id": normalized_id, "github_app_slug": normalized_slug}


def check_suite_matches_authority(
    metadata: Dict[str, Any], app_id: Any, app_slug: Any,
) -> bool:
    try:
        expected = canonical_ci_authority(app_id, app_slug)
    except ValueError:
        # Legacy/unbound correlations are deliberately not authorized by SHA alone.
        return False
    actual_id = str((metadata or {}).get("app_id") or "").strip()
    actual_slug = str((metadata or {}).get("app_slug") or "").strip().lower()
    return (
        (not expected["github_app_id"] or actual_id == expected["github_app_id"])
        and (not expected["github_app_slug"] or actual_slug == expected["github_app_slug"])
    )


def transition_autofix_state(
    state: Dict[str, Any], target: str, **updates: Any,
) -> Dict[str, Any]:
    """Return one legal immutable state transition; same-phase replay is a no-op."""
    current = str((state or {}).get("phase") or "")
    if target not in AUTOFIX_TRANSITIONS:
        raise WorkflowTransitionInvalid("unknown AutoFix phase: %s" % target)
    if current == target:
        return copy.deepcopy(state)
    if target not in AUTOFIX_TRANSITIONS.get(current, set()):
        raise WorkflowTransitionInvalid(
            "illegal AutoFix transition: %s -> %s" % (current or "<created>", target)
        )
    value = copy.deepcopy(state or {})
    value.update(updates)
    value["phase"] = target
    value["workflow_revision"] = int(value.get("workflow_revision", 0) or 0) + 1
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(prefix: str, value: Dict[str, Any]) -> str:
    return prefix + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkflowEvent:
    event_id: str
    logical_event_key: str
    external_event_key: str
    source: str
    event_type: str
    tenant_id: str
    repository: str
    external_object_id: str
    head_sha: str
    external_status: str
    conclusion: str
    outcome: Optional[str]
    received_at: str
    metadata: Dict[str, Any]
    policy_result: str

    @classmethod
    def from_github_check_suite(
        cls, payload: Dict[str, Any], tenant_id: str, received_at: str,
    ) -> "WorkflowEvent":
        suite = payload.get("check_suite") or {}
        repository = str((payload.get("repository") or {}).get("full_name") or "").strip()
        object_id = str(suite.get("id") or "").strip()
        head_sha = str(suite.get("head_sha") or "").strip().lower()
        status = str(suite.get("status") or "").strip().lower()
        conclusion = str(suite.get("conclusion") or "").strip().lower()
        if not repository or not object_id or not SHA.fullmatch(head_sha):
            raise ValueError("invalid GitHub check_suite correlation payload")
        if status not in {"queued", "in_progress", "completed"}:
            raise ValueError("unsupported GitHub check_suite status: %s" % status)

        outcome = None
        if status != "completed":
            policy_result = "WORKFLOW_EVENT_NON_TERMINAL"
            conclusion = ""
        elif conclusion in CI_CONCLUSIONS:
            outcome = CI_CONCLUSIONS[conclusion]
            policy_result = outcome
        elif conclusion in AMBIGUOUS_CI_CONCLUSIONS or conclusion:
            policy_result = CIConclusionUnsupported.code
        else:
            policy_result = CIConclusionUnsupported.code

        external_identity = {
            "source": "github", "event_type": "check_suite",
            "tenant_id": str(tenant_id or "default"),
            "repository": repository.lower(), "external_object_id": object_id,
            "head_sha": head_sha,
        }
        logical_identity = {
            **external_identity, "status": status, "conclusion": conclusion,
        }
        external_key = _identity("github-check-suite-", external_identity)
        logical_key = _identity("workflow-logical-", logical_identity)
        app = suite.get("app") or {}
        pull_requests = suite.get("pull_requests") or []
        metadata = {
            "action": str(payload.get("action") or "")[:80],
            "app_id": app.get("id"),
            "app_slug": str(app.get("slug") or "")[:120],
            "pull_request_numbers": [
                item.get("number") for item in pull_requests[:20]
                if isinstance(item, dict) and isinstance(item.get("number"), int)
            ],
        }
        return cls(
            _identity("workflow-event-", {"logical_event_key": logical_key}),
            logical_key, external_key, "github", "check_suite",
            str(tenant_id or "default"), repository, object_id, head_sha,
            status, conclusion, outcome, received_at, metadata, policy_result,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "logical_event_key": self.logical_event_key,
            "external_event_key": self.external_event_key,
            "source": self.source,
            "event_type": self.event_type,
            "tenant_id": self.tenant_id,
            "repository": self.repository,
            "external_object_id": self.external_object_id,
            "head_sha": self.head_sha,
            "external_status": self.external_status,
            "conclusion": self.conclusion,
            "outcome": self.outcome,
            "received_at": self.received_at,
            "metadata": copy.deepcopy(self.metadata),
            "policy_result": self.policy_result,
        }
