"""Optional operator-set scope for this single-owner authenticated gateway."""

import json


def load_scope(path, allowed_agents):
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "owner_authorized_internal"
        or not isinstance(value.get("authorization_reference"), str)
        or not value["authorization_reference"].strip()
        or not isinstance(value.get("agents"), list)
        or not set(value["agents"]).issubset(allowed_agents)
    ):
        raise RuntimeError("Invalid operator caller-scope configuration")
    return value


def question_with_scope(question, agent, scope):
    if scope is None or agent not in scope["agents"]:
        return question
    # This is loaded from a local operator-controlled file after the gateway's
    # single private credential check, never from request payload fields.
    return (
        "[管理员配置的接收者上下文]\n"
        "仅在现有资料权限内使用以下管理员声明，不扩大专家访问或修改权限。\n"
        + scope["authorization_reference"]
        + "\n[/管理员配置的接收者上下文]\n\n用户问题：\n"
        + question
    )
