from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from social_media_favorites_archiver.models import Asset, AssetKind
from social_media_favorites_archiver.processors.ocr import (
    BoundingBox,
    RapidOCRBackend,
    process_ordered_images,
)


class FixtureEngine:
    def __call__(self, image_path: str) -> tuple[object, float]:
        name = Path(image_path).name
        if "empty" in name:
            return None, 0.01
        return (
            [
                [
                    [[2, 2], [90, 2], [90, 20], [2, 20]],
                    "错别字 OCR",
                    0.94,
                ],
                [
                    [[2, 24], [70, 24], [70, 42], [2, 42]],
                    "Second line",
                    0.71,
                ],
            ],
            0.02,
        )


def _image(path: Path, text: str, *, background: str = "white") -> None:
    image = Image.new("RGB", (160, 80), background)
    ImageDraw.Draw(image).text((5, 5), text, fill="black")
    image.save(path)


def test_ocr_blocks_preserve_box_order_confidence_raw_text_and_corrections(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    _image(path, "fixture")
    backend = RapidOCRBackend(engine=FixtureEngine(), terminology={"错别字": "正确词"})

    result = backend.recognize(path, asset_id="image-1", timestamp=1.5)

    assert [block.ordinal for block in result.blocks] == [0, 1]
    assert result.blocks[0].raw_text == "错别字 OCR"
    assert result.blocks[0].text == "正确词 OCR"
    assert result.blocks[0].confidence == 0.94
    assert result.blocks[0].timestamp == 1.5
    assert result.blocks[0].corrections[0].original == "错别字"
    assert result.blocks[0].provenance[0] == "rapidocr"


def test_bounding_box_requires_four_points() -> None:
    with pytest.raises(ValidationError):
        BoundingBox(points=((0, 0), (1, 0), (1, 1)))


def test_ordered_images_stay_grouped_and_no_text_is_explicit(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    empty = tmp_path / "empty.png"
    _image(first, "first")
    _image(empty, "")
    assets = (
        Asset(
            asset_id="image-1",
            ordinal=0,
            kind=AssetKind.IMAGE,
            local_path=first,
            quality="original",
        ),
        Asset(
            asset_id="image-2",
            ordinal=1,
            kind=AssetKind.IMAGE,
            local_path=empty,
            quality="high",
        ),
    )

    results = process_ordered_images(assets, RapidOCRBackend(engine=FixtureEngine()))

    assert [result.asset_id for result in results] == ["image-1", "image-2"]
    assert results[0].quality == "original"
    assert results[1].blocks == ()
    assert results[1].verified_no_text is True


def test_generated_rotated_and_low_contrast_images_are_valid_inputs(tmp_path: Path) -> None:
    normal = tmp_path / "normal.png"
    low_contrast = tmp_path / "low-contrast.png"
    rotated = tmp_path / "rotated.png"
    _image(normal, "TEXT")
    _image(low_contrast, "TEXT", background="#dddddd")
    with Image.open(normal) as image:
        image.rotate(90, expand=True).save(rotated)

    backend = RapidOCRBackend(engine=FixtureEngine())
    assert backend.recognize(normal, asset_id="normal").blocks
    assert backend.recognize(low_contrast, asset_id="low").blocks
    assert backend.recognize(rotated, asset_id="rotated").blocks
