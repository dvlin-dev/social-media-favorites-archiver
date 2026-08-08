from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from social_media_favorites_archiver.models import ContentType, Platform, SourceAvailability

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sanitized"
REPOSITORY_ROOT = Path(__file__).parent.parent
ALLOWED_URL_HOSTS = {
    "example.invalid",
    "www.bilibili.com",
    "space.bilibili.com",
    "i0.hdslb.com",
}
SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "local_storage",
    "profile_path",
    "raw_response",
    "request_headers",
    "secret",
    "signature",
    "signed_url",
    "token",
    "xsec",
)
SENSITIVE_QUERY_PARTS = ("auth", "key", "signature", "signed", "token", "xsec")
SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?im)^(?:set-)?cookie\s*:"),
    re.compile(r"(?im)^(?:proxy-)?authorization\s*:"),
)
PRIVATE_PATHS = (
    re.compile(r"/(?:Users|home)/[^/\s]+/"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\"),
)
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
OPAQUE_PATTERN = re.compile(r"^[A-Za-z0-9_+/=-]{32,}$")


def _fixtures() -> tuple[Path, ...]:
    return tuple(sorted(FIXTURE_ROOT.rglob("*")))


def _walk(value: object, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], object]]:
    yield path, value
    if isinstance(value, dict):
        for key, nested in value.items():
            yield from _walk(nested, (*path, str(key)))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk(nested, (*path, str(index)))


def _entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def test_sanitized_fixture_tree_contains_only_small_json_documents() -> None:
    files = _fixtures()

    assert files
    assert all(path.is_file() for path in files)
    assert all(path.suffix == ".json" for path in files)
    assert all(path.stat().st_size <= 100_000 for path in files)
    for path in files:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict), path


@pytest.mark.parametrize("path", _fixtures(), ids=lambda path: path.name)
def test_fixture_declares_synthetic_only_evidence(path: Path) -> None:
    fixture = json.loads(path.read_text(encoding="utf-8"))

    assert fixture["evidence"]["values"] == "synthetic-placeholders-only"


@pytest.mark.parametrize("path", _fixtures(), ids=lambda path: path.name)
def test_fixture_contains_no_secret_private_path_or_sensitive_structural_value(
    path: Path,
) -> None:
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)

    for pattern in (*SECRET_PATTERNS, *PRIVATE_PATHS):
        assert pattern.search(raw) is None, (path, pattern.pattern)
    for node_path, value in _walk(parsed):
        if node_path:
            normalized_key = node_path[-1].lower().replace("-", "_")
            assert not any(part in normalized_key for part in SENSITIVE_KEY_PARTS), (
                path,
                node_path,
            )
        if isinstance(value, str) and OPAQUE_PATTERN.fullmatch(value):
            assert _entropy(value) < 4.25, (path, node_path, "high-entropy opaque value")


@pytest.mark.parametrize("path", _fixtures(), ids=lambda path: path.name)
def test_fixture_urls_are_placeholder_or_approved_public_shapes_without_signatures(
    path: Path,
) -> None:
    raw = path.read_text(encoding="utf-8")

    for match in URL_PATTERN.finditer(raw):
        url = match.group(0).rstrip(".,);]")
        parsed = urlsplit(url)
        assert parsed.hostname in ALLOWED_URL_HOSTS, (path, parsed.hostname)
        assert parsed.username is None and parsed.password is None
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            assert not any(part in key.lower() for part in SENSITIVE_QUERY_PARTS), (
                path,
                key,
            )


def test_deterministic_fake_adapters_cover_every_platform_content_shape() -> None:
    import asyncio

    from tests.fakes import deterministic_adapters

    async def exercise() -> list[tuple[Platform, ContentType, SourceAvailability, dict]]:
        observed: list[tuple[Platform, ContentType, SourceAvailability, dict]] = []
        for adapter in deterministic_adapters():
            collections = await adapter.list_collections()
            assert collections.complete is True
            favorites = await adapter.list_favorites(collections.items[0])
            assert favorites.complete is True
            for reference in favorites.items:
                item = await adapter.fetch_item(reference)
                observed.append(
                    (
                        item.platform,
                        item.content_type,
                        item.source_availability,
                        item.platform_metadata,
                    )
                )
        return observed

    observed = asyncio.run(exercise())
    shapes = {(platform, content_type, availability) for platform, content_type, availability, _ in observed}

    assert {
        (Platform.BILIBILI, ContentType.VIDEO, SourceAvailability.AVAILABLE),
        (Platform.XIAOHONGSHU, ContentType.ARTICLE, SourceAvailability.AVAILABLE),
        (Platform.XIAOHONGSHU, ContentType.GALLERY, SourceAvailability.AVAILABLE),
        (Platform.XIAOHONGSHU, ContentType.VIDEO, SourceAvailability.AVAILABLE),
        (Platform.XIAOHONGSHU, ContentType.ARTICLE, SourceAvailability.UNAVAILABLE),
        (Platform.DOUYIN, ContentType.VIDEO, SourceAvailability.AVAILABLE),
        (Platform.DOUYIN, ContentType.GALLERY, SourceAvailability.AVAILABLE),
        (Platform.DOUYIN, ContentType.VIDEO, SourceAvailability.UNAVAILABLE),
    }.issubset(shapes)
    bilibili_metadata = [
        metadata for platform, _, _, metadata in observed if platform == Platform.BILIBILI
    ]
    assert any(metadata.get("subtitle_available") is True for metadata in bilibili_metadata)
    assert any(metadata.get("requires_local_asr") is True for metadata in bilibili_metadata)
    assert any(metadata.get("parts") for metadata in bilibili_metadata)


def test_ci_security_contribution_and_license_hardening_contract() -> None:
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    security = (REPOSITORY_ROOT / "SECURITY.md").read_text(encoding="utf-8").lower()
    contributing = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").lower()
    model_licenses = (
        REPOSITORY_ROOT / "docs" / "third-party-model-licenses.md"
    ).read_text(encoding="utf-8").lower()
    license_check = REPOSITORY_ROOT / "scripts" / "check_dependency_licenses.py"

    assert all(
        command in workflow
        for command in (
            "pytest tests/unit",
            "pytest tests/contract",
            "pytest tests/integration",
            "pytest tests/test_fixture_privacy.py",
            "--cov=social_media_favorites_archiver",
            "--cov-fail-under=70",
            "ruff check .",
            "mypy src",
            "uv build",
            "check_dependency_licenses.py",
        )
    )
    assert "not heavyweight and not live" in workflow
    assert all(
        phrase in security
        for phrase in (
            "personal account",
            "anti-bot",
            "private vulnerability",
            "github security advisory",
            "redact",
        )
    )
    assert all(
        phrase in contributing
        for phrase in ("test-driven", "sanitized fixture", "ruff", "mypy", "pytest")
    )
    assert all(
        phrase in model_licenses
        for phrase in ("not redistributed", "funasr", "mlx-whisper", "rapidocr", "whisper.cpp")
    )
    assert license_check.is_file()
