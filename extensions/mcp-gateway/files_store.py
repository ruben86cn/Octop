"""Gateway-owned text attachments only; never reads arbitrary host files."""

import base64
import json
import sqlite3
import uuid
from pathlib import Path

from authorization import owner_id


class Files:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "index.sqlite3", check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, agent TEXT, filename TEXT, remote TEXT)"
        )
        self.db.commit()
        if "owner" not in [r[1] for r in self.db.execute("PRAGMA table_info(files)")]:
            self.db.execute("ALTER TABLE files ADD COLUMN owner TEXT NOT NULL DEFAULT 'legacy'")
            self.db.commit()

    def validate(self, filename, content):
        if (
            Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or not filename.lower().endswith((".txt", ".md", ".csv", ".json"))
            or len(filename) > 120
        ):
            raise ValueError("invalid_filename_or_type")
        data = base64.b64decode(content, validate=True)
        if len(data) > 24000:
            raise ValueError("file_too_large")
        data.decode("utf-8")
        return data

    def put(self, agent, filename, data, remote):
        fid = uuid.uuid4().hex
        (self.root / fid).write_bytes(data)
        self.db.execute(
            "INSERT INTO files (id,agent,filename,remote,owner) VALUES (?,?,?,?,?)",
            (fid, agent, filename, json.dumps(remote), owner_id()),
        )
        self.db.commit()
        return fid

    def get(self, fid, agent=None):
        r = self.db.execute(
            "SELECT agent,filename,remote FROM files WHERE id=? AND owner=?", (fid, owner_id())
        ).fetchone()
        if not r or (agent is not None and r[0] != agent):
            raise ValueError("file_not_found_or_wrong_expert")
        return {
            "agent_id": r[0],
            "filename": r[1],
            "remote": json.loads(r[2]),
            "data": (self.root / fid).read_bytes(),
        }
