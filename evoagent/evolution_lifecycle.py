"""Evidence-backed attribution routing and shared evolution-candidate lifecycle."""

import copy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Dict, Optional
import uuid


ATTRIBUTION_STATUSES = {"SUPPORTED", "INSUFFICIENT_EVIDENCE", "UNKNOWN"}
EVOLUTION_SURFACES = {"GLOBAL_PROMPT", "SKILL", "NO_SUPPORTED_EVOLUTION"}
CANDIDATE_STATUSES = {
    "CREATED", "VALIDATING", "REJECTED", "READY_FOR_PROMOTION", "PROMOTED",
}
CANDIDATE_TRANSITIONS = {
    "CREATED": {"VALIDATING"},
    "VALIDATING": {"REJECTED", "READY_FOR_PROMOTION"},
    "REJECTED": set(),
    "READY_FOR_PROMOTION": {"PROMOTED"},
    "PROMOTED": set(),
}


class PromotionError(ValueError):
    code = "PROMOTION_CONFLICT"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PromotionStaleParent(PromotionError):
    code = "PROMOTION_STALE_PARENT"


class PromotionConflict(PromotionError):
    code = "PROMOTION_CONFLICT"


class PromotionIntegrityConflict(PromotionError):
    code = "PROMOTION_INTEGRITY_CONFLICT"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized_change_hash(change: Any) -> str:
    return hashlib.sha256(canonical_json(change).encode("utf-8")).hexdigest()


def candidate_identity(
    tenant_id: str, surface: str, target_id: str,
    parent_version: Optional[int], change_hash: str,
) -> str:
    value = {
        "tenant_id": tenant_id, "surface": surface, "target_id": target_id,
        "parent_version": parent_version, "change_hash": change_hash,
    }
    return "evolution-candidate-" + hashlib.sha256(
        canonical_json(value).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class AttributionResult:
    attribution_id: str
    failure_id: int
    task_id: str
    status: str
    failure_layer: str
    actor: str
    semantic_cause: str
    target_surface: str
    target_id: str
    evidence_refs: list
    alternative_hypotheses: list
    method: str
    reason: str
    created_at: str

    @classmethod
    def create(
        cls, failure_id: int, task_id: str, value: Dict[str, Any], created_at: str,
    ) -> "AttributionResult":
        status = str(value.get("status") or "UNKNOWN").upper()
        if status == "DETERMINISTIC":
            status = "SUPPORTED"
        if status not in ATTRIBUTION_STATUSES:
            status = "UNKNOWN"
        surface = str(
            value.get("target_surface") or value.get("evolution_surface")
            or "NO_SUPPORTED_EVOLUTION"
        ).upper()
        if surface not in EVOLUTION_SURFACES:
            surface = "NO_SUPPORTED_EVOLUTION"
        target_id = str(
            value.get("target_id") or value.get("evolution_target") or ""
        )
        if surface == "SKILL" and not target_id:
            surface = "NO_SUPPORTED_EVOLUTION"
        if status != "SUPPORTED":
            surface, target_id = "NO_SUPPORTED_EVOLUTION", ""
        return cls(
            str(uuid.uuid4()), int(failure_id), str(task_id), status,
            str(value.get("failure_layer") or value.get("first_divergence") or "UNKNOWN"),
            str(value.get("actor") or ""),
            str(value.get("semantic_cause") or value.get("root_cause") or ""),
            surface, target_id,
            copy.deepcopy(value.get("evidence_refs") or []),
            copy.deepcopy(value.get("alternative_hypotheses") or []),
            str(value.get("method") or (
                "DETERMINISTIC" if value.get("status") == "DETERMINISTIC" else "MODEL"
            )),
            str(value.get("reason") or "")[:2000], str(created_at),
        )

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self.__dict__)


class EvolutionRouter:
    """Route only evidence-supported causes to the two supported surfaces."""

    @staticmethod
    def route(attribution: Dict[str, Any]) -> Dict[str, str]:
        if str(attribution.get("status")) != "SUPPORTED":
            return {"surface": "NO_SUPPORTED_EVOLUTION", "target_id": ""}
        cause = str(attribution.get("semantic_cause") or "").upper()
        surface = str(attribution.get("target_surface") or "").upper()
        target = str(attribution.get("target_id") or "")
        if cause == "SKILL_GUIDANCE_GAP" and surface == "SKILL" and target:
            return {"surface": "SKILL", "target_id": target}
        if cause == "SYSTEM_POLICY_GAP" and surface == "GLOBAL_PROMPT":
            return {"surface": "GLOBAL_PROMPT", "target_id": "llm-review"}
        return {"surface": "NO_SUPPORTED_EVOLUTION", "target_id": ""}


@dataclass(frozen=True)
class EvolutionCandidate:
    candidate_id: str
    tenant_id: str
    surface: str
    target_id: str
    parent_version: Optional[int]
    parent_release_id: str
    attribution_id: str
    source_failure_ids: list
    evidence_refs: list
    change: Any
    change_hash: str
    generation_method: str
    generation_model: str
    generation_config: Dict[str, Any]
    status: str
    validation_result: Dict[str, Any]
    final_evaluation_result: Dict[str, Any]
    surface_version: Dict[str, Any]
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls, tenant_id: str, surface: str, target_id: str,
        parent_version: Optional[int], parent_release_id: str,
        attribution_id: str, failure_ids: list, evidence_refs: list,
        change: Any, generation: Dict[str, Any], created_at: str,
    ) -> "EvolutionCandidate":
        if surface not in {"GLOBAL_PROMPT", "SKILL"}:
            raise ValueError("unsupported evolution candidate surface")
        digest = normalized_change_hash(change)
        identity = candidate_identity(
            tenant_id, surface, target_id, parent_version, digest,
        )
        return cls(
            identity, tenant_id, surface, target_id, parent_version,
            parent_release_id, attribution_id,
            sorted({int(value) for value in failure_ids}),
            copy.deepcopy(evidence_refs), copy.deepcopy(change), digest,
            str(generation.get("method") or "MODEL"),
            str(generation.get("model") or ""),
            copy.deepcopy(generation.get("config") or {}),
            "CREATED", {}, {}, {}, str(created_at), str(created_at),
        )

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self.__dict__)


class CandidateLifecycle:
    """Shared persistence, dedup, evaluation and promotion state transitions."""

    def __init__(self, store):
        self.store = store

    def create(self, candidate: EvolutionCandidate) -> Dict[str, Any]:
        return self.store.put_evolution_candidate(candidate.to_dict())

    def evaluate(
        self, candidate_id: str, evaluator: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        candidate = self.store.get_evolution_candidate(candidate_id)
        if not candidate:
            raise ValueError("evolution candidate not found")
        if candidate["status"] not in {"CREATED", "VALIDATING"}:
            return candidate
        if candidate["status"] == "CREATED":
            self.store.transition_evolution_candidate(candidate_id, "VALIDATING", {})
        try:
            result = evaluator(candidate)
        except Exception as exc:
            result = {"eligible": False, "reason": str(exc)[:1000], "error": True}
        target = "READY_FOR_PROMOTION" if result.get("eligible") else "REJECTED"
        return self.store.transition_evolution_candidate(
            candidate_id, target, {
                # This is the operational validation/gating result. It is not an
                # independent final benchmark and must not be labelled as one.
                "validation_result": copy.deepcopy(result),
            },
        )
