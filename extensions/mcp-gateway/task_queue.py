import asyncio
import hashlib
import json
import sqlite3
import time
import uuid

from answer_contract import is_model_failure, validate_answer
from authorization import current, owner_id


class Queue:
    def __init__(self, path, backend, allowed, timeout=300):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, agent TEXT, text TEXT, conversation TEXT, idem TEXT UNIQUE, fingerprint TEXT, status TEXT, result TEXT, error TEXT, created REAL)"
        )
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(jobs)")]
        if "request_conversation" not in cols:
            self.db.execute("ALTER TABLE jobs ADD COLUMN request_conversation TEXT")
        if "thread_id" not in cols:
            self.db.execute("ALTER TABLE jobs ADD COLUMN thread_id TEXT")
        if "owner" not in cols:
            self.db.execute("ALTER TABLE jobs ADD COLUMN owner TEXT NOT NULL DEFAULT 'legacy'")
        self.resolve_principal = None
        self.db.execute(
            "UPDATE jobs SET status='interrupted', error='gateway_shutdown; upstream may still be running' WHERE status='running'"
        )
        self.db.commit()
        self.backend = backend
        self.allowed = allowed
        self.timeout = timeout
        self.worker = None
        self._lock = asyncio.Lock()

    def view(self, jid):
        r = self.db.execute(
            "SELECT id,agent,conversation,status,result,error,thread_id,created FROM jobs WHERE id=? AND owner=?",
            (jid, owner_id()),
        ).fetchone()
        if not r:
            return {"ok": False, "error_code": "job_not_found", "retryable": False}
        result = json.loads(r[4]) if r[4] else None
        detail = {
            "ok": True,
            "job_id": r[0],
            "agent_id": r[1],
            "conversation_id": r[2],
            "status": r[3],
            "result": result,
            "error_code": r[5],
            "thread_id": r[6],
            "retryable": False,
            "poll_after_seconds": 2 if r[3] in ("queued", "running") else 0,
            "created_at": r[7],
        }
        # Preserve the original raw row for audit; do not continue advertising
        # historical framework failures as successful business answers.
        if (
            r[3] == "succeeded"
            and isinstance(result, dict)
            and is_model_failure(result.get("answer"))
        ):
            detail.update(status="failed", result=None, error_code="backend_model_failed")
        if detail["status"] == "failed":
            detail["retryable"] = self._retryable(r[5])
        return detail

    def _retryable(self, error_text):
        if not error_text:
            return False
        code = error_text.split(";", 1)[0].strip()
        # Timeout/disconnect/model failure may follow upstream acceptance.
        # Only pre-submission connection/auth failures are safe to retry.
        return code in ("backend_unavailable", "backend_login_failed")

    def submit(self, agent, text, conversation=None, idem=None):
        if not isinstance(agent, str) or agent not in self.allowed:
            return {"ok": False, "error_code": "agent_not_allowed", "retryable": False}
        principal = current.get()
        if principal is not None and agent not in principal.agents:
            return {"ok": False, "error_code": "agent_not_allowed", "retryable": False}
        if idem is not None and (not isinstance(idem, str) or len(idem) > 100):
            return {"ok": False, "error_code": "invalid_idempotency_key", "retryable": False}
        if idem and owner_id() != "legacy":
            idem = hashlib.sha256((owner_id() + ":" + idem).encode()).hexdigest()
        if conversation is not None and not isinstance(conversation, str):
            return {"ok": False, "error_code": "invalid_conversation_id", "retryable": False}
        requested = conversation or None
        if requested and (not isinstance(requested, str) or len(requested) > 100):
            return {"ok": False, "error_code": "invalid_conversation_id", "retryable": False}
        fp = hashlib.sha256(
            json.dumps([agent, text, requested], ensure_ascii=False).encode()
        ).hexdigest()
        if idem:
            if len(idem) > 100:
                return {"ok": False, "error_code": "invalid_idempotency_key", "retryable": False}
            r = self.db.execute(
                "SELECT id,fingerprint,agent,text,conversation,request_conversation FROM jobs WHERE idem=? AND owner=?",
                (idem, owner_id()),
            ).fetchone()
            if r:
                # Legacy rows fingerprinted the generated ID. Compare original
                # fields too, without generating another conversation on retry.
                legacy = (
                    r[5] is None
                    and r[2] == agent
                    and r[3] == text
                    and (requested is None or requested == r[4])
                )
                if r[1] == fp or legacy:
                    detail = self.view(r[0])
                    detail["idempotent"] = True
                    return detail
                return {"ok": False, "error_code": "idempotency_conflict", "retryable": False}
        conversation = (
            uuid.uuid4().hex if not requested or requested.startswith("new:") else requested
        )
        if self.db.execute(
            "SELECT 1 FROM jobs WHERE conversation=? AND agent<>? AND owner=? LIMIT 1",
            (conversation, agent, owner_id()),
        ).fetchone():
            return {"ok": False, "error_code": "conversation_agent_mismatch", "retryable": False}
        if (
            self.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()[0]
            >= 100
        ):
            return {
                "ok": False,
                "error_code": "queue_full",
                "retryable": True,
                "retry_after_seconds": 5,
            }
        jid = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO jobs (id,agent,text,conversation,idem,fingerprint,status,result,error,created) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (jid, agent, text, conversation, idem, fp, "queued", None, None, time.time()),
        )
        self.db.execute(
            "UPDATE jobs SET request_conversation=?,owner=? WHERE id=?",
            (json.dumps(requested), owner_id(), jid),
        )
        self.db.commit()
        self.start()
        return self.view(jid)

    def bind_thread(self, job_id, thread_id):
        if thread_id:
            self.db.execute(
                "UPDATE jobs SET thread_id=? WHERE id=? AND thread_id IS NULL", (thread_id, job_id)
            )
            self.db.commit()

    def start(self):
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.run())

    async def run(self):
        while True:
            r = self.db.execute(
                "SELECT id,agent,text,conversation,thread_id,owner FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if not r:
                return
            jid, agent, text, conv, thread, owner = r
            principal = self.resolve_principal(owner) if self.resolve_principal else None
            context_token = current.set(principal)
            async with self._lock:
                self.db.execute(
                    "UPDATE jobs SET status='running' WHERE id=? AND status='queued'", (jid,)
                )
                self.db.commit()
            try:
                if self.resolve_principal and (principal is None or agent not in principal.agents):
                    raise RuntimeError("authorization_revoked")
                result = await asyncio.wait_for(self.backend(agent, text, conv), self.timeout)
                validate_answer(result)
                status = "succeeded"
                error = None
                out = dict(result)
                out.pop("thread_id", None)
                self.db.execute(
                    "UPDATE jobs SET status=?,result=?,error=?,thread_id=? WHERE id=?",
                    (
                        status,
                        json.dumps(out, ensure_ascii=False),
                        None,
                        result.get("thread_id"),
                        jid,
                    ),
                )
                self.db.commit()
            except asyncio.CancelledError:
                self.db.execute(
                    "UPDATE jobs SET status='interrupted',error='gateway_shutdown; upstream may still be running' WHERE id=?",
                    (jid,),
                )
                self.db.commit()
                raise
            except TimeoutError:
                status = "failed"
                error = "backend_timeout"
                self.db.execute(
                    "UPDATE jobs SET status=?,result=?,error=? WHERE id=?",
                    (status, None, error, jid),
                )
                self.db.commit()
            except Exception as exc:
                # classifiable backend errors
                code = getattr(exc, "code", None) or str(exc)
                if code in (
                    "authorization_revoked",
                    "backend_busy",
                    "backend_capacity_limited",
                    "backend_model_failed",
                    "backend_not_configured",
                    "backend_login_failed",
                    "backend_unavailable",
                    "backend_failed",
                    "backend_restarted",
                    "backend_timeout",
                    "backend_empty_response",
                    "backend_missing_final_response",
                    "backend_output_limit",
                    "backend_frame_too_large",
                    "backend_disconnected_before_done",
                    "upstream_error",
                    "hitl",
                    "approval_required",
                    "invalid_conversation_id",
                ):
                    code = code
                else:
                    code = "backend_failed"
                status = "failed"
                error = code
                self.db.execute(
                    "UPDATE jobs SET status=?,result=?,error=?,thread_id=COALESCE(?,thread_id) WHERE id=?",
                    (status, None, error, getattr(exc, "thread_id", None), jid),
                )
                self.db.commit()

            finally:
                current.reset(context_token)
