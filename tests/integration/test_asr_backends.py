import shutil
import subprocess
from pathlib import Path

import pytest

from social_media_favorites_archiver.processors.asr import (
    ASRRequest,
    MlxWhisperBackend,
    TranscriptionOutcome,
    extract_audio,
)


@pytest.mark.heavyweight
def test_mlx_whisper_transcribes_generated_chinese_clip(tmp_path: Path) -> None:
    pytest.importorskip("mlx_whisper")
    say = shutil.which("say")
    if say is None:
        pytest.skip("macOS speech synthesizer is unavailable")
    source = tmp_path / "chinese.aiff"
    audio = tmp_path / "chinese.wav"
    subprocess.run(
        [say, "-v", "Ting-Ting", "这是一个用于本地语音识别测试的短句。", "-o", str(source)],
        check=True,
        timeout=30,
    )
    extract_audio(source, audio, timeout_seconds=30)
    backend = MlxWhisperBackend(model="mlx-community/whisper-tiny")

    result = backend.transcribe(
        audio,
        ASRRequest(language="zh", hotwords=("语音识别",), timeout_seconds=180),
    )

    assert result.outcome == TranscriptionOutcome.SPEECH
    assert result.segments
    assert result.corrected_text
    assert all(
        current.start_time <= current.end_time <= following.start_time
        for current, following in zip(result.segments, result.segments[1:], strict=False)
    )
