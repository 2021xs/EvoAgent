import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SelfEvolutionDemoTests(unittest.TestCase):
    def test_controlled_demo_closes_the_production_self_evolution_loop(self):
        with tempfile.TemporaryDirectory() as output_directory:
            completed = subprocess.run(
                [
                    sys.executable,
                    os.path.join(ROOT, "scripts", "run_self_evolution_demo.py"),
                    "--output-dir",
                    output_directory,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            with open(
                os.path.join(output_directory, "report.json"),
                "r",
                encoding="utf-8",
            ) as handle:
                report = json.load(handle)

            self.assertTrue(report["success"])
            self.assertEqual(
                report["status"], "SELF_EVOLUTION_MVP_END_TO_END_VALIDATED"
            )
            stages = report["stages"]
            self.assertFalse(stages["baseline"]["expected_found"])
            self.assertEqual(stages["baseline"]["worker_skill"]["source"], "disk")
            self.assertEqual(stages["attribution"]["first_divergence"], "DISCOVERY")
            self.assertEqual(stages["attribution"]["root_cause"], "SKILL_GUIDANCE_GAP")
            self.assertEqual(stages["attribution"]["status"], "SUPPORTED")
            self.assertEqual(stages["evolution"]["decision"], "ready_for_promotion")
            self.assertTrue(all(stages["evolution"]["gates"].values()))
            self.assertEqual(
                stages["evolution"]["source_replay"]["baseline"][0]["fn"], 1
            )
            self.assertEqual(
                stages["evolution"]["source_replay"]["candidate"][0]["tp"], 1
            )
            self.assertFalse(stages["evolution"]["version"]["active"])
            self.assertTrue(stages["promotion"]["active"])
            self.assertEqual(stages["promotion"]["source_failure_resolved"], 0)
            self.assertTrue(stages["after_promotion"]["expected_found"])
            self.assertEqual(
                stages["after_promotion"]["worker_skill"]["source"], "evolved-db"
            )
            self.assertEqual(
                stages["after_promotion"]["worker_skill"]["content_sha256"],
                stages["promotion"]["runtime_content_sha256"],
            )
            self.assertIsNone(stages["rollback"]["active_db_override"])
            self.assertTrue(stages["rollback"]["bundled_hash_restored"])
            self.assertFalse(stages["post_rollback"]["expected_found"])
            self.assertEqual(stages["post_rollback"]["worker_skill"]["source"], "disk")
            self.assertEqual(
                stages["post_rollback"]["worker_skill"]["content_sha256"],
                stages["baseline"]["worker_skill"]["content_sha256"],
            )

            serialized = json.dumps(report, sort_keys=True)
            self.assertNotIn("candidate_id", serialized)
            self.assertNotIn("worker_execution_snapshots", serialized)
            with open(
                os.path.join(output_directory, "security-review.patch"),
                "r",
                encoding="utf-8",
            ) as handle:
                patch = handle.read()
            self.assertIn("+## CSV formula injection", patch)
            self.assertNotIn("resources/", patch)


if __name__ == "__main__":
    unittest.main()
