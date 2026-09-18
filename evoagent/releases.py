"""Immutable executable release identities for durable Review Tasks."""

import copy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Dict


RELEASE_SPEC_SCHEMA_VERSION = 1


class ReleaseError(RuntimeError):
    """Base class for release persistence and materialization failures."""


class ReleaseNotFound(ReleaseError):
    """The pinned Release does not exist in the caller's tenant scope."""


class ReleaseIntegrityError(ReleaseError):
    """Stored immutable Release content failed integrity validation."""


def canonical_release_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def release_spec_sha256(spec: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_release_json(spec).encode("utf-8")).hexdigest()


def release_identity(tenant_id: str, spec_sha256: str) -> str:
    digest = hashlib.sha256(canonical_release_json({
        "tenant_id": tenant_id,
        "spec_sha256": spec_sha256,
    }).encode("utf-8")).hexdigest()
    return "release-" + digest


@dataclass(frozen=True)
class ReleaseBundle:
    release_id: str
    tenant_id: str
    spec: Dict[str, Any]
    spec_sha256: str
    parent_release_id: str
    created_at: str

    @classmethod
    def create(
        cls, tenant_id: str, spec: Dict[str, Any], created_at: str,
        parent_release_id: str = "",
    ) -> "ReleaseBundle":
        tenant = str(tenant_id or "default")
        normalized = copy.deepcopy(spec)
        if normalized.get("schema_version") != RELEASE_SPEC_SCHEMA_VERSION:
            raise ReleaseIntegrityError("unsupported Release specification schema")
        digest = release_spec_sha256(normalized)
        return cls(
            release_identity(tenant, digest), tenant, normalized, digest,
            str(parent_release_id or ""), str(created_at),
        )

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "ReleaseBundle":
        try:
            bundle = cls(
                str(record["release_id"]), str(record["tenant_id"]),
                copy.deepcopy(record["spec"]), str(record["spec_sha256"]),
                str(record.get("parent_release_id") or ""),
                str(record["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseIntegrityError("stored Release is malformed") from exc
        actual_hash = release_spec_sha256(bundle.spec)
        if actual_hash != bundle.spec_sha256:
            raise ReleaseIntegrityError("stored Release specification hash mismatch")
        if release_identity(bundle.tenant_id, actual_hash) != bundle.release_id:
            raise ReleaseIntegrityError("stored Release identity mismatch")
        if bundle.spec.get("schema_version") != RELEASE_SPEC_SCHEMA_VERSION:
            raise ReleaseIntegrityError("unsupported Release specification schema")
        return bundle

    def to_dict(self) -> Dict[str, Any]:
        return {
            "release_id": self.release_id,
            "tenant_id": self.tenant_id,
            "spec": copy.deepcopy(self.spec),
            "spec_sha256": self.spec_sha256,
            "parent_release_id": self.parent_release_id,
            "created_at": self.created_at,
        }
