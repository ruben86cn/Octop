"""Local token registry. Raw secrets are returned once, never persisted."""

import contextvars
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from identity_profile import validate_profile


@dataclass(frozen=True)
class Principal:
    id: str
    agents: frozenset
    feedback: bool = False
    owner_scope: bool = False
    profile: dict = field(default_factory=lambda: validate_profile({}))


current = contextvars.ContextVar("gateway_principal", default=None)


def owner_id():
    p = current.get()
    return p.id if p else "legacy"


class Registry:
    def __init__(self, path, allowed, legacy_key):
        self.allowed = frozenset(allowed)
        self.legacy_key = legacy_key
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        Path(path).chmod(0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS tokens (id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL, agents TEXT NOT NULL, feedback INTEGER NOT NULL, expires REAL, revoked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL)"
        )
        if "profile" not in [r[1] for r in self.db.execute("PRAGMA table_info(tokens)")]:
            self.db.execute("ALTER TABLE tokens ADD COLUMN profile TEXT NOT NULL DEFAULT '{}'")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS auth_audit (id INTEGER PRIMARY KEY,event TEXT,token_id TEXT,detail TEXT,created REAL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.commit()

    def principal(self, token_id):
        if token_id == "legacy":
            return Principal(
                "legacy",
                self.allowed,
                True,
                True,
                validate_profile(
                    {
                        "tier": "management",
                        "roles": ["网关所有者"],
                        "description": "原单一私人网关凭证的所有者，保留既有已批准权限。",
                    }
                ),
            )
        row = self.db.execute(
            "SELECT agents,feedback,expires,revoked,profile FROM tokens WHERE id=?", (token_id,)
        ).fetchone()
        if not row or row[3] or (row[2] is not None and row[2] <= time.time()):
            return None
        agents = frozenset(json.loads(row[0])) & self.allowed
        feedback_agent = self.feedback_agent()
        agents = agents - {feedback_agent}
        if row[1] and feedback_agent in self.allowed:
            agents = agents | {feedback_agent}
        return Principal(
            token_id, agents, bool(row[1]), False, validate_profile(json.loads(row[4]))
        )

    def authenticate(self, header):
        if self.legacy_key and hmac.compare_digest(header, ("Bearer " + self.legacy_key).encode()):
            return self.principal("legacy")
        if not header.startswith(b"Bearer "):
            return None
        digest = hashlib.sha256(header[7:]).hexdigest()
        row = self.db.execute("SELECT id FROM tokens WHERE digest=?", (digest,)).fetchone()
        return self.principal(row[0]) if row else None

    def issue(self, name, agents, feedback=False, expires=None, profile=None):
        if not isinstance(name, str) or not name.strip() or not set(agents).issubset(self.allowed):
            raise ValueError("invalid_name_or_agents")
        if expires is not None and expires <= time.time():
            raise ValueError("expiry_must_be_in_future")
        profile = validate_profile({} if profile is None else profile)
        raw = "octop_" + secrets.token_urlsafe(32)
        tid = secrets.token_hex(12)
        self.db.execute(
            "INSERT INTO tokens (id,name,digest,agents,feedback,expires,revoked,created,profile) VALUES (?,?,?,?,?,?,0,?,?)",
            (
                tid,
                name,
                hashlib.sha256(raw.encode()).hexdigest(),
                json.dumps(sorted(set(agents))),
                int(feedback),
                expires,
                time.time(),
                json.dumps(profile, ensure_ascii=False),
            ),
        )
        self.db.commit()
        return {
            "token_id": tid,
            "token": raw,
            "name": name,
            "agents": sorted(set(agents)),
            "feedback": feedback,
            "expires": expires,
            "profile": profile,
        }

    def revoke(self, tid):
        cursor = self.db.execute("UPDATE tokens SET revoked=1 WHERE id=?", (tid,))
        self.db.commit()
        return cursor.rowcount == 1

    def listing(self):
        return [
            dict(
                zip(
                    (
                        "token_id",
                        "name",
                        "agents",
                        "feedback",
                        "expires",
                        "revoked",
                        "created",
                        "profile",
                    ),
                    (
                        r[0],
                        r[1],
                        json.loads(r[2]),
                        bool(r[3]),
                        r[4],
                        bool(r[5]),
                        r[6],
                        json.loads(r[7]),
                    ),
                    strict=False,
                )
            )
            for r in self.db.execute(
                "SELECT id,name,agents,feedback,expires,revoked,created,profile FROM tokens ORDER BY created"
            )
        ]

    def update_token(self, token_id, name, agents, feedback, expires, profile):
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 120
            or not isinstance(agents, list)
            or any(not isinstance(a, str) for a in agents)
            or not set(agents).issubset(self.allowed)
        ):
            raise ValueError("invalid_name_or_agents")
        if not isinstance(feedback, bool):
            raise ValueError("invalid_feedback_flag")
        if expires is not None and (
            isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or expires <= time.time()
        ):
            raise ValueError("expiry_must_be_in_future")
        profile = validate_profile(profile)
        agents = sorted(set(agents) - {self.feedback_agent()})
        with self.db:
            row = self.db.execute(
                "UPDATE tokens SET name=?,agents=?,feedback=?,expires=?,profile=? WHERE id=? AND revoked=0",
                (
                    name.strip(),
                    json.dumps(agents),
                    int(feedback),
                    expires,
                    json.dumps(profile, ensure_ascii=False),
                    token_id,
                ),
            )
            if row.rowcount != 1:
                raise ValueError("active_token_not_found")
            self.db.execute(
                "INSERT INTO auth_audit (event,token_id,detail,created) VALUES (?,?,?,?)",
                (
                    "token_updated",
                    token_id,
                    json.dumps(
                        {
                            "name": name,
                            "agents": agents,
                            "feedback": feedback,
                            "expires": expires,
                            "profile": profile,
                        },
                        ensure_ascii=False,
                    ),
                    time.time(),
                ),
            )

    def update_profile(self, token_id, profile):
        profile = validate_profile(profile)
        with self.db:
            row = self.db.execute(
                "UPDATE tokens SET profile=? WHERE id=? AND revoked=0",
                (json.dumps(profile, ensure_ascii=False), token_id),
            )
            if row.rowcount != 1:
                raise ValueError("active_token_not_found")
            self.db.execute(
                "INSERT INTO auth_audit (event,token_id,detail,created) VALUES (?,?,?,?)",
                (
                    "identity_updated",
                    token_id,
                    json.dumps(profile, ensure_ascii=False),
                    time.time(),
                ),
            )

    def feedback_agent(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='feedback_agent' ").fetchone()
        return row[0] if row else None

    def set_feedback_agent(self, agent):
        if agent not in self.allowed:
            raise ValueError("agent_not_in_gateway_allowlist")
        self.db.execute("INSERT OR REPLACE INTO settings VALUES ('feedback_agent',?)", (agent,))
        self.db.commit()
