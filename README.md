# Social Media Favorites Archiver

Archive a user's own Bilibili, Xiaohongshu/RedNote, and Douyin favorites into a local Markdown or Obsidian knowledge base. The CLI discovers saved collections through an authorized dedicated Chrome profile, renders fast skeleton notes, and then runs durable local media, ASR, OCR, fusion, verification, and cleanup stages.

## Support matrix

| Platform | Supported saved content | Processing |
|---|---|---|
| Bilibili / B站 | Videos, unavailable entries, multi-part metadata | Native subtitle preservation when available; otherwise local ASR; ordered chapters for multi-part metadata |
| Xiaohongshu / 小红书 / RedNote | Text notes, image posts, ordered galleries, videos, unavailable entries | Source text, ordered image OCR, local video ASR, adaptive frame OCR, and timeline fusion |
| Douyin / 抖音 | Image posts, ordered galleries, videos, unavailable entries | Ordered image OCR, local video ASR, adaptive frame OCR, burned-caption deduplication, and visual-only text preservation |

The release exercised one real representative item per platform through the complete heavy pipeline. Other content shapes were inventoried from authorized metadata and covered by sanitized contract/integration fixtures; they are not described as real end-to-end passes. See the [live verification report](docs/verification/2026-08-08-live-e2e.md) for the exact coverage gaps.

Platform page and private API changes can temporarily break adapters. The project reports layout/completeness failures and pauses safely; it does not bypass captchas, device checks, access controls, rate limits, signatures, or anti-bot systems.

## Privacy boundary

Local by default:

- Dedicated Chrome/CDP session and page-context discovery
- Bilibili metadata/media negotiation through `yt-dlp`
- SQLite identity, collection membership, queue, extraction, and audit state
- FFmpeg audio extraction and adaptive video-frame sampling
- Local Whisper/FunASR-compatible ASR and RapidOCR
- ASR × OCR fusion, Markdown rendering, indexes, verification, and bounded asset cleanup

The only optional cloud component is text-only OpenAI-compatible enrichment. It is disabled by default and sends only an allowlist of redacted title/author/platform/original-text/transcript/OCR fields when explicitly enabled. It never sends media, source/media URLs, local paths, browser state, cookies, headers, or raw platform responses.

Never put platform credentials, Cookie values, authorization headers, signed URLs, or browser data in configuration, commands, fixtures, logs, issues, or commits. `smfa doctor`, `status`, and `report` expose presence booleans, counts, and sanitized diagnostic codes only.

## Prerequisites

- macOS or Linux with a user-authorized Chrome/Chromium session exposed through CDP
- [`uv`](https://docs.astral.sh/uv/) for immutable CLI installation
- FFmpeg on `PATH` for video/audio processing
- Enough local space for the configured cache quota and filesystem reserve
- Optional Apple Silicon MLX Whisper, FunASR, faster-whisper, or whisper.cpp backend
- Optional `rapidocr-onnxruntime`/ONNX Runtime for local OCR

The CLI's `smfa doctor` command checks the configured browser, paths, FFmpeg, ASR/OCR backends, and optional enrichment-variable presence without displaying secret values.

## Installation

Install the immutable 1.0.0 Git tag:

```bash
uv tool install git+https://github.com/dvlin-dev/social-media-favorites-archiver.git@v1.0.0
smfa --help
```

For a source checkout used by contributors:

```bash
uv sync --locked --group dev
uv run smfa --help
```

## Dedicated Chrome login

Use a separate profile rather than a normal browsing profile. Example macOS launch command:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=/absolute/path/to/dedicated-smfa-profile
```

Then check and establish each requested platform session:

```bash
smfa doctor --config /absolute/path/to/config.yml
smfa login bilibili --config /absolute/path/to/config.yml
smfa login xiaohongshu --config /absolute/path/to/config.yml
smfa login douyin --config /absolute/path/to/config.yml
```

Complete QR, captcha, account, or device confirmation in Chrome when the platform requests it. Re-run the login check afterward. Do not copy cookies or browser databases into the repository or CLI arguments.

## Configuration

Create a user-owned YAML file outside the repository:

```yaml
vault_path: /absolute/path/to/archive-vault
state_db_path: /absolute/path/to/archive-state/archive.db
cache_path: /absolute/path/to/archive-cache
cache_quota_bytes: 21474836480
browser_cdp_url: http://127.0.0.1:9222
browser_profile_path: /absolute/path/to/dedicated-smfa-profile
enabled_platforms: [bilibili, xiaohongshu, douyin]
concurrency: 2
retries: 3
early_stop_threshold: 20
cleanup_policy: after-verified
asr_backend: auto
ocr_backend: rapidocr
enrichment_enabled: false
```

Every YAML setting also has an `SMFA_`-prefixed environment form. Optional enrichment uses `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL`; these are not required for the base archive and should stay in the user's secure environment.

## First sync

List authorized collections without writing archive state:

```bash
smfa collections all --config /absolute/path/to/config.yml
```

Render skeleton notes first and leave durable heavy work queued:

```bash
smfa sync all --metadata-only --config /absolute/path/to/config.yml
```

Drain local assets, ASR, OCR, fusion, optional enrichment, render, verify, and cleanup stages:

```bash
smfa sync all --foreground --config /absolute/path/to/config.yml
```

Use `--limit` only for representative validation. A limited or interrupted enumeration never marks unseen favorites removed. Use `--full` periodically when complete reconciliation is intended.

Inspect only sanitized aggregate state:

```bash
smfa status --json --config /absolute/path/to/config.yml
smfa report --json --config /absolute/path/to/config.yml
```

## Obsidian output

Every item has a stable `smfa_id`. Skeleton notes contain source metadata, collections, and processing status; completed stages fill the same note in place. Moving or renaming a note remains safe because later syncs locate it by `smfa_id`. User prose, unknown frontmatter, and user tags are preserved. Damaged generated-region markers produce a conflict instead of overwriting the file.

Assets use relative vault links and deterministic item-owned paths. Cleanup targets only registered cache media after derivative and final-note verification; it never treats the vault or browser profile as disposable data.

## Scheduling

Complete one manual doctor, login, collection listing, metadata sync, foreground drain, and report before scheduling. A macOS LaunchAgent or Linux systemd user timer can invoke `smfa sync all --metadata-only` on an interval and reuse the dedicated session. Scheduled jobs cannot answer QR/captcha/device prompts and service definitions must not embed secrets.

See the [scheduling examples](skill/social-media-favorites-archiver/references/troubleshooting.md#optional-scheduling) for launchd and systemd templates.

## Troubleshooting

- Run `smfa doctor` first for missing browser, FFmpeg, ASR, OCR, path, or configuration prerequisites.
- Run `smfa status --json` and `smfa report --json` for sanitized run and queue state.
- Repair an expired session with `smfa login <platform>`, then resume the same sync.
- Requeue safe local failures with `smfa retry failed`.
- A layout-change or incomplete-enumeration diagnostic must be fixed against authorized structural evidence; never force partial data to complete.
- Page-exposed media URLs can expire. Re-enumerate before retrying a media failure.
- `smfa cleanup` previews eligible item-owned files. Apply deletion only after reviewing the preview and successful verification state.

Exit codes are `0` success, `1` operational failure, `2` invalid usage, `3` user login/action required, and `130` safe interruption.

## Upgrade

Release tags are immutable. Install a newer explicit tag after reading [CHANGELOG.md](CHANGELOG.md):

```bash
uv tool install --force git+https://github.com/dvlin-dev/social-media-favorites-archiver.git@vX.Y.Z
smfa doctor --config /absolute/path/to/config.yml
```

The SQLite migrator upgrades supported older schemas on the next command. Back up the user-owned vault and state database before a major-version migration.

## Uninstall and data retention

Remove the executable:

```bash
uv tool uninstall social-media-favorites-archiver
```

Uninstalling does not delete the vault, SQLite database, dedicated browser profile, configuration, model cache, or downloaded assets. Retain or remove those user-owned paths manually after inspecting them. Use `smfa cleanup` before uninstalling if verified temporary media should be removed under the application's ownership checks.

## Development and verification

```bash
uv run pytest -q -m "not heavyweight and not live"
uv run ruff check .
uv run mypy src
uv run python scripts/check_dependency_licenses.py
```

The [detailed design](docs/superpowers/specs/2026-08-08-social-media-favorites-archiver-design.md), [implementation plan](docs/superpowers/plans/2026-08-08-social-media-favorites-archiver-implementation.md), and [release evidence](docs/verification/2026-08-08-release.md) describe the architecture, execution gates, and sanitized verification record.
