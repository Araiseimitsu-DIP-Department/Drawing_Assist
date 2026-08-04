from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import logging
import math
import re
from threading import RLock
import unicodedata

import fitz
import numpy as np
from PIL import Image


@dataclass(frozen=True)
class LocalOcrLine:
    text: str
    score: float
    quad: tuple[tuple[float, float], ...]

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return (
            min(point[0] for point in self.quad),
            min(point[1] for point in self.quad),
            max(point[0] for point in self.quad),
            max(point[1] for point in self.quad),
        )

    @property
    def direction(self) -> tuple[float, float]:
        start = self.quad[0]
        end = self.quad[1]
        length = math.dist(start, end) or 1.0
        return ((end[0] - start[0]) / length, (end[1] - start[1]) / length)


@dataclass(frozen=True)
class LocalOcrPage:
    width: int
    height: int
    scale_x: float
    scale_y: float
    image: Image.Image
    lines: tuple[LocalOcrLine, ...]


_ENGINE_LOCK = RLock()


@lru_cache(maxsize=1)
def _engine():
    # RapidOCR ships its ONNX models inside the Python package.  PyInstaller
    # collects those files into the application, so client PCs never download
    # a model and do not need a Windows Japanese language pack.
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    from rapidocr import RapidOCR

    return RapidOCR()


def local_ocr_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import rapidocr  # noqa: F401
    except ImportError:
        return False
    return True


def analyze_page(page: fitz.Page) -> LocalOcrPage:
    """Run one reusable, fully local OCR pass for an image drawing page."""

    maximum_dimension = max(page.rect.width, page.rect.height)
    zoom = max(1.8, min(2.2, 2400 / maximum_dimension))
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        colorspace=fitz.csRGB,
        alpha=False,
        annots=False,
    )
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    # Passing an ndarray avoids a temporary PNG and its disk I/O.
    array = np.asarray(image)
    with _ENGINE_LOCK:
        result = _engine()(array, return_word_box=False)

    boxes = result.boxes if result.boxes is not None else []
    texts = result.txts if result.txts is not None else []
    scores = result.scores if result.scores is not None else []
    scale_x = image.width / page.rect.width
    scale_y = image.height / page.rect.height
    lines: list[LocalOcrLine] = []
    for box, text, score in zip(boxes, texts, scores):
        normalized = str(text or "").strip()
        confidence = float(score or 0.0)
        if not normalized or confidence < 0.42 or len(box) < 4:
            continue
        quad = tuple(
            (float(point[0]) / scale_x, float(point[1]) / scale_y)
            for point in box[:4]
        )
        lines.append(LocalOcrLine(normalized, confidence, quad))
    return LocalOcrPage(
        image.width,
        image.height,
        scale_x,
        scale_y,
        image,
        tuple(lines),
    )


def _analyze_detail_angles_serial(
    page: fitz.Page,
    ocr_page: LocalOcrPage,
) -> tuple[LocalOcrLine, ...]:
    """Read steep angle callouts above detail-view captions."""

    caption_pattern = re.compile(
        r"^[A-Z]{1,2}.*(?:\u8a73|\u8be6|\u8a73\u7d30|\u8be6\u7ec6)"
    )
    angle_pattern = re.compile(
        r"^[^0-9]{0,2}(\d{1,3})(?:\s*[\u00b0\u00ba])?$"
    )
    common_angles = {15, 30, 45, 60, 90, 120}
    results: list[LocalOcrLine] = []
    zoom = 4.0

    for caption in ocr_page.lines:
        caption_text = unicodedata.normalize("NFKC", caption.text).upper()
        if caption_pattern.search(caption_text) is None:
            continue
        caption_rect = fitz.Rect(caption.rect)
        center_x = (caption_rect.x0 + caption_rect.x1) / 2
        clip = fitz.Rect(
            center_x - 80,
            caption_rect.y0 - 120,
            center_x + 80,
            caption_rect.y1 + 5,
        ) & page.rect
        if clip.is_empty:
            continue
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            clip=clip,
            colorspace=fitz.csRGB,
            alpha=False,
            annots=False,
        )
        source = Image.frombytes(
            "RGB", (pixmap.width, pixmap.height), pixmap.samples
        )
        passes: list[tuple[float, object]] = []
        rotated = source.rotate(-90, expand=True, fillcolor="white")
        with _ENGINE_LOCK:
            first_result = _engine()(
                np.asarray(rotated), return_word_box=False
            )
        passes.append((-90.0, first_result))
        first_texts = [
            unicodedata.normalize("NFKC", str(value or "")).strip()
            for value in (
                first_result.txts
                if first_result.txts is not None
                else []
            )
        ]
        first_has_angle = any(
            angle_pattern.fullmatch(value)
            and int(angle_pattern.fullmatch(value).group(1)) in common_angles
            for value in first_texts
        )
        # A steep 30-degree callout is sometimes split into a bare zero by the
        # clockwise pass.  Retry only that suspicious region at its natural
        # slant; this recovers it without doubling every detail-view OCR call.
        if not first_has_angle and any(value in {"0", "3"} for value in first_texts):
            rotated = source.rotate(75, expand=True, fillcolor="white")
            with _ENGINE_LOCK:
                second_result = _engine()(
                    np.asarray(rotated), return_word_box=False
                )
            passes.append((75.0, second_result))

        for rotation, ocr_result in passes:
            boxes = ocr_result.boxes if ocr_result.boxes is not None else []
            texts = ocr_result.txts if ocr_result.txts is not None else []
            scores = ocr_result.scores if ocr_result.scores is not None else []
            rotated_width = float(
                source.rotate(rotation, expand=True).width
            )
            rotated_height = float(
                source.rotate(rotation, expand=True).height
            )
            radians = math.radians(rotation)
            cosine = math.cos(radians)
            sine = math.sin(radians)
            for box, raw_text, raw_score in zip(boxes, texts, scores):
                text = unicodedata.normalize(
                    "NFKC", str(raw_text or "")
                ).strip()
                score = float(raw_score or 0.0)
                match = angle_pattern.fullmatch(text)
                if match is None or score < 0.68:
                    continue
                value = int(match.group(1))
                if value not in common_angles:
                    continue
                page_points: list[tuple[float, float]] = []
                for point in box[:4]:
                    shifted_x = float(point[0]) - rotated_width / 2
                    shifted_y = float(point[1]) - rotated_height / 2
                    source_x = (
                        shifted_x * cosine - shifted_y * sine
                        + source.width / 2
                    )
                    source_y = (
                        shifted_x * sine + shifted_y * cosine
                        + source.height / 2
                    )
                    page_points.append(
                        (
                            clip.x0 + source_x / zoom,
                            clip.y0 + source_y / zoom,
                        )
                    )
                line = LocalOcrLine(
                    f"{value}\u00b0", score, tuple(page_points)
                )
                line_center = (
                    (line.rect[0] + line.rect[2]) / 2,
                    (line.rect[1] + line.rect[3]) / 2,
                )
                if any(
                    math.dist(
                        line_center,
                        (
                            (existing.rect[0] + existing.rect[2]) / 2,
                            (existing.rect[1] + existing.rect[3]) / 2,
                        ),
                    )
                    < 8
                    for existing in results
                ):
                    continue
                results.append(line)
    return tuple(results)


def analyze_detail_angles(
    page: fitz.Page,
    ocr_page: LocalOcrPage,
) -> tuple[LocalOcrLine, ...]:
    """Read steep detail-view angles with parallel local OCR crops."""

    caption_pattern = re.compile(
        r"^[A-Z]{1,2}.*(?:\u8a73|\u8be6|\u8a73\u7d30|\u8be6\u7ec6)"
    )
    angle_pattern = re.compile(
        r"^[^0-9]{0,2}(\d{1,3})(?:\s*[\u00b0\u00ba])?$"
    )
    common_angles = {15, 30, 45, 60, 90, 120}
    zoom = 4.0
    regions: list[tuple[fitz.Rect, Image.Image]] = []
    for caption in ocr_page.lines:
        caption_text = unicodedata.normalize("NFKC", caption.text).upper()
        if caption_pattern.search(caption_text) is None:
            continue
        caption_rect = fitz.Rect(caption.rect)
        # Title-block and note text is not a detail-view angle source.
        if caption_rect.y0 > page.rect.height * 0.45:
            continue
        center_x = (caption_rect.x0 + caption_rect.x1) / 2
        clip = fitz.Rect(
            center_x - 80,
            caption_rect.y0 - 120,
            center_x + 80,
            caption_rect.y1 + 5,
        ) & page.rect
        if clip.is_empty:
            continue
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            clip=clip,
            colorspace=fitz.csRGB,
            alpha=False,
            annots=False,
        )
        regions.append(
            (
                clip,
                Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples
                ),
            )
        )

    def read_region(region: tuple[fitz.Rect, Image.Image]) -> list[LocalOcrLine]:
        clip, source = region

        def run(rotation: float):
            rotated = source.rotate(rotation, expand=True, fillcolor="white")
            # ONNX Runtime sessions support concurrent inference. Each RapidOCR
            # call owns its result, so detail crops can use all available cores.
            return rotation, rotated.size, _engine()(
                np.asarray(rotated), return_word_box=False
            )

        passes = [run(-90.0)]
        first_result = passes[0][2]
        first_texts = [
            unicodedata.normalize("NFKC", str(value or "")).strip()
            for value in (
                first_result.txts
                if first_result.txts is not None
                else []
            )
        ]
        first_has_angle = any(
            angle_pattern.fullmatch(value)
            and int(angle_pattern.fullmatch(value).group(1)) in common_angles
            for value in first_texts
        )
        if not first_has_angle and any(value in {"0", "3"} for value in first_texts):
            passes.append(run(75.0))

        found: list[LocalOcrLine] = []
        for rotation, rotated_size, ocr_result in passes:
            boxes = ocr_result.boxes if ocr_result.boxes is not None else []
            texts = ocr_result.txts if ocr_result.txts is not None else []
            scores = ocr_result.scores if ocr_result.scores is not None else []
            radians = math.radians(rotation)
            cosine = math.cos(radians)
            sine = math.sin(radians)
            for box, raw_text, raw_score in zip(boxes, texts, scores):
                text = unicodedata.normalize(
                    "NFKC", str(raw_text or "")
                ).strip()
                match = angle_pattern.fullmatch(text)
                score = float(raw_score or 0.0)
                if match is None or score < 0.68:
                    continue
                value = int(match.group(1))
                if value not in common_angles:
                    continue
                points: list[tuple[float, float]] = []
                for point in box[:4]:
                    shifted_x = float(point[0]) - rotated_size[0] / 2
                    shifted_y = float(point[1]) - rotated_size[1] / 2
                    source_x = (
                        shifted_x * cosine - shifted_y * sine
                        + source.width / 2
                    )
                    source_y = (
                        shifted_x * sine + shifted_y * cosine
                        + source.height / 2
                    )
                    points.append(
                        (
                            clip.x0 + source_x / zoom,
                            clip.y0 + source_y / zoom,
                        )
                    )
                found.append(
                    LocalOcrLine(f"{value}\u00b0", score, tuple(points))
                )
        return found

    if not regions:
        return ()
    with ThreadPoolExecutor(max_workers=min(4, len(regions))) as executor:
        observed = [line for group in executor.map(read_region, regions) for line in group]
    deduplicated: list[LocalOcrLine] = []
    for line in observed:
        center = (
            (line.rect[0] + line.rect[2]) / 2,
            (line.rect[1] + line.rect[3]) / 2,
        )
        if any(
            math.dist(
                center,
                (
                    (existing.rect[0] + existing.rect[2]) / 2,
                    (existing.rect[1] + existing.rect[3]) / 2,
                ),
            )
            < 8
            for existing in deduplicated
        ):
            continue
        deduplicated.append(line)
    return tuple(deduplicated)
