"""Immutable runtime Releases plus legacy canary/shadow bookkeeping."""
import hashlib
from typing import Dict, Optional


class ReleaseManager:
    def __init__(self, store):
        self.store = store

    def create(
        self, tenant_id: str, spec: Dict[str, object], parent_release_id: str = "",
    ) -> dict:
        """Create or reuse one immutable executable ReleaseBundle."""
        return self.store.put_release(tenant_id, spec, parent_release_id)

    def active(self, tenant_id: str) -> Optional[dict]:
        return self.store.get_active_release(tenant_id)

    def ensure_active(self, tenant_id: str, initial_spec: Dict[str, object]) -> dict:
        """Bootstrap one tenant's active Release without replacing an existing pointer."""
        active = self.active(tenant_id)
        if active:
            return active
        created = self.create(tenant_id, initial_spec)
        return self.activate(tenant_id, created["release_id"])

    def activate(self, tenant_id: str, release_id: str) -> dict:
        """Atomically move the active pointer; immutable Releases are never rewritten."""
        return self.store.activate_release(tenant_id, release_id)

    def publish(
        self, tenant_id: str, spec: Dict[str, object], parent_release_id: str = "",
    ) -> dict:
        """Create/reuse and activate a validated runtime composition."""
        release = self.create(tenant_id, spec, parent_release_id)
        return self.activate(tenant_id, release["release_id"])

    def rollback(self, tenant_id: str, release_id: str) -> dict:
        """Rollback by pointer movement; already-pinned Tasks remain unchanged."""
        return self.activate(tenant_id, release_id)

    def configure(self, tenant_id: str, skill_name: str, config: Dict[str, object]) -> dict:
        canary = int(config.get("canary_percent", 0))
        shadow = int(config.get("shadow_percent", 0))
        if not 0 <= canary <= 100 or not 0 <= shadow <= 100:
            raise ValueError("canary_percent and shadow_percent must be between 0 and 100")
        if config.get("candidate_version") is None:
            raise ValueError("candidate_version is required")
        self.store.save_deployment(tenant_id, skill_name, config)
        return self.store.get_deployment(tenant_id, skill_name)

    def assignment(self, tenant_id: str, skill_name: str, key: str) -> Dict[str, object]:
        deployment = self.store.get_deployment(tenant_id, skill_name)
        if not deployment or deployment["status"] != "running":
            return {"lane": "stable", "shadow": False, "deployment": None}
        bucket = int(hashlib.sha256(
            ("%s:%s:%s" % (tenant_id, skill_name, key)).encode("utf-8")
        ).hexdigest()[:8], 16) % 100
        return {
            "lane": "canary" if bucket < deployment["canary_percent"] else "stable",
            "shadow": bucket < deployment["shadow_percent"],
            "deployment": deployment,
        }

    def observe(
        self, tenant_id: str, skill_name: str, failed: bool,
        lane: str = "canary",
    ) -> Optional[dict]:
        if lane != "canary":
            return self.store.get_deployment(tenant_id, skill_name)
        result = self.store.record_deployment_result(tenant_id, skill_name, failed)
        if result and result["status"] == "rolled_back":
            self.store.create_alert(
                tenant_id, "rollout:%s" % skill_name, "critical",
                "Canary %s was automatically rolled back after exceeding its error budget." % skill_name,
            )
        return result

    def observe_shadow(
        self, tenant_id: str, skill_name: str, task_id: str, lane: str,
        primary: Dict[str, object], candidate: Optional[Dict[str, object]],
        candidate_failed: bool = False,
    ) -> Optional[dict]:
        primary_keys = set(primary.get("finding_keys", []))
        candidate_keys = set((candidate or {}).get("finding_keys", []))
        union = primary_keys | candidate_keys
        disagreement = len(primary_keys ^ candidate_keys) / len(union) if union else 0.0
        result = self.store.record_shadow_observation(
            tenant_id, skill_name, task_id, lane, primary, candidate,
            disagreement, candidate_failed,
        )
        if result and result["status"] == "promoted":
            self.store.create_alert(
                tenant_id, "rollout-promoted:%s" % skill_name, "info",
                "Candidate %s was automatically promoted after shadow verification." % skill_name,
            )
        return result
