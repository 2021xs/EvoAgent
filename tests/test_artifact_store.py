import os
import tempfile
import unittest

from evoagent.artifacts import (
    Artifact,
    ArtifactAccessDenied,
    ArtifactCorrupted,
    ArtifactIntegrityConflict,
    ArtifactRuntime,
    ArtifactScope,
    ToolExecutionKey,
    artifact_from_store,
)
from evoagent.store import TaskStore


class ArtifactStoreContract:
    store = None

    def make_artifact(self, content=None, logical_key="logical:1"):
        return Artifact.for_tool_result(
            ArtifactScope("tenant-a", "org/repo", "task-a"),
            producer="security",
            source_revision="abc123",
            logical_execution_key=logical_key,
            content=content or {
                "evidence_id": "read_file:e1",
                "tool": "read_file",
                "output": "hello",
            },
            evidence_id="read_file:e1",
            metadata={"tool": "read_file"},
        )

    def test_idempotent_write_and_lookup(self):
        artifact = self.make_artifact()
        first = artifact_from_store(self.store.put_artifact(artifact.to_dict()))
        retry = self.make_artifact()
        self.assertNotEqual(artifact.artifact_id, retry.artifact_id)
        second = artifact_from_store(self.store.put_artifact(retry.to_dict()))
        by_key = artifact_from_store(self.store.get_artifact_by_logical_execution_key(
            artifact.logical_execution_key, "tenant-a", "org/repo", "task-a"
        ))
        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(first.artifact_id, by_key.artifact_id)
        self.assertEqual("hello", by_key.content["output"])

    def test_integrity_conflict_fails_closed(self):
        first = self.make_artifact()
        self.store.put_artifact(first.to_dict())
        second = self.make_artifact({
            "evidence_id": "read_file:e2", "tool": "read_file", "output": "changed"
        })
        with self.assertRaises(ArtifactIntegrityConflict):
            self.store.put_artifact(second.to_dict())

    def test_artifact_id_does_not_grant_cross_task_access(self):
        artifact = self.make_artifact()
        stored = self.store.put_artifact(artifact.to_dict())
        with self.assertRaises(ArtifactAccessDenied):
            self.store.get_artifact(
                stored["artifact_id"], "tenant-a", "org/repo", "task-b"
            )

    def test_write_scope_must_match_owning_task(self):
        value = self.make_artifact().to_dict()
        value["repository"] = "other/repo"
        with self.assertRaises(ArtifactAccessDenied):
            self.store.put_artifact(value)


class SQLiteArtifactStoreTests(ArtifactStoreContract, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = TaskStore(self.temp.name)
        self.store.create("task-a", "org/repo", 1, {}, tenant_id="tenant-a")
        self.store.create("task-b", "org/repo", 2, {}, tenant_id="tenant-a")

    def tearDown(self):
        self.temp.close()

    def test_bounded_materialization_keeps_full_content(self):
        artifact = self.make_artifact({
            "evidence_id": "read_file:e1", "tool": "read_file", "output": "x" * 20000
        })
        stored = artifact_from_store(self.store.put_artifact(artifact.to_dict()))
        runtime = ArtifactRuntime(
            self.store, artifact.scope, "abc123", "assignment-1", 0,
            "security", "assignment",
        )
        view = runtime.materialize(stored.artifact_id, offset=100, max_chars=250)
        self.assertEqual(250, len(view["content"]))
        self.assertEqual({"start": 100, "end": 350}, view["range"])
        self.assertTrue(view["truncated"])
        self.assertEqual("x" * 20000, runtime.get(stored.artifact_id).content["output"])

    def test_execution_key_is_canonical_and_excludes_runtime_run_id(self):
        common = dict(
            task_id="task-a", assignment_id="assignment-1", revision_round=0,
            role="security", interaction_id="assignment", step=1,
            tool_name="read_file", source_revision="abc123",
        )
        left = ToolExecutionKey(arguments={"path": "a.py", "start_line": 1}, **common)
        right = ToolExecutionKey(arguments={"start_line": 1, "path": "a.py"}, **common)
        self.assertEqual(left.value, right.value)
        self.assertNotIn("run_id", left.payload())

    def test_corrupted_content_is_rejected_on_materialization(self):
        artifact = self.make_artifact()
        self.store.put_artifact(artifact.to_dict())
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE artifacts SET content_json=? WHERE artifact_id=?",
                ('{"output":"tampered"}', artifact.artifact_id),
            )
        runtime = ArtifactRuntime(
            self.store, artifact.scope, "abc123", "assignment-1", 0,
            "security", "assignment",
        )
        with self.assertRaises(ArtifactCorrupted):
            runtime.get(artifact.artifact_id)


@unittest.skipUnless(
    os.environ.get("EVOAGENT_TEST_POSTGRES_URL"),
    "set EVOAGENT_TEST_POSTGRES_URL to run the PostgreSQL artifact contract",
)
class PostgresArtifactStoreTests(ArtifactStoreContract, unittest.TestCase):
    def setUp(self):
        from evoagent.postgres_store import PostgresTaskStore
        self.store = PostgresTaskStore(os.environ["EVOAGENT_TEST_POSTGRES_URL"])
        for task_id in ("task-a", "task-b"):
            with self.store._connect() as conn:
                conn.execute("DELETE FROM artifacts WHERE task_id=%s", (task_id,))
                conn.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
            self.store.create(task_id, "org/repo", 1, {}, tenant_id="tenant-a")


if __name__ == "__main__":
    unittest.main()
