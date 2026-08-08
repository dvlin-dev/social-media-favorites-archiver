"""Fail CI when direct runtime dependencies drift outside the reviewed license inventory."""

from __future__ import annotations

import json
import re
import tomllib
from importlib.metadata import PackageNotFoundError, metadata, version
from pathlib import Path

EXPECTED_LICENSES = {
    "filelock": ("mit",),
    "httpx": ("bsd",),
    "imagehash": ("bsd",),
    "pillow": ("mit-cmu", "hpnd"),
    "playwright": ("apache-2.0", "apache software"),
    "pydantic": ("mit",),
    "pydantic-settings": ("mit",),
    "pyyaml": ("mit",),
    "rapidfuzz": ("mit",),
    "typer": ("mit",),
    "yt-dlp": ("unlicense",),
}
OPTIONAL_LICENSES = {
    "faster-whisper": ("mit",),
    "funasr": ("mit",),
    "mlx-whisper": ("mit",),
    "onnxruntime": ("mit",),
    "rapidocr-onnxruntime": ("apache-2.0", "apache software"),
}


def _normalized_name(requirement: str) -> str:
    name = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip()
    return re.sub(r"[-_.]+", "-", name).lower()


def _license_text(distribution: str) -> str:
    package = metadata(distribution)
    candidates = (
        package.get("License-Expression"),
        package.get("License"),
        " ".join(
            value
            for value in package.get_all("Classifier", ())
            if value.startswith("License ::")
        ),
    )
    return " ".join(value for value in candidates if value).strip().lower()


def _check(distribution: str, expected: tuple[str, ...]) -> dict[str, str]:
    license_text = _license_text(distribution)
    if not license_text or not any(marker in license_text for marker in expected):
        raise SystemExit(f"unreviewed license metadata for {distribution}")
    if "agpl" in license_text or "gnu general public license" in license_text:
        raise SystemExit(f"strong copyleft dependency requires explicit review: {distribution}")
    return {
        "distribution": distribution,
        "version": version(distribution),
        "license": license_text[:160],
    }


def main() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    runtime_names = {_normalized_name(value) for value in project["dependencies"]}
    if runtime_names != set(EXPECTED_LICENSES):
        missing = sorted(runtime_names - set(EXPECTED_LICENSES))
        stale = sorted(set(EXPECTED_LICENSES) - runtime_names)
        raise SystemExit(f"dependency license inventory drift: missing={missing}, stale={stale}")
    inventory = [_check(name, EXPECTED_LICENSES[name]) for name in sorted(runtime_names)]
    optional: list[dict[str, str]] = []
    for name, expected in sorted(OPTIONAL_LICENSES.items()):
        try:
            optional.append(_check(name, expected))
        except PackageNotFoundError:
            continue
    print(json.dumps({"runtime": inventory, "installed_optional": optional}, sort_keys=True))


if __name__ == "__main__":
    main()
