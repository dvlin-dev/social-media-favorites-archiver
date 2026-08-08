# Security policy

## Supported scope

This project is for archiving favorites from the operator's own personal account into a local vault. It is not a scraping service, credential broker, platform-verification bypass, or anti-bot evasion toolkit. Use it only where the account owner is authorized to access and archive the material, and respect platform terms and content rights.

The current development branch receives security fixes. Published release support will be documented with each release; until a stable release exists, pin a reviewed commit and upgrade deliberately.

## Private vulnerability reporting

Please do not open a public issue containing a vulnerability, private content, account identifier, path, log bundle, screenshot, Cookie, token, signed URL, or reproduction archive. Submit a private vulnerability report through the repository's GitHub Security Advisory page. Include only the smallest sanitized reproduction needed to understand the boundary failure.

If GitHub private reporting is unavailable, open a content-free public issue asking the maintainer to enable a private channel. Do not attach the exploit or sensitive evidence publicly.

## Sensitive-data boundaries

- Authentication remains in a dedicated Chrome profile. The application does not accept platform passwords or cookies in configuration.
- Do not bypass captcha, device confirmation, rate limits, platform verification, or other anti-bot controls.
- Browser profiles, live databases, vaults, downloads, media, raw responses, and screenshots must remain under ignored local paths.
- Redact authorization headers, cookies, bearer tokens, API keys, signed query values, private paths, email addresses, phone numbers, and private content before sharing diagnostics.
- Optional cloud enrichment is text-only. It must never receive media, browser state, signed asset URLs, raw platform responses, or local absolute paths.
- `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` are read only at enrichment call time. Reports expose presence booleans, never their values.
- Cleanup can delete only an exact registered item-owned file after containment, hash, derivative, render, and final-verification gates pass.

## Response expectations

Maintainers will acknowledge a private report when available, reproduce it with synthetic data, assess affected versions, and coordinate a fix and disclosure. Never send live account artifacts merely to accelerate triage; a deterministic sanitized fixture is the preferred evidence.
