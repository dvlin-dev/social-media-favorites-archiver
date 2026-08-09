from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
README = ROOT / "README.md"
SKILL = ROOT / "skill" / "social-media-favorites-archiver" / "SKILL.md"
CHANGELOG = ROOT / "CHANGELOG.md"
RELEASE_REPORT = ROOT / "docs" / "verification" / "2026-08-08-release.md"
PUBLIC_INSTALL = (
    "uv tool install "
    "git+https://github.com/dvlin-dev/social-media-favorites-archiver.git@v1.0.1"
)


def test_release_version_and_immutable_skill_install_target() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    skill = SKILL.read_text(encoding="utf-8")

    assert pyproject["project"]["version"] == "1.0.1"
    assert PUBLIC_INSTALL in skill
    assert "{baseDir}/../.." not in skill


def test_readme_documents_the_complete_local_first_user_lifecycle() -> None:
    readme = README.read_text(encoding="utf-8")
    lowered = readme.lower()

    assert all(
        heading in lowered
        for heading in (
            "## support matrix",
            "## privacy boundary",
            "## prerequisites",
            "## installation",
            "## dedicated chrome login",
            "## configuration",
            "## first sync",
            "## obsidian output",
            "## scheduling",
            "## troubleshooting",
            "## upgrade",
            "## uninstall and data retention",
        )
    )
    assert all(
        phrase in readme
        for phrase in (
            "smfa doctor",
            "smfa sync all --metadata-only",
            "smfa sync all --foreground",
            "SQLite",
            "yt-dlp",
            "FFmpeg",
            "RapidOCR",
            "OpenAI-compatible",
        )
    )
    assert "Platform page and private API changes can temporarily break adapters" in readme
    assert "does not bypass" in lowered
    assert "one real representative item per platform" in lowered


def test_release_docs_and_examples_contain_no_private_or_secret_shapes() -> None:
    texts = [
        README.read_text(encoding="utf-8"),
        SKILL.read_text(encoding="utf-8"),
        CHANGELOG.read_text(encoding="utf-8"),
        RELEASE_REPORT.read_text(encoding="utf-8"),
    ]
    combined = "\n".join(texts)

    assert "/Users/" not in combined
    assert re.search(r"\bsk-[A-Za-z0-9_-]{20,}\b", combined) is None
    assert re.search(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", combined) is None
    assert "Cookie:" not in combined
    assert "Authorization:" not in combined
    assert "https://www.bilibili.com/video/" not in combined
    assert "https://www.xiaohongshu.com/explore/" not in combined
    assert "https://www.douyin.com/video/" not in combined


def test_changelog_and_release_report_start_the_immutable_release_record() -> None:
    changelog = CHANGELOG.read_text(encoding="utf-8")
    report = RELEASE_REPORT.read_text(encoding="utf-8")

    assert "## [1.0.0] - 2026-08-09" in changelog
    assert "### Added" in changelog
    assert "### Security" in changelog
    assert "# 1.0.0 release verification" in report
    assert all(section in report for section in ("## Build", "## Gates", "## Publication"))
