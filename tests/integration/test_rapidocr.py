from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from social_media_favorites_archiver.processors.ocr import RapidOCRBackend


@pytest.mark.heavyweight
def test_rapidocr_reads_generated_high_contrast_text(tmp_path: Path) -> None:
    pytest.importorskip("rapidocr_onnxruntime")
    image_path = tmp_path / "ocr.png"
    image = Image.new("RGB", (640, 180), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 64)
    draw.text((30, 45), "HELLO 123", fill="black", font=font)
    image.save(image_path)

    result = RapidOCRBackend().recognize(image_path, asset_id="generated-image")

    combined = " ".join(block.text for block in result.blocks).replace(" ", "")
    assert "HELLO" in combined.upper()
    assert "123" in combined
