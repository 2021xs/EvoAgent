"""Durable, task-scoped artifacts for repository tool evidence.

Only ``TOOL_RESULT`` is produced in Phase 1.  The broader type vocabulary is
kept here so callers do not need another persistence model when later phases
begin producing other durable evidence.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import uuid
from typing import Any, Dict, Optional, Protocol


MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_TASK_ARTIFACT_BYTES = 4 * 1024 * 1024
DEFAULT_ARTIFACT_READ_CHARS = 2000
MAX_ARTIFACT_READ_CHARS = 12000


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ArtifactType(str, Enum):
    TOOL_RESULT = "TOOL_RESULT"
    CANDIDATE_FINDING = "CANDIDATE_FINDING"
    AGENT_RESULT = "AGENT_RESULT"
    CRITIC_DECISION = "CRITIC_DECISION"
    EVALUATION_RESULT = "EVALUATION_RESULT"


class ArtifactError(RuntimeError):
    code = "ARTIFACT_ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.message)


class ArtifactPersistFailed(ArtifactError):
    code = "ARTIFACT_PERSIST_FAILED"


class ArtifactIntegrityConflict(ArtifactError):
    code = "ARTIFACT_INTEGRITY_CONFLICT"


class ArtifactNotFound(ArtifactError):
    code = "ARTIFACT_NOT_FOUND"


class ArtifactAccessDenied(ArtifactError):
    code = "ARTIFACT_ACCESS_DENIED"


class ArtifactCorrupted(ArtifactError):
    code = "ARTIFACT_CORRUPTED"


class ArtifactStore(Protocol):
    def put_artifact(self, artifact: Dict[str, Any]) -> Dict[str, Any]: ...

    def get_artifact(
        self, artifact_id: str, tenant_id: str, repository: str, task_id: str,
    ) -> Dict[str, Any]: ...

    def get_artifact_by_logical_execution_key(
        self, logical_execution_key: str, tenant_id: str,
        repository: str, task_id: str,
    ) -> Optional[Dict[str, Any]]: ...


@dataclass(frozen=True)
class ArtifactScope:
    tenant_id: str
    repository: str
    task_id: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "repository": self.repository,
            "task_id": self.task_id,
        }


@dataclass(frozen=True)
class ToolExecutionKey:
    """Replay-stable identity for one logical repository read.

    ``run_id`` is intentionally absent: an unfinished worker may be rebuilt
    with a new runtime run.  Assignment/message identity, revision round and
    tool-loop step are persisted or deterministically reconstructed instead.
    """

    task_id: str
    assignment_id: str
    revision_round: int
    role: str
    interaction_id: str
    step: int
    tool_name: str
    arguments: Dict[str, Any]
    source_revision: str

    def payload(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "task_id": self.task_id,
            "assignment_id": self.assignment_id,
            "revision_round": int(self.revision_round),
            "role": self.role,
            "interaction_id": self.interaction_id,
            "step": int(self.step),
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "source_revision": self.source_revision,
        }

    @property
    def value(self) -> str:
        return "tool-exec:" + _sha256_text(_canonical_json(self.payload()))


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    artifact_type: str
    evidence_id: str
    tool: str
    content_hash: str
    content_size_bytes: int
    logical_execution_key: str
    output_preview: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "evidence_id": self.evidence_id,
            "tool": self.tool,
            "content_hash": self.content_hash,
            "content_size_bytes": self.content_size_bytes,
            "logical_execution_key": self.logical_execution_key,
            "output_preview": self.output_preview,
        }


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    artifact_type: str
    scope: ArtifactScope
    producer: str
    source_revision: str
    logical_execution_key: str
    content_hash: str
    content_size_bytes: int
    content: Any
    evidence_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @classmethod
    def for_tool_result(
        cls,
        scope: ArtifactScope,
        producer: str,
        source_revision: str,
        logical_execution_key: str,
        content: Any,
        evidence_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "Artifact":
        try:
            serialized = _canonical_json(content)
        except (TypeError, ValueError) as exc:
            raise ArtifactPersistFailed(
                "tool result is not JSON-compatible"
            ) from exc
        size = len(serialized.encode("utf-8"))
        if size > MAX_ARTIFACT_BYTES:
            raise ArtifactPersistFailed(
                "tool result is %d bytes; the supported limit is %d"
                % (size, MAX_ARTIFACT_BYTES)
            )
        return cls(
            artifact_id="artifact:" + uuid.uuid4().hex,
            artifact_type=ArtifactType.TOOL_RESULT.value,
            scope=scope,
            producer=producer,
            source_revision=source_revision,
            logical_execution_key=logical_execution_key,
            content_hash=_sha256_text(serialized),
            content_size_bytes=size,
            content=content,
            evidence_id=evidence_id,
            metadata=dict(metadata or {}),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Artifact":
        return cls(
            artifact_id=value["artifact_id"],
            artifact_type=value["artifact_type"],
            scope=ArtifactScope(
                value["tenant_id"], value["repository"], value["task_id"]
            ),
            producer=value["producer"],
            source_revision=value.get("source_revision", ""),
            logical_execution_key=value["logical_execution_key"],
            content_hash=value["content_hash"],
            content_size_bytes=int(value["content_size_bytes"]),
            content=value["content"],
            evidence_id=value.get("evidence_id", ""),
            metadata=dict(value.get("metadata") or {}),
            created_at=value["created_at"],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            **self.scope.to_dict(),
            "producer": self.producer,
            "source_revision": self.source_revision,
            "logical_execution_key": self.logical_execution_key,
            "content_hash": self.content_hash,
            "content_size_bytes": self.content_size_bytes,
            "content": self.content,
            "evidence_id": self.evidence_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }

    def verify(self) -> None:
        if self.artifact_type not in {item.value for item in ArtifactType}:
            raise ArtifactCorrupted("stored artifact type is unknown")
        if not self.artifact_id.startswith("artifact:") or len(self.content_hash) != 64:
            raise ArtifactCorrupted("stored artifact identity is malformed")
        if not all((self.scope.tenant_id, self.scope.repository, self.scope.task_id)):
            raise ArtifactCorrupted("stored artifact scope is incomplete")
        if self.artifact_type == ArtifactType.TOOL_RESULT.value and (
            not self.evidence_id or not self.logical_execution_key
            or not str(self.metadata.get("tool") or "")
        ):
            raise ArtifactCorrupted("stored Tool Result provenance is incomplete")
        serialized = _canonical_json(self.content)
        actual_hash = _sha256_text(serialized)
        actual_size = len(serialized.encode("utf-8"))
        if actual_hash != self.content_hash or actual_size != self.content_size_bytes:
            raise ArtifactCorrupted("stored artifact content failed integrity verification")

    def ref(self, preview_chars: int = DEFAULT_ARTIFACT_READ_CHARS) -> ArtifactRef:
        output = self.content.get("output") if isinstance(self.content, dict) else self.content
        preview = _canonical_json(output)[:max(0, preview_chars)]
        return ArtifactRef(
            artifact_id=self.artifact_id,
            artifact_type=self.artifact_type,
            evidence_id=self.evidence_id,
            tool=str(self.metadata.get("tool", "")),
            content_hash=self.content_hash,
            content_size_bytes=self.content_size_bytes,
            logical_execution_key=self.logical_execution_key,
            output_preview=preview,
        )


def artifact_from_store(value: Optional[Dict[str, Any]]) -> Optional[Artifact]:
    if value is None:
        return None
    artifact = Artifact.from_dict(value)
    artifact.verify()
    return artifact


class ArtifactRuntime:
    """Scope-bound write-before-reference and replay service for one worker."""

    def __init__(
        self,
        store: ArtifactStore,
        scope: ArtifactScope,
        source_revision: str,
        assignment_id: str,
        revision_round: int,
        role: str,
        interaction_id: str,
        producer_run_id: str = "",
    ):
        self.store = store
        self.scope = scope
        self.source_revision = source_revision
        self.assignment_id = assignment_id
        self.revision_round = revision_round
        self.role = role
        self.interaction_id = interaction_id
        self.producer_run_id = producer_run_id

    def execution_key(
        self, step: int, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolExecutionKey:
        return ToolExecutionKey(
            task_id=self.scope.task_id,
            assignment_id=self.assignment_id,
            revision_round=self.revision_round,
            role=self.role,
            interaction_id=self.interaction_id,
            step=step,
            tool_name=tool_name,
            arguments=arguments,
            source_revision=self.source_revision,
        )

    def invoke(
        self, registry, tool_name: str, arguments: Dict[str, Any], step: int,
    ) -> tuple:
        key = self.execution_key(step, tool_name, arguments)
        try:
            stored = self.store.get_artifact_by_logical_execution_key(
                key.value, **self.scope.to_dict()
            )
        except ArtifactError:
            raise
        except Exception as exc:
            raise ArtifactPersistFailed("artifact replay lookup failed: %s" % exc) from exc
        if stored is not None:
            artifact = artifact_from_store(stored)
            return self._working_view(artifact), artifact.ref(), True

        result = registry.invoke(tool_name, arguments)
        if not isinstance(result, dict) or not result.get("evidence_id"):
            raise ArtifactPersistFailed(
                "artifact-replay tools must return an evidence-bearing object"
            )
        artifact = Artifact.for_tool_result(
            scope=self.scope,
            producer=self.role,
            source_revision=self.source_revision,
            logical_execution_key=key.value,
            content=result,
            evidence_id=str(result["evidence_id"]),
            metadata={
                "tool": tool_name,
                "arguments": arguments,
                "execution_key_payload": key.payload(),
                "assignment_id": self.assignment_id,
                "revision_round": self.revision_round,
                "interaction_id": self.interaction_id,
                "producer_run_id": self.producer_run_id,
            },
        )
        try:
            persisted = self.store.put_artifact(artifact.to_dict())
            artifact = artifact_from_store(persisted)
        except ArtifactError:
            raise
        except Exception as exc:
            raise ArtifactPersistFailed("artifact write failed: %s" % exc) from exc
        return self._working_view(artifact), artifact.ref(), False

    def get(self, artifact_id: str) -> Artifact:
        try:
            stored = self.store.get_artifact(artifact_id, **self.scope.to_dict())
        except ArtifactError:
            raise
        except Exception as exc:
            raise ArtifactPersistFailed("artifact read failed: %s" % exc) from exc
        return artifact_from_store(stored)

    def materialize(
        self, artifact_id: str, offset: int = 0,
        max_chars: int = DEFAULT_ARTIFACT_READ_CHARS,
    ) -> Dict[str, Any]:
        artifact = self.get(artifact_id)
        limit = max(1, min(int(max_chars), MAX_ARTIFACT_READ_CHARS))
        serialized = _canonical_json(artifact.content)
        start = min(len(serialized), max(0, int(offset)))
        end = min(len(serialized), start + limit)
        return {
            "artifact_id": artifact.artifact_id,
            "content_hash": artifact.content_hash,
            "total_size_bytes": artifact.content_size_bytes,
            "total_chars": len(serialized),
            "range": {"start": start, "end": end},
            "truncated": end < len(serialized),
            "content": serialized[start:end],
        }

    def resolve_ref(
        self, reference: Dict[str, Any], max_chars: int = DEFAULT_ARTIFACT_READ_CHARS,
    ) -> Dict[str, Any]:
        artifact = self.get(str(reference.get("artifact_id") or ""))
        expected = artifact.ref().to_dict()
        for field in (
            "artifact_id", "artifact_type", "evidence_id", "tool",
            "content_hash", "logical_execution_key",
        ):
            if str(reference.get(field) or "") != str(expected.get(field) or ""):
                raise ArtifactCorrupted("artifact reference %s does not match storage" % field)
        output = (
            artifact.content.get("output")
            if isinstance(artifact.content, dict) else artifact.content
        )
        return {
            "evidence_id": artifact.evidence_id,
            "tool": str(artifact.metadata.get("tool", "")),
            "output_preview": _canonical_json(output)[:max(0, int(max_chars))],
            "content_available": True,
        }

    @staticmethod
    def _working_view(artifact: Artifact) -> Dict[str, Any]:
        result = dict(artifact.content)
        output = result.get("output")
        serialized = _canonical_json(output)
        if len(serialized) > DEFAULT_ARTIFACT_READ_CHARS:
            result["output"] = serialized[:DEFAULT_ARTIFACT_READ_CHARS]
            result["artifact_truncated"] = True
        result["artifact_ref"] = artifact.ref().to_dict()
        return result
