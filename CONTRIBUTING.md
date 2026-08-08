# Contributing

Thanks for helping improve a local-first personal favorites archiver. Contributions must preserve the project's authenticated-browser, privacy, and no-bypass boundaries.

## Development setup

Use Python 3.11 or newer and `uv`:

```bash
uv sync --locked --group dev
uv run smfa --help
```

Work on a topic branch, keep unrelated user changes intact, and make small commits. Use test-driven development: add or change the focused test, observe the intended failure, implement the smallest complete behavior, and run focused plus regression checks.

## Required checks

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run pytest tests/test_fixture_privacy.py -q
uv run python scripts/check_dependency_licenses.py
```

Heavyweight local-model tests and live platform tests are opt-in. Never make a live test a default CI dependency, and never claim a live platform/content path passed from a mock.

## Sanitized fixture policy

Every committed sanitized fixture must be a small JSON document containing only synthetic placeholder values and an `evidence.values: synthetic-placeholders-only` marker. Fixtures may preserve necessary field shape, ordering, types, and pagination relationships, but must not contain:

- cookies, authorization headers, bearer tokens, API keys, signatures, signed URLs, or browser storage;
- user identifiers, private profile paths, private titles/text/captions, real media, or screenshots;
- opaque high-entropy values copied from a response;
- a URL with authentication/signature query parameters.

Run the privacy test before committing fixture changes. When live evidence is required, inspect only the authorized structural shape and hand-author a deterministic regression fixture.

## Code and architecture

- Keep platform request authentication in the browser page or the upstream extractor; do not reproduce private signing algorithms in Python.
- Keep local ASR/OCR and cloud text enrichment separate. Media is local-only.
- Treat incomplete enumeration as incomplete; removal reconciliation requires a full successful cursor walk.
- Preserve unknown frontmatter, user tags, and the user note region.
- Add migrations transactionally and keep jobs idempotent, leased, and restartable.

New dependencies require a license-inventory update and an explanation of why the standard library or an existing package is insufficient. Models and model runtimes retain their own licenses; do not add model weights to this repository.

## Security reports

Follow [SECURITY.md](SECURITY.md). Use a private GitHub Security Advisory for vulnerabilities and redact all evidence. Public pull requests and issues must contain synthetic data only.
