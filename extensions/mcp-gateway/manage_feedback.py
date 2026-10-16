"""Trusted local administrator CLI for reviewing, approving and applying changes."""

import argparse
import getpass
import json
import sqlite3
from pathlib import Path

import httpx
from apply_changes import apply_plan
from feedback_store import FeedbackStore
from runtime_paths import data_dir


class PromptBackend:
    def __init__(self, username):
        self.client = httpx.Client(base_url="http://127.0.0.1:8088", timeout=30)
        response = self.client.post(
            "/api/auth/login",
            json={
                "username": username,
                "password": getpass.getpass("Octop password (not stored): "),
            },
        )
        if response.status_code != 200:
            raise ValueError("administrator_login_failed")
        self.client.headers["Authorization"] = "Bearer " + response.json()["access_token"]

    def get_prompt(self, agent):
        response = self.client.get("/api/agents/" + agent)
        response.raise_for_status()
        return response.json().get("system_prompt") or ""

    def set_prompt(self, agent, content):
        response = self.client.patch("/api/agents/" + agent, json={"system_prompt": content})
        response.raise_for_status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--middleware", type=Path, default=data_dir())
    parser.add_argument("--agents-root", type=Path, default=Path.home() / ".octop/data/agents")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--state")
    show = sub.add_parser("show")
    show.add_argument("feedback_id")
    plan = sub.add_parser("plan")
    plan.add_argument("feedback_id")
    plan.add_argument("--file", type=Path, required=True)
    approve = sub.add_parser("approve")
    approve.add_argument("feedback_id")
    approve.add_argument("--plan-hash", required=True)
    reject = sub.add_parser("reject")
    reject.add_argument("feedback_id")
    reject.add_argument("--reason", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("feedback_id")
    apply.add_argument("--plan-hash", required=True)
    apply.add_argument("--username", default="admin")
    args = parser.parse_args()
    root = args.middleware.resolve()
    actor = getpass.getuser()
    store = FeedbackStore(root / "feedback.sqlite3")
    jobs = sqlite3.connect("file:" + str(root / "tasks.sqlite3") + "?mode=ro", uri=True)
    for (fid,) in store.db.execute("SELECT id FROM feedback WHERE state='analyzing'").fetchall():
        store.refresh(fid, jobs)
    jobs.close()
    if args.command == "list":
        result = store.listing(state=args.state)
    elif args.command == "show":
        result = store.get(args.feedback_id, admin=True)
    elif args.command == "plan":
        result = {
            "plan_hash": store.set_plan(args.feedback_id, json.loads(args.file.read_text()), actor),
            "state": "pending_approval",
        }
    elif args.command == "approve":
        store.approve(args.feedback_id, args.plan_hash, actor)
        result = {"state": "approved", "plan_hash": args.plan_hash}
    elif args.command == "reject":
        store.reject(args.feedback_id, actor, args.reason)
        result = {"state": "rejected"}
    else:
        record = store.get(args.feedback_id, admin=True)
        if not record or record["state"] != "approved" or record["approved_hash"] != args.plan_hash:
            raise ValueError("plan_not_approved")
        backend = (
            PromptBackend(args.username)
            if any(op["kind"] == "system_prompt" for op in record["plan"]["operations"])
            else None
        )
        try:
            result = apply_plan(
                store, args.feedback_id, args.plan_hash, actor, args.agents_root, backend
            )
        finally:
            if backend:
                backend.client.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
