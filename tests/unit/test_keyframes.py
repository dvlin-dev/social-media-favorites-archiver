from pathlib import Path

from PIL import Image, ImageDraw

from social_media_favorites_archiver.processors.keyframes import (
    CandidateSource,
    FrameCandidate,
    analyze_frame,
    select_keyframes,
)


def _frame(path: Path, *, background: str, caption: str) -> None:
    image = Image.new("RGB", (320, 180), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 130, 320, 180), fill="white")
    draw.text((10, 145), caption, fill="black")
    image.save(path)


def test_repeated_frames_are_deduplicated_but_changed_burned_captions_survive(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.png"
    repeated_path = tmp_path / "repeated.png"
    changed_caption_path = tmp_path / "changed-caption.png"
    _frame(first_path, background="navy", caption="STEP ONE")
    _frame(repeated_path, background="navy", caption="STEP ONE")
    _frame(changed_caption_path, background="navy", caption="STEP TWO")
    candidates = tuple(
        analyze_frame(
            FrameCandidate(
                timestamp=float(index),
                path=path,
                sources=(CandidateSource.INTERVAL,),
            )
        )
        for index, path in enumerate((first_path, repeated_path, changed_caption_path))
    )

    selected = select_keyframes(candidates, duplicate_distance=3, text_change_distance=2)

    assert [frame.path.name for frame in selected] == ["first.png", "changed-caption.png"]


def test_scene_cut_is_retained_with_interval_candidates(tmp_path: Path) -> None:
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    _frame(before, background="navy", caption="SAME")
    _frame(after, background="orange", caption="SAME")
    candidates = (
        analyze_frame(
            FrameCandidate(
                timestamp=0,
                path=before,
                sources=(CandidateSource.INTERVAL,),
            )
        ),
        analyze_frame(
            FrameCandidate(
                timestamp=1,
                path=after,
                sources=(CandidateSource.SCENE, CandidateSource.INTERVAL),
                scene_score=0.9,
            )
        ),
    )

    selected = select_keyframes(candidates)

    assert len(selected) == 2
    assert CandidateSource.SCENE in selected[1].sources


def test_keyframe_selection_is_deterministic_by_timestamp(tmp_path: Path) -> None:
    one = tmp_path / "one.png"
    two = tmp_path / "two.png"
    _frame(one, background="black", caption="ONE")
    _frame(two, background="white", caption="TWO")
    later = analyze_frame(
        FrameCandidate(timestamp=2, path=two, sources=(CandidateSource.INTERVAL,))
    )
    earlier = analyze_frame(
        FrameCandidate(timestamp=1, path=one, sources=(CandidateSource.INTERVAL,))
    )

    selected = select_keyframes((later, earlier))

    assert [frame.timestamp for frame in selected] == [1, 2]

