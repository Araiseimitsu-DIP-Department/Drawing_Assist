from __future__ import annotations

from dataclasses import dataclass, replace
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

from drawing_assist.drawing_text_normalizer import normalize_drawing_text
from drawing_assist.image_preprocessor import (
    prepare_raster_for_rapidocr,
    prepare_raster_for_structure,
)
from drawing_assist.ocr_config import (
    DETAIL_ANGLE_ZOOM,
    LOCAL_OCR_MIN_CONFIDENCE,
    LOCAL_OCR_ZOOM_MAX,
    LOCAL_OCR_ZOOM_MIN,
    LOCAL_OCR_ZOOM_NUMERATOR,
    SCANNED_OCR_ZOOM_MAX,
    SCANNED_OCR_ZOOM_MIN,
    SCANNED_OCR_ZOOM_NUMERATOR,
    SCANNED_TILE_ZOOM_MAX,
    SCANNED_TILE_ZOOM_MIN,
    SCANNED_TILE_ZOOM_NUMERATOR,
    SCANNED_TILE_MIN_CONFIDENCE,
)


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


def _restore_rotated_quad(
    box,
    rotation: int,
    *,
    source_width: int,
    source_height: int,
    scale_x: float,
    scale_y: float,
) -> tuple[tuple[float, float], ...]:
    """Map OCR points from a PIL-rotated raster back to PDF coordinates."""

    restored: list[tuple[float, float]] = []
    for point in box[:4]:
        rotated_x = float(point[0])
        rotated_y = float(point[1])
        if rotation == 90:
            source_x = source_width - rotated_y
            source_y = rotated_x
        elif rotation == 270:
            source_x = rotated_y
            source_y = source_height - rotated_x
        else:
            source_x = rotated_x
            source_y = rotated_y
        restored.append((source_x / scale_x, source_y / scale_y))
    return tuple(restored)


def analyze_page(page: fitz.Page, *, scanned: bool = False) -> LocalOcrPage:
    """Run one reusable, fully local OCR pass for an image drawing page."""

    maximum_dimension = max(page.rect.width, page.rect.height)
    if scanned:
        zoom = max(
            SCANNED_OCR_ZOOM_MIN,
            min(SCANNED_OCR_ZOOM_MAX, SCANNED_OCR_ZOOM_NUMERATOR / maximum_dimension),
        )
    else:
        zoom = max(
            LOCAL_OCR_ZOOM_MIN,
            min(LOCAL_OCR_ZOOM_MAX, LOCAL_OCR_ZOOM_NUMERATOR / maximum_dimension),
        )
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        colorspace=fitz.csRGB,
        alpha=False,
        annots=False,
    )
    source_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    # Windows OCR と同等のコントラスト補正を軽量版で適用する。
    image = prepare_raster_for_rapidocr(source_image)
    structure_image = prepare_raster_for_structure(source_image)
    array = np.asarray(image)
    with _ENGINE_LOCK:
        result = _engine()(array, return_word_box=False)

        # Dimension text is commonly printed vertically. Explicitly rotate
        # the complete page once so the recognizer sees those glyphs as an
        # ordinary horizontal line. RapidOCR's angle classifier handles the
        # remaining 180-degree direction, so a second vertical pass is not
        # necessary.
        rotated_result = (
            _engine()(
                np.asarray(image.rotate(90, expand=True, fillcolor="white")),
                return_word_box=False,
            )
            if scanned
            else None
        )

        # Long-line cleanup recovers text joined to drafting lines, while the
        # conservative raster remains better for some faint or diagonal text.
        # Keep both observations on scanned drawings and let the geometric
        # candidate filter reject anything that is not a real dimension.
        legacy_result = (
            _engine()(np.asarray(structure_image), return_word_box=False)
            if scanned
            else None
        )

    scale_x = image.width / page.rect.width
    scale_y = image.height / page.rect.height
    lines: list[LocalOcrLine] = []
    for ocr_result, rotation in (
        (result, 0),
        (legacy_result, 0),
        (rotated_result, 90),
    ):
        if ocr_result is None:
            continue
        boxes = ocr_result.boxes if ocr_result.boxes is not None else []
        texts = ocr_result.txts if ocr_result.txts is not None else []
        scores = ocr_result.scores if ocr_result.scores is not None else []
        for box, text, score in zip(boxes, texts, scores):
            normalized = normalize_drawing_text(str(text or "").strip())
            confidence = float(score or 0.0)
            if (
                not normalized
                or confidence < LOCAL_OCR_MIN_CONFIDENCE
                or len(box) < 4
            ):
                continue
            quad = _restore_rotated_quad(
                box,
                rotation,
                source_width=image.width,
                source_height=image.height,
                scale_x=scale_x,
                scale_y=scale_y,
            )
            lines.append(LocalOcrLine(normalized, confidence, quad))
    return LocalOcrPage(
        image.width,
        image.height,
        scale_x,
        scale_y,
        structure_image,
        merge_ocr_lines(tuple(lines)),
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
    zoom = DETAIL_ANGLE_ZOOM

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
    zoom = DETAIL_ANGLE_ZOOM
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


def _line_center(line: LocalOcrLine) -> tuple[float, float]:
    return (
        (line.rect[0] + line.rect[2]) / 2,
        (line.rect[1] + line.rect[3]) / 2,
    )


def merge_ocr_lines(
    *groups: tuple[LocalOcrLine, ...],
) -> tuple[LocalOcrLine, ...]:
    """複数OCR結果を重複除去して統合する。"""

    def _text_quality(text: str) -> tuple[int, int, float]:
        compact = unicodedata.normalize("NFKC", text).replace(" ", "")
        quality = 0
        if "±" in compact or "士" in compact:
            quality += 3
        if re.search(r"[±士]\d+\.\d+", compact):
            quality += 2
        if re.search(r"[±士]\d{2,}(?:\D|$)", compact):
            quality -= 2
        if re.search(r"[±+\-]0\.?$", compact):
            quality -= 3
        stem = re.split(r"[±+\-－−士土]", compact, maxsplit=1)[0]
        digit_count = len(re.sub(r"\D", "", stem))
        quality += min(5, digit_count)
        # 接頭辞付きは寸法らしい
        if re.match(r"^[φΦØ⌀CR]", compact):
            quality += 1
        return (quality, len(compact), 0.0)

    def _same_reading_family(left: str, right: str) -> bool:
        """同一寸法の別読み（C.15 vs 0.15±0.025 等）かどうか。"""

        left_nums = re.findall(r"\d+(?:\.\d+)?", left)
        right_nums = re.findall(r"\d+(?:\.\d+)?", right)
        if not left_nums or not right_nums:
            return False
        try:
            return abs(float(left_nums[0]) - float(right_nums[0])) < 1e-6
        except ValueError:
            return False

    def _likely_digit_truncation(left: str, right: str) -> bool:
        """47.85 と 7.85 のように先頭桁が欠けた同一寸法かどうか。"""

        left_nums = re.findall(r"\d+(?:\.\d+)?", left)
        right_nums = re.findall(r"\d+(?:\.\d+)?", right)
        if not left_nums or not right_nums:
            return False
        left_digits = left_nums[0].replace(".", "")
        right_digits = right_nums[0].replace(".", "")
        if left_digits == right_digits:
            return False
        return left_digits.endswith(right_digits) or right_digits.endswith(left_digits)

    merged: list[LocalOcrLine] = []
    for group in groups:
        for line in group:
            center = _line_center(line)
            duplicate = False
            for index, existing in enumerate(merged):
                existing_center = _line_center(existing)
                distance = math.dist(center, existing_center)
                if existing.text == line.text and distance <= 18.0:
                    if line.score > existing.score:
                        merged[index] = line
                    duplicate = True
                    break
                # ほぼ同位置かつ同一寸法の別読みは、公差表記が整っている方を残す
                if (
                    distance <= 10.0
                    and _same_reading_family(existing.text, line.text)
                ):
                    line_quality = (
                        _text_quality(line.text)[0],
                        len(line.text),
                        line.score,
                    )
                    existing_quality = (
                        _text_quality(existing.text)[0],
                        len(existing.text),
                        existing.score,
                    )
                    if line_quality > existing_quality:
                        merged[index] = line
                    duplicate = True
                    break
                # 同位置の桁欠け（47.85 vs 7.85）だけを統合する
                if distance <= 8.0 and _likely_digit_truncation(
                    existing.text, line.text
                ):
                    if _text_quality(line.text) > _text_quality(existing.text):
                        merged[index] = line
                    duplicate = True
                    break
            if not duplicate:
                merged.append(line)
    return tuple(merged)


_EXPLICIT_TOLERANCE_IN_LINE = re.compile(
    r"(?:±|士|亇|干|土|\+\s*\d|[−－一-]\s*\d)",
)
_TOLERANCE_ONLY_LINE = re.compile(
    r"^[±+\-−－]?\s*\d+(?:[.,]\d+)?$",
)
_NOMINAL_START = re.compile(
    r"^[φΦØ⌀RCＲＣ（(]?\d",
)


def _line_rect(line: LocalOcrLine) -> fitz.Rect:
    return fitz.Rect(line.rect)


def join_nearby_tolerance_ocr_lines(
    lines: tuple[LocalOcrLine, ...],
) -> tuple[LocalOcrLine, ...]:
    """寸法値と分離して読まれた公差記号を結合する。"""

    def _try_pair(
        left: LocalOcrLine,
        right: LocalOcrLine,
    ) -> float | None:
        left_text = unicodedata.normalize("NFKC", left.text)
        right_text = unicodedata.normalize("NFKC", right.text)
        if _EXPLICIT_TOLERANCE_IN_LINE.search(left_text):
            return None
        if not _NOMINAL_START.search(left_text.lstrip("△▲A")):
            return None
        right_stripped = right_text.strip()
        # 右辺は公差断片のみ。Φ16±0.1 のような完成寸法は巻き込まない
        if _NOMINAL_START.search(right_stripped.lstrip("△▲A")) and (
            _EXPLICIT_TOLERANCE_IN_LINE.search(right_stripped)
            or len(right_stripped) > 6
        ):
            return None
        left_needs_tol_digits = bool(
            re.search(r"[±士土亇干]0?\.$", left_text)
            or left_text.rstrip().endswith(("±", "士", "土"))
        )
        if not (
            right_stripped.startswith(("±", "+", "-", "＋", "－", "士", "土", "亇", "干"))
            or _TOLERANCE_ONLY_LINE.fullmatch(right_stripped)
            or (left_needs_tol_digits and re.fullmatch(r"\d{1,3}", right_stripped))
        ):
            return None
        left_rect = _line_rect(left)
        right_rect = _line_rect(right)
        if left_rect.is_empty or right_rect.is_empty:
            return None
        min_height = max(1.0, min(left_rect.height, right_rect.height))
        vertical_overlap = min(left_rect.y1, right_rect.y1) - max(
            left_rect.y0, right_rect.y0
        )
        horizontal_overlap = min(left_rect.x1, right_rect.x1) - max(
            left_rect.x0, right_rect.x0
        )
        horizontal_gap = right_rect.x0 - left_rect.x1
        vertical_gap = right_rect.y0 - left_rect.y1
        horizontal_match = (
            vertical_overlap >= min_height * 0.3
            and right_rect.x0 >= left_rect.x0 - left_rect.height * 0.6
            and -left_rect.height * 0.5
            <= horizontal_gap
            <= max(18.0, left_rect.height * 3.2)
        )
        vertical_match = (
            horizontal_overlap >= min(left_rect.width, right_rect.width) * 0.35
            and -min_height * 0.35
            <= vertical_gap
            <= max(18.0, left_rect.height * 3.2)
            and abs((left_rect.x0 + left_rect.x1) / 2 - (right_rect.x0 + right_rect.x1) / 2)
            <= max(left_rect.width, right_rect.width) * 0.75
        )
        if not horizontal_match and not vertical_match:
            return None
        return right.score

    joined: list[LocalOcrLine] = []
    consumed: set[int] = set()
    indexed = list(enumerate(lines))
    for left_index, left in indexed:
        if left_index in consumed:
            continue
        best: tuple[float, int, LocalOcrLine] | None = None
        for right_index, right in indexed:
            if right_index == left_index or right_index in consumed:
                continue
            score = _try_pair(left, right)
            if score is None:
                continue
            if best is None or score > best[0]:
                best = (score, right_index, right)
        if best is None:
            continue
        _, right_index, right = best
        left_text = unicodedata.normalize("NFKC", left.text)
        merged_text = normalize_drawing_text(f"{left_text}{right.text}")
        xs = [point[0] for point in (*left.quad, *right.quad)]
        ys = [point[1] for point in (*left.quad, *right.quad)]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        merged_quad = (
            (x0, y0),
            (x1, y0),
            (x1, y1),
            (x0, y1),
        )
        joined.append(
            LocalOcrLine(
                merged_text,
                max(left.score, right.score),
                merged_quad,
            )
        )
        consumed.add(left_index)
        consumed.add(right_index)

    # 結合済み断片は落とすが、未結合の完成寸法は必ず残す
    retained = [line for index, line in indexed if index not in consumed]
    return tuple(retained + joined)


def enrich_scanned_ocr_page(
    ocr_page: LocalOcrPage,
    tile_lines: tuple[LocalOcrLine, ...] | None = None,
) -> LocalOcrPage:
    """ページOCRとタイルOCRを統合し、分離公差を結合する。"""

    merged = merge_ocr_lines(ocr_page.lines, tile_lines or ())
    # 数値 + ± + 公差値 の3分割にも対応するため2回結合する
    combined = join_nearby_tolerance_ocr_lines(merged)
    combined = join_nearby_tolerance_ocr_lines(combined)
    return replace(ocr_page, lines=combined)


def build_tile_ocr_page(
    page: fitz.Page,
    tile_lines: tuple[LocalOcrLine, ...],
) -> LocalOcrPage:
    """ページOCRが使えないとき、タイル結果だけで検出用ページを作る。"""

    maximum_dimension = max(page.rect.width, page.rect.height)
    zoom = max(
        SCANNED_OCR_ZOOM_MIN,
        min(SCANNED_OCR_ZOOM_MAX, SCANNED_OCR_ZOOM_NUMERATOR / maximum_dimension),
    )
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        colorspace=fitz.csRGB,
        alpha=False,
        annots=False,
    )
    source_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    image = prepare_raster_for_rapidocr(source_image)
    combined = join_nearby_tolerance_ocr_lines(tile_lines)
    combined = join_nearby_tolerance_ocr_lines(combined)
    return LocalOcrPage(
        width=image.width,
        height=image.height,
        scale_x=image.width / page.rect.width,
        scale_y=image.height / page.rect.height,
        image=image,
        lines=combined,
    )


def analyze_scanned_page_tiles(page: fitz.Page) -> tuple[LocalOcrLine, ...]:
    """画像PDFの図面領域を高解像度タイルOCRで読み取る。"""

    maximum_dimension = max(page.rect.width, page.rect.height)
    zoom = max(
        SCANNED_TILE_ZOOM_MIN,
        min(SCANNED_TILE_ZOOM_MAX, SCANNED_TILE_ZOOM_NUMERATOR / maximum_dimension),
    )
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        colorspace=fitz.csRGB,
        alpha=False,
        annots=False,
    )
    source_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    image = prepare_raster_for_rapidocr(source_image)
    scale_x = image.width / page.rect.width
    scale_y = image.height / page.rect.height
    tile_width = min(image.width, max(1000, int(260 * scale_x)))
    tile_height = min(image.height, max(860, int(200 * scale_y)))
    step_x = max(520, int(tile_width * 0.48))
    step_y = max(440, int(tile_height * 0.45))
    left = int(image.width * 0.03)
    right = int(image.width * 0.95)
    top = int(image.height * 0.08)
    # 下端付近の公差付き寸法も拾う（色分け側の bottom_limit=0.93 と揃える）
    bottom = int(image.height * 0.93)

    lines: list[LocalOcrLine] = []
    y = top
    while y < bottom:
        y1 = min(image.height, y + tile_height)
        x = left
        while x < right:
            x1 = min(image.width, x + tile_width)
            crop_image = image.crop((x, y, x1, y1))
            crop = np.asarray(crop_image)
            with _ENGINE_LOCK:
                result = _engine()(crop, return_word_box=False)
            # 縦書き寸法はページ全体の90度パスで拾う。
            # タイルごとの回転OCRは処理時間が倍増するため行わない。
            boxes = [] if result.boxes is None else result.boxes
            texts = [] if result.txts is None else result.txts
            scores = [] if result.scores is None else result.scores
            for box, text, score in zip(boxes, texts, scores):
                normalized = normalize_drawing_text(str(text or "").strip())
                confidence = float(score or 0.0)
                if (
                    not normalized
                    or confidence < SCANNED_TILE_MIN_CONFIDENCE
                    or len(box) < 4
                ):
                    continue
                quad = tuple(
                    (
                        (float(point[0]) + x) / scale_x,
                        (float(point[1]) + y) / scale_y,
                    )
                    for point in box[:4]
                )
                lines.append(LocalOcrLine(normalized, confidence, quad))
            if x1 >= right:
                break
            x += step_x
        if y1 >= bottom:
            break
        y += step_y
    return merge_ocr_lines(tuple(lines))
