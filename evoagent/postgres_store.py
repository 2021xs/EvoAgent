"""PostgreSQL persistence backend.

The implementation mirrors TaskStore's public API and is selected when
EVOAGENT_DATABASE_URL starts with postgres. psycopg is an optional production
dependency so local development can remain zero-config.
"""
import hashlib
import json
from typing import Any, Dict, Optional

from .artifacts import (
    ArtifactAccessDenied,
    ArtifactIntegrityConflict,
    ArtifactNotFound,
    ArtifactPersistFailed,
    MAX_TASK_ARTIFACT_BYTES,
)
from .models import ReviewReport, TaskState, TraceEvent
from .releases import ReleaseBundle, ReleaseNotFound
from .store import utc_now


class PostgresTaskStore:
    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL mode requires: pip install psycopg[binary]") from exc
        self.psycopg = psycopg
        self.dict_row = dict_row
        self.url = url
        self._init()

    def _connect(self):
        return self.psycopg.connect(self.url, row_factory=self.dict_row)

    def _init(self) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, repository TEXT NOT NULL,
                pull_request INTEGER, release_id TEXT NOT NULL DEFAULT '',
                input_json JSONB NOT NULL, report_json JSONB,
                error TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS trace_events (
                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), step INTEGER NOT NULL,
                state TEXT NOT NULL, message TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS failure_cases (
                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL, category TEXT NOT NULL,
                payload_json JSONB NOT NULL, resolved BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS skill_versions (
                id BIGSERIAL PRIMARY KEY, skill_name TEXT NOT NULL, version INTEGER NOT NULL,
                prompt TEXT NOT NULL, score DOUBLE PRECISION NOT NULL, active BOOLEAN NOT NULL DEFAULT FALSE,
                parent_version INTEGER, created_at TIMESTAMPTZ NOT NULL, UNIQUE(skill_name, version))""",
            """CREATE TABLE IF NOT EXISTS installations (
                installation_id BIGINT PRIMARY KEY, account_login TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS evaluation_cases (
                id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, split TEXT NOT NULL,
                diff TEXT NOT NULL, expected_json JSONB NOT NULL, source TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS evolution_runs (
                id TEXT PRIMARY KEY, skill_name TEXT NOT NULL, candidate_version INTEGER NOT NULL,
                baseline_version INTEGER, decision TEXT NOT NULL, candidate_score DOUBLE PRECISION NOT NULL,
                baseline_score DOUBLE PRECISION NOT NULL, metrics_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS skill_artifact_versions (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL DEFAULT 'default',
                skill_name TEXT NOT NULL, version INTEGER NOT NULL,
                artifact_json JSONB NOT NULL, artifact_sha256 TEXT NOT NULL,
                score DOUBLE PRECISION NOT NULL, active BOOLEAN NOT NULL DEFAULT FALSE,
                parent_version INTEGER, created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(tenant_id, skill_name, version))""",
            """CREATE TABLE IF NOT EXISTS skill_evolution_runs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL DEFAULT 'default',
                skill_name TEXT NOT NULL, candidate_version INTEGER NOT NULL,
                baseline_version INTEGER, decision TEXT NOT NULL,
                candidate_score DOUBLE PRECISION NOT NULL, baseline_score DOUBLE PRECISION NOT NULL,
                metrics_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",
            "ALTER TABLE skill_artifact_versions ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE skill_evolution_runs ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS release_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE installations ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            """CREATE TABLE IF NOT EXISTS checkpoints (
                task_id TEXT NOT NULL REFERENCES tasks(id), node TEXT NOT NULL, status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1, state_json JSONB NOT NULL, error TEXT,
                updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(task_id,node))""",
            """CREATE TABLE IF NOT EXISTS task_payloads (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id), diff TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS agent_messages (
                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                sender TEXT NOT NULL, recipient TEXT NOT NULL, kind TEXT NOT NULL,
                correlation_id TEXT NOT NULL, content_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS webhook_deliveries (
                delivery_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, event_type TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, task_id TEXT, received_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS memberships (
                user_id TEXT NOT NULL REFERENCES users(id), tenant_id TEXT NOT NULL, role TEXT NOT NULL,
                PRIMARY KEY(user_id,tenant_id))""",
            """CREATE TABLE IF NOT EXISTS repository_grants (
                tenant_id TEXT NOT NULL, repository TEXT NOT NULL, auto_fix BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY(tenant_id,repository))""",
            """CREATE TABLE IF NOT EXISTS audit_log (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, resource TEXT NOT NULL, detail_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS deployments (
                tenant_id TEXT NOT NULL, skill_name TEXT NOT NULL, stable_version INTEGER,
                candidate_version INTEGER, canary_percent INTEGER NOT NULL DEFAULT 0,
                shadow_percent INTEGER NOT NULL DEFAULT 0, max_error_rate DOUBLE PRECISION NOT NULL DEFAULT .1,
                min_samples INTEGER NOT NULL DEFAULT 20, status TEXT NOT NULL DEFAULT 'stable',
                samples INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(tenant_id,skill_name))""",
            """CREATE TABLE IF NOT EXISTS alerts (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, alert_key TEXT NOT NULL,
                severity TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE(tenant_id,alert_key,status))""",
            """CREATE TABLE IF NOT EXISTS agent_memories (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, repository TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '', agent TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                keywords_json JSONB NOT NULL, metadata_json JSONB NOT NULL,
                importance DOUBLE PRECISION NOT NULL DEFAULT .5,
                created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ)""",
            """CREATE INDEX IF NOT EXISTS idx_agent_memories_lookup
                ON agent_memories(tenant_id,repository,scope,created_at)""",
            """CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY, artifact_type TEXT NOT NULL,
                tenant_id TEXT NOT NULL, repository TEXT NOT NULL,
                task_id TEXT NOT NULL REFERENCES tasks(id), producer TEXT NOT NULL,
                source_revision TEXT NOT NULL, logical_execution_key TEXT NOT NULL,
                content_hash TEXT NOT NULL, content_size_bytes BIGINT NOT NULL,
                content_json JSONB NOT NULL, evidence_id TEXT NOT NULL,
                metadata_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(tenant_id,repository,task_id,logical_execution_key))""",
            """CREATE INDEX IF NOT EXISTS idx_artifacts_task
                ON artifacts(tenant_id,repository,task_id,created_at)""",
            """CREATE TABLE IF NOT EXISTS releases (
                release_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                spec_json JSONB NOT NULL, spec_sha256 TEXT NOT NULL,
                parent_release_id TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(tenant_id,spec_sha256))""",
            """CREATE TABLE IF NOT EXISTS active_releases (
                tenant_id TEXT PRIMARY KEY, release_id TEXT NOT NULL REFERENCES releases(release_id),
                previous_release_id TEXT NOT NULL DEFAULT '', updated_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS release_activations (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL,
                release_id TEXT NOT NULL, previous_release_id TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL)""",
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)

    @staticmethod
    def _artifact_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["content"] = value.pop("content_json")
        value["metadata"] = value.pop("metadata_json")
        value["created_at"] = value["created_at"].isoformat()
        return value

    def put_artifact(self, artifact: Dict[str, Any]) -> Dict[str, Any]:
        scope = (artifact["tenant_id"], artifact["repository"], artifact["task_id"])
        with self._connect() as conn:
            # The task row serializes both logical-key insertion and its byte budget.
            owner = conn.execute(
                "SELECT tenant_id,repository FROM tasks WHERE id=%s FOR UPDATE",
                (artifact["task_id"],),
            ).fetchone()
            if not owner:
                raise ArtifactPersistFailed("artifact task does not exist")
            if (owner["tenant_id"], owner["repository"]) != scope[:2]:
                raise ArtifactAccessDenied("artifact scope does not match its owning task")
            existing = conn.execute(
                "SELECT * FROM artifacts WHERE tenant_id=%s AND repository=%s AND task_id=%s "
                "AND logical_execution_key=%s",
                (*scope, artifact["logical_execution_key"]),
            ).fetchone()
            if existing:
                value = self._artifact_from_row(existing)
                if value["content_hash"] != artifact["content_hash"]:
                    raise ArtifactIntegrityConflict(
                        "logical tool execution already has different immutable content"
                    )
                return value
            total = conn.execute(
                "SELECT COALESCE(SUM(content_size_bytes),0) AS n FROM artifacts "
                "WHERE tenant_id=%s AND repository=%s AND task_id=%s", scope,
            ).fetchone()["n"]
            if int(total) + int(artifact["content_size_bytes"]) > MAX_TASK_ARTIFACT_BYTES:
                raise ArtifactPersistFailed("task artifact byte budget would be exceeded")
            row = conn.execute(
                "INSERT INTO artifacts(artifact_id,artifact_type,tenant_id,repository,task_id,"
                "producer,source_revision,logical_execution_key,content_hash,content_size_bytes,"
                "content_json,evidence_id,metadata_json,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s) RETURNING *",
                (
                    artifact["artifact_id"], artifact["artifact_type"], *scope,
                    artifact["producer"], artifact.get("source_revision", ""),
                    artifact["logical_execution_key"], artifact["content_hash"],
                    int(artifact["content_size_bytes"]),
                    json.dumps(artifact["content"], ensure_ascii=False, sort_keys=True),
                    artifact.get("evidence_id", ""),
                    json.dumps(artifact.get("metadata", {}), ensure_ascii=False, sort_keys=True),
                    artifact["created_at"],
                ),
            ).fetchone()
        return self._artifact_from_row(row)

    def get_artifact(
        self, artifact_id: str, tenant_id: str, repository: str, task_id: str,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=%s", (artifact_id,)
            ).fetchone()
        if not row:
            raise ArtifactNotFound("artifact does not exist")
        value = self._artifact_from_row(row)
        if (value["tenant_id"], value["repository"], value["task_id"]) != (
            tenant_id, repository, task_id
        ):
            raise ArtifactAccessDenied("artifact is outside the current task scope")
        return value

    def get_artifact_by_logical_execution_key(
        self, logical_execution_key: str, tenant_id: str, repository: str, task_id: str,
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE tenant_id=%s AND repository=%s AND task_id=%s "
                "AND logical_execution_key=%s",
                (tenant_id, repository, task_id, logical_execution_key),
            ).fetchone()
        return self._artifact_from_row(row) if row else None

    def create(
        self, task_id: str, repository: str, pull_request: Optional[int],
        payload: Dict[str, Any], tenant_id: str = "default", release_id: str = "",
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            if release_id:
                owner = conn.execute(
                    "SELECT tenant_id FROM releases WHERE release_id=%s", (release_id,)
                ).fetchone()
                if not owner or str(owner["tenant_id"]) != str(tenant_id):
                    raise ReleaseNotFound("Task Release does not exist in the tenant scope")
            conn.execute(
                "INSERT INTO tasks(id,state,repository,pull_request,release_id,input_json,"
                "report_json,error,created_at,updated_at,tenant_id,cancel_requested) "
                "VALUES (%s,%s,%s,%s,%s,%s::jsonb,NULL,NULL,%s,%s,%s,FALSE)",
                (task_id, TaskState.PENDING.value, repository, pull_request,
                 release_id, json.dumps(payload), now, now, tenant_id),
            )

    @staticmethod
    def _release_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["spec"] = value.pop("spec_json")
        value["created_at"] = value["created_at"].isoformat()
        return ReleaseBundle.from_record(value).to_dict()

    def put_release(
        self, tenant_id: str, spec: Dict[str, Any], parent_release_id: str = "",
    ) -> Dict[str, Any]:
        bundle = ReleaseBundle.create(tenant_id, spec, utc_now(), parent_release_id)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM releases WHERE tenant_id=%s AND spec_sha256=%s",
                (bundle.tenant_id, bundle.spec_sha256),
            ).fetchone()
            if existing:
                return self._release_from_row(existing)
            if bundle.parent_release_id:
                parent = conn.execute(
                    "SELECT tenant_id FROM releases WHERE release_id=%s",
                    (bundle.parent_release_id,),
                ).fetchone()
                if not parent or str(parent["tenant_id"]) != bundle.tenant_id:
                    raise ReleaseNotFound("parent Release does not exist in the tenant scope")
            row = conn.execute(
                "INSERT INTO releases(release_id,tenant_id,spec_json,spec_sha256,"
                "parent_release_id,created_at) VALUES (%s,%s,%s::jsonb,%s,%s,%s) "
                "ON CONFLICT(tenant_id,spec_sha256) DO NOTHING RETURNING *",
                (
                    bundle.release_id, bundle.tenant_id,
                    json.dumps(bundle.spec, ensure_ascii=False, sort_keys=True),
                    bundle.spec_sha256, bundle.parent_release_id, bundle.created_at,
                ),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM releases WHERE tenant_id=%s AND spec_sha256=%s",
                    (bundle.tenant_id, bundle.spec_sha256),
                ).fetchone()
        return self._release_from_row(row)

    def get_release(self, release_id: str, tenant_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM releases WHERE release_id=%s AND tenant_id=%s",
                (release_id, tenant_id),
            ).fetchone()
        if not row:
            raise ReleaseNotFound("Release does not exist in the tenant scope")
        return self._release_from_row(row)

    def get_active_release(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT r.* FROM active_releases a JOIN releases r "
                "ON r.release_id=a.release_id WHERE a.tenant_id=%s", (tenant_id,)
            ).fetchone()
        return self._release_from_row(row) if row else None

    def activate_release(self, tenant_id: str, release_id: str) -> Dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            target = conn.execute(
                "SELECT * FROM releases WHERE release_id=%s AND tenant_id=%s FOR UPDATE",
                (release_id, tenant_id),
            ).fetchone()
            if not target:
                raise ReleaseNotFound("Release does not exist in the tenant scope")
            current = conn.execute(
                "SELECT release_id FROM active_releases WHERE tenant_id=%s FOR UPDATE",
                (tenant_id,),
            ).fetchone()
            previous = str(current["release_id"]) if current else ""
            conn.execute(
                "INSERT INTO active_releases(tenant_id,release_id,previous_release_id,updated_at) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(tenant_id) DO UPDATE SET "
                "release_id=EXCLUDED.release_id,previous_release_id=EXCLUDED.previous_release_id,"
                "updated_at=EXCLUDED.updated_at",
                (tenant_id, release_id, previous, now),
            )
            if previous != release_id:
                conn.execute(
                    "INSERT INTO release_activations(tenant_id,release_id,"
                    "previous_release_id,created_at) VALUES (%s,%s,%s,%s)",
                    (tenant_id, release_id, previous, now),
                )
        return self._release_from_row(target)

    def list_releases(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM releases WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
                (tenant_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._release_from_row(row) for row in rows]

    def transition(self, task_id: str, event: TraceEvent) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE tasks SET state=%s,updated_at=%s WHERE id=%s", (event.state.value, event.created_at, task_id))
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (%s,%s,%s,%s,%s)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )

    def succeed(self, task_id: str, report: ReviewReport, event: TraceEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,report_json=%s::jsonb,updated_at=%s WHERE id=%s",
                (TaskState.SUCCESS.value, json.dumps(report.to_dict(), ensure_ascii=False), event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (%s,%s,%s,%s,%s)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )

    def fail(self, task_id: str, error: str, event: TraceEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,error=%s,updated_at=%s WHERE id=%s",
                (TaskState.FAILED.value, error[:2000], event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (%s,%s,%s,%s,%s)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )

    def get(self, task_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            query = "SELECT * FROM tasks WHERE id=%s"
            params = [task_id]
            if tenant_id is not None:
                query += " AND tenant_id=%s"
                params.append(tenant_id)
            row = conn.execute(query, params).fetchone()
            if not row:
                return None
            events = conn.execute(
                "SELECT step,state,message,created_at FROM trace_events WHERE task_id=%s ORDER BY id", (task_id,)
            ).fetchall()
            messages = conn.execute(
                "SELECT sender,recipient,kind,correlation_id,content_json,created_at "
                "FROM agent_messages WHERE task_id=%s ORDER BY id", (task_id,)
            ).fetchall()
        value = dict(row)
        value["input"] = value.pop("input_json")
        value["report"] = value.pop("report_json")
        value["trace"] = [dict(item) for item in events]
        value["collaboration"] = []
        for message in messages:
            item = dict(message)
            item["content"] = item.pop("content_json")
            item["created_at"] = item["created_at"].isoformat()
            value["collaboration"].append(item)
        for key in ("created_at", "updated_at"):
            value[key] = value[key].isoformat()
        for item in value["trace"]:
            item["created_at"] = item["created_at"].isoformat()
        return value

    def record_agent_message(self, task_id: str, message: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_messages(task_id,sender,recipient,kind,correlation_id,"
                "content_json,created_at) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (task_id, message["sender"], message["recipient"], message["kind"],
                 message.get("correlation_id", ""),
                 json.dumps(message.get("content", {}), ensure_ascii=False), utc_now()),
            )

    def save_agent_memory(self, memory: Dict[str, Any]) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO agent_memories(id,tenant_id,repository,task_id,agent,scope,kind,"
                "content,keywords_json,metadata_json,importance,created_at,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET "
                "importance=GREATEST(agent_memories.importance,EXCLUDED.importance),"
                "expires_at=EXCLUDED.expires_at RETURNING *",
                (
                    memory["id"], memory["tenant_id"], memory["repository"],
                    memory.get("task_id", ""), memory.get("agent", ""), memory["scope"],
                    memory["kind"], memory["content"],
                    json.dumps(memory.get("keywords", []), ensure_ascii=False),
                    json.dumps(memory.get("metadata", {}), ensure_ascii=False),
                    float(memory.get("importance", 0.5)), memory["created_at"],
                    memory.get("expires_at"),
                ),
            ).fetchone()
        return self._memory_from_row(row)

    def list_agent_memories(
        self, tenant_id: str, repository: str, scopes: tuple,
        limit: int = 100,
    ) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_memories WHERE tenant_id=%s AND repository=%s "
                "AND scope=ANY(%s) AND (expires_at IS NULL OR expires_at>%s) "
                "ORDER BY importance DESC,created_at DESC LIMIT %s",
                (
                    tenant_id, repository, list(scopes), utc_now(),
                    max(1, min(limit, 500)),
                ),
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def delete_agent_memories(self, task_id: str = "", scope: str = "") -> int:
        clauses = []
        params = []
        if task_id:
            clauses.append("task_id=%s")
            params.append(task_id)
        if scope:
            clauses.append("scope=%s")
            params.append(scope)
        if not clauses:
            raise ValueError("memory deletion requires task_id or scope")
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE " + " AND ".join(clauses), params
            )
            return cursor.rowcount

    def purge_expired_agent_memories(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE expires_at IS NOT NULL AND expires_at<=%s",
                (utc_now(),),
            )
            return cursor.rowcount

    @staticmethod
    def _memory_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["keywords"] = value.pop("keywords_json")
        value["metadata"] = value.pop("metadata_json")
        for key in ("created_at", "expires_at"):
            if value.get(key) is not None:
                value[key] = value[key].isoformat()
        return value

    def list_tasks(self, limit: int = 50, tenant_id: Optional[str] = None) -> list:
        with self._connect() as conn:
            where = " WHERE tenant_id=%s" if tenant_id is not None else ""
            params = ([tenant_id] if tenant_id is not None else []) + [max(1, min(limit, 200))]
            rows = conn.execute(
                "SELECT id,state,repository,pull_request,release_id,error,created_at,updated_at,tenant_id "
                "FROM tasks" + where + " ORDER BY created_at DESC LIMIT %s", params
            ).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["created_at"] = value["created_at"].isoformat()
            value["updated_at"] = value["updated_at"].isoformat()
        return values

    def record_failure_case(self, task_id: str, category: str, payload: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO failure_cases(task_id,category,payload_json,created_at) VALUES (%s,%s,%s::jsonb,%s)",
                (task_id, category, json.dumps(payload, ensure_ascii=False), utc_now()),
            )

    def list_failure_cases(
        self, unresolved_only: bool = False, limit: int = 100,
        tenant_id: Optional[str] = None,
    ) -> list:
        joins = " f"
        clauses = []
        params = []
        if tenant_id is not None:
            joins += " JOIN tasks t ON t.id=f.task_id"
            clauses.append("t.tenant_id=%s")
            params.append(tenant_id)
        if unresolved_only:
            clauses.append("f.resolved=FALSE")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT f.* FROM failure_cases" + joins + where
                + " ORDER BY f.id DESC LIMIT %s", params
            ).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["payload"] = value.pop("payload_json")
            value["created_at"] = value["created_at"].isoformat()
        return values

    def list_task_failure_cases(
        self, task_id: str, tenant_id: Optional[str] = None,
    ) -> list:
        joins = " f"
        clauses = ["f.task_id=%s"]
        params = [task_id]
        if tenant_id is not None:
            joins += " JOIN tasks t ON t.id=f.task_id"
            clauses.append("t.tenant_id=%s")
            params.append(tenant_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT f.* FROM failure_cases" + joins
                + " WHERE " + " AND ".join(clauses)
                + " ORDER BY f.id DESC",
                params,
            ).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["payload"] = value.pop("payload_json")
            value["created_at"] = value["created_at"].isoformat()
        return values

    def resolve_failure_cases(self, case_ids: list) -> None:
        ids = [int(value) for value in case_ids]
        if not ids:
            return
        with self._connect() as conn:
            conn.execute("UPDATE failure_cases SET resolved=TRUE WHERE id=ANY(%s)", (ids,))

    def save_evaluation_case(
        self, name: str, split: str, diff: str, expected: list,
        source: str = "manual", active: bool = True,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO evaluation_cases(name,split,diff,expected_json,source,active,created_at) "
                "VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s) ON CONFLICT(name) DO NOTHING RETURNING *",
                (name, split, diff, json.dumps(expected, ensure_ascii=False), source, active, utc_now()),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM evaluation_cases WHERE name=%s", (name,)
                ).fetchone()
                if (
                    row["split"] != split
                    or row["diff"] != diff
                    or row["expected_json"] != expected
                ):
                    raise ValueError(
                        "evaluation case names are immutable; use a new name for revised content"
                    )
        value = dict(row)
        value["expected"] = value.pop("expected_json")
        value["created_at"] = value["created_at"].isoformat()
        return value

    def list_evaluation_cases(
        self, split: Optional[str] = None, active_only: bool = True, limit: int = 100,
    ) -> list:
        clauses = []
        params = []
        if split:
            clauses.append("split=%s")
            params.append(split)
        if active_only:
            clauses.append("active=TRUE")
        query = "SELECT * FROM evaluation_cases"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id LIMIT %s"
        params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["expected"] = value.pop("expected_json")
            value["created_at"] = value["created_at"].isoformat()
        return values

    def save_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evolution_runs(id,skill_name,candidate_version,baseline_version,decision,"
                "candidate_score,baseline_score,metrics_json,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (
                    run["id"], run["skill_name"], run["candidate_version"], run.get("baseline_version"),
                    run["decision"], run["candidate_score"], run["baseline_score"],
                    json.dumps(run["metrics"], ensure_ascii=False), run["created_at"],
                ),
            )
        return run

    def list_evolution_runs(self, limit: int = 50) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evolution_runs ORDER BY created_at DESC LIMIT %s",
                (max(1, min(limit, 200)),),
            ).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["metrics"] = value.pop("metrics_json")
            value["created_at"] = value["created_at"].isoformat()
        return values

    def update_evolution_run(self, run_id: str, decision: str, metrics: Dict[str, Any]) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE evolution_runs SET decision=%s,metrics_json=%s::jsonb WHERE id=%s",
                (decision, json.dumps(metrics, ensure_ascii=False), run_id),
            )
            return cursor.rowcount == 1

    def get_active_skill_version(self, skill_name: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name=%s AND active=TRUE ORDER BY version DESC LIMIT 1",
                (skill_name,),
            ).fetchone()
        return dict(row) if row else None

    def save_skill_version(self, skill_name: str, prompt: str, score: float, activate: bool = False) -> Dict[str, Any]:
        active = self.get_active_skill_version(skill_name)
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (skill_name,))
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM skill_versions WHERE skill_name=%s",
                (skill_name,),
            ).fetchone()
            version = int(row["version"]) + 1
            if activate:
                conn.execute("UPDATE skill_versions SET active=FALSE WHERE skill_name=%s", (skill_name,))
            conn.execute(
                "INSERT INTO skill_versions(skill_name,version,prompt,score,active,parent_version,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (skill_name, version, prompt, score, activate, active["version"] if active else None, utc_now()),
            )
        return {"skill_name": skill_name, "version": version, "score": score, "active": activate}

    def list_skill_versions(self, skill_name: str) -> list:
        with self._connect() as conn:
            return list(conn.execute("SELECT * FROM skill_versions WHERE skill_name=%s ORDER BY version DESC", (skill_name,)).fetchall())

    def activate_skill_version(self, skill_name: str, version: int) -> bool:
        with self._connect() as conn:
            exists = conn.execute("SELECT 1 FROM skill_versions WHERE skill_name=%s AND version=%s", (skill_name, version)).fetchone()
            if not exists:
                return False
            conn.execute("UPDATE skill_versions SET active=FALSE WHERE skill_name=%s", (skill_name,))
            conn.execute("UPDATE skill_versions SET active=TRUE WHERE skill_name=%s AND version=%s", (skill_name, version))
        return True

    def save_skill_artifact(
        self, skill_name: str, artifact: Dict[str, Any], score: float,
        activate: bool = False, tenant_id: str = "default",
    ) -> Dict[str, Any]:
        artifact_json = json.dumps(
            artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        artifact_sha256 = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
        active = self.get_active_skill_artifact(skill_name, tenant_id)
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("artifact:" + skill_name,))
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM skill_artifact_versions "
                "WHERE tenant_id=%s AND skill_name=%s", (tenant_id, skill_name),
            ).fetchone()
            version = int(row["version"]) + 1
            if activate:
                conn.execute(
                    "UPDATE skill_artifact_versions SET active=FALSE WHERE tenant_id=%s AND skill_name=%s",
                    (tenant_id, skill_name),
                )
            created_at = utc_now()
            conn.execute(
                "INSERT INTO skill_artifact_versions(tenant_id,skill_name,version,artifact_json,"
                "artifact_sha256,score,active,parent_version,created_at) "
                "VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)",
                (tenant_id, skill_name, version, artifact_json, artifact_sha256, float(score), activate,
                 active["version"] if active else None, created_at),
            )
        return {
            "tenant_id": tenant_id, "skill_name": skill_name, "version": version, "score": float(score),
            "active": activate, "parent_version": active["version"] if active else None,
            "artifact_sha256": artifact_sha256, "created_at": created_at,
        }

    @staticmethod
    def _decode_skill_artifact(row) -> Dict[str, Any]:
        value = dict(row)
        value["artifact"] = value.pop("artifact_json")
        value["active"] = bool(value["active"])
        if hasattr(value.get("created_at"), "isoformat"):
            value["created_at"] = value["created_at"].isoformat()
        return value

    def get_active_skill_artifact(
        self, skill_name: str, tenant_id: str = "default",
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=%s AND skill_name=%s "
                "AND active=TRUE ORDER BY version DESC LIMIT 1", (tenant_id, skill_name),
            ).fetchone()
        return self._decode_skill_artifact(row) if row else None

    def list_active_skill_artifacts(self, tenant_id: str = "default") -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=%s AND active=TRUE "
                "ORDER BY skill_name", (tenant_id,)
            ).fetchall()
        return [self._decode_skill_artifact(row) for row in rows]

    def list_skill_artifact_versions(
        self, skill_name: str, tenant_id: str = "default",
    ) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=%s AND skill_name=%s "
                "ORDER BY version DESC", (tenant_id, skill_name),
            ).fetchall()
        return [self._decode_skill_artifact(row) for row in rows]

    def activate_skill_artifact(
        self, skill_name: str, version: int, tenant_id: str = "default",
    ) -> bool:
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM skill_artifact_versions v WHERE v.tenant_id=%s "
                "AND v.skill_name=%s AND v.version=%s AND (v.active=TRUE OR EXISTS ("
                "SELECT 1 FROM skill_evolution_runs r WHERE r.tenant_id=v.tenant_id "
                "AND r.skill_name=v.skill_name AND r.candidate_version=v.version "
                "AND r.decision IN ('ready_for_promotion','activated')))",
                (tenant_id, skill_name, version),
            ).fetchone()
            if not exists:
                return False
            conn.execute(
                "UPDATE skill_artifact_versions SET active=FALSE WHERE tenant_id=%s AND skill_name=%s",
                (tenant_id, skill_name),
            )
            conn.execute(
                "UPDATE skill_artifact_versions SET active=TRUE WHERE tenant_id=%s AND skill_name=%s "
                "AND version=%s", (tenant_id, skill_name, version),
            )
        return True

    def deactivate_skill_artifact_override(
        self, skill_name: str, tenant_id: str = "default",
    ) -> bool:
        """Clear one tenant's active DB override without deleting its history."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE skill_artifact_versions SET active=FALSE "
                "WHERE tenant_id=%s AND skill_name=%s AND active=TRUE",
                (tenant_id, skill_name),
            )
            return cursor.rowcount > 0

    def save_skill_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO skill_evolution_runs(id,tenant_id,skill_name,candidate_version,baseline_version,"
                "decision,candidate_score,baseline_score,metrics_json,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (run["id"], run.get("tenant_id", "default"), run["skill_name"], run["candidate_version"],
                 run.get("baseline_version"), run["decision"], run["candidate_score"],
                 run["baseline_score"], json.dumps(run["metrics"], ensure_ascii=False),
                 run["created_at"]),
            )
        return run

    def list_skill_evolution_runs(
        self, limit: int = 50, tenant_id: Optional[str] = None,
    ) -> list:
        with self._connect() as conn:
            if tenant_id is None:
                rows = conn.execute(
                    "SELECT * FROM skill_evolution_runs ORDER BY created_at DESC LIMIT %s",
                    (max(1, min(limit, 200)),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM skill_evolution_runs WHERE tenant_id=%s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, max(1, min(limit, 200))),
                ).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["metrics"] = value.pop("metrics_json")
            value["created_at"] = value["created_at"].isoformat()
        return values

    def save_task_payload(self, task_id: str, diff: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO task_payloads(task_id,diff,created_at) VALUES (%s,%s,%s) "
                "ON CONFLICT(task_id) DO UPDATE SET diff=EXCLUDED.diff,created_at=EXCLUDED.created_at",
                (task_id, diff, utc_now()),
            )

    def update_task_input(self, task_id: str, updates: Dict[str, Any]) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT input_json FROM tasks WHERE id=%s", (task_id,)
            ).fetchone()
            if not row:
                raise ValueError("task not found")
            value = dict(row["input_json"])
            value.update(updates)
            conn.execute(
                "UPDATE tasks SET input_json=%s::jsonb,updated_at=%s WHERE id=%s",
                (json.dumps(value, ensure_ascii=False), utc_now(), task_id),
            )

    def get_task_payload(self, task_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT diff FROM task_payloads WHERE task_id=%s", (task_id,)).fetchone()
        return row["diff"] if row else None

    def save_checkpoint(
        self, task_id: str, node: str, state: Dict[str, Any], status: str = "completed",
        attempt: int = 1, error: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints(task_id,node,status,attempt,state_json,error,updated_at) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT(task_id,node) DO UPDATE SET "
                "status=EXCLUDED.status,attempt=EXCLUDED.attempt,state_json=EXCLUDED.state_json,"
                "error=EXCLUDED.error,updated_at=EXCLUDED.updated_at",
                (task_id, node, status, attempt, json.dumps(state, ensure_ascii=False),
                 error[:2000] or None, utc_now()),
            )

    def load_checkpoints(self, task_id: str) -> Dict[str, Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT node,status,attempt,state_json,error,updated_at FROM checkpoints "
                "WHERE task_id=%s ORDER BY updated_at", (task_id,)
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            item["state"] = item.pop("state_json")
            item["updated_at"] = item["updated_at"].isoformat()
            result[item.pop("node")] = item
        return result

    def request_cancel(self, task_id: str, tenant_id: Optional[str] = None) -> bool:
        query = "UPDATE tasks SET cancel_requested=TRUE,updated_at=%s WHERE id=%s"
        params = [utc_now(), task_id]
        if tenant_id is not None:
            query += " AND tenant_id=%s"
            params.append(tenant_id)
        with self._connect() as conn:
            cursor = conn.execute(query, params)
            return cursor.rowcount > 0

    def is_cancelled(self, task_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM tasks WHERE id=%s", (task_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def cancel(self, task_id: str, event: TraceEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,updated_at=%s WHERE id=%s",
                (TaskState.CANCELLED.value, event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )

    def claim_webhook(
        self, delivery_id: str, tenant_id: str, event_type: str, payload_sha256: str,
    ) -> bool:
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery is required")
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO webhook_deliveries"
                "(delivery_id,tenant_id,event_type,payload_sha256,received_at) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(delivery_id) DO NOTHING RETURNING delivery_id",
                (delivery_id, tenant_id, event_type, payload_sha256, utc_now()),
            ).fetchone()
            if row:
                return True
            existing = conn.execute(
                "SELECT payload_sha256 FROM webhook_deliveries WHERE delivery_id=%s",
                (delivery_id,),
            ).fetchone()
            if existing and existing["payload_sha256"] != payload_sha256:
                raise ValueError("delivery id was already used with a different payload")
            return False

    def complete_webhook(self, delivery_id: str, task_id: Optional[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE webhook_deliveries SET task_id=%s WHERE delivery_id=%s",
                (task_id, delivery_id),
            )

    def get_webhook(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM webhook_deliveries WHERE delivery_id=%s", (delivery_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_user(
        self, user_id: str, username: str, password_hash: str,
        tenant_id: str, role: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users(id,username,password_hash,created_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(username) DO NOTHING",
                (user_id, username, password_hash, utc_now()),
            )
            row = conn.execute("SELECT id FROM users WHERE username=%s", (username,)).fetchone()
            conn.execute(
                "INSERT INTO memberships(user_id,tenant_id,role) VALUES (%s,%s,%s) "
                "ON CONFLICT(user_id,tenant_id) DO UPDATE SET role=EXCLUDED.role",
                (row["id"], tenant_id, role),
            )

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,username,password_hash,active FROM users WHERE username=%s",
                (username,),
            ).fetchone()
            if not row:
                return None
            memberships = conn.execute(
                "SELECT tenant_id,role FROM memberships WHERE user_id=%s", (row["id"],)
            ).fetchall()
        value = dict(row)
        value["memberships"] = [dict(item) for item in memberships]
        return value

    def grant_repository(self, tenant_id: str, repository: str, auto_fix: bool = False) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO repository_grants(tenant_id,repository,auto_fix) VALUES (%s,%s,%s) "
                "ON CONFLICT(tenant_id,repository) DO UPDATE SET auto_fix=EXCLUDED.auto_fix",
                (tenant_id, repository, auto_fix),
            )

    def repository_allowed(
        self, tenant_id: str, repository: str, require_auto_fix: bool = False,
    ) -> bool:
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM repository_grants WHERE tenant_id=%s", (tenant_id,)
            ).fetchone()["n"]
            row = conn.execute(
                "SELECT auto_fix FROM repository_grants WHERE tenant_id=%s AND repository=%s",
                (tenant_id, repository),
            ).fetchone()
        return True if total == 0 else bool(row and (not require_auto_fix or row["auto_fix"]))

    def audit(
        self, tenant_id: str, actor: str, action: str, resource: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                (tenant_id, actor, action, resource,
                 json.dumps(detail or {}, ensure_ascii=False), utc_now()),
            )

    def list_audit(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT actor,action,resource,detail_json,created_at FROM audit_log "
                "WHERE tenant_id=%s ORDER BY id DESC LIMIT %s",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [{**dict(row), "detail": row["detail_json"],
                 "created_at": row["created_at"].isoformat()} for row in rows]

    def save_deployment(self, tenant_id: str, skill_name: str, config: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO deployments(tenant_id,skill_name,stable_version,candidate_version,"
                "canary_percent,shadow_percent,max_error_rate,min_samples,status,samples,errors,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s) "
                "ON CONFLICT(tenant_id,skill_name) DO UPDATE SET stable_version=EXCLUDED.stable_version,"
                "candidate_version=EXCLUDED.candidate_version,canary_percent=EXCLUDED.canary_percent,"
                "shadow_percent=EXCLUDED.shadow_percent,max_error_rate=EXCLUDED.max_error_rate,"
                "min_samples=EXCLUDED.min_samples,status=EXCLUDED.status,samples=0,errors=0,"
                "updated_at=EXCLUDED.updated_at",
                (tenant_id, skill_name, config.get("stable_version"), config.get("candidate_version"),
                 int(config.get("canary_percent", 0)), int(config.get("shadow_percent", 0)),
                 float(config.get("max_error_rate", .1)), int(config.get("min_samples", 20)),
                 config.get("status", "running"), utc_now()),
            )

    def get_deployment(self, tenant_id: str, skill_name: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deployments WHERE tenant_id=%s AND skill_name=%s",
                (tenant_id, skill_name),
            ).fetchone()
        return dict(row) if row else None

    def record_deployment_result(
        self, tenant_id: str, skill_name: str, failed: bool,
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE deployments SET samples=samples+1,errors=errors+%s,updated_at=%s "
                "WHERE tenant_id=%s AND skill_name=%s RETURNING *",
                (int(failed), utc_now(), tenant_id, skill_name),
            ).fetchone()
            if not row:
                return None
            value = dict(row)
            if (value["status"] == "running" and value["samples"] >= value["min_samples"]
                    and value["errors"] / value["samples"] > value["max_error_rate"]):
                conn.execute(
                    "UPDATE deployments SET status='rolled_back',canary_percent=0,shadow_percent=0,"
                    "updated_at=%s WHERE tenant_id=%s AND skill_name=%s",
                    (utc_now(), tenant_id, skill_name),
                )
                value["status"] = "rolled_back"
        return value

    def create_alert(
        self, tenant_id: str, alert_key: str, severity: str, message: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO alerts(tenant_id,alert_key,severity,message,status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,'open',%s,%s) ON CONFLICT(tenant_id,alert_key,status) DO NOTHING",
                (tenant_id, alert_key, severity, message[:1000], utc_now(), utc_now()),
            )

    def list_alerts(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE tenant_id=%s ORDER BY id DESC LIMIT %s",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_installation(
        self, installation_id: int, account_login: str, tenant_id: str = "default"
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO installations(installation_id,account_login,created_at,tenant_id) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(installation_id) DO UPDATE "
                "SET account_login=EXCLUDED.account_login,created_at=EXCLUDED.created_at,"
                "tenant_id=EXCLUDED.tenant_id",
                (installation_id, account_login, utc_now(), tenant_id),
            )

    def installation_tenant(self, installation_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tenant_id FROM installations WHERE installation_id=%s",
                (installation_id,),
            ).fetchone()
        return row["tenant_id"] if row else None

    def dashboard_stats(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        with self._connect() as conn:
            where = " WHERE tenant_id=%s" if tenant_id is not None else ""
            params = (tenant_id,) if tenant_id is not None else ()
            row = conn.execute(
                "SELECT COUNT(*) AS total,COUNT(*) FILTER(WHERE state='SUCCESS') AS success,"
                "COUNT(*) FILTER(WHERE state='FAILED') AS failed FROM tasks" + where,
                params,
            ).fetchone()
            if tenant_id is None:
                failures = conn.execute(
                    "SELECT COUNT(*) AS n FROM failure_cases WHERE resolved=FALSE"
                ).fetchone()["n"]
            else:
                failures = conn.execute(
                    "SELECT COUNT(*) AS n FROM failure_cases f JOIN tasks t ON t.id=f.task_id "
                    "WHERE f.resolved=FALSE AND t.tenant_id=%s", (tenant_id,)
                ).fetchone()["n"]
            skills = conn.execute(
                "SELECT COUNT(*) AS n FROM skill_versions WHERE active=TRUE"
            ).fetchone()["n"]
            if tenant_id is None:
                skills += conn.execute(
                    "SELECT COUNT(*) AS n FROM skill_artifact_versions WHERE active=TRUE"
                ).fetchone()["n"]
            else:
                skills += conn.execute(
                    "SELECT COUNT(*) AS n FROM skill_artifact_versions "
                    "WHERE tenant_id=%s AND active=TRUE", (tenant_id,)
                ).fetchone()["n"]
        return {"tasks_total": row["total"], "tasks_success": row["success"], "tasks_failed": row["failed"],
                "success_rate": round(row["success"] / row["total"], 4) if row["total"] else 0.0,
                "unresolved_failure_cases": failures, "active_skill_versions": skills}


def create_store(database_url: str, sqlite_path: str):
    if database_url.startswith(("postgres://", "postgresql://")):
        return PostgresTaskStore(database_url)
    from .store import TaskStore
    return TaskStore(sqlite_path)
