# Repository execution instructions

These instructions apply to every Agent working in this repository.

## Source of truth

Before taking implementation action, read these files completely:

1. `docs/superpowers/specs/2026-08-08-social-media-favorites-archiver-design.md`
2. `docs/superpowers/plans/2026-08-08-social-media-favorites-archiver-implementation.md`

Use the `executing-plans` skill and execute the implementation plan in numeric task order. If implementation evidence changes an architectural decision, update both documents and explain why.

## Progress protocol

- Keep the plan's status tracker current. Exactly one top-level task may be `in_progress`.
- Check each `- [ ]` item only after its stated evidence exists.
- Use test-driven development, run the task's focused and regression checks, and commit each completed top-level task.
- After every completed top-level task, post a progress update in the current conversation with:
  - task number and name;
  - files changed;
  - tests/checks and results;
  - commit hash;
  - blockers or user action, if any;
  - next task.
- After posting the update, continue immediately. Do not wait for approval between tasks.
- Pause only for login/QR/captcha/device confirmation, account/publication authorization, the plan's ClawHub MIT-0 notice, or a genuine blocker that cannot be resolved safely.
- Never mark a live platform/content type as passed without real end-to-end evidence. If a required type is missing, ask once for a representative item in a dedicated test collection.

## Safety and release rules

- Never print, log, commit, screenshot, or place in fixtures any secret value, Cookie, authorization header, signed private URL, browser profile data, or private media/content.
- `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` may already exist locally. Check presence only; do not reveal values. Cloud enrichment is optional and text-only.
- Live vaults, databases, browser state, downloads, and raw evidence belong under ignored temporary paths, never Git.
- Do not bypass platform verification or anti-bot controls.
- Do not publish or claim stable support until all preceding gates in the plan pass.
- Preserve unrelated user changes. Use small, intentional commits and keep the worktree auditable.
