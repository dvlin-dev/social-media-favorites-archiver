# Social Media Favorites Archiver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, validate, and publish an agent-discoverable open-source Skill that synchronizes a user's own Bilibili, Xiaohongshu/RedNote, and Douyin favorites into a local Markdown/Obsidian knowledge base with local ASR, OCR, and safe optional text enrichment.

**Architecture:** A Python 3.11+ CLI and orchestrator use platform adapters to perform a fast metadata enumeration and immediately render Markdown skeletons, then process durable background jobs for assets, transcription, OCR, timeline fusion, enrichment, indexing, and safe cleanup. SQLite records identity, collection membership, job leases, source fingerprints, assets, and derivations; a separately installable nested Skill invokes the CLI and carries the discovery metadata used by Skills.sh and ClawHub.

**Tech Stack:** Python 3.11+, `uv`, Typer, Pydantic, standard-library SQLite in WAL mode, Playwright over Chrome CDP, `yt-dlp`, FFmpeg, FunASR/SenseVoice, `mlx-whisper` or `whisper.cpp` on Apple Silicon, RapidOCR/ONNX Runtime, pytest, Ruff, mypy, GitHub Actions, optional OpenAI-compatible Responses/Chat API.

---

## 0. How to execute this plan

The detailed design is [../specs/2026-08-08-social-media-favorites-archiver-design.md](../specs/2026-08-08-social-media-favorites-archiver-design.md). This implementation plan is the operational source of truth. If implementation evidence requires an architectural change, update both documents in the same commit and explain the change in the progress report.

Execution rules:

- Work through top-level tasks in numeric order. Do not skip a release gate because later work appears runnable.
- At the start of a task, set only that task's tracker status to `in_progress`. At completion, set it to `completed`, check every satisfied box in that task, and set the next task to `in_progress` only when work actually begins.
- Use test-driven development: create or change the named test first, run it and observe the expected failure, implement the smallest correct behavior, then rerun the focused and regression suites.
- Commit after every completed top-level task. Never combine unrelated user changes into these commits.
- After each top-level task, post a concise progress message in the current Agent conversation containing: task number/name, files changed, commands and results, commit hash, blockers, and the next task. Immediately continue to the next task without waiting for approval.
- Pause only for a user-only login/QR/captcha/device-confirmation step, an account or publication authorization problem, the ClawHub MIT-0 notice in Task 22, or a real blocker that cannot be resolved safely.
- Never print, log, commit, screenshot, or include in fixtures any secret value, Cookie, authorization header, private media URL, browser profile data, or private item content. It is acceptable to report only whether an environment variable is present.
- Do not declare a platform/type verified unless a real end-to-end item was processed. If the user's favorites lack a required type, ask once for a representative item to be added to a dedicated test collection.
- Do not publish to GitHub releases, Skills.sh, or ClawHub until every preceding gate is green.

### Task status tracker

Update the `Status` cell as work progresses. Exactly one task may be `in_progress`.

| Task | Deliverable | Status |
|---:|---|---|
| 0 | Preflight and execution baseline | completed |
| 1 | Python project scaffold and CI | completed |
| 2 | Configuration and doctor command | completed |
| 3 | Domain model and SQLite schema | completed |
| 4 | Durable queue, leases, and state machine | completed |
| 5 | Markdown renderer, note protection, and indexes | completed |
| 6 | Asset safety, redaction, and cleanup barrier | completed |
| 7 | Browser session and adapter contract | completed |
| 8 | Bilibili adapter | pending |
| 9 | Local ASR backends | pending |
| 10 | OCR and adaptive keyframes | pending |
| 11 | ASR × OCR timeline fusion | pending |
| 12 | Two-stage sync, early-stop, and reconciliation | pending |
| 13 | Xiaohongshu adapter | pending |
| 14 | Douyin adapter | pending |
| 15 | Optional OpenAI-compatible enrichment | pending |
| 16 | CLI, scheduling, and run reports | pending |
| 17 | Automated hardening and sanitized fixtures | pending |
| 18 | Three-platform live end-to-end validation | pending |
| 19 | Skill packaging and trigger evaluations | pending |
| 20 | User documentation and GitHub 1.0 release | pending |
| 21 | Skills.sh installation and discovery | pending |
| 22 | ClawHub publication and verification | pending |
| 23 | Clean-room post-publication verification | pending |
| 24 | Final audit and handoff | pending |

### Release gates

| Gate | Required evidence | Blocks |
|---|---|---|
| G1 Core | Unit, contract, integration tests; lint and types green | Live testing |
| G2 Privacy | No secret leakage; paths contained; cleanup barrier proven | Live testing |
| G3 Bilibili | Subtitle and ASR fallback paths pass on real favorites | Stable claim |
| G4 Xiaohongshu | Text, multi-image, and video paths pass on real favorites | Stable claim |
| G5 Douyin | Gallery and burned-caption video paths pass on real favorites | Stable claim |
| G6 Idempotency | Rerun, moved note, damaged marker, incomplete enumeration, and multi-collection cases pass | 1.0 release |
| G7 Skill | Positive/negative trigger evals and isolated install pass | Registry publication |
| G8 Distribution | GitHub release, Skills.sh, and ClawHub installs all pass from clean directories | Completion |

### Target repository map

```text
social-media-favorites-archiver/
├── AGENTS.md
├── LICENSE
├── README.md
├── SECURITY.md
├── CONTRIBUTING.md
├── pyproject.toml
├── .github/workflows/ci.yml
├── src/social_media_favorites_archiver/
│   ├── cli.py
│   ├── config.py
│   ├── diagnostics.py
│   ├── models.py
│   ├── orchestrator.py
│   ├── queue.py
│   ├── adapters/{base,bilibili,xiaohongshu,douyin}.py
│   ├── browser/{session,interception}.py
│   ├── processors/{subtitles,asr,ocr,keyframes,fusion,terminology,enrichment}.py
│   ├── storage/{database,migrations,assets,markdown,indexes}.py
│   └── safety/{cleanup,redaction,paths}.py
├── skill/social-media-favorites-archiver/
│   ├── SKILL.md
│   ├── LICENSE
│   ├── .clawhubignore
│   └── references/{configuration,platform-bilibili,platform-xiaohongshu,platform-douyin,troubleshooting}.md
├── tests/{unit,contract,integration}/
├── tests/fixtures/sanitized/
├── evals/evals.json
└── docs/verification/
```

## Task 0: Preflight and execution baseline

**Files:**

- Modify: this plan
- Inspect: `AGENTS.md`
- Inspect: `docs/superpowers/specs/2026-08-08-social-media-favorites-archiver-design.md`
- Create: `docs/verification/preflight.md`

- [x] Read `AGENTS.md`, the complete detailed design, and this complete plan before changing source code.
- [x] Run `git status --short`, `git log -5 --oneline`, and `git remote -v`; record only non-sensitive repository facts in `docs/verification/preflight.md`.
- [x] Run `python3 --version`, `uv --version`, `ffmpeg -version`, `node --version`, `npx --version`, `yt-dlp --version`, and `uname -m`; record availability and versions, not full host diagnostics.
- [x] Check only presence booleans for `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL`. Never echo values. Record `present` or `absent` and note that enrichment remains optional.
- [x] Estimate free disk space and record a conservative default cache quota; do not scan unrelated user directories.
- [x] Verify that the GitHub remote belongs to the intended repository and that the current branch can be pushed without overwriting unrelated work.
- [x] Commit with `docs: record implementation preflight`.
- [x] Update the task tracker and report evidence, commit hash, and Task 1 as the next task.

## Task 1: Python project scaffold and CI

**Files:**

- Create: `pyproject.toml`
- Create: `src/social_media_favorites_archiver/__init__.py`
- Create: `src/social_media_favorites_archiver/cli.py`
- Create: `tests/unit/test_cli.py`
- Create: `.github/workflows/ci.yml`
- Create: `.gitignore`
- Create: `LICENSE`

- [x] Add a failing CLI test asserting `smfa --help` exits successfully and exposes `doctor`, `login`, `sync`, `status`, `retry`, and `cleanup`.
- [x] Configure Python `>=3.11`, the `src` package layout, `smfa = social_media_favorites_archiver.cli:app`, Ruff, mypy, pytest, and coverage in `pyproject.toml`.
- [x] Use a lean base dependency set: Typer, Pydantic, pydantic-settings, PyYAML, Playwright, `yt-dlp`, `httpx`, `filelock`, Pillow, ImageHash, and RapidFuzz. Put platform/model-heavy packages in named optional dependency groups.
- [x] Implement the minimal Typer application required to make the CLI test pass without pretending any sync behavior exists.
- [x] Ignore `.env`, browser profiles, cookies, SQLite files, caches, generated vaults, model files, coverage output, and `work/`.
- [x] Add the root MIT license for application source code.
- [x] Add a GitHub Actions matrix for `ubuntu-latest` and `macos-14` with Python 3.11 and 3.12; CI must run `ruff check .`, `mypy src`, and `pytest`, and must not require live logins or model downloads.
- [x] Run `uv sync --group dev`, then `uv run ruff check .`, `uv run mypy src`, and `uv run pytest tests/unit/test_cli.py -q`.
- [x] Commit with `build: scaffold Python CLI and CI`.
- [x] Update the tracker and report results before continuing to Task 2.

## Task 2: Configuration and doctor command

**Files:**

- Create: `src/social_media_favorites_archiver/config.py`
- Create: `src/social_media_favorites_archiver/diagnostics.py`
- Modify: `src/social_media_favorites_archiver/cli.py`
- Create: `tests/unit/test_config.py`
- Create: `tests/unit/test_diagnostics.py`
- Create: `skill/social-media-favorites-archiver/references/configuration.md`

- [x] Write failing tests for config precedence (`CLI > environment > config file > defaults`), path expansion, cache quota validation, model-backend selection, and safe missing optional LLM settings.
- [x] Define typed settings for vault, state DB, cache, browser CDP/profile, enabled platforms, concurrency, retries, early-stop threshold, cleanup policy, ASR/OCR backends, terminology dictionary, and optional enrichment.
- [x] Use project-prefixed environment variables for application settings while reading the existing `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` only in the enrichment provider.
- [x] Write failing doctor tests that assert secret values are never present in terminal output or structured diagnostics.
- [x] Implement `smfa doctor [--json]` checks for Python, FFmpeg, browser/CDP readiness, yt-dlp, writable configured directories, DB schema compatibility, selected ASR/OCR backends, disk quota, and optional enrichment presence booleans.
- [x] Document a safe sample config and environment-variable reference without real tokens, cookies, or private paths.
- [x] Run `uv run pytest tests/unit/test_config.py tests/unit/test_diagnostics.py -q` plus lint and types.
- [x] Commit with `feat: add typed configuration and safe diagnostics`.
- [x] Update the tracker and report results before continuing to Task 3.

## Task 3: Domain model and SQLite schema

**Files:**

- Create: `src/social_media_favorites_archiver/models.py`
- Create: `src/social_media_favorites_archiver/storage/database.py`
- Create: `src/social_media_favorites_archiver/storage/migrations.py`
- Create: `tests/unit/test_models.py`
- Create: `tests/integration/test_database.py`

- [x] Write failing tests for canonical platform IDs, collections, normalized items, ordered assets, text segments, extraction records, and per-collection membership state.
- [x] Define Pydantic models that preserve source timestamps, author, original URL, ordered content blocks, media type, content availability, and adapter provenance without leaking raw private responses into Markdown.
- [x] Write failing migration tests for a fresh DB, repeat migration, WAL mode, foreign keys, and upgrade from the immediately previous schema version.
- [x] Implement a standard-library `sqlite3` repository and numbered transactional migrations for: `items`, `collections`, `item_collections`, `assets`, `jobs`, `runs`, `extractions`, `enrichments`, and `schema_migrations`.
- [x] Store `source_revision`/lightweight metadata fingerprint separately from downloaded asset SHA-256. Never use a post-transcription content hash to decide whether media needs downloading.
- [x] Represent removal on `item_collections`; derive item-level removal only when no active membership remains.
- [x] Add unique constraints that enforce canonical identity and idempotent jobs without preventing one item from belonging to multiple collections.
- [x] Run focused tests, then the full test suite, lint, and types.
- [x] Commit with `feat: add normalized models and SQLite state`.
- [x] Update the tracker and report results before continuing to Task 4.

## Task 4: Durable queue, leases, and state machine

**Files:**

- Create: `src/social_media_favorites_archiver/queue.py`
- Create: `tests/unit/test_queue.py`
- Create: `tests/integration/test_queue_recovery.py`
- Modify: `src/social_media_favorites_archiver/storage/database.py`

- [x] Write failing tests for legal transitions among `pending`, `running`, `succeeded`, `retryable`, `needs_auth`, `blocked`, and `failed`.
- [x] Write failing concurrency tests proving only one worker can lease a job/item, an expired lease is recoverable, heartbeat extends an active lease, and retry uses capped exponential backoff with jitter.
- [x] Implement atomic SQLite lease acquisition using `lease_owner`, `lease_until`, attempt count, next-attempt time, and last sanitized diagnostic code.
- [x] Make each processing stage independently resumable and idempotent; a worker crash must not force a complete item restart.
- [x] Keep platform-wide `needs_auth` from blocking unrelated platforms.
- [x] Add a per-canonical-item file lock around filesystem rendering and cleanup as a second boundary beyond DB leasing.
- [x] Run recovery and concurrency tests repeatedly with `for i in {1..10}; do uv run pytest tests/unit/test_queue.py tests/integration/test_queue_recovery.py -q || break; done` and require all ten runs to pass.
- [x] Commit with `feat: add recoverable job queue and leases`.
- [x] Update the tracker and report results before continuing to Task 5.

## Task 5: Markdown renderer, note protection, and indexes

**Files:**

- Create: `src/social_media_favorites_archiver/storage/markdown.py`
- Create: `src/social_media_favorites_archiver/storage/indexes.py`
- Create: `tests/unit/test_markdown.py`
- Create: `tests/integration/test_note_resync.py`

- [x] Write golden tests for the metadata skeleton and completed note, including stable `smfa_id`, platform ID, source URL, author, collections, favorite state, processing status, dates, ordered images, inline per-image OCR, transcript segments, and provenance.
- [x] Define generated-region markers and write failing tests proving user text outside the generated region survives resync.
- [x] Locate existing notes by scanning/indexing frontmatter `smfa_id`, not only by remembered path, so user moves and renames do not create duplicates.
- [x] If markers are missing or malformed, create a conflict diagnostic and preserve the file unchanged; never append a second generated block.
- [x] Merge frontmatter while preserving user-defined fields and user-added tags; own only documented `smfa_*` and generated fields.
- [x] Write Markdown atomically through a temporary file plus `fsync`/rename where supported.
- [x] Generate deterministic author, collection, tag, and topic/MOC index notes using Obsidian links without requiring Obsidian-specific storage.
- [x] Generate timestamp deep links only where the source platform has a verified stable time parameter; for Bilibili use the verified source form and otherwise show plain timestamps.
- [x] Run golden, move/rename, marker-corruption, and frontmatter-merge tests.
- [x] Commit with `feat: render protected Markdown notes and indexes`.
- [x] Update the tracker and report results before continuing to Task 6.

## Task 6: Asset safety, redaction, and cleanup barrier

**Files:**

- Create: `src/social_media_favorites_archiver/storage/assets.py`
- Create: `src/social_media_favorites_archiver/safety/paths.py`
- Create: `src/social_media_favorites_archiver/safety/redaction.py`
- Create: `src/social_media_favorites_archiver/safety/cleanup.py`
- Create: `tests/unit/test_paths.py`
- Create: `tests/unit/test_redaction.py`
- Create: `tests/integration/test_cleanup_barrier.py`

- [x] Write failing path tests for traversal, symlink escape, invalid filenames, collisions, excessive length, and cross-item asset ownership.
- [x] Implement canonical path containment, slug plus stable-ID filenames, atomic streaming downloads, allowed MIME checks, size limits, and SHA-256 verification.
- [x] Write redaction tests containing fake cookies, bearer tokens, query signatures, URLs, and OpenAI keys; assert logs and diagnostic bundles contain only redacted forms.
- [x] Implement structured logging that never serializes raw browser responses or environment values by default.
- [x] Write cleanup barrier tests proving the temporary video is retained until transcript/subtitles, required keyframes, OCR, fusion, Markdown render, assets, and file verification all succeed.
- [x] Make cleanup idempotent, auditable, path-contained, and limited to explicit item-owned temporary files. Never recursively delete a broad directory.
- [x] Add configurable cache quota and low-disk behavior that pauses heavy jobs safely.
- [x] Run all safety tests and inspect test logs for fake secret leakage.
- [x] Commit with `feat: secure assets logs and media cleanup`.
- [x] Update the tracker and report results before continuing to Task 7.

## Task 7: Browser session and adapter contract

**Files:**

- Create: `src/social_media_favorites_archiver/adapters/base.py`
- Create: `src/social_media_favorites_archiver/browser/session.py`
- Create: `src/social_media_favorites_archiver/browser/interception.py`
- Create: `tests/contract/test_adapter_contract.py`
- Create: `tests/unit/test_browser_session.py`

- [x] Define the adapter contract: `check_session`, `begin_login`, `list_collections`, `list_favorites`, `fetch_item`, `download_assets`, and `diagnose`.
- [x] Create a reusable contract suite for cursor progression, stable IDs, ordered assets, session expiry, sanitized diagnostics, retries, and completeness signals.
- [x] Implement Playwright connection to an explicitly configured real Chrome CDP endpoint/profile; do not copy the user's default browser database or store passwords.
- [x] Support page-driven navigation, XHR/fetch response interception, and requests executed in the authenticated page context.
- [x] Explicitly forbid Python-side reimplementation of Xiaohongshu `x-s` or Douyin `a_bogus`; `httpx` may download only adapter-obtained static assets under safety controls.
- [x] On QR, captcha, or device confirmation, checkpoint state and return a user-action instruction without bypass attempts.
- [x] Test with a local mock page/server only; no live platform dependency in CI.
- [x] Commit with `feat: add authenticated browser and adapter contract`.
- [x] Update the tracker and report results before continuing to Task 8.

## Task 8: Bilibili adapter

**Files:**

- Create: `src/social_media_favorites_archiver/adapters/bilibili.py`
- Create: `src/social_media_favorites_archiver/processors/subtitles.py`
- Create: `tests/contract/test_bilibili_adapter.py`
- Create: `tests/integration/test_bilibili_fixture.py`
- Create: `skill/social-media-favorites-archiver/references/platform-bilibili.md`

- [ ] Record a sanitized, minimal fixture from an authorized test item or construct an equivalent contract fixture without private cookies, signed media URLs, or copyrighted media bytes.
- [ ] Write failing adapter tests for collection discovery, favorite enumeration, canonical BV/AV identity, multi-part entries, unavailable/private items, and completeness flags.
- [ ] Wrap current `yt-dlp` Bilibili favorites/item extractors and browser-cookie capability. Do not implement WBI signing, playback URL negotiation, or subtitle APIs independently.
- [ ] Ensure cookie-bearing command arguments and yt-dlp diagnostic output are redacted.
- [ ] Normalize native/human/AI subtitle provenance and segments; make subtitle availability a probe, not an assumption.
- [ ] If no usable subtitle exists, schedule media/audio acquisition for local ASR without marking the item failed.
- [ ] Test multi-part ordering and ensure all parts render as chapters in one primary note.
- [ ] Run contract/integration tests and a read-only login/session check; defer real content E2E assertions to Task 18.
- [ ] Commit with `feat: add Bilibili favorites adapter`.
- [ ] Update the tracker and report results before continuing to Task 9.

## Task 9: Local ASR backends

**Files:**

- Create: `src/social_media_favorites_archiver/processors/asr.py`
- Create: `src/social_media_favorites_archiver/processors/terminology.py`
- Create: `tests/unit/test_asr.py`
- Create: `tests/integration/test_asr_backends.py`

- [ ] Define an ASR protocol that returns timestamped `TextSegment` objects with backend/model/language/confidence provenance and an explicit no-speech outcome.
- [ ] Write backend-selection tests: FunASR/SenseVoice is the Chinese-oriented default; Apple Silicon may select `mlx-whisper` or `whisper.cpp`; Linux/NVIDIA may select configured `faster-whisper`; unavailable backends produce actionable doctor output.
- [ ] Implement FFmpeg audio extraction with bounded subprocess time, checked exit codes, and sanitized commands.
- [ ] Implement at least one working local backend on the development Mac and retain adapters for the documented fallbacks; do not silently fall back to a cloud model.
- [ ] Support configurable hotwords/domain vocabulary and preserve raw ASR text alongside deterministic corrected text plus correction provenance.
- [ ] Add short, long, multilingual, silence, corrupt-audio, cancellation, and retry fixtures that are redistributable or generated by tests.
- [ ] Benchmark a representative short Chinese clip and record backend, architecture, real-time factor, peak disk use, and limitations in the platform reference without claiming universal performance.
- [ ] Run ASR tests locally; mark heavyweight backend tests with explicit pytest markers so CI can skip model downloads while still testing the protocol.
- [ ] Commit with `feat: add local timestamped ASR backends`.
- [ ] Update the tracker and report results before continuing to Task 10.

## Task 10: OCR and adaptive keyframes

**Files:**

- Create: `src/social_media_favorites_archiver/processors/ocr.py`
- Create: `src/social_media_favorites_archiver/processors/keyframes.py`
- Create: `tests/unit/test_ocr.py`
- Create: `tests/unit/test_keyframes.py`
- Create: `tests/integration/test_rapidocr.py`

- [ ] Define timestamped OCR blocks with bounding boxes, source image/frame, order, confidence, raw text, corrected text, and provenance.
- [ ] Use RapidOCR with ONNX Runtime as the macOS default; keep PaddleOCR out of required 1.0 dependencies.
- [ ] For image posts, preserve platform order, prefer verified original/high-quality images, and keep OCR output attached to its image.
- [ ] Implement video candidate frames using scene changes plus a configurable maximum interval, then remove near-duplicates with perceptual hashing.
- [ ] Add a text-region-change heuristic so persistent burned captions are captured even when the overall scene barely changes.
- [ ] Apply domain terminology correction as auditable post-processing, not as a false claim of OCR model hotword support.
- [ ] Add fixtures for Chinese text, rotated text, low contrast, repeated captions, scene cuts, image order, and no-text images.
- [ ] Run focused OCR/keyframe tests and record local model/runtime compatibility in diagnostics documentation.
- [ ] Commit with `feat: add RapidOCR and adaptive keyframes`.
- [ ] Update the tracker and report results before continuing to Task 11.

## Task 11: ASR × OCR timeline fusion

**Files:**

- Create: `src/social_media_favorites_archiver/processors/fusion.py`
- Create: `tests/unit/test_fusion.py`
- Create: `tests/fixtures/sanitized/fusion_cases.json`

- [ ] Create failing table-driven tests for exact duplicates, punctuation differences, ASR/OCR character errors, partially overlapping time windows, persistent titles, visual-only labels, conflicting readings, and repeated captions.
- [ ] Normalize punctuation, whitespace, simplified/traditional variants where configured, and common OCR confusions while retaining original strings.
- [ ] Align segments by overlap/nearby time windows, then use deterministic similarity thresholds to merge spoken burned captions.
- [ ] Preserve non-spoken visual information such as titles, ingredient quantities, labels, code, and annotations as separate visual segments.
- [ ] Preserve ambiguous conflicts with both readings and provenance instead of silently choosing one.
- [ ] Produce one clean chronological transcript plus an auditable segment map back to ASR and OCR inputs.
- [ ] Add property/invariant tests: stable ordering, deterministic output, no unexplained text loss, and no duplicate merged segment IDs.
- [ ] Run `uv run pytest tests/unit/test_fusion.py -q` and the processor regression suite.
- [ ] Commit with `feat: fuse ASR and OCR timelines`.
- [ ] Update the tracker and report results before continuing to Task 12.

## Task 12: Two-stage sync, early-stop, and reconciliation

**Files:**

- Create: `src/social_media_favorites_archiver/orchestrator.py`
- Create: `tests/integration/test_two_stage_sync.py`
- Create: `tests/integration/test_incremental_sync.py`
- Create: `tests/integration/test_reconciliation.py`

- [ ] Write an integration test proving enumeration creates all metadata skeletons before any heavy media job completes.
- [ ] Write a test proving heavy jobs update the same note in place and a restart resumes outstanding stages without duplicates.
- [ ] Implement the two stages: lightweight collection/item enumeration and skeleton render, followed by persistent heavy processing jobs.
- [ ] Implement incremental early-stop only after a configurable consecutive run of known unchanged canonical IDs, with adapter ordering/completeness evidence and a force-full-sync option.
- [ ] Treat re-favorited old items as active/newly observed even when their canonical IDs were previously known.
- [ ] Reconcile removals only for a collection whose current enumeration completed successfully. Never mark unseen items removed after auth expiry, rate limit, parse failure, cancellation, or partial pagination.
- [ ] Update memberships per collection and derive item removal only after every active membership is gone.
- [ ] Separate lightweight source fingerprints from asset hashes and extraction versions so changes to ASR/OCR code can reprocess derivations without redownloading unchanged assets when safe.
- [ ] Add cleanup scheduling only after the full derivative barrier from Task 6 passes.
- [ ] Run all orchestrator/reconciliation/idempotency tests.
- [ ] Commit with `feat: orchestrate two-stage incremental sync`.
- [ ] Update the tracker and report results before continuing to Task 13.

## Task 13: Xiaohongshu adapter

**Files:**

- Create: `src/social_media_favorites_archiver/adapters/xiaohongshu.py`
- Create: `tests/contract/test_xiaohongshu_adapter.py`
- Create: `tests/integration/test_xiaohongshu_fixture.py`
- Create: `skill/social-media-favorites-archiver/references/platform-xiaohongshu.md`

- [ ] Capture only sanitized structural fixtures from an authorized session; remove cookies, tokens, signatures, private URLs, user identifiers, captions, and media before commit.
- [ ] Write contract tests for collection/favorite pagination, pure text, ordered multi-image, video, unavailable notes, session expiry, and complete/partial enumeration.
- [ ] Implement authenticated page navigation and structured-response interception/page-context extraction. Do not calculate `x-s` in Python.
- [ ] Prefer actual high-quality image URLs exposed by the page/response, including `imageList[].urlDefault` when present; verify dimensions, MIME, length, and hash; record a quality downgrade only when originals are unavailable.
- [ ] Preserve source text and ordered image blocks; render OCR immediately below each image.
- [ ] Treat local ASR plus adaptive frame OCR as the normal video path when no real machine-readable subtitle is exposed.
- [ ] Map page/layout changes to a clear adapter diagnostic and pause safely rather than returning incomplete data as complete.
- [ ] Pass the generic adapter suite and Xiaohongshu sanitized fixture tests.
- [ ] Commit with `feat: add Xiaohongshu favorites adapter`.
- [ ] Update the tracker and report results before continuing to Task 14.

## Task 14: Douyin adapter

**Files:**

- Create: `src/social_media_favorites_archiver/adapters/douyin.py`
- Create: `tests/contract/test_douyin_adapter.py`
- Create: `tests/integration/test_douyin_fixture.py`
- Create: `skill/social-media-favorites-archiver/references/platform-douyin.md`

- [ ] Capture only sanitized structural fixtures under the same privacy rules as Task 13.
- [ ] Write contract tests for favorite collections, video, image/gallery, subtitle-probe outcomes, unavailable items, auth expiry, and complete/partial enumeration.
- [ ] Implement authenticated page navigation and XHR/page-context extraction. Do not calculate `a_bogus` in Python.
- [ ] Probe for a usable text/subtitle track, but size and schedule the default video pipeline for local ASR plus adaptive frame OCR.
- [ ] Feed the two extraction streams through timeline fusion so spoken burned captions are not duplicated and visual-only information remains.
- [ ] Process image/gallery posts as ordered images with inline OCR rather than forcing them through a video path.
- [ ] Detect layout/API drift and pause with a sanitized diagnostic rather than silently dropping items.
- [ ] Pass the generic adapter suite and Douyin sanitized fixture tests.
- [ ] Commit with `feat: add Douyin favorites adapter`.
- [ ] Update the tracker and report results before continuing to Task 15.

## Task 15: Optional OpenAI-compatible enrichment

**Files:**

- Create: `src/social_media_favorites_archiver/processors/enrichment.py`
- Create: `tests/unit/test_enrichment.py`
- Create: `tests/integration/test_openai_compatible.py`
- Modify: `skill/social-media-favorites-archiver/references/configuration.md`

- [ ] Write tests for disabled mode, missing variables, configured custom base URL/model, structured-output validation, retryable errors, permanent errors, and local fallback.
- [ ] Read `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` at call time without logging values or persisting the key.
- [ ] Send only extracted text plus minimal non-sensitive context. Never upload audio, video, images, cookies, browser state, signed asset URLs, or full raw platform responses.
- [ ] Generate a concise summary, normalized tags, and optional topic/MOC suggestions under a strict schema; preserve source text separately.
- [ ] Store provider/model/prompt-version provenance and sanitized request metrics, not secrets or raw authorization metadata.
- [ ] Use mocked HTTP for CI. On this machine, run one opt-in real text-only smoke test using the existing environment and record only pass/fail, latency, model identifier if already non-secret, and token counts if provided.
- [ ] Confirm the full archive/sync path works with enrichment disabled.
- [ ] Commit with `feat: add optional text-only enrichment`.
- [ ] Update the tracker and report results before continuing to Task 16.

## Task 16: CLI, scheduling, and run reports

**Files:**

- Modify: `src/social_media_favorites_archiver/cli.py`
- Create: `src/social_media_favorites_archiver/reporting.py`
- Create: `tests/unit/test_cli_commands.py`
- Create: `tests/integration/test_cli_sync.py`
- Create: `skill/social-media-favorites-archiver/references/troubleshooting.md`

- [ ] Implement and test `smfa doctor`, `login`, `collections`, `sync`, `status`, `retry`, `cleanup`, and `report` with stable exit codes and `--json` where automation benefits.
- [ ] Make `smfa sync` expose platform/collection filters, metadata-only mode, foreground drain mode, force-full mode, item limits for validation, and dry-run/read-only inspection where applicable.
- [ ] Show skeleton enumeration progress separately from heavy queue progress.
- [ ] Generate a sanitized per-run report with counts by platform/type/stage, failures, needs-auth actions, cleanup results, durations, and next-safe commands.
- [ ] Add graceful cancellation that checkpoints cursors, releases or expires leases safely, and never runs false removal reconciliation.
- [ ] Document optional launchd/systemd scheduling examples that invoke the CLI and never embed secrets in service files.
- [ ] Run CLI integration tests using fake adapters and a temporary vault/DB.
- [ ] Commit with `feat: complete CLI and run reporting`.
- [ ] Update the tracker and report results before continuing to Task 17.

## Task 17: Automated hardening and sanitized fixtures

**Files:**

- Modify: `.github/workflows/ci.yml`
- Create/modify: `tests/fixtures/sanitized/**`
- Create: `tests/test_fixture_privacy.py`
- Create: `SECURITY.md`
- Create: `CONTRIBUTING.md`

- [ ] Audit every test fixture and add an automated denylist/entropy/privacy test for cookies, bearer tokens, common key formats, signed query parameters, private profile paths, and unapproved media.
- [ ] Add deterministic fake adapters that exercise all platform content shapes without live network access.
- [ ] Run unit, contract, and integration groups separately in CI; mark heavyweight local-model and live tests opt-in.
- [ ] Add coverage reporting and set an evidence-based threshold that cannot be met by excluding core orchestration, storage, safety, or processing modules.
- [ ] Add dependency/license inventory checks and document third-party model licenses separately from the project's code license.
- [ ] Document responsible use, personal-account scope, no anti-bot bypass, private vulnerability reporting, and redaction expectations.
- [ ] Run `uv run ruff check .`, `uv run mypy src`, `uv run pytest -q`, and the built package install test in a temporary virtual environment.
- [ ] Confirm G1 and G2 are satisfied before live content testing.
- [ ] Commit with `test: harden CI fixtures and security boundaries`.
- [ ] Update the tracker and report results before continuing to Task 18.

## Task 18: Three-platform live end-to-end validation

**Files:**

- Create: `docs/verification/2026-08-08-live-e2e.md`
- Modify only if bugs are found: implementation and tests named by the failing path
- Never commit: live vault, DB, browser profile, cookies, downloaded media, raw screenshots, raw private text, or signed URLs

Use a gitignored path such as `work/e2e-vault` and a dedicated test collection where possible. Before each platform, run `smfa doctor` and reuse the user's authorized Chrome session. Pause for the user only if QR/captcha/device confirmation is required.

Live matrix:

| Platform | Required real item | Required evidence |
|---|---|---|
| Bilibili | Video with usable native subtitle, if one is available | Subtitle provenance and no unnecessary ASR |
| Bilibili | Video without usable subtitle | Local ASR fallback, timestamped transcript, safe cleanup |
| Bilibili | Multi-part video, if available | Ordered parts rendered as one note with chapters |
| Xiaohongshu | Text-only note | Source text and metadata preserved |
| Xiaohongshu | Multi-image note | Original/high-quality ordered images and OCR under each image |
| Xiaohongshu | Video | ASR plus adaptive frame OCR; fusion where relevant |
| Douyin | Spoken video with burned captions | ASR × OCR deduplication and visual-only text preservation |
| Douyin | Image/gallery post | Ordered images with inline OCR |
| Douyin | Silent/no-speech visual-text video, if available | No-speech outcome plus useful visual OCR; use a redistributable fixture only if no live favorite exists |

- [ ] Verify all required types exist. If any mandatory live type is absent, ask once for the user to add a representative item; do not fabricate success. Treat the rows marked “if available” as documented coverage gaps rather than hard failures when truly unavailable.
- [ ] Run metadata-only enumeration first and prove the full collection skeleton list is visible before heavy jobs finish.
- [ ] Drain a bounded representative heavy queue and verify the same Markdown files are filled in place.
- [ ] Complete every available Bilibili row and record sanitized item aliases (`BILI-1`, not real titles/URLs), commands, outcomes, timings, and cleanup evidence.
- [ ] Complete every Xiaohongshu row under the same evidence rules.
- [ ] Complete every Douyin row under the same evidence rules.
- [ ] Rerun the exact bounded sync and prove there are no duplicate notes, assets, ASR/OCR records, or jobs.
- [ ] Move and rename one generated note, resync, and prove it is found by `smfa_id`.
- [ ] Damage a generated-region marker in a disposable test note, resync, and prove the tool reports conflict without overwriting user content.
- [ ] Simulate incomplete enumeration with a sanitized fixture and prove no unseen membership is marked removed.
- [ ] Put one test item in two collections, remove it from one in the fixture/test collection, and prove only that membership changes.
- [ ] Prove temporary video is deleted only after every derivative and final file check succeeds; prove failed derivative retains recoverable media within quota.
- [ ] Run the optional configured text-only enrichment on at least one extracted item without exposing input/output in the committed report; also verify disabled mode.
- [ ] Scan the repository, logs, report, and Git diff for private content and secrets before committing the sanitized evidence report.
- [ ] Fix every discovered product bug with a failing regression test before updating the corresponding matrix row to pass.
- [ ] Confirm G3–G6 and commit with `test: validate three-platform live workflows`.
- [ ] Update the tracker and report results before continuing to Task 19.

## Task 19: Skill packaging and trigger evaluations

**Files:**

- Create: `skill/social-media-favorites-archiver/SKILL.md`
- Create: `skill/social-media-favorites-archiver/LICENSE`
- Create: `skill/social-media-favorites-archiver/.clawhubignore`
- Create/modify: `skill/social-media-favorites-archiver/references/*.md`
- Create: `evals/evals.json`
- Create: `tests/unit/test_skill_bundle.py`

- [ ] Use the `skill-creator` methodology to draft, validate, and improve the Skill; read its current instructions before editing the bundle.
- [ ] Keep the directory and frontmatter name exactly `social-media-favorites-archiver` using lowercase letters and hyphens.
- [ ] Start with this 124-character one-line discovery description and change it only if trigger evaluation produces evidence: `Sync a user's Bilibili/B站, Xiaohongshu/小红书/RedNote, and Douyin/抖音 favorites into local Markdown/Obsidian with local ASR/OCR.`
- [ ] Make `SKILL.md` concise: when to use, when not to use, safety constraints, environment check, installation/invocation, login pauses, progress behavior, and references for platform details.
- [ ] Configure optional OpenAI variables under OpenClaw metadata as optional (`required: false`); never put optional enrichment variables in `requires.env`.
- [ ] Make the Skill install/invoke the public Python CLI reproducibly, for example through `uv tool install git+https://github.com/dvlin-dev/social-media-favorites-archiver.git@v1.0.0` after the tag exists. Before the tag, test the local package or pinned commit and update the bundle during Task 20.
- [ ] Put an MIT-0 license in the distributable Skill directory and document that the application source remains MIT. This matches ClawHub's Skill distribution terms without silently relicensing unrelated application code.
- [ ] Keep the Skill bundle below ClawHub's 50 MB limit. Use `.clawhubignore` so tests, raw docs, application source, caches, and local evidence are not included when publishing the nested folder.
- [ ] Add positive evals for Chinese and English personal-favorites backup/migration/organization requests across all three platforms.
- [ ] Add negative evals for a single video transcription, a single-image OCR request, ordinary video summarization, public-account scraping, marketing copy, reposting, commenting, and unrelated bookmark products.
- [ ] Run multiple trigger-eval passes, inspect false positives/negatives, refine only from evidence, and record scores in a sanitized evaluation report or test output.
- [ ] Test bundle structure, name/parent match, description length, no secrets, valid references, optional env metadata, size, and install command.
- [ ] Commit with `feat: package and evaluate agent skill`.
- [ ] Update the tracker and report results before continuing to Task 20.

## Task 20: User documentation and GitHub 1.0 release

**Files:**

- Modify: `README.md`
- Modify: `skill/social-media-favorites-archiver/SKILL.md`
- Modify: `skill/social-media-favorites-archiver/references/*.md`
- Create: `CHANGELOG.md`
- Create: `docs/verification/2026-08-08-release.md`

- [ ] Document supported platforms/types, privacy boundary, local/cloud split, prerequisites, installation, Chrome/CDP login, config, first metadata sync, heavy queue drain, Obsidian output, scheduling, troubleshooting, upgrade, and uninstall/data-retention behavior.
- [ ] Clearly state that platform changes can temporarily break private collection adapters and that the project does not bypass platform controls.
- [ ] Document local components: browser/yt-dlp/FFmpeg/SQLite/ASR/OCR/assets. Document the only optional cloud component: text-only OpenAI-compatible enrichment.
- [ ] Ensure examples never contain real paths, usernames, cookies, URLs, tokens, or private content.
- [ ] Change the Skill's CLI install reference from its development target to the prospective immutable `v1.0.0` tag before creating the release commit; validate bundle structure locally without claiming the tag already exists.
- [ ] Build wheel/sdist, inspect package contents, install the wheel in a fresh temporary environment, and run `smfa --help` plus `smfa doctor`.
- [ ] Run the full test/lint/type/privacy suite and verify the worktree contains no live artifacts.
- [ ] Commit the exact release tree, create annotated tag `v1.0.0` on that commit, push commit and tag, and create a GitHub release with accurate support claims and checksums/artifacts as configured.
- [ ] After the tag is public, rerun the Skill's real public CLI installation command. If a correction is required, issue a patch release rather than moving or rewriting `v1.0.0`.
- [ ] Record the GitHub release URL, commit/tag, artifact checks, and gates in the release verification document.
- [ ] Commit any post-release documentation-only evidence with `docs: record 1.0 release verification` and push.
- [ ] Update the tracker and report results before continuing to Task 21.

## Task 21: Skills.sh installation and discovery

**Files:**

- Modify: `docs/verification/2026-08-08-release.md`

Current official model: Skills.sh discovers skills hosted in GitHub repositories through the `skills` CLI and anonymous installation telemetry; there is no separate source upload step. Recheck the current [Skills.sh documentation](https://skills.sh/docs), [CLI reference](https://skills.sh/docs/cli), and [FAQ](https://skills.sh/docs/faq) before publication in case the registry workflow changed.

- [ ] From a fresh temporary directory outside the repository, inspect available skills with `npx -y skills add dvlin-dev/social-media-favorites-archiver --list`.
- [ ] Install only the nested Skill with `npx -y skills add dvlin-dev/social-media-favorites-archiver --skill social-media-favorites-archiver --copy -y`.
- [ ] Inspect the installed copy and verify its name, description, references, version/install target, optional environment declarations, and absence of unrelated repo/private files.
- [ ] Run a clean Agent/CLI discovery smoke test using one positive and one negative prompt from `evals/evals.json`.
- [ ] Check discovery immediately after the telemetry-generating installation, then after 15 and 60 minutes if needed. Verify the actual URL rather than assuming it; a likely route is `https://skills.sh/dvlin-dev/social-media-favorites-archiver/social-media-favorites-archiver`.
- [ ] Record exact install command, observed Skills.sh URL/status, date, and non-sensitive output in the release verification document.
- [ ] If discovery does not appear after a reasonable index window, verify the public repo/default branch/SKILL structure and CLI telemetry behavior, fix the packaging issue, repeat installation, and report the remaining external indexing delay honestly.
- [ ] Commit with `docs: verify Skills.sh distribution` and push.
- [ ] Update the tracker and report results before continuing to Task 22.

## Task 22: ClawHub publication and verification

**Files:**

- Modify: `docs/verification/2026-08-08-release.md`
- Modify if required by validation: `skill/social-media-favorites-archiver/**`

Before publication, recheck the current official [ClawHub guide](https://docs.openclaw.ai/clawhub), [Skill format](https://docs.openclaw.ai/clawhub/skill-format), [creating Skills guide](https://docs.openclaw.ai/tools/creating-skills), and [OpenClaw Skills guide](https://docs.openclaw.ai/tools/skills). If current official syntax differs from this plan, update the plan with the evidence before executing the changed command.

- [ ] Before publishing, tell the user that ClawHub publishes Skill bundles under MIT-0 while the application repository remains MIT. The user already requested publication, so continue unless they object or the platform requests new legal/account authorization.
- [ ] Install or update the official CLI (`npm i -g clawhub`), run `clawhub login` if needed, and verify the active account with `clawhub whoami`. Pause only for user-owned browser/account confirmation.
- [ ] Validate the nested folder, parent/name match, one-line description under 160 characters, MIT-0 bundle license, optional environment metadata, references, ignore rules, and size under 50 MB.
- [ ] Publish from the nested folder with:

  ```bash
  clawhub skill publish skill/social-media-favorites-archiver \
    --slug social-media-favorites-archiver \
    --name "Social Media Favorites Archiver" \
    --version 1.0.0 \
    --changelog "Initial stable release: Bilibili, Xiaohongshu, and Douyin favorites to local Markdown with local ASR/OCR." \
    --tags latest
  ```

- [ ] Capture the exact qualified reference and public URL returned by ClawHub; do not assume the ClawHub owner handle matches the GitHub handle.
- [ ] Wait for the ClawHub security scan. If it is held or fails, inspect the report, fix the bundle/security issue, increment the version when required, republish, and do not mark this task complete until it passes.
- [ ] Verify with `openclaw skills search "social media favorites archiver"`, `openclaw skills install <qualified-ref>`, `openclaw skills verify <qualified-ref>`, and `openclaw skills verify <qualified-ref> --card` using the exact published reference.
- [ ] Record account handle, qualified reference, version, scan status, URL, and sanitized verification results in the release report.
- [ ] Commit with `docs: record ClawHub publication` and push.
- [ ] Update the tracker and report results before continuing to Task 23.

## Task 23: Clean-room post-publication verification

**Files:**

- Modify: `docs/verification/2026-08-08-release.md`
- Modify if defects are found: affected implementation, Skill bundle, tests, changelog, and version

- [ ] Create two fresh temporary directories: one for a Skills.sh/GitHub CLI installation and one for the ClawHub installation. Do not reuse development virtualenvs or installed Skill folders.
- [ ] Install from each distribution path and verify the nested bundle contains only intended files.
- [ ] From each clean install, invoke the Skill with a positive personal-favorites prompt and verify it leads to the pinned public CLI installation and `smfa doctor`.
- [ ] Invoke a negative single-link transcription/OCR prompt and verify the Skill does not claim that unrelated task.
- [ ] Run a non-destructive metadata-only or bounded test-collection flow using authorized session state; do not commit the resulting private vault.
- [ ] Verify both installations point at the immutable released application version and have matching user-visible behavior.
- [ ] Test documented uninstall/removal steps without deleting the user's real vault, browser profile, or model cache.
- [ ] If a defect is found, add a regression test, issue the necessary patch release, republish/update both registries, and rerun this entire task.
- [ ] Record final G8 evidence and commands in the release report.
- [ ] Commit with `test: verify published skill from clean installs` and push.
- [ ] Update the tracker and report results before continuing to Task 24.

## Task 24: Final audit and handoff

**Files:**

- Modify: this plan
- Modify: `docs/verification/2026-08-08-release.md`
- Modify if necessary: `README.md`

- [ ] Confirm every checkbox in Tasks 0–23 is completed and evidenced. No required three-platform/type row may be silently waived; a user-accepted limitation must remain visibly documented and cannot be relabeled as a pass.
- [ ] Confirm Tasks 0–23 are `completed` and all release gates G1–G8 have evidence.
- [ ] Run final `git status --short`, full tests, lint, types, package build/install smoke test, fixture privacy scan, and Skill bundle validation.
- [ ] Confirm the worktree is clean, all intended commits/tags are pushed, and no live artifacts or secrets exist in Git history introduced by this work.
- [ ] Verify the README links the detailed design, this implementation plan, GitHub release, Skills.sh listing, and ClawHub listing.
- [ ] Prepare the final conversation report containing: shipped features, representative live matrix result, test totals, version/tag, GitHub release URL, Skills.sh URL, ClawHub URL/reference, known limitations, and safe first command for the user.
- [ ] Check this task's completed boxes, set Task 24 to `completed`, commit with `docs: complete implementation plan`, push, and confirm `git status --short` is empty; then send the prepared final report.
- [ ] Do not call the project complete until the final report and all public install paths have been verified.

## Completion definition

This project is complete only when a fresh Agent can discover the Skill for a request about the user's own saved Bilibili/Xiaohongshu/Douyin collections, install it from either public distribution path, perform a safe authenticated sync, see immediate Markdown skeletons, receive completed local ASR/OCR/fused content later, rerun without duplication, preserve manual notes, and verify that no raw media or credentials were sent to the optional cloud enrichment service.
