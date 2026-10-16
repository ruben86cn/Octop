import asyncio
import contextlib
import tempfile
import unittest

from task_queue import Queue


class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + "/tasks.db"
        self.calls = []

        async def backend(agent, text, conv):
            self.calls.append((agent, text, conv))
            await asyncio.sleep(0.03)
            if text == "fail":
                raise RuntimeError()
            return {"answer": "answer:" + text}

        self.backend = backend
        self.q = Queue(self.path, backend, {"A", "B"})

    async def asyncTearDown(self):
        if self.q.worker and not self.q.worker.done():
            self.q.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.q.worker
        self.q.db.close()
        self.temp.cleanup()

    async def test_queue(self):
        a = self.q.submit("A", "one")
        b = self.q.submit("B", "two")
        self.assertEqual(b["status"], "queued")
        await self.q.worker
        self.assertEqual(self.q.view(a["job_id"])["status"], "succeeded")
        self.assertEqual(self.q.view(b["job_id"])["status"], "succeeded")
        self.assertEqual(len(self.calls), 2)

    async def test_idempotency(self):
        a = self.q.submit("A", "one", idem="key")
        b = self.q.submit("A", "one", idem="key")
        self.assertEqual(a["job_id"], b["job_id"])
        self.assertTrue(b.get("idempotent"))
        self.assertEqual(
            self.q.submit("A", "different", idem="key")["error_code"], "idempotency_conflict"
        )
        await self.q.worker
        self.assertEqual(len(self.calls), 1)

    async def test_conversation(self):
        a = self.q.submit("A", "one")
        await self.q.worker
        b = self.q.submit("A", "two", a["conversation_id"])
        await self.q.worker
        self.assertEqual(a["conversation_id"], b["conversation_id"])
        c = self.q.submit("B", "three", a["conversation_id"])
        self.assertEqual(c["error_code"], "conversation_agent_mismatch")

    async def test_restart(self):
        a = self.q.submit("A", "one")
        await self.q.worker
        q2 = Queue(self.path, self.backend, {"A", "B"})
        self.assertEqual(q2.view(a["job_id"])["status"], "succeeded")
        q2.db.close()

    async def test_interrupted(self):
        a = self.q.submit("A", "one")
        await asyncio.sleep(0.005)
        self.q.worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.q.worker
        self.assertEqual(self.q.view(a["job_id"])["status"], "interrupted")

    async def test_failure(self):
        a = self.q.submit("A", "fail")
        await self.q.worker
        self.assertEqual(self.q.view(a["job_id"])["status"], "failed")

    async def test_allowlist(self):
        self.assertFalse(self.q.submit("other", "hi")["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
