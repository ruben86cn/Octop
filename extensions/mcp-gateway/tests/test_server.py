import asyncio
import os
import unittest
from unittest.mock import patch

import httpx
from server import create_app
from starlette.testclient import TestClient
from task_queue import Queue

HEAD = {"Authorization": "Bearer test-only-key-not-a-real-credential-" + "x" * 32}


async def fake(agent, text, jid):
    await asyncio.sleep(0.03)
    if text == "fail":
        raise RuntimeError("secret must never escape")
    return {"answer": "fake:" + text, "thread_id": "test"}


class SessionIsolation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def backend(agent, text, conversation):
            return {"answer": "fake:" + text, "session_key": "external-gateway:" + conversation}

        self.queue = Queue(":memory:", backend, {"A"})

    async def asyncTearDown(self):
        if self.queue.worker:
            await self.queue.worker
        self.queue.db.close()

    async def completed(self, task):
        self.assertTrue(task["ok"])
        await self.queue.worker
        result = self.queue.view(task["job_id"])
        self.assertEqual(result["status"], "succeeded")
        return result

    async def test_omitted_conversation_creates_distinct_ids(self):
        first = self.queue.submit("A", "first")
        second = self.queue.submit("A", "second")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(first["conversation_id"])
        self.assertTrue(second["conversation_id"])
        self.assertNotEqual(first["conversation_id"], second["conversation_id"])

    async def test_continuation_reuses_session_key(self):
        first = await self.completed(self.queue.submit("A", "first"))
        second = await self.completed(
            self.queue.submit("A", "second", conversation=first["conversation_id"])
        )
        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual(first["result"]["session_key"], second["result"]["session_key"])

    async def test_omitted_conversation_isolates_session_key(self):
        first = await self.completed(self.queue.submit("A", "first"))
        second = await self.completed(self.queue.submit("A", "second"))
        self.assertNotEqual(first["result"]["session_key"], second["result"]["session_key"])


class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(fake)), base_url="http://localhost"
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_auth(self):
        self.assertEqual((await self.client.get("/v1/experts")).status_code, 401)

    async def test_allowlist(self):
        r = await self.client.post(
            "/v1/tasks", headers=HEAD, json={"agent_id": "other", "text": "hi"}
        )
        self.assertEqual(r.status_code, 400)

    async def test_commands(self):
        for text in ["/delete all", "hi @other"]:
            self.assertEqual(
                (
                    await self.client.post(
                        "/v1/tasks", headers=HEAD, json={"agent_id": "A", "text": text}
                    )
                ).status_code,
                400,
            )

    async def test_success_capacity(self):
        r = await self.client.post("/v1/tasks", headers=HEAD, json={"agent_id": "A", "text": "hi"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(
            (
                await self.client.post(
                    "/v1/tasks", headers=HEAD, json={"agent_id": "A", "text": "hi"}
                )
            ).status_code,
            202,
        )
        await asyncio.sleep(0.06)
        result = (await self.client.get("/v1/tasks/" + r.json()["job_id"], headers=HEAD)).json()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["answer"], "fake:hi")

    async def test_failure(self):
        r = await self.client.post(
            "/v1/tasks", headers=HEAD, json={"agent_id": "A", "text": "fail"}
        )
        await asyncio.sleep(0.06)
        result = (await self.client.get("/v1/tasks/" + r.json()["job_id"], headers=HEAD)).json()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret", str(result))

    async def test_body_limit(self):
        self.assertEqual(
            (await self.client.post("/v1/tasks", headers=HEAD, content=b"x" * 70000)).status_code,
            413,
        )

    async def test_missing_backend(self):
        os.environ.pop("OCTOP_LOCAL_TOKEN", None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://localhost"
        ) as c:
            r = await c.post("/v1/tasks", headers=HEAD, json={"agent_id": "A", "text": "hi"})
            await asyncio.sleep(0.02)
            self.assertEqual(
                (await c.get("/v1/tasks/" + r.json()["job_id"], headers=HEAD)).json()["status"],
                "failed",
            )


class Protocol(unittest.TestCase):
    def test_mcp(self):
        with TestClient(create_app(fake), base_url="http://localhost") as c:
            head = dict(HEAD, Accept="application/json, text/event-stream")

            def rpc(method, params, ident=1):
                return c.post(
                    "/mcp",
                    headers=head,
                    json={"jsonrpc": "2.0", "id": ident, "method": method, "params": params},
                )

            r = rpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            )
            self.assertEqual(r.status_code, 200)
            self.assertIn("result", r.json())
            r = rpc("tools/list", {})
            self.assertEqual(
                {t["name"] for t in r.json()["result"]["tools"]},
                {
                    "list_experts",
                    "submit_task",
                    "get_task",
                    "ask_expert",
                    "upload_file",
                    "download_file",
                    "ask_with_files",
                    "submit_feedback",
                    "get_feedback",
                    "list_feedback",
                },
            )

            async def directory_get(*args, **kwargs):
                return httpx.Response(200, json=[{"agent_id": "A", "name": "Example"}])

            with patch("server.httpx.AsyncClient.get", directory_get):
                r = rpc("tools/call", {"name": "list_experts", "arguments": {}})
            self.assertFalse(r.json()["result"].get("isError", False))
            r = rpc(
                "tools/call", {"name": "submit_task", "arguments": {"agent_id": "A", "text": "hi"}}
            )
            self.assertFalse(r.json()["result"].get("isError", False))
            import json
            import time

            job = json.loads(r.json()["result"]["content"][0]["text"])["job_id"]
            time.sleep(0.1)
            r = rpc("tools/call", {"name": "get_task", "arguments": {"job_id": job}})
            self.assertIn("fake:hi", str(r.json()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
