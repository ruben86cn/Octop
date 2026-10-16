"""Operator-only local token management; raw tokens are displayed once."""

import argparse
import json
import os
from pathlib import Path

from authorization import Registry
from runtime_paths import data_dir


def main():
    root = data_dir()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(root / "authorization.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue")
    issue.add_argument("--name", required=True)
    issue.add_argument("--agents", required=True)
    issue.add_argument("--feedback", action="store_true")
    issue.add_argument("--profile", type=Path, help="Administrator-defined identity profile JSON")
    issue.add_argument("--expires", type=float, help="Unix timestamp")
    sub.add_parser("list")
    identity = sub.add_parser("identity")
    identity.add_argument("token_id")
    identity.add_argument("--profile", type=Path, required=True)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("token_id")
    update = sub.add_parser("update")
    update.add_argument("token_id")
    update.add_argument("--agents", required=True)
    update.add_argument("--feedback", action="store_true")
    feedback = sub.add_parser("feedback-agent")
    feedback.add_argument("agent_id")
    args = parser.parse_args()
    allowed = os.environ.get("OCTOP_GATEWAY_AGENTS")
    if allowed is None:
        allowed = json.loads((root / "daemon-config.json").read_text())["agents"]
    registry = Registry(args.db, set(filter(None, allowed.split(","))), "")
    if args.command == "issue":
        result = registry.issue(
            args.name,
            args.agents.split(","),
            args.feedback,
            args.expires,
            json.loads(args.profile.read_text()) if args.profile else None,
        )
    elif args.command == "identity":
        registry.update_profile(args.token_id, json.loads(args.profile.read_text()))
        result = {"updated": True, "token_id": args.token_id}
    elif args.command == "list":
        result = registry.listing()
    elif args.command == "revoke":
        result = {"revoked": registry.revoke(args.token_id)}
    elif args.command == "update":
        agents = set(filter(None, args.agents.split(",")))
        if not agents.issubset(registry.allowed):
            parser.error("agent_not_in_gateway_allowlist")
        c = registry.db.execute(
            "UPDATE tokens SET agents=?,feedback=? WHERE id=? AND revoked=0",
            (json.dumps(sorted(agents)), int(args.feedback), args.token_id),
        )
        registry.db.commit()
        result = {"updated": c.rowcount == 1}
    else:
        registry.set_feedback_agent(args.agent_id)
        result = {"feedback_agent": args.agent_id}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
