import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from server import create_app
from starlette.testclient import TestClient


class Protocol(unittest.TestCase):
    def test_session_isolation(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {
                    "OCTOP_GATEWAY_FILES": tmp + "/files",
                    "OCTOP_GATEWAY_DB": tmp + "/jobs.db",
                    "OCTOP_GATEWAY_KEY": "test-only-" + "x" * 40,
                    "OCTOP_GATEWAY_AGENTS": "A",
                },
            ),
        ):

            async def backend(agent, text, conversation):
                return {"answer": "fake:" + text, "session_key": "external-gateway:" + conversation}

            with TestClient(create_app(backend), base_url="http://localhost") as c:
                headers = {
                    "Authorization": "Bearer " + os.environ["OCTOP_GATEWAY_KEY"],
                    "Accept": "application/json, text/event-stream",
                }

                def call(name, arguments):
                    response = c.post(
                        "/mcp",
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": name, "arguments": arguments},
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    result = response.json()["result"]
                    self.assertFalse(result.get("isError", False))
                    value = result["structuredContent"]
                    self.assertTrue(value["ok"])
                    return value

                def completed(task):
                    deadline = time.monotonic() + 2
                    while task["status"] in ("queued", "running"):
                        self.assertLess(time.monotonic(), deadline, "fake backend did not finish")
                        time.sleep(0.01)
                        task = call("get_task", {"job_id": task["job_id"]})
                    self.assertEqual(task["status"], "succeeded")
                    return task

                first = call("submit_task", {"agent_id": "A", "text": "first"})
                second = call("submit_task", {"agent_id": "A", "text": "second"})
                self.assertTrue(first["conversation_id"])
                self.assertTrue(second["conversation_id"])
                self.assertNotEqual(first["conversation_id"], second["conversation_id"])
                first = completed(first)
                second = completed(second)
                self.assertNotEqual(first["result"]["session_key"], second["result"]["session_key"])
                continuation = completed(
                    call(
                        "submit_task",
                        {
                            "agent_id": "A",
                            "text": "continue",
                            "conversation_id": first["conversation_id"],
                        },
                    )
                )
                self.assertEqual(first["conversation_id"], continuation["conversation_id"])
                self.assertEqual(
                    first["result"]["session_key"], continuation["result"]["session_key"]
                )

    def test_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OCTOP_GATEWAY_FILES"] = tmp + "/files"
            os.environ["OCTOP_GATEWAY_DB"] = tmp + "/jobs.db"
            os.environ["OCTOP_GATEWAY_KEY"] = "test-only-" + "x" * 40
            os.environ["OCTOP_GATEWAY_AGENTS"] = "A"

            async def backend(a, t, c):
                await asyncio.sleep(0.02)
                return {"answer": "fake:" + t}

            with TestClient(create_app(backend), base_url="http://localhost") as c:
                self.assertEqual(c.post("/mcp", json={}).status_code, 401)
                h = {
                    "Authorization": "Bearer " + os.environ["OCTOP_GATEWAY_KEY"],
                    "Accept": "application/json, text/event-stream",
                }

                def rpc(method, params):
                    r = c.post(
                        "/mcp",
                        headers=h,
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    )
                    self.assertEqual(r.status_code, 200)
                    return r.json()["result"]

                rpc(
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "regression", "version": "1"},
                    },
                )
                tools = rpc("tools/list", {})["tools"]
                self.assertEqual(len(tools), 10)
                self.assertIn("submit_feedback", [t["name"] for t in tools])
                self.assertTrue(all("outputSchema" in t for t in tools))

                def call(name, args):
                    r = rpc("tools/call", {"name": name, "arguments": args})
                    self.assertIn("structuredContent", r)
                    return r["structuredContent"]

                a = call(
                    "submit_task", {"agent_id": "A", "text": "first", "idempotency_key": "one"}
                )
                b = call("submit_task", {"agent_id": "A", "text": "second"})
                self.assertIn(b["status"], ("queued", "running"))
                time.sleep(0.1)
                self.assertEqual(call("get_task", {"job_id": a["job_id"]})["status"], "succeeded")
                self.assertEqual(
                    call(
                        "submit_task", {"agent_id": "A", "text": "first", "idempotency_key": "one"}
                    )["job_id"],
                    a["job_id"],
                )
                answer = call("ask_expert", {"agent_id": "A", "text": "simple", "wait_seconds": 1})
                self.assertEqual(answer["status"], "succeeded")
                self.assertEqual(answer["result"]["answer"], "fake:simple")
                r = c.post("/v1/tasks", headers=h, json={"agent_id": "A", "text": "rest"})
                self.assertEqual(r.status_code, 202)


if __name__ == "__main__":
    unittest.main(verbosity=2)
