from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import logger


def _resolve_local_path(image_ref: str) -> Path | None:
    if not image_ref:
        return None

    parsed = urlparse(image_ref)
    if parsed.scheme in {"http", "https"}:
        return None

    if parsed.scheme == "file":
        return Path(parsed.path)

    return Path(image_ref)


def _is_near_white(r: int, g: int, b: int, threshold: int) -> bool:
    return r >= threshold and g >= threshold and b >= threshold


def crop_outer_white_background(image_ref: str, threshold: int = 248) -> str:
    """Crop transparent/near-white border area from a rendered help image.

    Returns the original image reference when post-processing is not possible.
    """

    if importlib.util.find_spec("PIL") is None:
        logger.warning("[helpmenu] 图片后处理已启用，但未安装 Pillow，跳过裁剪。")
        return image_ref

    image_path = _resolve_local_path(image_ref)
    if image_path is None or not image_path.exists() or not image_path.is_file():
        return image_ref

    try:
        pil_image_module = importlib.import_module("PIL.Image")
        with pil_image_module.open(image_path) as image:
            rgba_image = image.convert("RGBA")
            width, height = rgba_image.size
            pixels = rgba_image.load()

            left = width
            top = height
            right = -1
            bottom = -1

            for y in range(height):
                for x in range(width):
                    r, g, b, alpha = pixels[x, y]
                    # 完全透明像素视作背景，需要被裁掉。
                    if alpha == 0:
                        continue
                    # 白色背景也继续忽略，保持原有裁白边行为。
                    if _is_near_white(r, g, b, threshold):
                        continue
                    if x < left:
                        left = x
                    if y < top:
                        top = y
                    if x > right:
                        right = x
                    if y > bottom:
                        bottom = y

            if right < left or bottom < top:
                return image_ref

            cropped = rgba_image.crop((left, top, right + 1, bottom + 1))
            cropped.save(image_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[helpmenu] 图片后处理失败，跳过裁剪：{type(exc).__name__}: {exc}"
        )
        return image_ref

    return image_ref
