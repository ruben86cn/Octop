# Fork customizations on 1.0.2b8

Base: `f79015b078c4d5d4c309a1bf6f3cd39ac06cc18f` (official main, B8 plus
knowledge-work expert templates, PR #2021). This branch keeps B8 as an ancestor;
no older source tree or dependency downgrade replaces it.

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

### Local verification record

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
