# Fork customizations on 1.0.3b1

Original base: `f79015b078c4d5d4c309a1bf6f3cd39ac06cc18f` (official main, B8 plus
knowledge-work expert templates, PR #2021). Updated by merging official `main`
`dd9dce4` (v1.0.2) on 2026-10-19. Both the original customization commit and
the official release remain ancestors. The 2026-10-26 merge adds official main
`890111b` (v1.0.3b1); see the verification record below.

## Contents

- [External MCP gateway](../extensions/mcp-gateway/README.md): random scoped tokens,
  identity injection, web administration, independent feedback access, durable
  review queue and exact administrator-approved execution.
- [Optional local relay plugin](../plugins/local-relay-compat/README.md): restores
  the long conversational stream wait without editing installed dependencies;
  preserves explicit Responses/native tool search and B8 memory call settings.
- Japanese dashboard language alongside Chinese and English: browser detection,
  language selectors, initial setup, persisted user preference, Ant Design/dayjs,
  speech language and all current dashboard locale keys (including B8 steering,
  memory backup/restore, Telegram IDs and download links).
- Gateway administration also offers Chinese, English and Japanese. Its UI language
  does not change a token's identity or permissions.

## Localization scope and attribution

Japanese dashboard foundations and existing translations are adapted from upstream
[PR #1747](https://github.com/TencentCloud/Octop/pull/1747), commits `542870b` and
`1ae4158`. Applied as a three-way patch to the B8-based branch, then supplemented
with 323 newer strings and key/interpolation parity checks. Upstream code remains
under the repository license. The PR itself has not been merged upstream.

This is dashboard localization, not automatic translation of user knowledge,
expert prompts, model responses, plugin-provided content or server/CLI/IM prose.
The existing server zh/en locale contract is preserved: stored `ja` is retained
for the UI, and server-generated text falls back to English. Japanese bundled
UI tool labels are not overwritten by English hydration. Some server-owned
catalog descriptions (for example MBTI profiles) therefore remain English.

## Validation and rollout

Use `make all PYTEST_JOBS=4`, `make build-frontend`, frontend locale tests and the
gateway tests documented above. Test the gateway and dashboard in an isolated
environment before deployment. This branch does not modify a running installation,
automatically enable plugins, migrate production data or merge itself into main.

Maintain future official updates by merging upstream into this feature branch
and rerunning the checks. In particular, inspect upstream changes to locale
normalization, native chat frames and model middleware APIs before deployment.
Do not replace `src/octop` or a newer harness with copied old installations.

### Original B8 verification record

- Backend `make all PYTEST_JOBS=4`: passed (4,760 passed / 31 skipped at the
  full-suite checkpoint); the final setup-header regression was also checked
  with 126 locale/setup tests.
- Gateway suite: 75 passed. Extension Ruff check passed.
- Dashboard TypeScript/Vite production build passed; locale preference tests: 7 passed.
- Isolated Chromium smoke checks: Japanese dashboard login; gateway token
  creation, profile editing, feedback toggle, zh/en/ja roundtrip, revocation,
  desktop/mobile layout; no page JavaScript errors. No production data was used.
- Additional frontend-wide checks reproduce upstream baseline failures: Vitest
  has 6 failed files (6 failed tests plus 3 suite-loading failures) on both the
  untouched `f79015b0` baseline and this branch; ESLint has the same 3 errors and
  68 warnings. These unrelated failures were not hidden or changed here.

### 1.0.2 upstream merge (2026-10-19)

- Merged official `main` at `dd9dce4` (49 incoming commits) into
  `feature/mcp-authorization-ja-b8`; recovery branch:
  `backup/mcp-ja-before-upstream-20261019` at `93731c5`.
- Preserved the scoped MCP gateway, feedback approval workflow and optional
  relay plugin unchanged. The upstream agent MCP gateway remains a separate
  feature; it does not replace the external gateway or its authorization data.
- Updated 186 Japanese strings for upstream changes (decision models, manual
  memory, one-shot scheduling, navigation, exports and DingTalk). Locale keys
  and interpolation placeholders match the updated English bundle.
- Added `tests/integration/test_fork_gateway_chat.py`: actual Octop WebSocket
  routing with a simulated harness response, asserting final-answer selection
  and idle-turn confirmation for English and Japanese requests. Included in
  the custom gateway CI workflow.
- Custom backend regression selection: 109 passed. External gateway: 75 passed;
  extension/plugin Ruff check passed. Frontend locale preferences: 7 passed.
- TypeScript and Vite production build passed.
- Full frontend suite: 1,397 passed, 6 failed, with 3 additional suite-loading
  failures (6 failed files total). Unmodified official `dd9dce4` reproduces
  exactly the same failures (1,394 passed). ESLint likewise reports the same
  3 errors and 70 warnings in both trees. These upstream failures are outside
  this merge's scope.
- No running installation, production database, provider credentials or service
  configuration was changed. Real provider calls and deployment are not covered
  by these isolated checks.
- Full backend ship gate `make all PYTEST_JOBS=4` passed: Ruff, mypy (597
  source files), and 5,033 tests passed / 35 skipped. The two new gateway
  integration cases were additionally included in the 109-test custom run.

### 1.0.3b1 upstream merge (2026-10-26)

- Merged official `main` at `890111b` (35 incoming commits, beta release)
  into the custom branch, retaining `591e33c` and all prior custom history.
  Recovery branch: `backup/custom-before-upstream-20261026`.
- Preserved the external MCP gateway, feedback approvals, optional local relay,
  and Feishu per-client event-loop isolation without changing their code.
- Added 167 Japanese strings, updated 23 existing translations, and removed one
  retired key to match upstream resource sharing, user groups, model settings,
  storage access, and text assistance. Key and placeholder parity is checked.
- Updated development dependencies from the upstream lockfile, including
  octop-harness 1.0.5. This merge does not install into the running service or
  migrate production databases; deployment needs a separate runtime backup and
  dependency/migration check (upstream database schema is now 22).
- Custom regressions: 118 passed (Feishu isolation, relay compatibility, locale
  persistence/key parity, and external-gateway WebSocket answer contract).
  External MCP gateway: 75 passed; extension/plugin Ruff check passed.
- Frontend full suite: 1,452 passed, 8 failed tests, and 3 suite-loading failures
  (8 failed files). An untouched `890111b` checkout reproduces the exact same
  failure names (1,449 passed). ESLint in both trees reports the same 2 errors
  and 71 warnings. These upstream baseline failures remain outside this merge.
- Full backend `make all PYTEST_JOBS=4` passed: Ruff, mypy (613 source files),
  5,184 tests passed / 35 skipped. TypeScript and Vite production build passed.
