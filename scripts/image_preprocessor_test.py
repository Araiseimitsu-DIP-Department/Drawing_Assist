"""Smoke-test OpenCV raster preprocessing used before local OCR."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.image_preprocessor import prepare_raster_for_rapidocr


def main() -> None:
    image = Image.new("RGB", (900, 500), (225, 222, 212))
    draw = ImageDraw.Draw(image)
    draw.line((35, 260, 865, 260), fill=(102, 102, 98), width=2)
    draw.text((220, 225), "50.0 +-0.05", fill=(70, 70, 68))
    result = prepare_raster_for_rapidocr(image)
    values = np.asarray(result.convert("L"))
    assert result.mode == "RGB"
    assert result.size == image.size
    assert values.min() < 100, "fine drawing/text pixels were lost"
    assert values.max() > 200, "background separation was lost"
    print("OpenCV OCR preprocessing: OK")


if __name__ == "__main__":
    main()
