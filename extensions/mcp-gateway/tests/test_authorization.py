import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from authorization import Registry, current
from files_store import Files
from server import create_app
from starlette.testclient import TestClient


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.env = patch.dict(
            os.environ,
            {
                "OCTOP_GATEWAY_KEY": "k" * 40,
                "OCTOP_GATEWAY_AGENTS": "A,B,R",
                "OCTOP_AUTH_DB": root + "/auth.db",
                "OCTOP_GATEWAY_DB": root + "/jobs.db",
                "OCTOP_GATEWAY_FILES": root + "/files",
                "OCTOP_FEEDBACK_DB": root + "/feedback.db",
                "OCTOP_CALLER_SCOPE": root + "/absent",
            },
            clear=True,
        )
        self.env.start()
        self.registry = Registry(root + "/auth.db", {"A", "B", "R"}, "k" * 40)
        self.registry.set_feedback_agent("R")
        self.a = self.registry.issue("a", ["A"], True)
        self.b = self.registry.issue("b", ["A", "B"])
        self.calls = []

        async def backend(agent, text, conv):
            self.calls.append((agent, text, conv, current.get().id))
            await asyncio.sleep(0.01)
            return {"answer": "processed"}

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(backend)), base_url="http://localhost"
        )

    async def asyncTearDown(self):
        await asyncio.sleep(0.05)
        await self.client.aclose()
        self.registry.db.close()
        self.env.stop()
        self.tmp.cleanup()

    def headers(self, token):
        return {"Authorization": "Bearer " + token["token"]}

    async def submit(self, token, **kw):
        return (
            await self.client.post(
                "/v1/tasks",
                headers=self.headers(token),
                json={"agent_id": "A", "text": "hello", **kw},
            )
        ).json()

    async def test_scope_and_ownership(self):
        self.assertEqual(
            (await self.submit(self.a, agent_id="B"))["error_code"], "agent_not_allowed"
        )
        a = await self.submit(self.a, conversation_id="same", idempotency_key="same")
        b = await self.submit(self.b, conversation_id="same", idempotency_key="same")
        self.assertNotEqual(a["job_id"], b["job_id"])
        self.assertEqual(
            (await self.submit(self.a, conversation_id="same", idempotency_key="same"))["job_id"],
            a["job_id"],
        )
        other = (
            await self.client.get("/v1/tasks/" + a["job_id"], headers=self.headers(self.b))
        ).json()
        self.assertEqual(other["error_code"], "job_not_found")
        await asyncio.sleep(0.06)
        self.assertEqual([r[3] for r in self.calls], [self.a["token_id"], self.b["token_id"]])

    async def test_revoke_expiry_and_queued_recheck(self):
        await self.submit(self.a)
        self.registry.revoke(self.a["token_id"])
        self.assertEqual(
            (
                await self.client.post("/v1/tasks", headers=self.headers(self.a), json={})
            ).status_code,
            401,
        )
        await asyncio.sleep(0.03)
        self.assertEqual(self.calls, [])
        self.registry.db.execute(
            "UPDATE tokens SET expires=? WHERE id=?", (time.time() - 1, self.b["token_id"])
        )
        self.registry.db.commit()
        self.assertEqual(
            (await self.client.get("/v1/experts", headers=self.headers(self.b))).status_code, 401
        )

    async def test_feedback(self):
        body = {
            "target_agent_id": "A",
            "issue": "回答遗漏条件",
            "evidence": "例子",
            "suggestion": "补充条件",
            "idempotency_key": "feedback1",
        }
        a = (await self.client.post("/v1/feedback", headers=self.headers(self.a), json=body)).json()
        self.assertTrue(a["ok"])
        self.assertEqual(a["agent_id"], "R")
        self.assertEqual(
            (
                await self.client.post("/v1/feedback", headers=self.headers(self.a), json=body)
            ).json()["job_id"],
            a["job_id"],
        )
        self.assertEqual(
            (
                await self.client.post("/v1/feedback", headers=self.headers(self.b), json=body)
            ).json()["error_code"],
            "feedback_not_allowed",
        )
        body["target_agent_id"] = "B"
        self.assertEqual(
            (
                await self.client.post("/v1/feedback", headers=self.headers(self.a), json=body)
            ).json()["error_code"],
            "agent_not_allowed",
        )
        await asyncio.sleep(0.03)
        self.assertIn(self.a["token_id"], self.calls[0][1])
        row = (
            await self.client.get("/v1/feedback/" + a["feedback_id"], headers=self.headers(self.a))
        ).json()
        self.assertEqual(row["result"]["state"], "pending_review")
        self.assertNotIn("plan", row["result"])
        row = (
            await self.client.get("/v1/feedback/" + a["feedback_id"], headers=self.headers(self.b))
        ).json()
        self.assertEqual(row["error_code"], "feedback_not_found")

    async def test_file_isolation(self):
        files = Files(self.tmp.name + "/files")
        ctx = current.set(self.registry.principal(self.a["token_id"]))
        fid = files.put("A", "test.txt", b"secret", {"workspace_path": "inbound/test.txt"})
        current.reset(ctx)
        ctx = current.set(self.registry.principal(self.b["token_id"]))
        with self.assertRaises(ValueError):
            files.get(fid)
        current.reset(ctx)
        files.db.close()

    async def test_directory_filtered_and_permission_update(self):
        class Directory:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, *args, **kwargs):
                return httpx.Response(
                    200, json=[{"agent_id": a, "name": a} for a in ("A", "B", "R")]
                )

        with patch("server.httpx.AsyncClient", return_value=Directory()):
            result = (await self.client.get("/v1/experts", headers=self.headers(self.a))).json()
        self.assertEqual(result["agents"], ["A", "R"])
        self.assertEqual([x["agent_id"] for x in result["experts"]], ["A", "R"])
        job = await self.submit(self.b, agent_id="B")
        await asyncio.sleep(0.03)
        self.registry.db.execute(
            "UPDATE tokens SET agents=? WHERE id=?", ("[]", self.b["token_id"])
        )
        self.registry.db.commit()
        result = (
            await self.client.get("/v1/tasks/" + job["job_id"], headers=self.headers(self.b))
        ).json()
        self.assertEqual(result["error_code"], "job_not_found")
        self.assertEqual((await self.submit(self.b))["error_code"], "agent_not_allowed")

    async def test_native_session_namespace_and_no_owner_scope(self):
        sent = []

        class WS:
            def __init__(self):
                self.frames = []

            async def send(self, raw):
                value = json.loads(raw)
                sent.append(value)
                self.frames = [{"type": "token", "text": "OK"}, {"type": "done"}]

            async def recv(self):
                return json.dumps(self.frames.pop(0))

            async def close(self):
                pass

        async def connect(*args, **kwargs):
            return WS()

        with (
            patch.dict(os.environ, {"OCTOP_LOCAL_TOKEN": "test"}),
            patch("server.websockets.connect", connect),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app()), base_url="http://localhost"
            ) as client:
                for token in (self.a, self.b):
                    r = await client.post(
                        "/v1/tasks",
                        headers=self.headers(token),
                        json={"agent_id": "A", "text": "hello", "conversation_id": "same"},
                    )
                    self.assertTrue(r.json()["ok"])
                    await asyncio.sleep(0.03)
        self.assertNotEqual(sent[0]["session_key"], sent[1]["session_key"])
        for turn in sent:
            self.assertEqual(turn["conversation_mode"], "ask")
            self.assertNotIn("个人网关既有授权上下文", turn["text"])
            self.assertIn("不继承", turn["text"])

    async def test_no_plain_tokens_on_disk(self):
        for row in self.registry.db.execute("SELECT digest FROM tokens"):
            self.assertEqual(len(row[0]), 64)
        self.assertNotIn(self.a["token"], json.dumps(self.registry.listing()))


class MCPAuthorizationTests(unittest.TestCase):
    def test_mcp_context(self):
        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict(
                os.environ,
                {
                    "OCTOP_GATEWAY_KEY": "k" * 40,
                    "OCTOP_GATEWAY_AGENTS": "A,B",
                    "OCTOP_AUTH_DB": td + "/auth.db",
                    "OCTOP_GATEWAY_DB": td + "/jobs.db",
                    "OCTOP_GATEWAY_FILES": td + "/files",
                    "OCTOP_FEEDBACK_DB": td + "/feedback.db",
                    "OCTOP_CALLER_SCOPE": td + "/absent",
                },
                clear=True,
            ),
        ):
            reg = Registry(td + "/auth.db", {"A", "B"}, "k" * 40)
            token = reg.issue("limited", ["A"])

            async def backend(*args):
                return {"answer": "ok"}

            with TestClient(create_app(backend), base_url="http://localhost") as c:
                h = {
                    "Authorization": "Bearer " + token["token"],
                    "Accept": "application/json, text/event-stream",
                }
                r = c.post(
                    "/mcp",
                    headers=h,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "submit_task",
                            "arguments": {"agent_id": "B", "text": "hi"},
                        },
                    },
                )
                self.assertEqual(r.status_code, 200)
                self.assertIn("agent_not_allowed", r.text)
                r = c.post(
                    "/mcp",
                    headers=h,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "submit_task",
                            "arguments": {"agent_id": "A", "text": "hi"},
                        },
                    },
                )
                self.assertIn("job_id", r.text)
