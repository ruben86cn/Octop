# External MCP gateway: authorization, identity and feedback

This optional service runs alongside Octop **1.0.2b8**. It uses Octop's HTTP and
WebSocket APIs; it does not replace the dashboard chat, steering, memory storage,
Telegram filtering or other upstream features. The gateway admin interface offers
Chinese, English and Japanese via its language selector. Dashboard language is a
separate per-user preference.

## Start

Use Python 3.12+ and the repository's locked environment (`uv sync --frozen`), or
install `requirements.txt` in a separate virtual environment. From this directory:

```sh
# Store data outside the checkout. Restrict access to this directory.
export OCTOP_GATEWAY_DATA="$HOME/.octop/gateway"
export OCTOP_GATEWAY_AGENTS='EXPERT_A,EXPERT_B'
export OCTOP_GATEWAY_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export OCTOP_LOCAL_URL='http://127.0.0.1:8088'
export OCTOP_LOCAL_USERNAME='your-octop-admin'
# Supply OCTOP_LOCAL_PASSWORD from your service manager/secret store.
# OCTOP_LOCAL_TOKEN can instead supply an existing backend JWT.
python server.py
```

The server binds **127.0.0.1:8099**. `/mcp` is the MCP endpoint and `/admin/` is
the management UI. For remote use, place it behind an HTTPS reverse proxy and add
its exact hostname to `OCTOP_GATEWAY_HOSTS` (comma-separated). Preserve `Host` and
`Origin`; do not make the backend service public. A prefix such as `/mcp-api/` may
be stripped by the proxy before forwarding; the UI uses relative URLs.

`OCTOP_GATEWAY_KEY` is the legacy **owner** credential. Keep it private: it has the
whole configured allowlist and is not a restricted caller token. Never place it,
backend credentials, databases or customer files in Git. Use a single gateway
worker; its task scheduler is process-local.

Data defaults to `~/.octop/gateway`; `OCTOP_AUTH_DB`, `OCTOP_FEEDBACK_DB`,
`OCTOP_GATEWAY_DB`, `OCTOP_GATEWAY_FILES` and `OCTOP_CALLER_SCOPE` override individual
paths. `OCTOP_GATEWAY_PORT` overrides the port. `OCTOP_TASK_TIMEOUT` (default 300 s)
is the overall external task deadline, independent of model stream gap timeouts.
Set it above the model wait when long reasoning calls are expected.

## Tokens and identity

Sign in at `/admin/` with an existing Octop **administrator** account. Passwords
are validated by Octop, not stored by this gateway. Admin sessions expire in one
hour and on restart. Ordinary MCP tokens cannot administer the gateway.

Create a token, select allowed experts, then set its identity tier (`public`,
`partner`, `internal`, `management`), organization, department, roles, natural
language description and response guidance. The cryptographically random token
is shown once; only its digest is stored. Edit permissions/profile, set expiry,
or revoke it in the same UI. Changes apply to subsequent calls; queued work is
rechecked before dispatch. Already-dispatched Octop turns are not cancelled.

The authenticated identity is injected by the server. Natural-language role
claims in a question do not change code-enforced expert access. Model-based
response scope is not a substitute for storage/field-level protection of sensitive
information. Identity tiers do not grant approval rights.

Tasks, conversations, idempotency keys, files and feedback are isolated by token.
A token may only submit feedback about an expert it can access. The **Allow
feedback** switch is independent of normal expert selection. With it off, both
MCP feedback submission and direct calls to the feedback expert are rejected.

## Feedback expert and reviewed changes

Create an expert from `feedback-expert.json` through Octop, add its returned ID to
`OCTOP_GATEWAY_AGENTS`, restart the gateway, and bind it:

```sh
python manage_tokens.py feedback-agent FEEDBACK_EXPERT_ID
```

Alternatively `configure_feedback.py --middleware /path/to/gateway-data --username
ADMIN` creates/binds one using `daemon-config.json` containing an `agents` CSV.
It prompts for the administrator password without storing it. Restart with the
updated allowlist afterwards. It does not grant feedback access to existing tokens.

MCP tools `submit_feedback`, `get_feedback`, `list_feedback` expose only the
caller's feedback. The expert summarizes problems, distinguishes supplied
claims from verified evidence, identifies missing evidence, and proposes changes
and acceptance criteria. Successful analysis is **not** permission to edit.

State flow: `analyzing → pending_review → pending_approval → approved → applying
→ applied`. Failures and rejection remain in the audit trail. An administrator
uses the **trusted local OS account** to review and execute:

```sh
python manage_feedback.py list
python manage_feedback.py show FEEDBACK_ID
python manage_feedback.py plan FEEDBACK_ID --file /secure/changes.json
python manage_feedback.py approve FEEDBACK_ID --plan-hash EXACT_HASH
python manage_feedback.py apply FEEDBACK_ID --plan-hash EXACT_HASH
# or: python manage_feedback.py reject FEEDBACK_ID --reason 'Insufficient evidence'
```

Use `--middleware /legacy/data/path` before the subcommand for existing deployments.
`--agents-root` selects the local workspace parent. This executor is for **local
filesystem workspaces**; it does not infer or write remote/cloud workspace paths.
Plans contain an array of `{kind, path, expected_sha256, content}` file operations,
or `{kind: "system_prompt", expected_sha256, content}`. New files use a null
expected hash. File edits are limited to `AGENTS.md`, `MEMORY.md`, `IDENTITY.md`
and `.md/.txt/.json` under `knowledge/`; symlinks, escapes and executable files are
rejected. Prompt changes require a separate administrator API login.

Approval is bound to the exact plan hash. Editing the plan clears approval;
changed target content fails closed. Before-images and execution receipts are
persisted. Partial failure is **not** automatically retried or represented as a
rollback: review the receipt and snapshots before preparing a new plan. MCP and
web token management do not expose approval or execution endpoints.

## Existing deployment migration

1. Back up the current gateway data and code. Stop its supervisor and wait for
   active tasks; do not start two schedulers against the same task database.
2. Point the new service at the old database/file paths using the overrides above,
   or copy the complete data directory while stopped. Preserve credentials in
   the existing secret store. Token digests and feedback plans remain compatible.
3. Update the supervisor to run this `server.py` and supply environment variables.
   Machine-specific launchd, SSH tunnel and Keychain scripts are intentionally not
   published. Keep their existing configuration locally.
4. Verify login, token scope, feedback off/on, task ownership, attachments and an
   expert reply before switching traffic. Restore both code and database snapshots
   if rollback is needed; preserve any new records first.

## Tests

From the repository root:

```sh
uv run pytest -c extensions/mcp-gateway/pytest.ini extensions/mcp-gateway/tests -q
uv run ruff check extensions/mcp-gateway plugins/local-relay-compat
```

Tests use temporary storage and simulated backend responses. They cover token
revocation/expiry, queue revalidation, cross-token isolation, admin/CSRF checks,
feedback controls, exact approval, stale target rejection and final-answer framing.
