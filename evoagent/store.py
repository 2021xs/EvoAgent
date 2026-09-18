import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
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
from .evolution_lifecycle import CANDIDATE_TRANSITIONS
from .workflow_events import (
    AUTOFIX_CHECKPOINT,
    CI_TERMINAL_PHASES,
    canonical_ci_authority,
    check_suite_matches_authority,
    transition_autofix_state,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pull_request INTEGER,
                    release_id TEXT NOT NULL DEFAULT '',
                    input_json TEXT NOT NULL,
                    report_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS failure_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS attribution_results (
                    attribution_id TEXT PRIMARY KEY,
                    failure_id INTEGER NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    failure_layer TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    semantic_cause TEXT NOT NULL,
                    target_surface TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    alternative_hypotheses_json TEXT NOT NULL,
                    method TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(failure_id) REFERENCES failure_cases(id),
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS evolution_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    parent_version INTEGER,
                    parent_release_id TEXT NOT NULL,
                    attribution_id TEXT NOT NULL,
                    source_failure_ids_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    change_json TEXT NOT NULL,
                    change_hash TEXT NOT NULL,
                    generation_method TEXT NOT NULL,
                    generation_model TEXT NOT NULL,
                    generation_config_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    validation_result_json TEXT NOT NULL,
                    final_evaluation_result_json TEXT NOT NULL,
                    surface_version_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id,surface,target_id,parent_version,change_hash),
                    FOREIGN KEY(attribution_id) REFERENCES attribution_results(attribution_id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    score REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    parent_version INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(skill_name, version)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS installations (
                    installation_id INTEGER PRIMARY KEY,
                    account_login TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS trace_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS evaluation_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    split TEXT NOT NULL,
                    diff TEXT NOT NULL,
                    expected_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS evolution_runs (
                    id TEXT PRIMARY KEY,
                    skill_name TEXT NOT NULL,
                    candidate_version INTEGER NOT NULL,
                    baseline_version INTEGER,
                    decision TEXT NOT NULL,
                    candidate_score REAL NOT NULL,
                    baseline_score REAL NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_artifact_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    skill_name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    artifact_json TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    score REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    parent_version INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(tenant_id, skill_name, version)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_evolution_runs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    skill_name TEXT NOT NULL,
                    candidate_version INTEGER NOT NULL,
                    baseline_version INTEGER,
                    decision TEXT NOT NULL,
                    candidate_score REAL NOT NULL,
                    baseline_score REAL NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            self._ensure_column(
                conn, "skill_artifact_versions", "tenant_id", "TEXT NOT NULL DEFAULT 'default'"
            )
            self._ensure_column(
                conn, "skill_evolution_runs", "tenant_id", "TEXT NOT NULL DEFAULT 'default'"
            )
            self._ensure_column(conn, "tasks", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
            self._ensure_column(conn, "tasks", "cancel_requested", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "tasks", "release_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "installations", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS releases (
                    release_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    spec_sha256 TEXT NOT NULL,
                    parent_release_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(tenant_id, spec_sha256)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS active_releases (
                    tenant_id TEXT PRIMARY KEY,
                    release_id TEXT NOT NULL,
                    previous_release_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(release_id) REFERENCES releases(release_id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS release_activations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    previous_release_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS checkpoints (
                    task_id TEXT NOT NULL,
                    node TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    state_json TEXT NOT NULL,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, node),
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS task_payloads (
                    task_id TEXT PRIMARY KEY,
                    diff TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    task_id TEXT,
                    received_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS workflow_events (
                    event_id TEXT PRIMARY KEY,
                    logical_event_key TEXT NOT NULL UNIQUE,
                    external_event_key TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    external_object_id TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    external_status TEXT NOT NULL,
                    conclusion TEXT NOT NULL,
                    outcome TEXT,
                    received_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    policy_result TEXT NOT NULL,
                    processing_status TEXT NOT NULL DEFAULT 'PENDING',
                    processing_result TEXT NOT NULL DEFAULT '',
                    correlated_task_id TEXT,
                    applied_transition TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_workflow_events_correlation
                    ON workflow_events(tenant_id,repository,head_sha,processing_status)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS autofix_correlations (
                    task_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    repair_branch TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    ci_authority_app_id TEXT NOT NULL DEFAULT '',
                    ci_authority_app_slug TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_autofix_correlation_commit
                    ON autofix_correlations(tenant_id,repository,commit_sha)"""
            )
            self._ensure_column(
                conn, "autofix_correlations", "ci_authority_app_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn, "autofix_correlations", "ci_authority_app_slug",
                "TEXT NOT NULL DEFAULT ''",
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memberships (
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    PRIMARY KEY(user_id, tenant_id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS repository_grants (
                    tenant_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    auto_fix INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(tenant_id, repository)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS deployments (
                    tenant_id TEXT NOT NULL,
                    skill_name TEXT NOT NULL,
                    stable_version INTEGER,
                    candidate_version INTEGER,
                    canary_percent INTEGER NOT NULL DEFAULT 0,
                    shadow_percent INTEGER NOT NULL DEFAULT 0,
                    max_error_rate REAL NOT NULL DEFAULT 0.1,
                    min_samples INTEGER NOT NULL DEFAULT 20,
                    status TEXT NOT NULL DEFAULT 'stable',
                    samples INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, skill_name)
                )"""
            )
            self._ensure_column(
                conn, "deployments", "max_disagreement_rate",
                "REAL NOT NULL DEFAULT 0.2"
            )
            self._ensure_column(
                conn, "deployments", "auto_promote", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                conn, "deployments", "shadow_samples", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                conn, "deployments", "disagreements", "INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS release_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    skill_name TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    primary_json TEXT NOT NULL,
                    candidate_json TEXT,
                    disagreement REAL NOT NULL,
                    candidate_failed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    alert_key TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, alert_key, status)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_memories (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    agent TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_tenant_created ON tasks(tenant_id, created_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_memories_lookup "
                "ON agent_memories(tenant_id, repository, scope, created_at)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    artifact_type TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    producer TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    logical_execution_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    content_size_bytes INTEGER NOT NULL,
                    content_json TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(tenant_id, repository, task_id, logical_execution_key),
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_task "
                "ON artifacts(tenant_id, repository, task_id, created_at)"
            )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
        if column not in columns:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration))

    @staticmethod
    def _artifact_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["content"] = json.loads(value.pop("content_json"))
        value["metadata"] = json.loads(value.pop("metadata_json"))
        return value

    def put_artifact(self, artifact: Dict[str, Any]) -> Dict[str, Any]:
        """Insert immutable content or return the existing logical artifact."""
        scope = (artifact["tenant_id"], artifact["repository"], artifact["task_id"])
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute(
                "SELECT tenant_id,repository FROM tasks WHERE id=?", (artifact["task_id"],)
            ).fetchone()
            if not owner:
                raise ArtifactPersistFailed("artifact task does not exist")
            if (owner["tenant_id"], owner["repository"]) != scope[:2]:
                raise ArtifactAccessDenied("artifact scope does not match its owning task")
            existing = conn.execute(
                "SELECT * FROM artifacts WHERE tenant_id=? AND repository=? AND task_id=? "
                "AND logical_execution_key=?",
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
                "WHERE tenant_id=? AND repository=? AND task_id=?", scope,
            ).fetchone()["n"]
            if int(total) + int(artifact["content_size_bytes"]) > MAX_TASK_ARTIFACT_BYTES:
                raise ArtifactPersistFailed("task artifact byte budget would be exceeded")
            conn.execute(
                "INSERT INTO artifacts(artifact_id,artifact_type,tenant_id,repository,task_id,"
                "producer,source_revision,logical_execution_key,content_hash,content_size_bytes,"
                "content_json,evidence_id,metadata_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            )
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact["artifact_id"],)
            ).fetchone()
        return self._artifact_from_row(row)

    def get_artifact(
        self, artifact_id: str, tenant_id: str, repository: str, task_id: str,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
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
                "SELECT * FROM artifacts WHERE tenant_id=? AND repository=? AND task_id=? "
                "AND logical_execution_key=?",
                (tenant_id, repository, task_id, logical_execution_key),
            ).fetchone()
        return self._artifact_from_row(row) if row else None

    def create(
        self, task_id: str, repository: str, pull_request: Optional[int],
        payload: Dict[str, Any], tenant_id: str = "default", release_id: str = "",
    ) -> None:
        now = utc_now()
        with self._lock, self._connect() as conn:
            if release_id:
                owner = conn.execute(
                    "SELECT tenant_id FROM releases WHERE release_id=?", (release_id,)
                ).fetchone()
                if not owner or str(owner["tenant_id"]) != str(tenant_id):
                    raise ReleaseNotFound("Task Release does not exist in the tenant scope")
            conn.execute(
                "INSERT INTO tasks(id,state,repository,pull_request,release_id,input_json,"
                "report_json,error,created_at,updated_at,tenant_id,cancel_requested) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, 0)",
                (task_id, TaskState.PENDING.value, repository, pull_request,
                 release_id, json.dumps(payload), now, now, tenant_id),
            )

    @staticmethod
    def _release_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["spec"] = json.loads(value.pop("spec_json"))
        return ReleaseBundle.from_record(value).to_dict()

    def put_release(
        self, tenant_id: str, spec: Dict[str, Any], parent_release_id: str = "",
    ) -> Dict[str, Any]:
        bundle = ReleaseBundle.create(tenant_id, spec, utc_now(), parent_release_id)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM releases WHERE tenant_id=? AND spec_sha256=?",
                (bundle.tenant_id, bundle.spec_sha256),
            ).fetchone()
            if existing:
                return self._release_from_row(existing)
            if bundle.parent_release_id:
                parent = conn.execute(
                    "SELECT tenant_id FROM releases WHERE release_id=?",
                    (bundle.parent_release_id,),
                ).fetchone()
                if not parent or str(parent["tenant_id"]) != bundle.tenant_id:
                    raise ReleaseNotFound("parent Release does not exist in the tenant scope")
            conn.execute(
                "INSERT OR IGNORE INTO releases(release_id,tenant_id,spec_json,spec_sha256,"
                "parent_release_id,created_at) VALUES (?,?,?,?,?,?)",
                (
                    bundle.release_id, bundle.tenant_id,
                    json.dumps(bundle.spec, ensure_ascii=False, sort_keys=True),
                    bundle.spec_sha256, bundle.parent_release_id, bundle.created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM releases WHERE tenant_id=? AND spec_sha256=?",
                (bundle.tenant_id, bundle.spec_sha256),
            ).fetchone()
        return self._release_from_row(row)

    def get_release(self, release_id: str, tenant_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM releases WHERE release_id=? AND tenant_id=?",
                (release_id, tenant_id),
            ).fetchone()
        if not row:
            raise ReleaseNotFound("Release does not exist in the tenant scope")
        return self._release_from_row(row)

    def get_active_release(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT r.* FROM active_releases a JOIN releases r "
                "ON r.release_id=a.release_id WHERE a.tenant_id=?",
                (tenant_id,),
            ).fetchone()
        return self._release_from_row(row) if row else None

    def activate_release(self, tenant_id: str, release_id: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            target = conn.execute(
                "SELECT * FROM releases WHERE release_id=? AND tenant_id=?",
                (release_id, tenant_id),
            ).fetchone()
            if not target:
                raise ReleaseNotFound("Release does not exist in the tenant scope")
            current = conn.execute(
                "SELECT release_id FROM active_releases WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
            previous = str(current["release_id"]) if current else ""
            conn.execute(
                "INSERT INTO active_releases(tenant_id,release_id,previous_release_id,updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET "
                "release_id=excluded.release_id,previous_release_id=excluded.previous_release_id,"
                "updated_at=excluded.updated_at",
                (tenant_id, release_id, previous, now),
            )
            if previous != release_id:
                conn.execute(
                    "INSERT INTO release_activations(tenant_id,release_id,"
                    "previous_release_id,created_at) VALUES (?,?,?,?)",
                    (tenant_id, release_id, previous, now),
                )
        return self._release_from_row(target)

    def list_releases(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM releases WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._release_from_row(row) for row in rows]

    def transition(self, task_id: str, event: TraceEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                (event.state.value, event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id, step, state, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )

    def succeed(self, task_id: str, report: ReviewReport, event: TraceEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state = ?, report_json = ?, updated_at = ? WHERE id = ?",
                (TaskState.SUCCESS.value, json.dumps(report.to_dict(), ensure_ascii=False), event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id, step, state, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )

    def fail(self, task_id: str, error: str, event: TraceEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state = ?, error = ?, updated_at = ? WHERE id = ?",
                (TaskState.FAILED.value, error[:2000], event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id, step, state, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )

    def get(self, task_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            if tenant_id is None:
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ? AND tenant_id = ?", (task_id, tenant_id)
                ).fetchone()
            if row is None:
                return None
            events = conn.execute(
                "SELECT step, state, message, created_at FROM trace_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
            messages = conn.execute(
                "SELECT sender,recipient,kind,correlation_id,content_json,created_at "
                "FROM agent_messages WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        value = dict(row)
        value["input"] = json.loads(value.pop("input_json"))
        report_json = value.pop("report_json")
        value["report"] = json.loads(report_json) if report_json else None
        value["trace"] = [dict(item) for item in events]
        value["collaboration"] = []
        for message in messages:
            item = dict(message)
            item["content"] = json.loads(item.pop("content_json"))
            value["collaboration"].append(item)
        return value

    def record_agent_message(self, task_id: str, message: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_messages(task_id,sender,recipient,kind,correlation_id,"
                "content_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, message["sender"], message["recipient"], message["kind"],
                 message.get("correlation_id", ""),
                 json.dumps(message.get("content", {}), ensure_ascii=False), utc_now()),
            )

    def save_agent_memory(self, memory: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_memories(id,tenant_id,repository,task_id,agent,scope,kind,"
                "content,keywords_json,metadata_json,importance,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "importance=MAX(agent_memories.importance,excluded.importance),"
                "expires_at=excluded.expires_at",
                (
                    memory["id"], memory["tenant_id"], memory["repository"],
                    memory.get("task_id", ""), memory.get("agent", ""), memory["scope"],
                    memory["kind"], memory["content"],
                    json.dumps(memory.get("keywords", []), ensure_ascii=False),
                    json.dumps(memory.get("metadata", {}), ensure_ascii=False),
                    float(memory.get("importance", 0.5)), memory["created_at"],
                    memory.get("expires_at"),
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_memories WHERE id=?", (memory["id"],)
            ).fetchone()
        return self._memory_from_row(row)

    def list_agent_memories(
        self, tenant_id: str, repository: str, scopes: tuple,
        limit: int = 100,
    ) -> list:
        placeholders = ",".join("?" for _ in scopes)
        params = [tenant_id, repository, *scopes, utc_now(), max(1, min(limit, 500))]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_memories WHERE tenant_id=? AND repository=? "
                "AND scope IN (%s) AND (expires_at IS NULL OR expires_at>?) "
                "ORDER BY importance DESC,created_at DESC LIMIT ?" % placeholders,
                params,
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def delete_agent_memories(self, task_id: str = "", scope: str = "") -> int:
        clauses = []
        params = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if not clauses:
            raise ValueError("memory deletion requires task_id or scope")
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE " + " AND ".join(clauses), params
            )
            return cursor.rowcount

    def purge_expired_agent_memories(self) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE expires_at IS NOT NULL AND expires_at<=?",
                (utc_now(),),
            )
            return cursor.rowcount

    @staticmethod
    def _memory_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["keywords"] = json.loads(value.pop("keywords_json"))
        value["metadata"] = json.loads(value.pop("metadata_json"))
        return value

    def list_tasks(self, limit: int = 50, tenant_id: Optional[str] = None) -> list:
        with self._connect() as conn:
            if tenant_id is None:
                rows = conn.execute(
                "SELECT id,state,repository,pull_request,release_id,error,created_at,updated_at,tenant_id "
                    "FROM tasks ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id,state,repository,pull_request,error,created_at,updated_at,tenant_id "
                    "FROM tasks WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                    (tenant_id, max(1, min(limit, 200))),
                ).fetchall()
        return [dict(item) for item in rows]

    def record_failure_case(self, task_id: str, category: str, payload: Dict[str, Any]) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO failure_cases(task_id, category, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, category, json.dumps(payload, ensure_ascii=False), utc_now()),
            )
            return int(cursor.lastrowid)

    def get_failure_case(
        self, failure_id: int, tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        query = "SELECT f.* FROM failure_cases f"
        params = [int(failure_id)]
        if tenant_id is not None:
            query += " JOIN tasks t ON t.id=f.task_id WHERE f.id=? AND t.tenant_id=?"
            params.append(tenant_id)
        else:
            query += " WHERE f.id=?"
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    def save_attribution_result(self, value: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            failure = conn.execute(
                "SELECT task_id FROM failure_cases WHERE id=?", (int(value["failure_id"]),),
            ).fetchone()
            if not failure or str(failure["task_id"]) != str(value["task_id"]):
                raise ValueError("AttributionResult failure/task provenance mismatch")
            conn.execute(
                "INSERT INTO attribution_results(attribution_id,failure_id,task_id,status,"
                "failure_layer,actor,semantic_cause,target_surface,target_id,evidence_refs_json,"
                "alternative_hypotheses_json,method,reason,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value["attribution_id"], int(value["failure_id"]), value["task_id"],
                    value["status"], value["failure_layer"], value["actor"],
                    value["semantic_cause"], value["target_surface"], value["target_id"],
                    json.dumps(value.get("evidence_refs") or [], ensure_ascii=False),
                    json.dumps(value.get("alternative_hypotheses") or [], ensure_ascii=False),
                    value["method"], value["reason"], value["created_at"],
                ),
            )
        return self.get_attribution_result(value["attribution_id"])

    @staticmethod
    def _decode_attribution(row) -> Dict[str, Any]:
        value = dict(row)
        value["evidence_refs"] = json.loads(value.pop("evidence_refs_json"))
        value["alternative_hypotheses"] = json.loads(
            value.pop("alternative_hypotheses_json")
        )
        return value

    def get_attribution_result(self, attribution_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM attribution_results WHERE attribution_id=?", (attribution_id,),
            ).fetchone()
        return self._decode_attribution(row) if row else None

    def get_failure_attribution(self, failure_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM attribution_results WHERE failure_id=?", (int(failure_id),),
            ).fetchone()
        return self._decode_attribution(row) if row else None

    @staticmethod
    def _decode_evolution_candidate(row) -> Dict[str, Any]:
        value = dict(row)
        for key in (
            "source_failure_ids", "evidence_refs", "change", "generation_config",
            "validation_result", "final_evaluation_result", "surface_version",
        ):
            value[key] = json.loads(value.pop(key + "_json"))
        return value

    def put_evolution_candidate(self, value: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            provenance = conn.execute(
                "SELECT a.failure_id,t.tenant_id FROM attribution_results a "
                "JOIN tasks t ON t.id=a.task_id WHERE a.attribution_id=?",
                (value["attribution_id"],),
            ).fetchone()
            if not provenance or str(provenance["tenant_id"]) != str(value["tenant_id"]):
                raise ValueError("EvolutionCandidate attribution is outside its tenant scope")
            if int(provenance["failure_id"]) not in {
                int(item) for item in value.get("source_failure_ids") or []
            }:
                raise ValueError("EvolutionCandidate omits its attributed source failure")
            conn.execute(
                "INSERT OR IGNORE INTO evolution_candidates(candidate_id,tenant_id,surface,"
                "target_id,parent_version,parent_release_id,attribution_id,source_failure_ids_json,"
                "evidence_refs_json,change_json,change_hash,generation_method,generation_model,"
                "generation_config_json,status,validation_result_json,final_evaluation_result_json,"
                "surface_version_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value["candidate_id"], value["tenant_id"], value["surface"], value["target_id"],
                    value.get("parent_version"), value.get("parent_release_id", ""),
                    value["attribution_id"], json.dumps(value.get("source_failure_ids") or []),
                    json.dumps(value.get("evidence_refs") or [], ensure_ascii=False),
                    json.dumps(value.get("change"), ensure_ascii=False, sort_keys=True),
                    value["change_hash"], value["generation_method"], value["generation_model"],
                    json.dumps(value.get("generation_config") or {}, ensure_ascii=False),
                    value["status"], json.dumps(value.get("validation_result") or {}),
                    json.dumps(value.get("final_evaluation_result") or {}),
                    json.dumps(value.get("surface_version") or {}),
                    value["created_at"], value["updated_at"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM evolution_candidates WHERE tenant_id=? AND surface=? "
                "AND target_id=? AND parent_version IS ? AND change_hash=?",
                (value["tenant_id"], value["surface"], value["target_id"],
                 value.get("parent_version"), value["change_hash"]),
            ).fetchone()
        return self._decode_evolution_candidate(row)

    def get_evolution_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id=?", (candidate_id,),
            ).fetchone()
        return self._decode_evolution_candidate(row) if row else None

    def list_evolution_candidates(
        self, tenant_id: str, limit: int = 100,
    ) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evolution_candidates WHERE tenant_id=? "
                "ORDER BY created_at DESC LIMIT ?", (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._decode_evolution_candidate(row) for row in rows]

    def transition_evolution_candidate(
        self, candidate_id: str, target: str, updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id=?", (candidate_id,),
            ).fetchone()
            if not row:
                raise ValueError("evolution candidate not found")
            current = str(row["status"])
            if target not in CANDIDATE_TRANSITIONS.get(current, set()):
                raise ValueError("illegal evolution candidate transition: %s -> %s" % (current, target))
            existing = self._decode_evolution_candidate(row)
            fields = {
                "status": target,
                "validation_result_json": json.dumps(
                    updates.get("validation_result", existing["validation_result"])
                ),
                "final_evaluation_result_json": json.dumps(
                    updates.get("final_evaluation_result", existing["final_evaluation_result"])
                ),
                "surface_version_json": json.dumps(
                    updates.get("surface_version", existing["surface_version"])
                ),
                "updated_at": utc_now(),
            }
            conn.execute(
                "UPDATE evolution_candidates SET status=?,validation_result_json=?,"
                "final_evaluation_result_json=?,surface_version_json=?,updated_at=? "
                "WHERE candidate_id=?",
                (*fields.values(), candidate_id),
            )
            updated = conn.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id=?", (candidate_id,),
            ).fetchone()
        return self._decode_evolution_candidate(updated)

    def list_failure_cases(
        self, unresolved_only: bool = False, limit: int = 100,
        tenant_id: Optional[str] = None,
    ) -> list:
        query = "SELECT f.* FROM failure_cases f"
        params = []
        clauses = []
        if tenant_id is not None:
            query += " JOIN tasks t ON t.id=f.task_id"
            clauses.append("t.tenant_id = ?")
            params.append(tenant_id)
        if unresolved_only:
            clauses.append("f.resolved = 0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY f.id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            values.append(item)
        return values

    def list_task_failure_cases(
        self, task_id: str, tenant_id: Optional[str] = None,
    ) -> list:
        query = "SELECT f.* FROM failure_cases f"
        params = []
        if tenant_id is not None:
            query += " JOIN tasks t ON t.id=f.task_id"
            query += " WHERE f.task_id=? AND t.tenant_id=?"
            params.extend([task_id, tenant_id])
        else:
            query += " WHERE f.task_id=?"
            params.append(task_id)
        query += " ORDER BY f.id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            values.append(item)
        return values

    def resolve_failure_cases(self, case_ids: list) -> None:
        ids = [int(value) for value in case_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE failure_cases SET resolved = 1 WHERE id IN (%s)" % placeholders,
                ids,
            )

    def save_evaluation_case(
        self, name: str, split: str, diff: str, expected: list,
        source: str = "manual", active: bool = True,
    ) -> Dict[str, Any]:
        expected_json = json.dumps(expected, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM evaluation_cases WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["split"] != split
                    or existing["diff"] != diff
                    or json.loads(existing["expected_json"]) != expected
                ):
                    raise ValueError(
                        "evaluation case names are immutable; use a new name for revised content"
                    )
                row = existing
            else:
                conn.execute(
                    "INSERT INTO evaluation_cases(name,split,diff,expected_json,source,active,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (name, split, diff, expected_json, source, int(active), utc_now()),
                )
                row = conn.execute(
                    "SELECT * FROM evaluation_cases WHERE name = ?", (name,)
                ).fetchone()
        value = dict(row)
        value["expected"] = json.loads(value.pop("expected_json"))
        value["active"] = bool(value["active"])
        return value

    def list_evaluation_cases(
        self, split: Optional[str] = None, active_only: bool = True, limit: int = 100,
    ) -> list:
        clauses = []
        params = []
        if split:
            clauses.append("split = ?")
            params.append(split)
        if active_only:
            clauses.append("active = 1")
        query = "SELECT * FROM evaluation_cases"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["expected"] = json.loads(value.pop("expected_json"))
            value["active"] = bool(value["active"])
            values.append(value)
        return values

    def save_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO evolution_runs(id,skill_name,candidate_version,baseline_version,decision,"
                "candidate_score,baseline_score,metrics_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
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
                "SELECT * FROM evolution_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["metrics"] = json.loads(value.pop("metrics_json"))
            values.append(value)
        return values

    def update_evolution_run(self, run_id: str, decision: str, metrics: Dict[str, Any]) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE evolution_runs SET decision = ?, metrics_json = ? WHERE id = ?",
                (decision, json.dumps(metrics, ensure_ascii=False), run_id),
            )
            return cursor.rowcount == 1

    def save_skill_version(self, skill_name: str, prompt: str, score: float, activate: bool = False) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM skill_versions WHERE skill_name = ?", (skill_name,)
            ).fetchone()
            version = int(row["version"]) + 1
            parent = self.get_active_skill_version(skill_name)
            if activate:
                conn.execute("UPDATE skill_versions SET active = 0 WHERE skill_name = ?", (skill_name,))
            conn.execute(
                "INSERT INTO skill_versions(skill_name, version, prompt, score, active, parent_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (skill_name, version, prompt, score, int(activate), parent["version"] if parent else None, utc_now()),
            )
        return {"skill_name": skill_name, "version": version, "score": score, "active": activate}

    def get_active_skill_version(self, skill_name: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name = ? AND active = 1 ORDER BY version DESC LIMIT 1",
                (skill_name,),
            ).fetchone()
        return dict(row) if row else None

    def list_skill_versions(self, skill_name: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name = ? ORDER BY version DESC", (skill_name,)
            ).fetchall()
        return [dict(item) for item in rows]

    def activate_skill_version(self, skill_name: str, version: int) -> bool:
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM skill_versions v WHERE v.skill_name=? AND v.version=? "
                "AND (v.active=1 OR EXISTS (SELECT 1 FROM evolution_runs r "
                "WHERE r.skill_name=v.skill_name AND r.candidate_version=v.version "
                "AND r.decision IN ('activated','shadow_ready','ready_for_promotion')))",
                (skill_name, version),
            ).fetchone()
            if not exists:
                return False
            conn.execute("UPDATE skill_versions SET active = 0 WHERE skill_name = ?", (skill_name,))
            conn.execute(
                "UPDATE skill_versions SET active = 1 WHERE skill_name = ? AND version = ?", (skill_name, version)
            )
        return True

    def save_skill_artifact(
        self, skill_name: str, artifact: Dict[str, Any], score: float,
        activate: bool = False, tenant_id: str = "default",
    ) -> Dict[str, Any]:
        artifact_json = json.dumps(
            artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        artifact_sha256 = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM skill_artifact_versions "
                "WHERE tenant_id=? AND skill_name=?", (tenant_id, skill_name),
            ).fetchone()
            version = int(row["version"]) + 1
            parent = conn.execute(
                "SELECT version FROM skill_artifact_versions WHERE tenant_id=? AND skill_name=? "
                "AND active=1 ORDER BY version DESC LIMIT 1", (tenant_id, skill_name),
            ).fetchone()
            if activate:
                conn.execute(
                    "UPDATE skill_artifact_versions SET active=0 WHERE tenant_id=? AND skill_name=?",
                    (tenant_id, skill_name),
                )
            created_at = utc_now()
            conn.execute(
                "INSERT INTO skill_artifact_versions(tenant_id,skill_name,version,artifact_json,"
                "artifact_sha256,score,active,parent_version,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, skill_name, version, artifact_json, artifact_sha256, float(score),
                 int(activate), parent["version"] if parent else None, created_at),
            )
        return {
            "tenant_id": tenant_id, "skill_name": skill_name, "version": version, "score": float(score),
            "active": activate, "parent_version": parent["version"] if parent else None,
            "artifact_sha256": artifact_sha256, "created_at": created_at,
        }

    @staticmethod
    def _decode_skill_artifact(row) -> Dict[str, Any]:
        value = dict(row)
        value["artifact"] = json.loads(value.pop("artifact_json"))
        value["active"] = bool(value["active"])
        return value

    def get_active_skill_artifact(
        self, skill_name: str, tenant_id: str = "default",
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=? AND skill_name=? "
                "AND active=1 ORDER BY version DESC LIMIT 1", (tenant_id, skill_name),
            ).fetchone()
        return self._decode_skill_artifact(row) if row else None

    def list_active_skill_artifacts(self, tenant_id: str = "default") -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=? AND active=1 "
                "ORDER BY skill_name", (tenant_id,)
            ).fetchall()
        return [self._decode_skill_artifact(row) for row in rows]

    def list_skill_artifact_versions(
        self, skill_name: str, tenant_id: str = "default",
    ) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=? AND skill_name=? "
                "ORDER BY version DESC", (tenant_id, skill_name),
            ).fetchall()
        return [self._decode_skill_artifact(row) for row in rows]

    def activate_skill_artifact(
        self, skill_name: str, version: int, tenant_id: str = "default",
    ) -> bool:
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM skill_artifact_versions v WHERE v.tenant_id=? "
                "AND v.skill_name=? AND v.version=? AND (v.active=1 OR EXISTS ("
                "SELECT 1 FROM skill_evolution_runs r WHERE r.tenant_id=v.tenant_id "
                "AND r.skill_name=v.skill_name AND r.candidate_version=v.version "
                "AND r.decision IN ('ready_for_promotion','activated')))",
                (tenant_id, skill_name, version),
            ).fetchone()
            if not exists:
                return False
            conn.execute(
                "UPDATE skill_artifact_versions SET active=0 WHERE tenant_id=? AND skill_name=?",
                (tenant_id, skill_name),
            )
            conn.execute(
                "UPDATE skill_artifact_versions SET active=1 WHERE tenant_id=? AND skill_name=? "
                "AND version=?", (tenant_id, skill_name, version),
            )
        return True

    def deactivate_skill_artifact_override(
        self, skill_name: str, tenant_id: str = "default",
    ) -> bool:
        """Clear one tenant's active DB override without deleting its history."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE skill_artifact_versions SET active=0 "
                "WHERE tenant_id=? AND skill_name=? AND active=1",
                (tenant_id, skill_name),
            )
            return cursor.rowcount > 0

    def save_skill_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO skill_evolution_runs(id,tenant_id,skill_name,candidate_version,baseline_version,"
                "decision,candidate_score,baseline_score,metrics_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
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
                    "SELECT * FROM skill_evolution_runs ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(limit, 200)),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM skill_evolution_runs WHERE tenant_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (tenant_id, max(1, min(limit, 200))),
                ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["metrics"] = json.loads(value.pop("metrics_json"))
            values.append(value)
        return values

    def save_installation(
        self, installation_id: int, account_login: str, tenant_id: str = "default"
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO installations"
                "(installation_id,account_login,created_at,tenant_id) VALUES (?, ?, ?, ?)",
                (installation_id, account_login, utc_now(), tenant_id),
            )

    def installation_tenant(self, installation_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tenant_id FROM installations WHERE installation_id=?", (installation_id,)
            ).fetchone()
        return str(row["tenant_id"]) if row else None

    def save_checkpoint(
        self, task_id: str, node: str, state: Dict[str, Any], status: str = "completed",
        attempt: int = 1, error: str = "",
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints(task_id,node,status,attempt,state_json,error,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(task_id,node) DO UPDATE SET "
                "status=excluded.status,attempt=excluded.attempt,state_json=excluded.state_json,"
                "error=excluded.error,updated_at=excluded.updated_at",
                (task_id, node, status, attempt, json.dumps(state, ensure_ascii=False),
                 error[:2000] or None, utc_now()),
            )

    def load_checkpoints(self, task_id: str) -> Dict[str, Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT node,status,attempt,state_json,error,updated_at FROM checkpoints "
                "WHERE task_id=? ORDER BY updated_at", (task_id,)
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            item["state"] = json.loads(item.pop("state_json"))
            result[item.pop("node")] = item
        return result

    def save_task_payload(self, task_id: str, diff: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO task_payloads(task_id,diff,created_at) VALUES (?,?,?)",
                (task_id, diff, utc_now()),
            )

    def update_task_input(self, task_id: str, updates: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT input_json FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                raise ValueError("task not found")
            value = json.loads(row["input_json"])
            value.update(updates)
            conn.execute(
                "UPDATE tasks SET input_json=?,updated_at=? WHERE id=?",
                (json.dumps(value, ensure_ascii=False), utc_now(), task_id),
            )

    def get_task_payload(self, task_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT diff FROM task_payloads WHERE task_id=?", (task_id,)
            ).fetchone()
        return str(row["diff"]) if row else None

    def request_cancel(self, task_id: str, tenant_id: Optional[str] = None) -> bool:
        query = "UPDATE tasks SET cancel_requested=1,updated_at=? WHERE id=?"
        params = [utc_now(), task_id]
        if tenant_id is not None:
            query += " AND tenant_id=?"
            params.append(tenant_id)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(query, params)
            return cursor.rowcount > 0

    def is_cancelled(self, task_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT cancel_requested FROM tasks WHERE id=?", (task_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def cancel(self, task_id: str, event: TraceEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=?,updated_at=? WHERE id=?",
                (TaskState.CANCELLED.value, event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (?,?,?,?,?)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )

    def claim_webhook(
        self, delivery_id: str, tenant_id: str, event_type: str, payload_sha256: str,
    ) -> bool:
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery is required")
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO webhook_deliveries"
                    "(delivery_id,tenant_id,event_type,payload_sha256,received_at) VALUES (?,?,?,?,?)",
                    (delivery_id, tenant_id, event_type, payload_sha256, utc_now()),
                )
                return True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT payload_sha256 FROM webhook_deliveries WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                if row and row["payload_sha256"] != payload_sha256:
                    raise ValueError("delivery id was already used with a different payload")
                return False

    def complete_webhook(self, delivery_id: str, task_id: Optional[str]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE webhook_deliveries SET task_id=? WHERE delivery_id=?",
                (task_id, delivery_id),
            )

    def get_webhook(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM webhook_deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _workflow_event_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["metadata"] = json.loads(value.pop("metadata_json"))
        return value

    def _apply_workflow_event_tx(self, conn, event_id: str, fault_injector=None) -> Dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM workflow_events WHERE event_id=?", (event_id,),
        ).fetchone()
        if not row:
            raise ValueError("WORKFLOW_EVENT_UNSUPPORTED: event does not exist")
        event = self._workflow_event_from_row(row)
        if event["processing_status"] in {"APPLIED", "IGNORED", "CONFLICT", "AMBIGUOUS"}:
            return event
        now = utc_now()
        if not event.get("outcome"):
            conn.execute(
                "UPDATE workflow_events SET processing_status='IGNORED',"
                "processing_result=?,updated_at=? WHERE event_id=?",
                (event["policy_result"], now, event_id),
            )
            row = conn.execute("SELECT * FROM workflow_events WHERE event_id=?", (event_id,)).fetchone()
            return self._workflow_event_from_row(row)

        scoped_correlations = conn.execute(
            "SELECT * FROM autofix_correlations WHERE tenant_id=? AND repository=? "
            "AND commit_sha=? ORDER BY task_id",
            (event["tenant_id"], event["repository"], event["head_sha"]),
        ).fetchall()
        if not scoped_correlations:
            conn.execute(
                "UPDATE workflow_events SET processing_status='PENDING',"
                "processing_result='WORKFLOW_EVENT_UNMATCHED',updated_at=? WHERE event_id=?",
                (now, event_id),
            )
            row = conn.execute("SELECT * FROM workflow_events WHERE event_id=?", (event_id,)).fetchone()
            return self._workflow_event_from_row(row)
        correlations = [
            item for item in scoped_correlations
            if check_suite_matches_authority(
                event.get("metadata") or {}, item["ci_authority_app_id"],
                item["ci_authority_app_slug"],
            )
        ]
        if not correlations:
            conn.execute(
                "UPDATE workflow_events SET processing_status='IGNORED',"
                "processing_result='WORKFLOW_EVENT_NON_AUTHORITATIVE',updated_at=? "
                "WHERE event_id=?", (now, event_id),
            )
            row = conn.execute("SELECT * FROM workflow_events WHERE event_id=?", (event_id,)).fetchone()
            return self._workflow_event_from_row(row)
        if len(correlations) != 1:
            conn.execute(
                "UPDATE workflow_events SET processing_status='AMBIGUOUS',"
                "processing_result='WORKFLOW_CORRELATION_AMBIGUOUS',updated_at=? "
                "WHERE event_id=?", (now, event_id),
            )
            row = conn.execute("SELECT * FROM workflow_events WHERE event_id=?", (event_id,)).fetchone()
            return self._workflow_event_from_row(row)

        task_id = str(correlations[0]["task_id"])
        checkpoint = conn.execute(
            "SELECT * FROM checkpoints WHERE task_id=? AND node=?",
            (task_id, AUTOFIX_CHECKPOINT),
        ).fetchone()
        if not checkpoint:
            result = "WORKFLOW_TRANSITION_INVALID"
            status = "CONFLICT"
        else:
            state = json.loads(checkpoint["state_json"])
            phase = str(state.get("phase") or "")
            target = str(event["outcome"])
            if phase == "WAITING_FOR_CI":
                result_value = dict(state.get("result") or {})
                result_value.update({
                    "status": "ci-passed" if target == "CI_PASSED" else "ci-failed",
                    "ci_conclusion": event["conclusion"],
                    "ci_event_id": event_id,
                    "note": (
                        "The AutoFix draft passed its correlated CI check suite."
                        if target == "CI_PASSED" else
                        "The AutoFix draft failed its correlated CI check suite."
                    ),
                })
                next_state = transition_autofix_state(
                    state, target, result=result_value,
                    ci_event_id=event_id,
                    ci_external_object_id=event["external_object_id"],
                    ci_conclusion=event["conclusion"], ci_completed_at=now,
                )
                conn.execute(
                    "UPDATE checkpoints SET status='completed',state_json=?,error=NULL,"
                    "updated_at=? WHERE task_id=? AND node=?",
                    (json.dumps(next_state, ensure_ascii=False), now, task_id, AUTOFIX_CHECKPOINT),
                )
                if fault_injector:
                    fault_injector("after_workflow_transition")
                result = target
                status = "APPLIED"
            elif phase == target:
                result = "WORKFLOW_EVENT_DUPLICATE"
                status = "APPLIED"
            elif phase == "PR_CREATED":
                conn.execute(
                    "UPDATE workflow_events SET processing_status='PENDING',"
                    "processing_result='WORKFLOW_WAIT_NOT_DURABLE',correlated_task_id=?,"
                    "updated_at=? WHERE event_id=?", (task_id, now, event_id),
                )
                row = conn.execute("SELECT * FROM workflow_events WHERE event_id=?", (event_id,)).fetchone()
                return self._workflow_event_from_row(row)
            elif phase in CI_TERMINAL_PHASES:
                result = "WORKFLOW_EVENT_CONFLICT"
                status = "CONFLICT"
            else:
                result = "WORKFLOW_TRANSITION_INVALID"
                status = "CONFLICT"
        conn.execute(
            "UPDATE workflow_events SET processing_status=?,processing_result=?,"
            "correlated_task_id=?,applied_transition=?,updated_at=? WHERE event_id=?",
            (status, result, task_id, event.get("outcome") if status == "APPLIED" else "", now, event_id),
        )
        row = conn.execute("SELECT * FROM workflow_events WHERE event_id=?", (event_id,)).fetchone()
        return self._workflow_event_from_row(row)

    def record_and_apply_workflow_event(
        self, event: Dict[str, Any], fault_injector=None,
    ) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO workflow_events(event_id,logical_event_key,"
                "external_event_key,source,event_type,tenant_id,repository,external_object_id,"
                "head_sha,external_status,conclusion,outcome,received_at,metadata_json,"
                "policy_result,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event["event_id"], event["logical_event_key"], event["external_event_key"],
                    event["source"], event["event_type"], event["tenant_id"],
                    event["repository"], event["external_object_id"], event["head_sha"],
                    event["external_status"], event["conclusion"], event.get("outcome"),
                    event["received_at"], json.dumps(event.get("metadata") or {}, ensure_ascii=False),
                    event["policy_result"], utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM workflow_events WHERE logical_event_key=?",
                (event["logical_event_key"],),
            ).fetchone()
            if not row or str(row["event_id"]) != str(event["event_id"]):
                raise ValueError("WORKFLOW_EVENT_CONFLICT: logical identity collision")
            return self._apply_workflow_event_tx(conn, str(row["event_id"]), fault_injector)

    def suspend_autofix_for_ci(
        self, task_id: str, tenant_id: str, repository: str, commit_sha: str,
        repair_branch: str, pr_number: int, ci_authority: Dict[str, Any],
        waiting_result: Dict[str, Any], attempt: int,
        fault_injector=None,
    ) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT tenant_id,repository FROM tasks WHERE id=?", (task_id,),
            ).fetchone()
            if not task or task["tenant_id"] != tenant_id or task["repository"] != repository:
                raise ValueError("AutoFix correlation task scope mismatch")
            checkpoint = conn.execute(
                "SELECT * FROM checkpoints WHERE task_id=? AND node=?",
                (task_id, AUTOFIX_CHECKPOINT),
            ).fetchone()
            if not checkpoint:
                raise ValueError("AutoFix checkpoint is missing before CI suspension")
            current = json.loads(checkpoint["state_json"])
            phase = str(current.get("phase") or "")
            existing = conn.execute(
                "SELECT * FROM autofix_correlations WHERE task_id=?", (task_id,),
            ).fetchone()
            authority = canonical_ci_authority(
                ci_authority.get("github_app_id"), ci_authority.get("github_app_slug"),
            )
            identity = (
                tenant_id, repository, commit_sha, repair_branch, int(pr_number),
                authority["github_app_id"], authority["github_app_slug"],
            )
            if existing:
                stored = (
                    existing["tenant_id"], existing["repository"], existing["commit_sha"],
                    existing["repair_branch"], int(existing["pr_number"]),
                    existing["ci_authority_app_id"], existing["ci_authority_app_slug"],
                )
                if stored != identity:
                    raise ValueError("AutoFix durable correlation identity mismatch")
            else:
                conn.execute(
                    "INSERT INTO autofix_correlations(task_id,tenant_id,repository,commit_sha,"
                    "repair_branch,pr_number,ci_authority_app_id,ci_authority_app_slug,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (task_id, *identity, utc_now()),
                )
            if phase == "PR_CREATED":
                current = transition_autofix_state(
                    current, "WAITING_FOR_CI", result=waiting_result,
                    waiting_since=utc_now(), ci_authority=authority,
                )
                conn.execute(
                    "UPDATE checkpoints SET status='in_progress',attempt=?,state_json=?,"
                    "error=NULL,updated_at=? WHERE task_id=? AND node=?",
                    (attempt, json.dumps(current, ensure_ascii=False), utc_now(), task_id, AUTOFIX_CHECKPOINT),
                )
            elif phase not in {"WAITING_FOR_CI", *CI_TERMINAL_PHASES}:
                raise ValueError("WORKFLOW_TRANSITION_INVALID: cannot suspend from %s" % phase)
            pending = conn.execute(
                "SELECT event_id FROM workflow_events WHERE tenant_id=? AND repository=? "
                "AND head_sha=? AND processing_status='PENDING' ORDER BY received_at,event_id",
                (tenant_id, repository, commit_sha),
            ).fetchall()
            for event_row in pending:
                self._apply_workflow_event_tx(
                    conn, str(event_row["event_id"]), fault_injector,
                )
            checkpoint = conn.execute(
                "SELECT state_json FROM checkpoints WHERE task_id=? AND node=?",
                (task_id, AUTOFIX_CHECKPOINT),
            ).fetchone()
            return json.loads(checkpoint["state_json"])

    def get_workflow_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_events WHERE event_id=?", (event_id,),
            ).fetchone()
        return self._workflow_event_from_row(row) if row else None

    def list_autofix_correlations(
        self, tenant_id: str, repository: str, commit_sha: str,
    ) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM autofix_correlations WHERE tenant_id=? AND repository=? "
                "AND commit_sha=? ORDER BY task_id", (tenant_id, repository, commit_sha),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_user(
        self, user_id: str, username: str, password_hash: str,
        tenant_id: str, role: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?)",
                (user_id, username, password_hash, utc_now()),
            )
            row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            conn.execute(
                "INSERT INTO memberships(user_id,tenant_id,role) VALUES (?,?,?) "
                "ON CONFLICT(user_id,tenant_id) DO UPDATE SET role=excluded.role",
                (row["id"], tenant_id, role),
            )

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,username,password_hash,active FROM users WHERE username=?", (username,)
            ).fetchone()
            if not row:
                return None
            memberships = conn.execute(
                "SELECT tenant_id,role FROM memberships WHERE user_id=?", (row["id"],)
            ).fetchall()
        value = dict(row)
        value["memberships"] = [dict(item) for item in memberships]
        return value

    def grant_repository(self, tenant_id: str, repository: str, auto_fix: bool = False) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO repository_grants(tenant_id,repository,auto_fix) VALUES (?,?,?) "
                "ON CONFLICT(tenant_id,repository) DO UPDATE SET auto_fix=excluded.auto_fix",
                (tenant_id, repository, int(auto_fix)),
            )

    def repository_allowed(
        self, tenant_id: str, repository: str, require_auto_fix: bool = False,
    ) -> bool:
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM repository_grants WHERE tenant_id=?", (tenant_id,)
            ).fetchone()["n"]
            row = conn.execute(
                "SELECT auto_fix FROM repository_grants WHERE tenant_id=? AND repository=?",
                (tenant_id, repository),
            ).fetchone()
        if total == 0:
            return True
        return bool(row and (not require_auto_fix or row["auto_fix"]))

    def audit(
        self, tenant_id: str, actor: str, action: str, resource: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (tenant_id, actor, action, resource,
                 json.dumps(detail or {}, ensure_ascii=False), utc_now()),
            )

    def list_audit(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT actor,action,resource,detail_json,created_at FROM audit_log "
                "WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            values.append(item)
        return values

    def save_deployment(self, tenant_id: str, skill_name: str, config: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO deployments(tenant_id,skill_name,stable_version,candidate_version,"
                "canary_percent,shadow_percent,max_error_rate,min_samples,status,samples,errors,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,0,?) ON CONFLICT(tenant_id,skill_name) DO UPDATE SET "
                "stable_version=excluded.stable_version,candidate_version=excluded.candidate_version,"
                "canary_percent=excluded.canary_percent,shadow_percent=excluded.shadow_percent,"
                "max_error_rate=excluded.max_error_rate,min_samples=excluded.min_samples,"
                "status=excluded.status,samples=0,errors=0,updated_at=excluded.updated_at",
                (tenant_id, skill_name, config.get("stable_version"), config.get("candidate_version"),
                 int(config.get("canary_percent", 0)), int(config.get("shadow_percent", 0)),
                 float(config.get("max_error_rate", .1)), int(config.get("min_samples", 20)),
                 config.get("status", "running"), utc_now()),
            )
            conn.execute(
                "UPDATE deployments SET max_disagreement_rate=?,auto_promote=?,"
                "shadow_samples=0,disagreements=0 WHERE tenant_id=? AND skill_name=?",
                (float(config.get("max_disagreement_rate", .2)),
                 int(bool(config.get("auto_promote", False))), tenant_id, skill_name),
            )

    def get_deployment(self, tenant_id: str, skill_name: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deployments WHERE tenant_id=? AND skill_name=?",
                (tenant_id, skill_name),
            ).fetchone()
        return dict(row) if row else None

    def record_deployment_result(
        self, tenant_id: str, skill_name: str, failed: bool,
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE deployments SET samples=samples+1,errors=errors+?,updated_at=? "
                "WHERE tenant_id=? AND skill_name=?",
                (int(failed), utc_now(), tenant_id, skill_name),
            )
            row = conn.execute(
                "SELECT * FROM deployments WHERE tenant_id=? AND skill_name=?",
                (tenant_id, skill_name),
            ).fetchone()
            if not row:
                return None
            value = dict(row)
            if (
                value["status"] == "running"
                and value["samples"] >= value["min_samples"]
                and value["errors"] / value["samples"] > value["max_error_rate"]
            ):
                conn.execute(
                    "UPDATE deployments SET status='rolled_back',canary_percent=0,"
                    "shadow_percent=0,updated_at=? WHERE tenant_id=? AND skill_name=?",
                    (utc_now(), tenant_id, skill_name),
                )
                value["status"] = "rolled_back"
        return value

    def record_shadow_observation(
        self, tenant_id: str, skill_name: str, task_id: str, lane: str,
        primary: Dict[str, Any], candidate: Optional[Dict[str, Any]],
        disagreement: float, candidate_failed: bool = False,
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO release_observations(tenant_id,skill_name,task_id,lane,"
                "primary_json,candidate_json,disagreement,candidate_failed,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, skill_name, task_id, lane,
                 json.dumps(primary, ensure_ascii=False),
                 json.dumps(candidate, ensure_ascii=False) if candidate is not None else None,
                 float(disagreement), int(candidate_failed), utc_now()),
            )
            conn.execute(
                "UPDATE deployments SET shadow_samples=shadow_samples+1,"
                "disagreements=disagreements+?,updated_at=? "
                "WHERE tenant_id=? AND skill_name=?",
                (int(disagreement > 0), utc_now(), tenant_id, skill_name),
            )
            row = conn.execute(
                "SELECT * FROM deployments WHERE tenant_id=? AND skill_name=?",
                (tenant_id, skill_name),
            ).fetchone()
            if not row:
                return None
            value = dict(row)
            disagreement_rate = (
                value["disagreements"] / value["shadow_samples"]
                if value["shadow_samples"] else 0.0
            )
            error_rate = value["errors"] / value["samples"] if value["samples"] else 0.0
            if (
                value["status"] == "running" and value["auto_promote"]
                and value["shadow_samples"] >= value["min_samples"]
                and disagreement_rate <= value["max_disagreement_rate"]
                and error_rate <= value["max_error_rate"]
                and not candidate_failed
            ):
                conn.execute(
                    "UPDATE deployments SET status='promoted',stable_version=candidate_version,"
                    "canary_percent=0,shadow_percent=0,updated_at=? "
                    "WHERE tenant_id=? AND skill_name=?",
                    (utc_now(), tenant_id, skill_name),
                )
                value["status"] = "promoted"
        return value

    def list_release_observations(
        self, tenant_id: str, skill_name: str, limit: int = 100,
    ) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM release_observations WHERE tenant_id=? AND skill_name=? "
                "ORDER BY id DESC LIMIT ?",
                (tenant_id, skill_name, max(1, min(limit, 500))),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["primary"] = json.loads(item.pop("primary_json"))
            raw = item.pop("candidate_json")
            item["candidate"] = json.loads(raw) if raw else None
            values.append(item)
        return values

    def create_alert(
        self, tenant_id: str, alert_key: str, severity: str, message: str,
    ) -> None:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO alerts"
                "(tenant_id,alert_key,severity,message,status,created_at,updated_at) "
                "VALUES (?,?,?,?, 'open',?,?)",
                (tenant_id, alert_key, severity, message[:1000], now, now),
            )

    def list_alerts(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_stats(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        with self._connect() as conn:
            clause = " WHERE tenant_id=?" if tenant_id is not None else ""
            params = (tenant_id,) if tenant_id is not None else ()
            total = conn.execute("SELECT COUNT(*) AS n FROM tasks" + clause, params).fetchone()["n"]
            state_prefix = clause + (" AND " if clause else " WHERE ")
            success = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks" + state_prefix + "state='SUCCESS'", params
            ).fetchone()["n"]
            failed = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks" + state_prefix + "state='FAILED'", params
            ).fetchone()["n"]
            if tenant_id is None:
                failures = conn.execute(
                    "SELECT COUNT(*) AS n FROM failure_cases WHERE resolved=0"
                ).fetchone()["n"]
            else:
                failures = conn.execute(
                    "SELECT COUNT(*) AS n FROM failure_cases f JOIN tasks t ON t.id=f.task_id "
                    "WHERE f.resolved=0 AND t.tenant_id=?", (tenant_id,)
                ).fetchone()["n"]
            active_skills = conn.execute(
                "SELECT COUNT(*) AS n FROM skill_versions WHERE active = 1"
            ).fetchone()["n"]
            if tenant_id is None:
                active_skills += conn.execute(
                    "SELECT COUNT(*) AS n FROM skill_artifact_versions WHERE active=1"
                ).fetchone()["n"]
            else:
                active_skills += conn.execute(
                    "SELECT COUNT(*) AS n FROM skill_artifact_versions "
                    "WHERE tenant_id=? AND active=1", (tenant_id,)
                ).fetchone()["n"]
        return {
            "tasks_total": total, "tasks_success": success, "tasks_failed": failed,
            "success_rate": round(success / total, 4) if total else 0.0,
            "unresolved_failure_cases": failures, "active_skill_versions": active_skills,
        }
