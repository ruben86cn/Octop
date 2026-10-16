"""Deterministic executor: only exact, approved content changes; no model calls."""

import os
import re
import tempfile
from pathlib import Path

from feedback_store import digest, validate_plan


def content_path(root, agent, relative):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent):
        raise ValueError("invalid_agent_id")
    base = Path(root).resolve()
    path = base / agent
    if path.is_symlink() or not path.is_dir():
        raise ValueError("invalid_agent_workspace")
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlink_target_forbidden")
    if not path.resolve().is_relative_to(base / agent):
        raise ValueError("path_outside_workspace")
    return path


def read_content(path):
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("not_a_regular_file")
    if path.stat().st_size > 2000000:
        raise ValueError("existing_file_too_large")
    return path.read_bytes().decode("utf-8")


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".approved-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply_plan(store, fid, plan_hash, actor, agents_root, prompt_backend=None):
    plan = store.begin_apply(fid, plan_hash, actor)
    completed = []
    try:
        validate_plan(plan["operations"])
        agent = plan["target_agent_id"]
        snapshots = []
        for op in plan["operations"]:
            if op["kind"] == "file":
                before = read_content(content_path(agents_root, agent, op["path"]))
            else:
                if prompt_backend is None:
                    raise ValueError("administrator_backend_login_required")
                before = prompt_backend.get_prompt(agent)
            actual = None if before is None else digest(before)
            if actual != op["expected_sha256"]:
                raise ValueError("target_changed_since_review")
            snapshots.append({"operation": op, "before": before})
        store.snapshot(fid, actor, snapshots)
        # Recheck each target immediately before its write. There are no model
        # calls or commands embedded in plans; content is treated as data.
        for snapshot in snapshots:
            op = snapshot["operation"]
            if op["kind"] == "file":
                path = content_path(agents_root, agent, op["path"])
                current = read_content(path)
                if current != snapshot["before"]:
                    raise ValueError("target_changed_during_apply")
                atomic_write(path, op["content"])
                if read_content(path) != op["content"]:
                    raise ValueError("write_verification_failed")
            else:
                if prompt_backend.get_prompt(agent) != snapshot["before"]:
                    raise ValueError("target_changed_during_apply")
                prompt_backend.set_prompt(agent, op["content"])
                if prompt_backend.get_prompt(agent) != op["content"]:
                    raise ValueError("write_verification_failed")
            completed.append(
                {"kind": op["kind"], "path": op.get("path"), "sha256": digest(op["content"])}
            )
        detail = {"applied_operations": completed, "plan_hash": plan_hash}
        store.finish_apply(fid, actor, True, detail)
        return detail
    except Exception as exc:
        # A remote timeout can follow acceptance. Never automatically retry or
        # claim rollback; snapshots and the receipt allow explicit recovery.
        code = str(exc) if isinstance(exc, ValueError) else "execution_backend_or_filesystem_error"
        store.finish_apply(
            fid,
            actor,
            False,
            {"applied_operations": completed, "error_code": code, "requires_review": True},
        )
        raise
