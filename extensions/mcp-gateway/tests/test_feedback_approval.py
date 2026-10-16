import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apply_changes import apply_plan
from feedback_store import FeedbackStore, digest, validate_plan


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "agents/A/knowledge").mkdir(parents=True)
        self.target = self.root / "agents/A/knowledge/example.md"
        self.target.write_text("before")
        self.store = FeedbackStore(self.root / "feedback.db")
        self.jobs = sqlite3.connect(":memory:")
        self.jobs.execute("CREATE TABLE jobs (id,status,result,error)")
        self.jobs.execute(
            "INSERT INTO jobs VALUES (?,?,?,?)",
            ("job", "succeeded", json.dumps({"answer": "proposal"}), None),
        )
        self.fid = self.store.create("job", "token-a", "A", {"issue": "correction"})
        self.store.refresh(self.fid, self.jobs)

    def tearDown(self):
        self.store.db.close()
        self.jobs.close()
        self.tmp.cleanup()

    def plan(self, content="after"):
        return self.store.set_plan(
            self.fid,
            [
                {
                    "kind": "file",
                    "path": "knowledge/example.md",
                    "expected_sha256": digest("before"),
                    "content": content,
                }
            ],
            "admin",
        )

    def apply(self, sha):
        return apply_plan(self.store, self.fid, sha, "admin", self.root / "agents")

    def test_full_lifecycle_and_no_unapproved_execution(self):
        self.assertEqual(self.store.get(self.fid)["state"], "pending_review")
        sha = self.plan()
        with self.assertRaises(ValueError):
            self.apply(sha)
        self.assertEqual(self.target.read_text(), "before")
        self.store.approve(self.fid, sha, "admin")
        self.apply(sha)
        self.assertEqual(self.target.read_text(), "after")
        self.assertEqual(self.store.get(self.fid)["state"], "applied")
        with self.assertRaises(ValueError):
            self.apply(sha)
        self.assertIsNone(self.store.get(self.fid, "different-token"))
        self.assertNotIn("plan", self.store.get(self.fid, "token-a"))
        events = [r[0] for r in self.store.db.execute("SELECT event FROM feedback_audit")]
        self.assertIn("pre_execution_snapshot", events)

    def test_replan_invalidates_approval(self):
        first = self.plan()
        self.store.approve(self.fid, first, "admin")
        second = self.plan("different")
        self.assertNotEqual(first, second)
        with self.assertRaises(ValueError):
            self.apply(first)
        with self.assertRaises(ValueError):
            self.apply(second)
        with self.assertRaises(ValueError):
            self.store.approve(self.fid, first, "admin")
        self.store.approve(self.fid, second, "admin")
        self.apply(second)
        self.assertEqual(self.target.read_text(), "different")

    def test_stale_target_fails_without_overwriting(self):
        sha = self.plan()
        self.store.approve(self.fid, sha, "admin")
        self.target.write_text("new information")
        with self.assertRaisesRegex(ValueError, "target_changed"):
            self.apply(sha)
        self.assertEqual(self.target.read_text(), "new information")
        self.assertEqual(self.store.get(self.fid)["state"], "execution_failed")

    def test_path_escape_and_symlink_rejected(self):
        for path in ("../secret", "/etc/hosts", "knowledge/../../x", "knowledge/x.py"):
            with self.assertRaises(ValueError):
                validate_plan(
                    [{"kind": "file", "path": path, "content": "x", "expected_sha256": None}]
                )
        self.target.unlink()
        self.target.symlink_to(self.root / "outside")
        sha = self.plan()
        self.store.approve(self.fid, sha, "admin")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.apply(sha)
        self.assertFalse((self.root / "outside").exists())

    def test_reject_and_failed_analysis(self):
        self.store.reject(self.fid, "admin", "insufficient evidence")
        with self.assertRaises(ValueError):
            self.plan()
        self.jobs.execute(
            "INSERT INTO jobs VALUES (?,?,?,?)", ("failed", "failed", None, "backend_failed")
        )
        fid = self.store.create("failed", "token-a", "A", {})
        self.store.refresh(fid, self.jobs)
        self.assertEqual(self.store.get(fid)["state"], "analysis_failed")

    def test_system_prompt_exact_change(self):
        class Backend:
            value = "old prompt"

            def get_prompt(self, agent):
                return self.value

            def set_prompt(self, agent, value):
                self.value = value

        backend = Backend()
        sha = self.store.set_plan(
            self.fid,
            [
                {
                    "kind": "system_prompt",
                    "expected_sha256": digest("old prompt"),
                    "content": "new prompt",
                }
            ],
            "admin",
        )
        self.store.approve(self.fid, sha, "admin")
        apply_plan(self.store, self.fid, sha, "admin", self.root / "agents", backend)
        self.assertEqual(backend.value, "new prompt")
