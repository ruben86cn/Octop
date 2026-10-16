"""Run locally after deploying the upgrade; creates and binds the feedback expert."""

import argparse
import getpass
import json
from pathlib import Path

import httpx
from authorization import Registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--middleware", type=Path, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--model", help="Optional installed Octop model identifier")
    args = parser.parse_args()
    root = args.middleware.resolve()
    config_path = root / "daemon-config.json"
    config = json.loads(config_path.read_text())
    allowed = set(config["agents"].split(","))
    spec = json.loads(Path(__file__).with_name("feedback-expert.json").read_text())
    if args.model:
        spec["default_model"] = args.model
    password = getpass.getpass("Octop administrator password (not stored): ")
    with httpx.Client(base_url="http://127.0.0.1:8088", timeout=60) as client:
        response = client.post(
            "/api/auth/login", json={"username": args.username, "password": password}
        )
        if response.status_code != 200:
            raise SystemExit("Login failed; no changes made. HTTP " + str(response.status_code))
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        registry = Registry(root / "authorization.sqlite3", allowed, "")
        agent = registry.feedback_agent()
        if agent:
            print(json.dumps({"feedback_agent": agent, "created": False}))
            return
        response = client.post("/api/agents", json=spec)
        if response.status_code != 201:
            raise SystemExit("Agent creation failed. HTTP " + str(response.status_code))
        row = response.json()
        agent = row.get("agent_id")
        if not isinstance(agent, str) or not agent:
            raise SystemExit("Backend response missing agent_id; inspect Octop before retrying.")
        # Preserve original settings before extending the gateway allowlist.
        backup = config_path.with_name("daemon-config.before-auth-upgrade.json")
        if not backup.exists():
            backup.write_text(config_path.read_text())
            backup.chmod(0o600)
        allowed.add(agent)
        config["agents"] = ",".join(sorted(allowed))
        temporary = config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(config_path)
        registry.allowed = frozenset(allowed)
        registry.set_feedback_agent(agent)
        print(
            json.dumps(
                {
                    "feedback_agent": agent,
                    "created": True,
                    "next": "Restart gateway supervisor after backup and idle check.",
                }
            )
        )


if __name__ == "__main__":
    main()
