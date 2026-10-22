# Feishu multi-bot WebSocket isolation

## Failure and fix

With `octop-gateway 1.0.3` / `lark-oapi 1.7.3`, each Feishu channel thread
assigns the same SDK module-level `loop`. A reconnect on an older thread opens
its socket on that thread but schedules reception and frame handling on the
last assigned loop. Reception and then disconnect can fail with `attached to
a different loop`, leaving a live thread without a receiver.

Octop now selects `IsolatedFeishuChannel` through its own ChannelManager factory,
for both persistent channels and temporary credential probes. Other channel
kinds continue to use the upstream factory. No installed dependency is edited
and no global SDK class, loop or registry is replaced.

The adapter runs each client's connection, receiver, ping, frame handlers and
cleanup as owned tasks on the server's event loop. It rejects cross-loop reuse.
It retains SDK framing, event dispatch, acknowledgements and server-provided
reconnect policy. App-secret endpoint discovery uses cancellable HTTP with a
15-second timeout instead of the SDK's blocking, unbounded `requests.post`.

A receiver failure or cancellation is supervised and reconnects. Cleanup drains
receiver/frame tasks and closes the old socket before reconnecting; a close
exception cannot skip recovery. Socket close is bounded at five seconds.
PONG frames count as transport activity; lack of user messages is not a reason
to restart an idle bot. No received frames for `max(90s, 3 * PingInterval)` causes
recovery. A watchdog restarts terminated or stalled reconnect tasks. UI connection
status now checks the live receiver instead of treating initial registration
as a permanently healthy connection. Transport logs include the channel ID.

The adapter uses private SDK protocol helpers and is tested with lark-oapi 1.7.3;
rerun its regression suite before upgrading that dependency.

## Verification

`uv run pytest tests/unit/gateway/test_feishu_transport.py -q` covers:

- Two independent owner loops, with the SDK global overwritten, alternating
  reconnects three times per client; all recv/close operations stay on their owner.
- Receive and close exceptions, independent receiver cancellation, unaffected peer.
- Repeated connect calls release the lock; a socket without a receiver cannot
  report a successful reconnect.
- Clean channel shutdown with no surviving receiver/ping/frame tasks.
- Temporary credential probes do not disturb an existing connection.
- Idle PONG traffic preserves a connection; silent sockets reconnect.
- Real SDK group-mention event parsing/enqueue and ACK framing across reconnects.
- Endpoint configuration/error classification and watchdog recovery.

Nine transport tests passed. Nineteen transport/probe/runtime-status tests also
passed with the production dependency versions. `make all PYTEST_JOBS=4` passed:
5,045 passed, 34 skipped, plus Ruff and mypy (599 source files).

## Local candidate deployment (2026-10-22)

Candidate wheel and live evidence:
`~/.octop/releases/feishu-fix-20261022/`.
Rollback snapshot: `~/.octop/maintenance/backups/pre-feishu-fix-20261022/`
(`venv/`, stopped-service `data-cold/`, original launchd plist).
Only Octop's wheel was replaced; dependency versions, channel credentials,
group policies, prompts and knowledge content were not changed.

All five enabled Feishu channels connected. Each of the three reported bots was
reconnected twice through the normal API, then temporarily probed. All five
channels remained connected after each operation; no cross-loop errors appeared.
These are live transport checks, not a claim that a human group @ has completed
an end-to-end model response. Human group-message confirmation is recorded
separately in the deployment receipt when available.

For rollback, stop `system/octop`, preserve post-deployment data, reinstall the
previous `~/.octop/releases/68b829f/octop-1.0.2-py3-none-any.whl` with `--no-deps
--reinstall-package octop`, bootstrap `/Library/LaunchDaemons/octop.plist`, and
check health. This patch has no schema migration: do not restore the cold data
snapshot over newer conversations merely to revert the code.
