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

from drawing_assist.drawing_text_normalizer import (
    normalize_drawing_text,
    parse_dimension_token,
)
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
    agreement_count: int = 1

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
    structure_image = image
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

        # 通常画像で読めた文字数にかかわらず、罫線除去版も全頁OCRする。
        # この図面では後者が 15° と細い寸法公差を補うため、行数だけで
        # 省略すると候補数・精度が下がる。
        if scanned:
            structure_image = prepare_raster_for_structure(source_image)
            legacy_result = _engine()(
                np.asarray(structure_image), return_word_box=False
            )
        else:
            legacy_result = None

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

    caption_pattern = re.compile(r"^[A-Z]{1,2}.*(?:\u8a73|\u8be6)")
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
    """Read dimensions in detail views with parallel rotated OCR crops."""

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
        search = fitz.Rect(
            center_x - 90,
            caption_rect.y0 - 125,
            center_x + 90,
            caption_rect.y0 - 2,
        ) & page.rect
        if search.is_empty:
            continue
        tile_width = min(120.0, search.width)
        # Keep the complete vertical search band in one crop. The previous
        # pair of 105-point crops overlapped by more than 80%, doubling OCR
        # work while observing essentially the same callouts.
        tile_height = search.height
        x_starts = {
            search.x0,
            max(search.x0, search.x1 - tile_width),
        }
        y_starts = {search.y0}
        for tile_y in sorted(y_starts):
            for tile_x in sorted(x_starts):
                clip = fitz.Rect(
                    tile_x,
                    tile_y,
                    min(search.x1, tile_x + tile_width),
                    min(search.y1, tile_y + tile_height),
                )
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
            # RapidOCR keeps mutable preprocessing state. Serialize calls to
            # the single packaged engine so results cannot cross between
            # crops and ONNX sessions do not oversubscribe the client CPU.
            with _ENGINE_LOCK:
                result = _engine()(
                    np.asarray(rotated), return_word_box=False
                )
            return rotation, rotated.size, result

        # Detail-view callouts are often printed vertically or diagonally.
        # A fixed -90/75 pair misses common 15-degree text while a compact
        # rotation ensemble recovers it without relying on drawing-specific
        # coordinates or values.
        passes = [
            run(rotation)
            for rotation in (-45.0, -30.0, 0.0, 45.0)
        ]

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
                score = float(raw_score or 0.0)
                if score < 0.68:
                    continue
                normalized = normalize_drawing_text(text)
                match = angle_pattern.fullmatch(text)
                parsed = parse_dimension_token(normalized)
                output_text: str | None = None
                if match is not None:
                    value = int(match.group(1))
                    if value in common_angles:
                        output_text = f"{value}\u00b0"
                if output_text is None and parsed is not None:
                    output_text = parsed.normalized_text
                if output_text is None:
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
                    LocalOcrLine(output_text, score, tuple(points))
                )
        return found

    if not regions:
        return ()
    with ThreadPoolExecutor(max_workers=1) as executor:
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
                    merged[index] = replace(
                        existing if existing.score >= line.score else line,
                        agreement_count=existing.agreement_count
                        + line.agreement_count,
                    )
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
                        merged[index] = replace(
                            line,
                            agreement_count=existing.agreement_count
                            + line.agreement_count,
                        )
                    else:
                        merged[index] = replace(
                            existing,
                            agreement_count=existing.agreement_count
                            + line.agreement_count,
                        )
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
_NOMINAL_START = re.compile(
    r"^[φΦØ⌀RCＲＣ（(]?\d",
)


def _line_rect(line: LocalOcrLine) -> fitz.Rect:
    return fitz.Rect(line.rect)


_DIAMETER_ONLY = re.compile(r"^[φΦØ⌀]$")
_INCOMPLETE_DECIMAL = re.compile(r"^[φΦØ⌀RCＲＣ]?\d+\.$")
_SINGLE_FRACTION_DIGIT = re.compile(r"^[1-9]$")
_PAIR_PREFIX = 4
_PAIR_DECIMAL = 3
_PAIR_SIGNED_TOLERANCE = 2
_PAIR_BARE_TOLERANCE = 1


def _fragments_are_nearby(left: LocalOcrLine, right: LocalOcrLine) -> bool:
    """同一寸法の断片として近いか。縦書きは横方向へ広げない。"""

    left_rect = _line_rect(left)
    right_rect = _line_rect(right)
    if left_rect.is_empty or right_rect.is_empty:
        return False
    parallel = abs(
        left.direction[0] * right.direction[0]
        + left.direction[1] * right.direction[1]
    )
    if parallel < 0.72:
        return False
    min_height = max(1.0, min(left_rect.height, right_rect.height))
    vertical_overlap = min(left_rect.y1, right_rect.y1) - max(
        left_rect.y0, right_rect.y0
    )
    horizontal_overlap = min(left_rect.x1, right_rect.x1) - max(
        left_rect.x0, right_rect.x0
    )
    horizontal_gap = right_rect.x0 - left_rect.x1
    if vertical_overlap > 0:
        stack_gap = 0.0
    else:
        stack_gap = max(left_rect.y0, right_rect.y0) - min(
            left_rect.y1, right_rect.y1
        )
    horizontal_match = (
        vertical_overlap >= min_height * 0.3
        and right_rect.x0 >= left_rect.x0 - left_rect.height * 0.6
        and -left_rect.height * 0.5
        <= horizontal_gap
        <= max(18.0, left_rect.height * 3.2)
    )
    vertical_match = (
        horizontal_overlap >= min(left_rect.width, right_rect.width) * 0.35
        and stack_gap <= max(18.0, left_rect.height * 3.2)
        and abs(
            (left_rect.x0 + left_rect.x1) / 2 - (right_rect.x0 + right_rect.x1) / 2
        )
        <= max(left_rect.width, right_rect.width) * 0.75
    )
    # 縦長の断片は列方向だけ結合する。高さに比例した横ギャップだと
    # 隣接する別寸法（例: 12. と 16.05）を巻き込む。
    # ただし「5.」+「7」のような横書きの小数点続きは、右辺が細長い1桁でも結合する。
    left_text = unicodedata.normalize("NFKC", left.text)
    right_text = unicodedata.normalize("NFKC", right.text).strip()
    decimal_continuation = horizontal_match and _SINGLE_FRACTION_DIGIT.fullmatch(
        right_text
    ) and (
        _INCOMPLETE_DECIMAL.fullmatch(left_text.lstrip("△▲A")) is not None
        or re.search(
            r"(?:Rz\s*max|Rzmax|Ramax|Rmax|Ra)\s*\d+\.$",
            left_text,
            re.IGNORECASE,
        )
        is not None
    )
    if decimal_continuation:
        return True
    if _ocr_line_is_columnar(left) or _ocr_line_is_columnar(right):
        return vertical_match
    return horizontal_match or vertical_match


def _merge_dimension_fragment_text(left_text: str, right_text: str) -> str:
    """公差記号の重複（2.5+ と +0.25）を潰して結合する。"""

    left_stripped = unicodedata.normalize("NFKC", left_text).strip()
    right_stripped = unicodedata.normalize("NFKC", right_text).strip()
    if left_stripped[-1:] in "+-＋－" and right_stripped[:1] in "+-＋－±":
        right_stripped = right_stripped[1:]
    return normalize_drawing_text(f"{left_stripped}{right_stripped}")


def join_nearby_tolerance_ocr_lines(
    lines: tuple[LocalOcrLine, ...],
    *,
    mode: str = "all",
) -> tuple[LocalOcrLine, ...]:
    """寸法値と分離して読まれた公差記号・小数点断片を結合する。"""

    def _try_pair(
        left: LocalOcrLine,
        right: LocalOcrLine,
    ) -> tuple[int, float] | None:
        left_text = unicodedata.normalize("NFKC", left.text)
        right_text = unicodedata.normalize("NFKC", right.text)
        right_stripped = right_text.strip()
        if not _fragments_are_nearby(left, right):
            return None
        # φ と 12.9 が別行になった縦寸法を先に戻す
        if _DIAMETER_ONLY.fullmatch(left_text.lstrip("△▲A")):
            if mode == "tolerance":
                return None
            if _NOMINAL_START.search(right_stripped.lstrip("△▲A")):
                return (_PAIR_PREFIX, right.score)
            return None
        if _EXPLICIT_TOLERANCE_IN_LINE.search(left_text):
            return None
        # 12. + 9 のような小数点途切れを、公差結合より優先する
        if _INCOMPLETE_DECIMAL.fullmatch(left_text.lstrip("△▲A")):
            if mode == "tolerance":
                return None
            if _SINGLE_FRACTION_DIGIT.fullmatch(right_stripped):
                return (_PAIR_DECIMAL, right.score)
            return None
        if re.search(
            r"(?:Rz\s*max|Rzmax|Ramax|Rmax|Ra)\s*\d+\.$",
            left_text,
            re.IGNORECASE,
        ):
            if mode == "tolerance":
                return None
            if _SINGLE_FRACTION_DIGIT.fullmatch(right_stripped):
                return (_PAIR_DECIMAL, right.score)
            return None
        if not _NOMINAL_START.search(left_text.lstrip("△▲A")):
            return None
        # 1桁だけの左辺は小数点の続きであり、公差の公称にはしない
        if re.fullmatch(r"[φΦØ⌀RCＲＣ]?[0-9]$", left_text.lstrip("△▲A")):
            return None
        # 単独の 0 / 0. は公差値として未完成であり、別寸法の断片と誤結合しやすい。
        if re.fullmatch(r"0+\.?", right_stripped):
            return None
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
        signed_right = right_stripped.startswith(
            ("±", "+", "-", "＋", "－", "士", "土", "亇", "干")
        )
        # 符号なしは 0.05 のような小数公差だけ。整数 2 / 1815 は別寸法。
        unsigned_fraction = bool(re.fullmatch(r"0?\.\d+", right_stripped))
        leftover_digits = bool(
            left_needs_tol_digits and re.fullmatch(r"\d{1,3}", right_stripped)
        )
        if left_text.rstrip().endswith(("+", "-")) and not signed_right:
            if not unsigned_fraction:
                return None
        if not signed_right and not unsigned_fraction and not leftover_digits:
            return None
        if left_text.rstrip().endswith(("+", "-")) and signed_right:
            # 2.5+ と +0.25 は、完成した別寸法より優先して閉じる
            priority = _PAIR_DECIMAL
        elif signed_right:
            priority = _PAIR_SIGNED_TOLERANCE
        else:
            priority = _PAIR_BARE_TOLERANCE
        if mode == "continuation" and priority < _PAIR_DECIMAL:
            return None
        if mode == "tolerance" and priority >= _PAIR_DECIMAL:
            return None
        return (priority, right.score)

    def _merged_line(left: LocalOcrLine, right: LocalOcrLine) -> LocalOcrLine:
        left_text = unicodedata.normalize("NFKC", left.text)
        merged_text = _merge_dimension_fragment_text(left_text, right.text)
        axis_x, axis_y = left.direction
        normal_x, normal_y = -axis_y, axis_x
        points = (*left.quad, *right.quad)
        along = [point[0] * axis_x + point[1] * axis_y for point in points]
        across = [point[0] * normal_x + point[1] * normal_y for point in points]
        along0, along1 = min(along), max(along)
        across0, across1 = min(across), max(across)

        def projected_point(along_value: float, across_value: float):
            return (
                along_value * axis_x + across_value * normal_x,
                along_value * axis_y + across_value * normal_y,
            )

        merged_quad = (
            projected_point(along0, across0),
            projected_point(along1, across0),
            projected_point(along1, across1),
            projected_point(along0, across1),
        )
        return LocalOcrLine(
            merged_text,
            max(left.score, right.score),
            merged_quad,
        )

    indexed = list(enumerate(lines))
    candidates: list[tuple[int, float, float, int, int]] = []
    for left_index, left in indexed:
        for right_index, right in indexed:
            if right_index == left_index:
                continue
            pair = _try_pair(left, right)
            if pair is None:
                continue
            priority, score = pair
            distance = math.dist(_line_center(left), _line_center(right))
            candidates.append((priority, -distance, score, left_index, right_index))
    candidates.sort(reverse=True)
    consumed: set[int] = set()
    joined: list[LocalOcrLine] = []
    for _priority, _neg_distance, _score, left_index, right_index in candidates:
        if left_index in consumed or right_index in consumed:
            continue
        joined.append(_merged_line(lines[left_index], lines[right_index]))
        consumed.add(left_index)
        consumed.add(right_index)

    # 結合済み断片は落とすが、未結合の完成寸法は必ず残す
    retained = [line for index, line in indexed if index not in consumed]
    return tuple(retained + joined)


def join_split_dimension_ocr_lines(
    lines: tuple[LocalOcrLine, ...],
    *,
    rounds: int = 3,
) -> tuple[LocalOcrLine, ...]:
    """直径記号・小数点途切れ・公差断片を、安定するまで結合する。"""

    current = lines
    for _ in range(rounds):
        nxt = join_nearby_tolerance_ocr_lines(current, mode="continuation")
        if len(nxt) == len(current):
            break
        current = nxt
    for _ in range(max(2, rounds - 1)):
        nxt = join_nearby_tolerance_ocr_lines(current, mode="tolerance")
        if len(nxt) == len(current):
            break
        current = nxt
    return current


def enrich_scanned_ocr_page(
    ocr_page: LocalOcrPage,
    tile_lines: tuple[LocalOcrLine, ...] | None = None,
) -> LocalOcrPage:
    """ページOCRとタイルOCRを統合し、分離公差を結合する。"""

    merged = merge_ocr_lines(ocr_page.lines, tile_lines or ())
    # 直径記号 + 数値 + 公差 の分割にも対応するため複数回結合する
    combined = join_split_dimension_ocr_lines(merged)
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
    combined = join_split_dimension_ocr_lines(tile_lines)
    return LocalOcrPage(
        width=image.width,
        height=image.height,
        scale_x=image.width / page.rect.width,
        scale_y=image.height / page.rect.height,
        image=image,
        lines=combined,
    )


def analyze_scanned_page_tiles(
    page: fitz.Page,
    *,
    fast: bool = False,
) -> tuple[LocalOcrLine, ...]:
    """画像PDFの図面領域を高解像度タイルOCRで読み取る。"""

    maximum_dimension = max(page.rect.width, page.rect.height)
    if fast:
        zoom = max(4.8, min(5.4, 6000.0 / maximum_dimension))
    else:
        zoom = max(
            SCANNED_TILE_ZOOM_MIN,
            min(
                SCANNED_TILE_ZOOM_MAX,
                SCANNED_TILE_ZOOM_NUMERATOR / maximum_dimension,
            ),
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
    # The full-page pass already supplies broad coverage.  Use a small number
    # of larger, lightly overlapping tiles only to recover tiny horizontal
    # dimensions; the former ~30 heavily overlapping serial tiles dominated
    # the colour-detection runtime.
    if fast:
        tile_width = min(image.width, max(1800, int(420 * scale_x)))
        tile_height = min(image.height, max(1400, int(330 * scale_y)))
        step_x = max(1200, int(tile_width * 0.78))
        step_y = max(950, int(tile_height * 0.75))
    else:
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

    def covering_starts(
        start: int,
        stop: int,
        span: int,
        *,
        overlap_ratio: float,
    ) -> list[int]:
        """Cover a range evenly without duplicate trailing sliver tiles."""

        length = max(1, stop - start)
        if length <= span:
            return [start]
        effective_step = max(1, round(span * (1.0 - overlap_ratio)))
        count = max(2, math.ceil((length - span) / effective_step) + 1)
        final_start = max(start, stop - span)
        return [
            round(start + (final_start - start) * index / (count - 1))
            for index in range(count)
        ]

    # Keep every drawing pixel at the original OCR resolution. The former
    # range loop overlapped high-resolution tiles by more than 50% and then
    # added tiny edge tiles, multiplying OCR time without adding information.
    overlap_ratio = 0.12 if fast else 0.18
    x_starts = covering_starts(
        left,
        right,
        tile_width,
        overlap_ratio=overlap_ratio,
    )
    y_starts = covering_starts(
        top,
        bottom,
        tile_height,
        overlap_ratio=overlap_ratio,
    )

    for y in y_starts:
        y1 = min(image.height, y + tile_height)
        for x in x_starts:
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
    return merge_ocr_lines(tuple(lines))


def _ocr_line_is_columnar(line: LocalOcrLine) -> bool:
    """縦寸法列の種かどうか。OCR方向が横でも、枠が縦長なら列として扱う。"""

    rect = fitz.Rect(line.rect)
    width = max(rect.width, 0.1)
    height = max(rect.height, 0.1)
    return abs(line.direction[1]) >= 0.72 or height / width >= 1.6


def select_vertical_dimension_clips(
    page: fitz.Page,
    ocr_page: LocalOcrPage,
    *,
    limit: int = 8,
) -> tuple[fitz.Rect, ...]:
    """縦寸法の再OCR対象領域を、上限以内で選ぶ。"""

    observed: list[tuple[fitz.Rect, str, float]] = []
    for line in ocr_page.lines:
        text = unicodedata.normalize("NFKC", line.text).strip()
        rect = fitz.Rect(line.rect) & page.rect
        if (
            rect.is_empty
            or not _ocr_line_is_columnar(line)
            or not any(character.isdigit() for character in text)
            or re.search(r"[ぁ-んァ-ヶ一-龯]", text)
            or len(text) > 18
            or rect.x0 < page.rect.width * 0.04
            or rect.x1 > page.rect.width * 0.96
            or rect.y0 < page.rect.height * 0.07
            or rect.y1 > page.rect.height * 0.93
        ):
            continue
        observed.append((rect, text, line.score))
    if not observed:
        return ()

    clusters: list[tuple[fitz.Rect, list[str], float]] = []
    for rect, text, score in observed:
        nearby = fitz.Rect(rect.x0 - 42, rect.y0 - 48, rect.x1 + 42, rect.y1 + 48)
        merged_index = None
        for index, (cluster_rect, _texts, _score) in enumerate(clusters):
            if not (nearby & cluster_rect).is_empty:
                merged_index = index
                break
        if merged_index is None:
            clusters.append((fitz.Rect(rect), [text], score))
        else:
            cluster_rect, texts, cluster_score = clusters[merged_index]
            clusters[merged_index] = (
                cluster_rect | rect,
                [*texts, text],
                max(cluster_score, score),
            )

    ranked: list[tuple[float, fitz.Rect]] = []
    for cluster_rect, texts, score in clusters:
        if cluster_rect.width > cluster_rect.height * 1.35:
            # 完成した横寸法の行を縦クロップしても情報は増えない。
            continue
        if (
            cluster_rect.x0 > page.rect.width * 0.84
            and cluster_rect.y1 < page.rect.height * 0.35
            and len(texts) >= 3
        ) or (
            cluster_rect.x0 > page.rect.width * 0.62
            and cluster_rect.y0 > page.rect.height * 0.68
        ):
            continue
        compact_texts = [re.sub(r"\s+", "", text) for text in texts]
        joined = "".join(compact_texts)
        fit_code_hint = bool(re.search(r"[HhGg]\s*\d{1,2}", joined))
        diameter_hint = bool(re.search(r"[φΦØ⌀]", joined))
        tolerance_hint = bool(re.search(r"[±+\-−]", joined))
        short_fragment_count = sum(
            1 for text in compact_texts if 0 < len(text) <= 3
        )
        stacked_tolerance = (
            len(compact_texts) >= 2
            and short_fragment_count >= 1
            and tolerance_hint
        )
        incomplete_diameter = diameter_hint and (
            re.search(r"[φΦØ⌀]\d+\.?$", joined) is not None
            or short_fragment_count >= 1
        )
        # 短い数字の塊だけでは表題欄や注記を再OCRしてしまう。
        # 直径の途切れ・はめあい・積み上げ公差がある列だけを対象にする。
        if not (fit_code_hint or incomplete_diameter or stacked_tolerance):
            continue
        rank = (
            len(texts) * 1.5
            + score
            + (7.0 if fit_code_hint else 0.0)
            + (5.0 if incomplete_diameter else 0.0)
            + (2.0 if stacked_tolerance else 0.0)
        )
        center_x = (cluster_rect.x0 + cluster_rect.x1) / 2
        center_y = (cluster_rect.y0 + cluster_rect.y1) / 2
        width = min(180.0, max(110.0, cluster_rect.width + 56.0))
        height = min(220.0, max(105.0, cluster_rect.height + 64.0))
        clip = fitz.Rect(
            center_x - width / 2,
            center_y - height / 2,
            center_x + width / 2,
            center_y + height / 2,
        ) & page.rect
        ranked.append((rank, clip))

    return tuple(
        clip
        for _rank, clip in sorted(
            ranked,
            key=lambda item: item[0],
            reverse=True,
        )[:limit]
    )


def analyze_vertical_dimension_regions(
    page: fitz.Page,
    ocr_page: LocalOcrPage,
) -> tuple[LocalOcrLine, ...]:
    """Re-read likely vertical dimension columns as compact high-resolution crops.

    Full-page OCR often splits a fit such as ``diameter 24.95 g6`` into single
    digits.  Cropping only the observed vertical text clusters keeps the same
    recognition resolution while avoiding a multi-minute full-page Windows
    OCR fallback.
    """

    regions = select_vertical_dimension_clips(page, ocr_page, limit=8)
    zoom = 6.0
    found: list[LocalOcrLine] = []
    for clip in regions:
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
        rotated = source.rotate(270, expand=True, fillcolor="white")
        with _ENGINE_LOCK:
            result = _engine()(np.asarray(rotated), return_word_box=False)
        boxes = [] if result.boxes is None else result.boxes
        texts = [] if result.txts is None else result.txts
        scores = [] if result.scores is None else result.scores
        for box, raw_text, raw_score in zip(boxes, texts, scores):
            text = normalize_drawing_text(str(raw_text or "").strip())
            score = float(raw_score or 0.0)
            if not text or score < 0.64 or len(box) < 4:
                continue
            relative = _restore_rotated_quad(
                box,
                270,
                source_width=source.width,
                source_height=source.height,
                scale_x=zoom,
                scale_y=zoom,
            )
            quad = tuple(
                (clip.x0 + point[0], clip.y0 + point[1])
                for point in relative
            )
            found.append(LocalOcrLine(text, score, quad))
    return merge_ocr_lines(tuple(found))


def analyze_incomplete_dimension_regions(
    page: fitz.Page,
    ocr_page: LocalOcrPage,
    *,
    include_incomplete: bool = True,
) -> tuple[LocalOcrLine, ...]:
    """欠けた小寸法だけを、必要時に高解像度で読み直す。

    ページ全体のOCRで ``18.`` のように末尾桁・上付き公差が欠けた場合だけを
    対象にする。加えて ``R0.5`` / ``C0.3`` / ``30°`` のような小さな指示で
    上下公差が読み取れていない場合だけ、その文字の周囲を読み直す。

    全頁を追加OCRする方式は遅く、かえって候補を増やすため採用しない。候補は
    優先度順に最大4か所、取得結果も公差らしい断片だけを元のOCRへ追記する。
    """

    incomplete = re.compile(r"^[φΦØ⌀]?[0-9]{1,3}\.$")
    small_feature = re.compile(
        r"^(?:[RC][0-9]+[.,][0-9]+|[0-9]{1,2}[°º])(?:[+\-−－].*)?$"
    )
    tolerance_fragment = re.compile(r"(?:[+\-±].*[0-9]|^[+\-]?[0-9]+(?:[.,][0-9]+)?$)")
    regions: list[tuple[int, fitz.Rect, bool, float]] = []

    def has_nearby_explicit_tolerance(rect: fitz.Rect) -> bool:
        nearby = fitz.Rect(rect.x0 - 28, rect.y0 - 28, rect.x1 + 28, rect.y1 + 28)
        for observed in ocr_page.lines:
            candidate = fitz.Rect(observed.rect)
            if not candidate.intersects(nearby):
                continue
            text = unicodedata.normalize("NFKC", observed.text).replace(" ", "")
            if re.search(r"[+\-±]", text):
                return True
        return False

    for line in ocr_page.lines:
        text = unicodedata.normalize("NFKC", line.text).replace(" ", "")
        is_incomplete = (
            include_incomplete
            and incomplete.fullmatch(text) is not None
            and line.score >= 0.90
        )
        # A single ``+0.1`` is not a completed stacked tolerance.  The three
        # compact callouts below are deliberately re-read even when that
        # upper fragment is already visible, so their lower ``0`` is not left
        # outside the marker.
        focus_small_feature = re.fullmatch(
            r"(?:R0[.,]5|C0[.,]3|30°)(?:[+\-−－].*)?",
            text.replace("º", "°"),
        ) is not None
        is_small_feature = (
            small_feature.fullmatch(text) is not None
            and line.score >= 0.72
            and (
                focus_small_feature
                or not has_nearby_explicit_tolerance(fitz.Rect(line.rect))
            )
        )
        if not (is_incomplete or is_small_feature):
            continue
        direction = line.direction
        if is_incomplete and (abs(direction[0]) < 0.82 or abs(direction[1]) > 0.42):
            continue
        rect = fitz.Rect(line.rect)
        # 表題欄・枠線上の文字を避け、図面内の寸法だけを再OCRする。
        if (
            rect.is_empty
            or rect.y0 < page.rect.height * 0.12
            or rect.y1 > page.rect.height * 0.90
            or rect.x0 > page.rect.width * 0.78
        ):
            continue
        if is_incomplete:
            clip = fitz.Rect(
                rect.x0 - 26.0,
                rect.y0 - 24.0,
                rect.x1 + 96.0,
                rect.y1 + 30.0,
            ) & page.rect
        else:
            # 公差は文字の右上/右下だけでなく、斜め・縦の寸法では周囲に置かれる。
            # 小領域のまま全方向を広げ、元の文字と公差を同じOCR視野に収める。
            clip = fitz.Rect(
                rect.x0 - 30.0,
                rect.y0 - 30.0,
                rect.x1 + 42.0,
                rect.y1 + 42.0,
            ) & page.rect
        if any(
            (clip & existing).get_area() > clip.get_area() * 0.55
            for _priority, existing, _is_small_feature, _rotation in regions
        ):
            continue
        # These frequently use a two-tier ``+value / 0`` tolerance on the
        # supplied drawings.  Read them first when the compact OCR budget is
        # exhausted; other small R/C callouts remain eligible afterwards.
        focus = focus_small_feature
        baseline_angle = math.degrees(math.atan2(direction[1], direction[0]))
        # Only tilted small callouts need a second, aligned pass.  Horizontal
        # dimensions keep the single fast pass.
        alignment_rotation = (
            -baseline_angle
            if is_small_feature and abs(baseline_angle) >= 14.0
            else 0.0
        )
        regions.append((0 if focus else 1, clip, is_small_feature, alignment_rotation))

    # 小さな公差付き指示を先に処理する。既存の小数点欠けは従来どおり補助する。
    regions.sort(key=lambda item: (item[0], not item[2], item[1].y0, item[1].x0))
    regions = regions[:4]

    found: list[LocalOcrLine] = []
    zoom = 8.0
    for _priority, clip, only_tolerance_fragments, alignment_rotation in regions:
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            clip=clip,
            colorspace=fitz.csRGB,
            alpha=False,
            annots=False,
        )
        source = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        image = prepare_raster_for_rapidocr(source)
        rotations = [0.0]
        if alignment_rotation:
            rotations.append(alignment_rotation)
        for rotation in rotations:
            rotated = (
                image
                if rotation == 0.0
                else image.rotate(rotation, expand=True, fillcolor="white")
            )
            with _ENGINE_LOCK:
                result = _engine()(np.asarray(rotated), return_word_box=False)
            boxes = [] if result.boxes is None else result.boxes
            texts = [] if result.txts is None else result.txts
            scores = [] if result.scores is None else result.scores
            radians = math.radians(rotation)
            cosine = math.cos(radians)
            sine = math.sin(radians)
            for box, raw_text, raw_score in zip(boxes, texts, scores):
                text = normalize_drawing_text(str(raw_text or "").strip())
                score = float(raw_score or 0.0)
                if not text or score < 0.64 or len(box) < 4:
                    continue
                compact = unicodedata.normalize("NFKC", text).replace(" ", "")
                if only_tolerance_fragments and tolerance_fragment.search(compact) is None:
                    continue
                points: list[tuple[float, float]] = []
                for point in box[:4]:
                    shifted_x = float(point[0]) - rotated.width / 2
                    shifted_y = float(point[1]) - rotated.height / 2
                    source_x = shifted_x * cosine - shifted_y * sine + image.width / 2
                    source_y = shifted_x * sine + shifted_y * cosine + image.height / 2
                    points.append((clip.x0 + source_x / zoom, clip.y0 + source_y / zoom))
                found.append(LocalOcrLine(text, score, tuple(points)))
    return merge_ocr_lines(tuple(found))
