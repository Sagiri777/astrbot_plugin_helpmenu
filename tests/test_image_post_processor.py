import sys
import types
import asyncio
from importlib import util
from pathlib import Path

import pytest

fake_astrbot = types.ModuleType("astrbot")
fake_astrbot_api = types.ModuleType("astrbot.api")
fake_astrbot_api.logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)
fake_astrbot.api = fake_astrbot_api
sys.modules.setdefault("astrbot", fake_astrbot)
sys.modules.setdefault("astrbot.api", fake_astrbot_api)

fake_aiohttp = types.ModuleType("aiohttp")
fake_aiohttp.ClientTimeout = lambda *args, **kwargs: None
fake_aiohttp.ClientSession = object
sys.modules.setdefault("aiohttp", fake_aiohttp)

MODULE_PATH = Path(__file__).resolve().parent.parent / "image_post_processor.py"
SPEC = util.spec_from_file_location("image_post_processor", MODULE_PATH)
assert SPEC and SPEC.loader
IMAGE_POST_PROCESSOR = util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMAGE_POST_PROCESSOR)
crop_outer_white_background = IMAGE_POST_PROCESSOR.crop_outer_white_background


def test_crop_outer_white_background_removes_transparent_border(tmp_path: Path) -> None:
    pil_image = pytest.importorskip("PIL.Image")

    image_path = tmp_path / "transparent.png"
    image = pil_image.new("RGBA", (20, 20), (255, 255, 255, 0))
    for x in range(5, 15):
        for y in range(6, 14):
            image.putpixel((x, y), (255, 0, 0, 255))
    image.save(image_path)

    result_ref = asyncio.run(crop_outer_white_background(str(image_path)))

    assert result_ref == str(image_path)
    with pil_image.open(image_path) as cropped:
        assert cropped.size == (10, 8)


def test_crop_outer_white_background_keeps_missing_file() -> None:
    missing_ref = "/tmp/does-not-exist.png"
    assert asyncio.run(crop_outer_white_background(missing_ref)) == missing_ref


def test_crop_outer_white_background_ignores_near_transparent_edge(
    tmp_path: Path,
) -> None:
    pil_image = pytest.importorskip("PIL.Image")

    image_path = tmp_path / "near_transparent.png"
    image = pil_image.new("RGBA", (18, 12), (255, 255, 255, 0))

    for x in range(2, 16):
        for y in range(2, 10):
            image.putpixel((x, y), (0, 120, 255, 255))

    # Simulate a residual anti-aliased edge produced by browser screenshots.
    for y in range(12):
        image.putpixel((17, y), (255, 255, 255, 6))

    image.save(image_path)

    result_ref = asyncio.run(crop_outer_white_background(str(image_path)))

    assert result_ref == str(image_path)
    with pil_image.open(image_path) as cropped:
        assert cropped.size == (14, 8)


def test_crop_outer_white_background_downloads_remote_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pil_image = pytest.importorskip("PIL.Image")

    source_path = tmp_path / "source.png"
    image = pil_image.new("RGBA", (12, 12), (255, 255, 255, 0))
    for x in range(3, 9):
        for y in range(4, 8):
            image.putpixel((x, y), (20, 120, 220, 255))
    image.save(source_path)

    async def _fake_download(_image_ref: str) -> Path:
        return source_path

    monkeypatch.setattr(IMAGE_POST_PROCESSOR, "_download_remote_image", _fake_download)

    result_ref = asyncio.run(
        crop_outer_white_background("https://example.com/image.png")
    )

    result_path = Path(result_ref)
    assert result_path.exists()
    with pil_image.open(result_path) as cropped:
        assert cropped.size == (6, 4)
