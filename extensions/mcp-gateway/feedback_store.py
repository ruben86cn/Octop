"""Durable feedback and immutable, administrator-approved change plans."""

import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class FeedbackStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        Path(path).chmod(0o600)
        self.db.execute("""CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY, job_id TEXT UNIQUE NOT NULL, owner TEXT NOT NULL,
            target TEXT NOT NULL, record TEXT NOT NULL, state TEXT NOT NULL,
            analysis TEXT, plan TEXT, plan_hash TEXT, approved_hash TEXT,
            approved_by TEXT, approved_at REAL, created REAL NOT NULL,
            updated REAL NOT NULL, execution TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS feedback_audit (
            id INTEGER PRIMARY KEY, feedback_id TEXT NOT NULL, event TEXT NOT NULL,
            actor TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL)""")
        self.db.commit()

    def audit(self, fid, event, actor, detail):
        self.db.execute(
            "INSERT INTO feedback_audit (feedback_id,event,actor,detail,created) VALUES (?,?,?,?,?)",
            (fid, event, actor, canonical(detail), time.time()),
        )

    def create(self, job_id, owner, target, record):
        row = self.db.execute("SELECT id FROM feedback WHERE job_id=?", (job_id,)).fetchone()
        if row:
            return row[0]
        fid = uuid.uuid4().hex
        now = time.time()
        self.db.execute(
            "INSERT INTO feedback (id,job_id,owner,target,record,state,created,updated) VALUES (?,?,?,?,?,?,?,?)",
            (fid, job_id, owner, target, canonical(record), "analyzing", now, now),
        )
        self.audit(fid, "submitted", owner, {"job_id": job_id, "target": target})
        self.db.commit()
        return fid

    def get(self, fid, owner=None, admin=False):
        row = self.db.execute(
            "SELECT id,job_id,owner,target,record,state,analysis,plan,plan_hash,approved_hash,approved_by,approved_at,created,updated,execution FROM feedback WHERE id=?",
            (fid,),
        ).fetchone()
        if not row or (owner is not None and row[2] != owner):
            return None
        value = dict(
            zip(
                (
                    "feedback_id",
                    "job_id",
                    "source_token_id",
                    "target_agent_id",
                    "record",
                    "state",
                    "analysis",
                    "plan",
                    "plan_hash",
                    "approved_hash",
                    "approved_by",
                    "approved_at",
                    "created_at",
                    "updated_at",
                    "execution",
                ),
                row,
                strict=False,
            )
        )
        for key in ("record", "plan", "execution"):
            if value[key] is not None:
                value[key] = json.loads(value[key])
        if not admin:
            # Approved edits may contain internal knowledge; external callers
            # receive their own analysis and lifecycle, never file snapshots.
            value.pop("plan")
            value.pop("execution")
        return value

    def listing(self, owner=None, state=None, limit=100):
        query = "SELECT id FROM feedback WHERE 1=1"
        args = []
        if owner is not None:
            query += " AND owner=?"
            args.append(owner)
        if state is not None:
            query += " AND state=?"
            args.append(state)
        query += " ORDER BY created DESC LIMIT ?"
        args.append(max(1, min(limit, 100)))
        return [self.get(row[0], owner) for row in self.db.execute(query, args)]

    def refresh(self, fid, jobs):
        record = self.get(fid)
        if not record or record["state"] != "analyzing":
            return
        row = jobs.execute(
            "SELECT status,result,error FROM jobs WHERE id=?", (record["job_id"],)
        ).fetchone()
        if not row:
            return
        if row[0] == "succeeded":
            result = json.loads(row[1]) if row[1] else {}
            answer = result.get("answer")
            if (
                not isinstance(answer, str)
                or not answer.strip()
                or answer.lstrip().startswith("[model_call_failed]")
            ):
                state = "analysis_failed"
                answer = None
            else:
                state = "pending_review"
        elif row[0] in ("failed", "interrupted"):
            state = "analysis_failed"
            answer = None
        else:
            return
        self.db.execute(
            "UPDATE feedback SET state=?,analysis=?,updated=? WHERE id=?",
            (state, answer, time.time(), fid),
        )
        self.audit(fid, state, "gateway", {"job_status": row[0], "error_code": row[2]})
        self.db.commit()

    def set_plan(self, fid, operations, actor):
        record = self.get(fid, admin=True)
        if not record or record["state"] not in (
            "pending_review",
            "pending_approval",
            "approved",
            "execution_failed",
        ):
            raise ValueError("feedback_not_ready_for_plan")
        validate_plan(operations)
        plan = {"target_agent_id": record["target_agent_id"], "operations": operations}
        sha = digest(canonical(plan))
        self.db.execute(
            "UPDATE feedback SET plan=?,plan_hash=?,approved_hash=NULL,approved_by=NULL,approved_at=NULL,state='pending_approval',updated=? WHERE id=?",
            (canonical(plan), sha, time.time(), fid),
        )
        self.audit(fid, "plan_prepared", actor, {"plan_hash": sha})
        self.db.commit()
        return sha

    def approve(self, fid, plan_hash, actor):
        with self.db:
            row = self.db.execute(
                "UPDATE feedback SET state='approved',approved_hash=plan_hash,approved_by=?,approved_at=?,updated=? WHERE id=? AND state='pending_approval' AND plan_hash=?",
                (actor, time.time(), time.time(), fid, plan_hash),
            )
            if row.rowcount != 1:
                raise ValueError("plan_not_pending_or_hash_mismatch")
            self.audit(fid, "approved", actor, {"plan_hash": plan_hash})

    def reject(self, fid, actor, reason):
        with self.db:
            row = self.db.execute(
                "UPDATE feedback SET state='rejected',approved_hash=NULL,updated=? WHERE id=? AND state IN ('pending_review','pending_approval','approved','analysis_failed')",
                (time.time(), fid),
            )
            if row.rowcount != 1:
                raise ValueError("feedback_not_rejectable")
            self.audit(fid, "rejected", actor, {"reason": reason})

    def begin_apply(self, fid, plan_hash, actor):
        with self.db:
            row = self.db.execute(
                "UPDATE feedback SET state='applying',updated=? WHERE id=? AND state='approved' AND plan_hash=? AND approved_hash=?",
                (time.time(), fid, plan_hash, plan_hash),
            )
            if row.rowcount != 1:
                raise ValueError("plan_not_approved_or_already_claimed")
            record = self.get(fid, admin=True)
            if digest(canonical(record["plan"])) != plan_hash:
                raise ValueError("plan_content_hash_mismatch")
            self.audit(fid, "execution_started", actor, {"plan_hash": plan_hash})
        return record["plan"]

    def snapshot(self, fid, actor, items):
        self.audit(fid, "pre_execution_snapshot", actor, items)
        self.db.commit()

    def finish_apply(self, fid, actor, success, detail):
        state = "applied" if success else "execution_failed"
        self.db.execute(
            "UPDATE feedback SET state=?,execution=?,updated=? WHERE id=? AND state='applying'",
            (state, canonical(detail), time.time(), fid),
        )
        self.audit(fid, state, actor, detail)
        self.db.commit()


def validate_plan(operations):
    if not isinstance(operations, list) or not 1 <= len(operations) <= 20:
        raise ValueError("invalid_operations")
    seen = set()
    for op in operations:
        if not isinstance(op, dict):
            raise ValueError("invalid_operation")
        kind = op.get("kind")
        keys = {"kind", "expected_sha256", "content"} | ({"path"} if kind == "file" else set())
        if kind not in ("file", "system_prompt") or set(op) != keys:
            raise ValueError("invalid_operation_fields")
        if not isinstance(op["content"], str) or len(op["content"].encode()) > 1000000:
            raise ValueError("invalid_content")
        expected = op["expected_sha256"]
        if expected is not None and (
            not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected)
        ):
            raise ValueError("invalid_expected_sha256")
        if kind == "system_prompt" and expected is None:
            raise ValueError("system_prompt_requires_existing_hash")
        if kind == "file":
            rel = op["path"]
            if (
                not isinstance(rel, str)
                or not rel
                or "\\" in rel
                or Path(rel).is_absolute()
                or any(x in ("", ".", "..") for x in rel.split("/"))
            ):
                raise ValueError("invalid_path")
            if rel not in ("AGENTS.md", "MEMORY.md", "IDENTITY.md") and not (
                rel.startswith("knowledge/") and Path(rel).suffix in (".md", ".txt", ".json")
            ):
                raise ValueError("path_not_approved_content_area")
        key = (kind, op.get("path"))
        if key in seen:
            raise ValueError("duplicate_operation")
        seen.add(key)
