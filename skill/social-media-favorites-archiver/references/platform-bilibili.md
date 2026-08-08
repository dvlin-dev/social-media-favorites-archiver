# Bilibili

The adapter discovers collection IDs in the user's authenticated Bilibili page context, then delegates favorite-list extraction, BV/AV normalization, WBI handling, media format negotiation, multi-part videos, and native subtitle extraction to `yt-dlp`.

- Use only the dedicated Chrome profile configured for this application.
- Never copy cookies or browser databases into project files or CLI arguments.
- A QR, captcha, or device confirmation is a user-owned pause; do not bypass it.
- Native SRT data returned by `yt-dlp` is normalized with explicit human, AI, or unknown-platform provenance. When no usable subtitle exists, the item remains valid and schedules local ASR.
- Multi-part videos remain one canonical note with ordered part chapters.
- Private, deleted, or unavailable favorites retain safe metadata rather than disappearing silently.
- Real favorite content and signed media URLs belong only in ignored live-work paths, never fixtures or Git.
