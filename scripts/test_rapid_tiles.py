"""RapidOCRタイルスキャンの試験。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import fitz
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.drawing_text_normalizer import normalize_drawing_text, parse_dimension_token
from drawing_assist.image_preprocessor import prepare_raster_for_rapidocr
from drawing_assist.local_ocr import _ENGINE_LOCK, _engine

PDF = Path(r"c:\Users\SEIZOU20\Desktop\原図.pdf")


def main() -> None:
    doc = fitz.open(PDF)
    page = doc[0]
    zoom = 4.8
    pm = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        colorspace=fitz.csRGB,
        alpha=False,
        annots=False,
    )
    image = prepare_raster_for_rapidocr(
        Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
    )
    sx = image.width / page.rect.width
    sy = image.height / page.rect.height
    tw = min(image.width, max(1200, int(300 * sx)))
    th = min(image.height, max(1000, int(240 * sy)))
    step_x = max(800, int(tw * 0.72))
    step_y = max(680, int(th * 0.70))
    left = int(image.width * 0.03)
    right = int(image.width * 0.95)
    top = int(image.height * 0.08)
    bottom = int(image.height * 0.83)

    lines: list[tuple[str, float, tuple]] = []
    tiles = 0
    t0 = time.perf_counter()
    y = top
    while y < bottom:
        y1 = min(image.height, y + th)
        x = left
        while x < right:
            x1 = min(image.width, x + tw)
            crop = np.asarray(image.crop((x, y, x1, y1)))
            with _ENGINE_LOCK:
                result = _engine()(crop, return_word_box=False)
            boxes = [] if result.boxes is None else result.boxes
            texts = [] if result.txts is None else result.txts
            scores = [] if result.scores is None else result.scores
            for box, text, score in zip(boxes, texts, scores):
                norm = normalize_drawing_text(str(text or "").strip())
                if not norm or float(score or 0) < 0.42 or len(box) < 4:
                    continue
                quad = tuple(
                    ((float(point[0]) + x) / sx, (float(point[1]) + y) / sy)
                    for point in box[:4]
                )
                lines.append((norm, float(score), quad))
            tiles += 1
            if x1 >= right:
                break
            x += step_x
        if y1 >= bottom:
            break
        y += step_y

    parsed = [line for line in lines if parse_dimension_token(line[0])]
    print(f"tiles={tiles} time={time.perf_counter() - t0:.1f}s raw={len(lines)} parsed={len(parsed)}")

    seen: set[tuple] = set()
    for norm, score, quad in sorted(parsed, key=lambda item: -item[1]):
        cx = sum(point[0] for point in quad) / 4
        cy = sum(point[1] for point in quad) / 4
        key = (norm, round(cx / 8), round(cy / 8))
        if key in seen:
            continue
        seen.add(key)
        token = parse_dimension_token(norm)
        assert token is not None
        kind = token.prefix or token.degree or "L"
        print(
            f"  {norm!r} val={token.nominal_value} kind={kind} "
            f"pos=({cx / page.rect.width:.2f},{cy / page.rect.height:.2f}) score={score:.2f}"
        )
    doc.close()


if __name__ == "__main__":
    main()
