import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from authorization import Registry
from server import create_app


class AdminWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.temp.name
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
        self.reg = Registry(root + "/auth.db", {"A", "B", "R"}, "k" * 40)
        self.reg.set_feedback_agent("R")

        async def native(web, method, path, token=None, body=None):
            if path == "/api/auth/login":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "admin-session"
                        if body["password"] == "valid"
                        else "user-session"
                    },
                )
            if path == "/api/auth/me":
                return httpx.Response(
                    200,
                    json={
                        "username": "admin",
                        "role": "admin" if token == "admin-session" else "user",
                    },
                )
            return httpx.Response(
                200,
                json=[{"agent_id": a, "name": "Expert " + a} for a in ("A", "B", "R")],
                request=httpx.Request("GET", "http://localhost/"),
            )

        self.mock = patch("admin_web.AdminWeb.backend", native)
        self.mock.start()

        async def fake(*args):
            return {"answer": "ok"}

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(fake)),
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.mock.stop()
        self.env.stop()
        self.reg.db.close()
        self.temp.cleanup()

    async def login(self):
        response = await self.client.post(
            "/admin/api/login", json={"username": "admin", "password": "valid"}
        )
        self.assertEqual(response.status_code, 200)
        self.client.headers["X-CSRF-Token"] = response.json()["csrf"]
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        return response

    def body(self, feedback=False):
        return {
            "name": "合作方",
            "agents": ["A"],
            "feedback": feedback,
            "expires": None,
            "profile": {
                "tier": "partner",
                "description": "专业背景，但不知悉商业条款。",
                "roles": ["研究员"],
            },
        }

    async def test_admin_login_required_not_mcp_token(self):
        r = await self.client.get("/admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Content-Security-Policy", r.headers)
        self.assertEqual(
            (
                await self.client.get(
                    "/admin/api/tokens", headers={"Authorization": "Bearer " + "k" * 40}
                )
            ).status_code,
            401,
        )
        self.assertEqual(
            (
                await self.client.post(
                    "/admin/api/login", json={"username": "user", "password": "wrong"}
                )
            ).status_code,
            403,
        )

    async def test_create_edit_and_revoke(self):
        await self.login()
        r = await self.client.post("/admin/api/tokens", json=self.body(True))
        self.assertEqual(r.status_code, 201)
        issued = r.json()
        self.assertTrue(issued["token"].startswith("octop_"))
        p = self.reg.authenticate(("Bearer " + issued["token"]).encode())
        self.assertEqual(p.agents, {"A", "R"})
        self.assertEqual(p.profile["tier"], "partner")
        listed = (await self.client.get("/admin/api/tokens")).json()
        self.assertNotIn(issued["token"], json.dumps(listed))
        body = self.body(False)
        body["profile"]["description"] = "修改后的身份"
        r = await self.client.patch("/admin/api/tokens/" + issued["token_id"], json=body)
        self.assertEqual(r.status_code, 200)
        p = self.reg.principal(issued["token_id"])
        self.assertEqual(p.agents, {"A"})
        self.assertFalse(p.feedback)
        h = {"Authorization": "Bearer " + issued["token"]}
        self.assertEqual(
            (
                await self.client.post("/v1/tasks", headers=h, json={"agent_id": "R", "text": "hi"})
            ).json()["error_code"],
            "agent_not_allowed",
        )
        self.assertEqual(
            (
                await self.client.post(
                    "/v1/feedback", headers=h, json={"target_agent_id": "A", "issue": "hi"}
                )
            ).json()["error_code"],
            "feedback_not_allowed",
        )
        await self.client.delete("/admin/api/tokens/" + issued["token_id"])
        self.assertIsNone(self.reg.principal(issued["token_id"]))

    async def test_csrf_origin_and_validation(self):
        await self.login()
        self.assertEqual(
            (
                await self.client.post(
                    "/admin/api/tokens", json=self.body(), headers={"Origin": "http://evil.invalid"}
                )
            ).status_code,
            403,
        )
        self.assertEqual(
            (
                await self.client.post(
                    "/admin/api/tokens", json=self.body(), headers={"X-CSRF-Token": "wrong"}
                )
            ).status_code,
            403,
        )
        for body in (
            {**self.body(), "agents": ["R"]},
            {**self.body(), "feedback": "false"},
            {**self.body(), "expires": -1},
        ):
            self.assertEqual(
                (await self.client.post("/admin/api/tokens", json=body)).status_code, 400
            )

    async def test_feedback_not_bypassable_via_stored_allowlist(self):
        token = self.reg.issue("bad legacy config", ["A", "R"], False)
        self.assertNotIn("R", self.reg.principal(token["token_id"]).agents)

    async def test_directory_excludes_feedback_and_logout(self):
        await self.login()
        body = (await self.client.get("/admin/api/experts")).json()
        self.assertEqual([e["agent_id"] for e in body["experts"]], ["A", "B"])
        self.assertEqual(body["feedback_agent_id"], "R")
        await self.client.post("/admin/api/logout", json={})
        self.assertEqual((await self.client.get("/admin/api/tokens")).status_code, 401)
