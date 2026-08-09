# Changelog

All notable changes to this project are documented in this file. Release tags are immutable.

## [1.0.4] - 2026-08-09

### Fixed

- Install the documented FFmpeg prerequisite in CI unit/coverage jobs, make CLI help assertions independent of forced terminal color, and declare the optional MLX import consistently for strict cross-platform mypy.

## [1.0.3] - 2026-08-09

### Fixed

- Correct invalid YAML quoting in the package-install CI smoke step and add a regression test that parses the workflow and checks its required jobs.

## [1.0.2] - 2026-08-09

### Fixed

- Require Agents to copy the immutable Git-tag installation command exactly instead of substituting an unavailable PyPI package specification, moving branch, or other source.

## [1.0.1] - 2026-08-09

### Security

- Explicitly classify platform titles, descriptions, subtitles, OCR/ASR transcripts, author fields, and URLs as untrusted data rather than Agent instructions.
- Forbid following embedded prompts, commands, and links; Agent decisions use only fixed Skill commands and sanitized aggregate reports.
- Keep optional enrichment instructions separate from allowlisted structured text and require schema-validated output.

## [1.0.0] - 2026-08-09

### Added

- Local-first two-stage sync for a user's own Bilibili, Xiaohongshu/RedNote, and Douyin favorites.
- Durable SQLite jobs for media, ASR, OCR, timeline fusion, optional text-only enrichment, Markdown rendering, verification, and safe cleanup.
- Native subtitle preservation, local ASR fallback, ordered gallery OCR, adaptive video-frame OCR, and spoken/burned-caption fusion.
- Obsidian-safe `smfa_id` note relocation, user-region preservation, conflict detection, indexes, and collection-membership reconciliation.
- Typer CLI commands for doctor, login, collection discovery, sync, status, retry, cleanup, and sanitized reports.
- Nested `social-media-favorites-archiver` Agent Skill with Skills.sh and ClawHub distribution metadata.

### Changed

- Authenticated platform discovery uses a dedicated real Chrome/CDP session and separate platform tabs.
- Limited or interrupted enumeration is explicitly non-reconciling and safe to resume.
- Xiaohongshu page-default renditions accept decoded aspect-preserving media instead of requiring source-resolution equality.
- Douyin root pagination restarts from a stable first cursor on every bounded run.

### Security

- No platform secret, Cookie value, authorization header, browser state, signed URL, raw private response, or live content is committed or emitted in reports.
- Media processing remains local; optional cloud enrichment accepts text-only allowlisted fields and is disabled by default.
- Cleanup requires cache containment, database ownership, hash verification, a complete derivative barrier, and final-note verification.
- Captchas, device checks, access controls, rate limits, signatures, and anti-bot systems are never bypassed.

[1.0.4]: https://github.com/dvlin-dev/social-media-favorites-archiver/releases/tag/v1.0.4
[1.0.3]: https://github.com/dvlin-dev/social-media-favorites-archiver/releases/tag/v1.0.3
[1.0.2]: https://github.com/dvlin-dev/social-media-favorites-archiver/releases/tag/v1.0.2
[1.0.1]: https://github.com/dvlin-dev/social-media-favorites-archiver/releases/tag/v1.0.1
[1.0.0]: https://github.com/dvlin-dev/social-media-favorites-archiver/releases/tag/v1.0.0
