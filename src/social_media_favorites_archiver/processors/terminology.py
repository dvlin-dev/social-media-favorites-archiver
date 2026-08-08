"""Deterministic, auditable terminology correction."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field


class TerminologyCorrection(BaseModel):
    model_config = ConfigDict(frozen=True)

    original: str = Field(min_length=1)
    replacement: str = Field(min_length=1)
    occurrences: int = Field(ge=1)


class TerminologyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw_text: str
    corrected_text: str
    corrections: tuple[TerminologyCorrection, ...] = ()


class TerminologyCorrector:
    """Apply explicit replacements in configured order and record every change."""

    def __init__(self, replacements: Mapping[str, str] | None = None) -> None:
        self.replacements: tuple[tuple[str, str], ...] = tuple((replacements or {}).items())
        if any(not original or not replacement for original, replacement in self.replacements):
            msg = "terminology replacements must be non-empty"
            raise ValueError(msg)

    def apply(self, text: str) -> TerminologyResult:
        corrected = text
        changes: list[TerminologyCorrection] = []
        for original, replacement in self.replacements:
            occurrences = corrected.count(original)
            if occurrences == 0 or original == replacement:
                continue
            corrected = corrected.replace(original, replacement)
            changes.append(
                TerminologyCorrection(
                    original=original,
                    replacement=replacement,
                    occurrences=occurrences,
                )
            )
        return TerminologyResult(
            raw_text=text,
            corrected_text=corrected,
            corrections=tuple(changes),
        )

