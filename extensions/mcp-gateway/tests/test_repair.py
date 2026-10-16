import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
import websockets
from server import create_app
from task_queue import Queue


class RepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_namespace_and_legacy_idempotency(self):
        async def backend(*args):
            return {"answer": "OK"}

        q = Queue(":memory:", backend, {"A", "B"})
        a = q.submit("A", "x", conversation="new:fixed", idem="key")
        b = q.submit("A", "x", conversation="new:fixed", idem="key")
        self.assertEqual(a["job_id"], b["job_id"])
        self.assertEqual(
            q.submit("B", "x", a["conversation_id"])["error_code"], "conversation_agent_mismatch"
        )
        self.assertEqual(
            q.submit("A", "x", conversation="new:different", idem="key")["error_code"],
            "idempotency_conflict",
        )
        await q.worker
        q.db.execute(
            "UPDATE jobs SET request_conversation=NULL, fingerprint=? WHERE id=?",
            ("legacy", a["job_id"]),
        )
        q.db.commit()
        self.assertEqual(q.submit("A", "x", idem="key")["job_id"], a["job_id"])
        q.db.close()

    async def backend_case(self, frames, close=False, limit="1"):
        sent = []

        class FakeWS:
            async def send(self, raw):
                turn = json.loads(raw)
                sent.append(turn)
                if turn.get("type") == "user_turn":
                    # B8 echoes the actual wire input, including trusted identity.
                    for frame in frames:
                        for message in frame.get("data", {}).get("messages", []):
                            if message.get("type") == "human" and message.get("content") == "hello":
                                message["content"] = turn["text"]

            async def close(self):
                pass

            async def recv(self):
                if frames:
                    return json.dumps(frames.pop(0))
                if close:
                    raise websockets.exceptions.ConnectionClosed(None, None)
                await asyncio.sleep(5)

        async def connect(*args, **kwargs):
            return FakeWS()

        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict(
                os.environ,
                {
                    "OCTOP_GATEWAY_KEY": "x" * 40,
                    "OCTOP_GATEWAY_AGENTS": "A",
                    "OCTOP_GATEWAY_DB": td + "/tasks.db",
                    "OCTOP_GATEWAY_FILES": td + "/files",
                    "OCTOP_LOCAL_TOKEN": "fake",
                    "OCTOP_TASK_TIMEOUT": limit,
                },
                clear=True,
            ),
            patch("server.websockets.connect", connect),
        ):
            app = create_app()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as c:
                h = {"Authorization": "Bearer " + "x" * 40}
                r = await c.post(
                    "/v1/tasks",
                    headers=h,
                    json={"agent_id": "A", "text": "hello", "conversation_id": "unsafe:conv"},
                )
                jid = r.json()["job_id"]
                for _ in range(80):
                    await asyncio.sleep(0.03)
                    b = (await c.get("/v1/tasks/" + jid, headers=h)).json()
                    if b["status"] not in ("queued", "running"):
                        break
        return b, sent

    async def test_real_session_key_and_done(self):
        b, sent = await self.backend_case(
            [
                {"type": "token", "text": "OK", "thread_id": "thr_test"},
                {"type": "done"},
                {"type": "turn_status", "thread_id": "thr_test", "active": False},
            ]
        )
        self.assertEqual(b["status"], "succeeded")
        k = sent[0]["session_key"].split(":")
        self.assertEqual(k[0:2], ["A", "dashboard"])
        self.assertEqual(k[3], "dm")
        self.assertTrue(k[2].startswith("external-"))
        self.assertEqual(sent[0]["conversation_mode"], "ask")

    async def test_partial_disconnect_fails_closed(self):
        b, _ = await self.backend_case([{"type": "token", "text": "partial"}], close=True)
        self.assertEqual(b["status"], "failed")
        self.assertEqual(b["error_code"], "backend_disconnected_before_done")
        self.assertIsNone(b["result"])
        self.assertFalse(b["retryable"])

    async def test_silence_absolute_timeout(self):
        b, _ = await self.backend_case([])
        self.assertEqual(b["error_code"], "backend_timeout")
        self.assertFalse(b["retryable"])

    async def test_error_categories(self):
        for message, code in [
            ("429 quota secret", "backend_capacity_limited"),
            ("already running secret", "backend_busy"),
            ("model_call_failed secret", "backend_model_failed"),
        ]:
            b, _ = await self.backend_case([{"type": "error", "message": message}])
            self.assertEqual(b["error_code"], code)
            self.assertFalse(b["retryable"])
            self.assertNotIn("secret", str(b))

    async def test_secret_error_not_exposed(self):
        b, _ = await self.backend_case([{"type": "error", "message": "secret-value credential"}])
        self.assertEqual(b["error_code"], "upstream_error")
        self.assertNotIn("secret-value", str(b))


if __name__ == "__main__":
    unittest.main(verbosity=2)
