from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import logging
import math
import mimetypes
from multiprocessing import freeze_support
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
from threading import RLock, Thread
from typing import Any
import unicodedata
from urllib.parse import parse_qs, unquote, urlparse

import fitz
from PIL import Image, ImageDraw, ImageFont
import webview

from drawing_assist.local_ocr import (
    LocalOcrLine,
    LocalOcrPage,
    analyze_page,
    analyze_scanned_page_tiles,
    enrich_scanned_ocr_page,
    local_ocr_available,
)
from drawing_assist.drawing_text_normalizer import is_tolerance_fragment
from drawing_assist.ocr_config import APP_BUILD_ID
from drawing_assist.ocr_debug_logger import OcrPipelineRecorder

from drawing_assist.pdf_editor import (
    DimensionMarkingBatch,
    DimensionMarkingCandidate,
    DimensionMarkingEntry,
    DimensionMark,
    DimensionStyle,
    DrawingItem,
    GeneralToleranceBatchMark,
    Mark,
    ProcedureNoteMark,
    ReplacementMark,
    StampMark,
    StrikeMark,
    TextHit,
    ToleranceAddition,
    WorkRegionMark,
    WorkShapeMark,
    avoid_dimension_overlap,
    apply_item_to_page,
    detect_enclosed_region,
    detect_visual_text_group,
    dimension_label_rect,
    expand_work_region,
    export_pdf,
    find_text_group,
    infer_dimension_style,
    predict_work_outline,
    procedure_note_rect,
    render_page_preview,
    replacement_content_rect,
    stamp_mark_rect,
    strike_from_hit,
    _raw_text_lines,
)
from drawing_assist.general_tolerance import (
    GeneralToleranceCandidate,
    _has_dimension_line_support,
    _is_feature_control_frame,
    _is_full_page_image,
    _is_non_dimension_region,
    _is_visual_parenthetical,
    _ocr_reference_rects,
    _overlaps_reference_evidence,
    _map_rotated_rect,
    _normalize_raster_dimension_text,
    _prepare_raster_for_ocr,
    _run_windows_ocr,
    _run_windows_ocr_jobs,
    detect_general_tolerance_candidates,
    extract_drawing_tolerance_notes,
    toggle_candidate,
)


PDF_FILE_TYPES = ("PDFファイル (*.pdf)",)
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_UPLOAD_BYTES = 250 * 1024 * 1024


@dataclass(frozen=True)
class _DetectedDimensionMarking:
    """One original drawing dimension and its existing tolerance area."""

    rect: tuple[float, float, float, float]
    quad: tuple[tuple[float, float], ...]
    direction: tuple[float, float]
    source_text: str
    nominal_value: float
    kind: str
    tolerance_range: float | None
    reference: bool = False
    tolerance_rect: tuple[float, float, float, float] | None = None
    tolerance_quad: tuple[tuple[float, float], ...] | None = None


_MARKING_NUMBER_PATTERN = re.compile(
    r"(?P<prefix>[φΦØ⌀CRＣＲ]?)"
    r"(?P<number>\d{1,4}(?:[.,]\d{1,4})?)"
    r"(?P<degree>[°。]?)"
)
_MARKING_THREAD_PATTERN = re.compile(
    r"^M\s*\d+(?:[.,]\d+)?\s*[X×]\s*\d+(?:[.,]\d+)?$",
    re.IGNORECASE,
)
_MARKING_LIMIT_PATTERN = re.compile(
    r"^\s*(?P<prefix>[CR])\s*(?P<number>\d+(?:[.,]\d+)?)\s*"
    r"(?:\u4ee5\u4e0b|\u4ee5\u5185|MAX)\s*$",
    re.IGNORECASE,
)
_MARKING_CONTEXT_PATTERN = re.compile(
    r"(?:以下|以上|超え|SCALE|DATE|DWG|DRAWING|PAGE|SHEET|REV|"
    r"図番|品番|尺度|日付|材質|公差)",
    re.IGNORECASE,
)


_MARKING_NOTE_CONTEXT_PATTERN = re.compile(
    r"(?:\u6307\u793a\u306a\u304d|\u307e\u305f\u306f|\u3068\u3059\u308b)"
)

_SURFACE_ROUGHNESS_PATTERN = re.compile(
    r"(?:Rz\s*max|Rzmax|Ra\s*max|Ramax|Rmax|Rz|Ra)\s*",
    re.IGNORECASE,
)

_ROUGHNESS_OCR_PATTERN = re.compile(
    r"^\d\.\d\+0\.0\d$",
)

_FIT_TOLERANCE_PATTERN = re.compile(
    r"H\d|G\d|[gG]\d",
)


def _marking_quad_from_points(
    points: list[fitz.Point],
    direction: tuple[float, float],
    *,
    along_expand: float = 0.0,
    across_inset: float = 0.0,
) -> tuple[tuple[float, float], ...]:
    axis = fitz.Point(direction)
    axis_length = math.hypot(axis.x, axis.y) or 1.0
    axis /= axis_length
    normal = fitz.Point(-axis.y, axis.x)
    along = [point.x * axis.x + point.y * axis.y for point in points]
    across = [point.x * normal.x + point.y * normal.y for point in points]
    along_min = min(along) - along_expand
    along_max = max(along) + along_expand
    across_min = min(across) + across_inset
    across_max = max(across) - across_inset
    if across_max <= across_min:
        across_min = min(across)
        across_max = max(across)

    def point(along_value: float, across_value: float) -> tuple[float, float]:
        value = axis * along_value + normal * across_value
        return (float(value.x), float(value.y))

    return (
        point(along_min, across_min),
        point(along_max, across_min),
        point(along_max, across_max),
        point(along_min, across_max),
    )


def _marking_quad_bounds(
    quad: tuple[tuple[float, float], ...],
) -> tuple[float, float, float, float]:
    return (
        min(point[0] for point in quad),
        min(point[1] for point in quad),
        max(point[0] for point in quad),
        max(point[1] for point in quad),
    )


def _explicit_tolerance_range(
    group_text: str,
    nominal_value: float,
) -> float | None:
    """Read the total tolerance range from a merged dimension group."""

    normalized = unicodedata.normalize("NFKC", group_text)
    for variant in ("亇", "干", "土", "士"):
        normalized = normalized.replace(variant, "±")
    normalized = normalized.replace("−", "-").replace("－", "-")
    nominal_match: re.Match[str] | None = None
    for match in re.finditer(r"\d+(?:[.,]\d+)?", normalized):
        try:
            value = float(match.group(0).replace(",", "."))
        except ValueError:
            continue
        if abs(value - nominal_value) < 1e-7:
            nominal_match = match
            break
    if nominal_match is None:
        return None
    remainder = normalized[nominal_match.end() :]
    # ISO fit notation is part of the nominal (for example ``φ26g6``), not
    # a tolerance magnitude.  Ignore the grade digit before reading deviations.
    remainder = re.sub(r"^\s*[A-Za-z]\s*\d{1,2}", "", remainder)
    if not any(symbol in remainder for symbol in ("±", "+", "-")):
        return None
    values = [
        float(value.replace(",", "."))
        for value in re.findall(r"\d+(?:[.,]\d+)?", remainder)
    ]
    if not values:
        return None
    if "±" in remainder:
        return values[0] * 2
    if len(values) == 1:
        return values[0]
    if "+" in remainder and "-" in remainder:
        return abs(values[0]) + abs(values[1])
    return abs(max(values) - min(values))


def _is_plausible_tolerance_marking(
    text: str,
    nominal: float,
    tolerance_range: float | None,
) -> bool:
    """色分け対象として妥当な公差付き寸法かどうか。"""

    if tolerance_range is None:
        return False
    if tolerance_range > max(nominal * 0.6, 3.0):
        return False
    compact = unicodedata.normalize("NFKC", text).replace(" ", "")
    # OCR途中切れ（末尾が 0. や記号で終わる、先頭が小数点のみ、など）を除外する。
    if (
        compact.startswith(".")
        or compact.endswith((".", ":", ",", "+", "-", "±"))
        or re.search(r"[±+\-]0\.$", compact)
        or re.search(r"^\d+[±+\-]", compact) and not re.search(r"\d", compact.split("±")[-1] if "±" in compact else "")
        or re.fullmatch(r"\d{1,2}[±+\-]\d", compact)
        or re.search(r"\d\.[±+\-]", compact)
    ):
        return False
    if _FIT_TOLERANCE_PATTERN.search(text):
        return False
    if _ROUGHNESS_OCR_PATTERN.fullmatch(compact):
        return False
    if re.search(r"M\d", text, re.IGNORECASE):
        return False
    if re.search(r"[A-Z]{2,}\d", text) and "±" not in text and "+" not in text:
        return False
    if re.search(r"\+\d{2,}", text) and "±" not in text:
        return False
    return True


def _dimension_marking_kind(prefix: str, degree: str) -> str:
    if degree:
        return "angle"
    normalized = prefix.upper()
    if normalized in {"C", "Ｃ"}:
        return "chamfer"
    if normalized in {"R", "Ｒ"}:
        return "radius"
    if prefix in {"φ", "Φ", "Ø", "⌀"}:
        return "diameter"
    return "linear"


def _detect_dimension_markings(
    page: fitz.Page,
) -> list[_DetectedDimensionMarking]:
    """Detect original dimensions for color coding, including references.

    General-tolerance detection is deliberately conservative because it must
    never add a tolerance to an existing or reference dimension. Color coding
    has the opposite requirement, so it uses this broader, read-only pass.
    """

    expected_size = infer_dimension_style(page).font_size
    render_zoom = 4.0
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(render_zoom, render_zoom),
        colorspace=fitz.csGRAY,
        alpha=False,
        annots=False,
    )
    image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
    scale_x = image.width / page.rect.width
    scale_y = image.height / page.rect.height
    detected: list[_DetectedDimensionMarking] = []
    text_lines = _raw_text_lines(page)

    for block in page.get_text("rawdict").get("blocks", []):
        if block.get("type") not in {None, 0}:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans") or []
            chars = [
                char
                for span in spans
                for char in span.get("chars", [])
            ]
            raw_text = "".join(str(char.get("c") or "") for char in chars)
            normalized_raw_text = unicodedata.normalize("NFKC", raw_text)
            leading_space_count = len(normalized_raw_text) - len(
                normalized_raw_text.lstrip()
            )
            text = normalized_raw_text.strip()
            limit_callout = _MARKING_LIMIT_PATTERN.fullmatch(text)
            if (
                not text
                or not any(character.isdigit() for character in text)
                or (
                    limit_callout is None
                    and _MARKING_CONTEXT_PATTERN.search(text)
                )
            ):
                continue
            font_size = max(
                (float(span.get("size") or 0.0) for span in spans),
                default=0.0,
            )
            if not expected_size * 0.82 <= font_size <= expected_size * 1.23:
                continue

            textual_reference = False
            working_text = text
            text_offset = leading_space_count
            outer_reference = re.fullmatch(r"[（(]\s*(.+?)\s*[）)]", text)
            if outer_reference is not None:
                textual_reference = True
                working_text = outer_reference.group(1)
                text_offset = leading_space_count + text.find(working_text)

            thread_callout = bool(_MARKING_THREAD_PATTERN.fullmatch(working_text))
            match = _MARKING_NUMBER_PATTERN.match(working_text)
            if limit_callout is not None:
                nominal_value = float(
                    limit_callout.group("number").replace(",", ".")
                )
                core_start = 0
                core_end = len(text)
                kind = "limit"
                match = None
            elif thread_callout:
                first_number = re.search(r"\d+(?:[.,]\d+)?", working_text)
                if first_number is None:
                    continue
                nominal_value = float(first_number.group(0).replace(",", "."))
                core_start = text_offset
                core_end = text_offset + len(working_text)
                kind = "thread"
            else:
                if match is None:
                    continue
                suffix = working_text[match.end() :].strip()
                if suffix and not (
                    re.fullmatch(r"[（(][^）)]*[）)]", suffix)
                    or any(symbol in suffix for symbol in ("±", "亇", "+", "-", "−", "－"))
                ):
                    continue
                try:
                    nominal_value = float(
                        match.group("number").replace(",", ".")
                    )
                except ValueError:
                    continue
                if nominal_value <= 0 or nominal_value > 4000:
                    continue
                core_start = text_offset + match.start()
                core_end = text_offset + match.end()
                kind = _dimension_marking_kind(
                    match.group("prefix"),
                    match.group("degree"),
                )

            core_chars = [
                char
                for index, char in enumerate(chars)
                if core_start <= index < core_end
                and str(char.get("c") or "").strip()
            ]
            if not core_chars:
                continue
            direction_value = line.get("dir") or (1.0, 0.0)
            direction_point = fitz.Point(direction_value)
            direction_length = math.hypot(
                direction_point.x,
                direction_point.y,
            ) or 1.0
            direction = (
                float(direction_point.x / direction_length),
                float(direction_point.y / direction_length),
            )
            core_points: list[fitz.Point] = []
            for char in core_chars:
                try:
                    quad = fitz.recover_char_quad(direction_value, char)
                    core_points.extend((quad.ul, quad.ur, quad.lr, quad.ll))
                except (TypeError, ValueError):
                    char_rect = fitz.Rect(char.get("bbox"))
                    core_points.extend(
                        (
                            char_rect.top_left,
                            char_rect.top_right,
                            char_rect.bottom_right,
                            char_rect.bottom_left,
                        )
                    )
            core_quad = _marking_quad_from_points(
                core_points,
                direction,
                across_inset=max(0.12, font_size * 0.035),
            )
            core_rect = fitz.Rect(_marking_quad_bounds(core_quad))
            if (
                core_rect.y1 < page.rect.height * 0.11
                or core_rect.y0 > page.rect.height * 0.82
            ):
                continue

            center = fitz.Point(
                (core_rect.x0 + core_rect.x1) / 2,
                (core_rect.y0 + core_rect.y1) / 2,
            )
            hit = find_text_group(page, center, text_lines=text_lines)
            if hit is None:
                continue
            hit_number = re.search(
                r"\d+(?:[.,]\d+)?",
                unicodedata.normalize("NFKC", hit.nominal_text or hit.text),
            )
            if hit_number is not None and not thread_callout:
                try:
                    hit_value = float(hit_number.group(0).replace(",", "."))
                except ValueError:
                    continue
                # This raw line is one of the smaller upper/lower tolerance
                # values, not the nominal anchor selected by find_text_group.
                if abs(hit_value - nominal_value) > 1e-7:
                    continue

            # Some CAD writers draw the diameter sign with an unmapped glyph.
            # It is visible in the PDF but extracted as an empty text line.
            # ``find_text_group`` preserves that fact; extend only toward the
            # leading side so the marker covers φ without swallowing the
            # tolerance or the adjacent dimension line.
            if hit.preserved_prefix == "φ" and kind == "linear":
                kind = "diameter"
                axis = fitz.Point(direction)
                prefix_extent = max(2.2, font_size * 1.42)
                prefix_points = [
                    fitz.Point(point) - axis * prefix_extent
                    for point in core_quad
                ]
                core_quad = _marking_quad_from_points(
                    [fitz.Point(point) for point in core_quad] + prefix_points,
                    direction,
                    across_inset=max(0.12, font_size * 0.035),
                )
                core_rect = fitz.Rect(_marking_quad_bounds(core_quad))

            group_text = hit.text or text
            tolerance_range = _explicit_tolerance_range(
                group_text,
                nominal_value,
            )
            # Vector-only parentheses occur in several CAD exports.  Do not
            # confuse tolerance glyphs or a descriptor such as ``(二面幅)``
            # with reference-dimension parentheses.
            visual_reference_allowed = (
                tolerance_range is None
                and not thread_callout
                and not working_text[match.end() :].strip()
                if match is not None
                else False
            )
            reference = textual_reference or (
                visual_reference_allowed
                and _is_visual_parenthetical(
                    image,
                    core_rect,
                    direction,
                    scale_x=scale_x,
                    scale_y=scale_y,
                )
            )
            if reference:
                core_quad = _marking_quad_from_points(
                    [fitz.Point(point) for point in core_quad],
                    direction,
                    along_expand=max(1.8, font_size * 0.36),
                    across_inset=0.0,
                )
                core_rect = fitz.Rect(_marking_quad_bounds(core_quad))

            tolerance_quad = None
            tolerance_rect = None
            if tolerance_range is not None and hit.quad:
                axis = fitz.Point(direction)
                normal = fitz.Point(-axis.y, axis.x)
                group_points = [fitz.Point(point) for point in hit.quad]
                # A trailing parenthetical is a descriptor, not a reference
                # dimension. Include it in the same continuous marker, e.g.
                # 16+/-0.2(two-face width) and dia15.5+/-0.2(groove).
                if re.search(r"\([^()]+\)\s*$", working_text):
                    for char in chars:
                        if not str(char.get("c") or "").strip():
                            continue
                        try:
                            char_quad = fitz.recover_char_quad(
                                direction_value,
                                char,
                            )
                            group_points.extend(
                                (
                                    char_quad.ul,
                                    char_quad.ur,
                                    char_quad.lr,
                                    char_quad.ll,
                                )
                            )
                        except (TypeError, ValueError):
                            char_rect = fitz.Rect(char.get("bbox"))
                            group_points.extend(
                                (
                                    char_rect.top_left,
                                    char_rect.top_right,
                                    char_rect.bottom_right,
                                    char_rect.bottom_left,
                                )
                            )
                group_along = [
                    point.x * axis.x + point.y * axis.y
                    for point in group_points
                ]
                group_across = [
                    point.x * normal.x + point.y * normal.y
                    for point in group_points
                ]
                nominal_along = [
                    point[0] * axis.x + point[1] * axis.y
                    for point in core_quad
                ]
                tolerance_along_min = max(nominal_along) + 0.05
                tolerance_along_max = max(group_along)
                if tolerance_along_max - tolerance_along_min > 0.6:
                    tolerance_quad = _marking_quad_from_points(
                        [
                            axis * tolerance_along_min
                            + normal * min(group_across),
                            axis * tolerance_along_max
                            + normal * min(group_across),
                            axis * tolerance_along_max
                            + normal * max(group_across),
                            axis * tolerance_along_min
                            + normal * max(group_across),
                        ],
                        direction,
                        across_inset=max(0.1, font_size * 0.025),
                    )
                    tolerance_rect = _marking_quad_bounds(tolerance_quad)

            candidate = _DetectedDimensionMarking(
                rect=tuple(core_rect),
                quad=core_quad,
                direction=direction,
                source_text=working_text,
                nominal_value=nominal_value,
                kind=kind,
                tolerance_range=tolerance_range,
                reference=reference,
                tolerance_rect=tolerance_rect,
                tolerance_quad=tolerance_quad,
            )
            candidate_rect = fitz.Rect(candidate.rect)
            if any(
                (
                    candidate_rect & fitz.Rect(existing.rect)
                ).get_area()
                >= min(
                    candidate_rect.get_area(),
                    fitz.Rect(existing.rect).get_area(),
                )
                * 0.55
                for existing in detected
            ):
                continue
            detected.append(candidate)
    return sorted(detected, key=lambda item: (item.rect[1], item.rect[0]))


def _detect_scanned_dimension_markings(
    page: fitz.Page,
    ocr_script: Path,
    *,
    include_plain_dimensions: bool = False,
) -> list[_DetectedDimensionMarking]:
    """Detect explicit dimension+tolerance groups in a raster drawing."""

    maximum_dimension = max(page.rect.width, page.rect.height)
    zoom = max(2.4, min(3.5, 2800 / maximum_dimension))
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        colorspace=fitz.csRGB,
        alpha=False,
        annots=False,
    )
    source_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    image = _prepare_raster_for_ocr(source_image)
    image_width, image_height = image.size
    scale_x = image_width / page.rect.width
    scale_y = image_height / page.rect.height

    def lines_with_nearby_tolerances(result: dict) -> list[dict]:
        """Rejoin nominal and stacked tolerance rows split by raster OCR."""

        original = [
            line for line in (result.get("lines") or []) if line.get("words")
        ]
        combined = list(original)

        def word_bounds(line: dict) -> fitz.Rect:
            words = line.get("words") or []
            return fitz.Rect(
                min(float(word.get("x") or 0) for word in words),
                min(float(word.get("y") or 0) for word in words),
                max(
                    float(word.get("x") or 0)
                    + float(word.get("width") or 0)
                    for word in words
                ),
                max(
                    float(word.get("y") or 0)
                    + float(word.get("height") or 0)
                    for word in words
                ),
            )

        normalized = [
            re.sub(
                r"\s+",
                "",
                unicodedata.normalize(
                    "NFKC", str(line.get("text") or "")
                ),
            )
            for line in original
        ]
        bounds = [word_bounds(line) for line in original]
        tolerance_fragment = re.compile(
            r"^(?:[±+\-−－]?\s*0(?:[.,]\d+)?|[+\-−－]\d+(?:[.,]\d+)?)$"
        )
        for index, base_text in enumerate(normalized):
            if (
                re.fullmatch(r"[（(].+[）)]", base_text)
                or
                _MARKING_NUMBER_PATTERN.search(base_text) is None
                or _explicit_tolerance_range(base_text, float(
                    _MARKING_NUMBER_PATTERN.search(base_text).group("number").replace(",", ".")
                )) is not None
            ):
                continue
            base_rect = bounds[index]
            neighbors: list[tuple[float, int]] = []
            for other_index, fragment in enumerate(normalized):
                if other_index == index or tolerance_fragment.fullmatch(fragment) is None:
                    continue
                other_rect = bounds[other_index]
                horizontal_gap = max(
                    base_rect.x0 - other_rect.x1,
                    other_rect.x0 - base_rect.x1,
                    0.0,
                )
                vertical_gap = max(
                    base_rect.y0 - other_rect.y1,
                    other_rect.y0 - base_rect.y1,
                    0.0,
                )
                height = max(base_rect.height, other_rect.height, 1.0)
                if horizontal_gap > height * 2.2 or vertical_gap > height * 1.8:
                    continue
                neighbors.append(
                    (
                        math.dist(
                            (
                                (base_rect.x0 + base_rect.x1) / 2,
                                (base_rect.y0 + base_rect.y1) / 2,
                            ),
                            (
                                (other_rect.x0 + other_rect.x1) / 2,
                                (other_rect.y0 + other_rect.y1) / 2,
                            ),
                        ),
                        other_index,
                    )
                )
            if not neighbors:
                continue
            selected = [
                other_index
                for _distance, other_index in sorted(neighbors)[:2]
            ]
            combined.append(
                {
                    "text": base_text + "".join(normalized[i] for i in selected),
                    "words": list(original[index].get("words") or [])
                    + [
                        word
                        for selected_index in selected
                        for word in (original[selected_index].get("words") or [])
                    ],
                }
            )
        return combined

    def mapped_rect(words: list[dict[str, Any]], rotation: int) -> fitz.Rect:
        pixel_rect = fitz.Rect(
            min(float(word.get("x") or 0) for word in words),
            min(float(word.get("y") or 0) for word in words),
            max(
                float(word.get("x") or 0) + float(word.get("width") or 0)
                for word in words
            ),
            max(
                float(word.get("y") or 0) + float(word.get("height") or 0)
                for word in words
            ),
        )
        mapped = _map_rotated_rect(
            tuple(pixel_rect),
            rotation,
            image_width,
            image_height,
        )
        return fitz.Rect(
            mapped[0] / scale_x,
            mapped[1] / scale_y,
            mapped[2] / scale_x,
            mapped[3] / scale_y,
        ) & page.rect

    with tempfile.TemporaryDirectory(
        prefix="DrawingAssist-Marking-OCR-"
    ) as temp_name:
        temp_dir = Path(temp_name)
        jobs: list[dict[str, Any]] = []
        for rotation in (0, 90, 270):
            rotated = image if rotation == 0 else image.rotate(rotation, expand=True)
            image_path = temp_dir / f"marking-{rotation}.png"
            rotated.save(image_path)
            jobs.append(
                {
                    "rotation": rotation,
                    "path": image_path,
                    "tile": None,
                }
            )

        # Full-page OCR loses many small dimension strings on image PDFs.  Add
        # overlapping, enlarged horizontal tiles; the overlap keeps dimensions
        # near a tile boundary intact.  Vertical dimensions remain covered by
        # the rotated full-page passes above.
        tile_width = min(image_width, 1050)
        tile_height = min(image_height, 760)
        step_x = max(1, int(tile_width * 0.72))
        step_y = max(1, int(tile_height * 0.70))
        x_positions = list(range(0, max(1, image_width - tile_width + 1), step_x))
        y_positions = list(range(0, max(1, image_height - tile_height + 1), step_y))
        final_x = max(0, image_width - tile_width)
        final_y = max(0, image_height - tile_height)
        if not x_positions or x_positions[-1] != final_x:
            x_positions.append(final_x)
        if not y_positions or y_positions[-1] != final_y:
            y_positions.append(final_y)
        tile_index = 0
        for tile_y in y_positions:
            for tile_x in x_positions:
                crop = image.crop(
                    (
                        tile_x,
                        tile_y,
                        min(image_width, tile_x + tile_width),
                        min(image_height, tile_y + tile_height),
                    )
                )
                resize_factor = min(1.65, max(1.25, 1450 / max(crop.width, 1)))
                enlarged = crop.resize(
                    (
                        max(1, round(crop.width * resize_factor)),
                        max(1, round(crop.height * resize_factor)),
                    ),
                    Image.Resampling.LANCZOS,
                )
                image_path = temp_dir / f"tile-{tile_index}.png"
                enlarged.save(image_path)
                jobs.append(
                    {
                        "rotation": 0,
                        "path": image_path,
                        "tile": (tile_x, tile_y, resize_factor),
                    }
                )
                tile_index += 1

        results = _run_windows_ocr_jobs(
            jobs,
            ocr_script,
            path_index="path",
            max_workers=4,
        )

    detected: list[_DetectedDimensionMarking] = []
    for job, result in results:
        rotation = int(job["rotation"])
        tile = job["tile"]
        direction = {
            0: (1.0, 0.0),
            90: (0.0, 1.0),
            270: (0.0, -1.0),
        }[rotation]
        for line in lines_with_nearby_tolerances(result):
            words = line.get("words") or []
            if not words:
                continue
            text = unicodedata.normalize("NFKC", str(line.get("text") or ""))
            limit_callout = _MARKING_LIMIT_PATTERN.fullmatch(text.strip())
            if (
                not text
                or (
                    limit_callout is None
                    and _MARKING_CONTEXT_PATTERN.search(text)
                )
            ):
                continue
            compact = _normalize_raster_dimension_text(text)
            compact = compact.lstrip("△▲◆◇")
            reference = bool(re.fullmatch(r"[（(].+[）)]", compact))
            working = compact[1:-1] if reference else compact
            thread_callout = bool(_MARKING_THREAD_PATTERN.fullmatch(working))
            match = _MARKING_NUMBER_PATTERN.search(working)
            if limit_callout is None and match is None:
                continue
            if (
                limit_callout is None
                and not thread_callout
                and working[:1] in {"+", "-", "−"}
            ):
                # A detached stacked tolerance row is not a dimension by
                # itself.  Coloring it separately creates the diagonal blank
                # marks reported on scanned drawings.
                continue
            try:
                nominal = float(
                    (
                        limit_callout.group("number")
                        if limit_callout is not None
                        else match.group("number")
                    ).replace(",", ".")
                )
            except ValueError:
                continue
            if nominal <= 0 or nominal > 4000:
                continue
            if limit_callout is not None:
                kind = "limit"
            elif thread_callout:
                kind = "thread"
            else:
                kind = _dimension_marking_kind(
                    match.group("prefix"),
                    match.group("degree"),
                )
            tolerance_range = _explicit_tolerance_range(working, nominal)
            # OCR sometimes finds a number and a plus/minus character inside
            # an entire note or title-block row.  Those long strings caused
            # large, unrelated highlights.  A drawing dimension is a compact
            # expression; allow only its normal symbols and short OCR noise.
            expression_check = working.replace("士", "").replace("土", "")
            if (
                len(working) > 30
                or match is not None and match.start() > 2
                or "RZ" in working
                or re.search(r"[ぁ-んァ-ヶ一-龯]", expression_check)
                or (
                    not thread_callout
                    and re.search(r"[A-BD-QS-WYZ]", expression_check)
                )
                or working.count("(") != working.count(")")
                or re.search(
                    r"[^0-9A-ZφΦØ⌀CR.,±士土+\-−－一°。()（）/×]",
                    working,
                )
            ):
                continue
            # Plain OCR numbers are too ambiguous for global color coding.
            # They are still covered by the selected general-tolerance batch.
            if (
                tolerance_range is None
                and not thread_callout
                and limit_callout is None
                and not reference
                and not include_plain_dimensions
            ):
                continue
            if tile is None:
                rect = mapped_rect(words, rotation)
            else:
                tile_x, tile_y, resize_factor = tile
                pixel_rect = fitz.Rect(
                    min(float(word.get("x") or 0) for word in words),
                    min(float(word.get("y") or 0) for word in words),
                    max(
                        float(word.get("x") or 0)
                        + float(word.get("width") or 0)
                        for word in words
                    ),
                    max(
                        float(word.get("y") or 0)
                        + float(word.get("height") or 0)
                        for word in words
                    ),
                )
                rect = fitz.Rect(
                    (tile_x + pixel_rect.x0 / resize_factor) / scale_x,
                    (tile_y + pixel_rect.y0 / resize_factor) / scale_y,
                    (tile_x + pixel_rect.x1 / resize_factor) / scale_x,
                    (tile_y + pixel_rect.y1 / resize_factor) / scale_y,
                ) & page.rect
            if (
                rect.is_empty
                or rect.y1 < page.rect.height * 0.08
                or rect.y0 > page.rect.height * 0.86
                or rect.width > page.rect.width * 0.32
                or rect.height > page.rect.height * 0.25
                or (
                    rect.x1 < page.rect.width * 0.22
                    and rect.y1 < page.rect.height * 0.32
                )
            ):
                continue
            if _is_feature_control_frame(
                image,
                rect,
                direction,
                scale_x=scale_x,
                scale_y=scale_y,
            ):
                continue
            # 明示公差がある寸法は、OCR枠のずれで括弧・寸法線判定が外れやすい。
            # 素の数値だけ厳密に幾何確認し、公差付きは誤除外を避ける。
            if not reference and tolerance_range is None:
                reference = _is_visual_parenthetical(
                    image,
                    rect,
                    direction,
                    scale_x=scale_x,
                    scale_y=scale_y,
                )
            if tolerance_range is None and not _has_dimension_line_support(
                image,
                rect,
                direction,
                kind if kind in {"linear", "diameter", "angle", "chamfer", "radius"} else "linear",
                scale_x=scale_x,
                scale_y=scale_y,
                strict=False,
            ):
                continue
            quad = _marking_quad_from_points(
                [
                    rect.top_left,
                    rect.top_right,
                    rect.bottom_right,
                    rect.bottom_left,
                ],
                direction,
                # Windows OCR often omits the leading diameter glyph from its
                # word box even though it recognizes the following value.
                # Extend by roughly one small glyph so φ is painted too.
                along_expand=max(
                    1.5,
                    min(5.0, max(rect.width, rect.height) * 0.07),
                ),
                across_inset=max(0.08, min(rect.width, rect.height) * 0.025),
            )
            candidate = _DetectedDimensionMarking(
                rect=_marking_quad_bounds(quad),
                quad=quad,
                direction=direction,
                source_text=working,
                nominal_value=nominal,
                kind=kind,
                tolerance_range=tolerance_range,
                reference=reference,
            )
            candidate_rect = fitz.Rect(candidate.rect)
            if any(
                (candidate_rect & fitz.Rect(existing.rect)).get_area()
                >= 0.45
                * min(candidate_rect.get_area(), fitz.Rect(existing.rect).get_area())
                for existing in detected
            ):
                continue
            detected.append(candidate)
    reference_rects = tuple(
        fitz.Rect(candidate.rect)
        for candidate in detected
        if candidate.reference
    )
    verified = [
        candidate
        for candidate in detected
        if not candidate.reference
        and not _overlaps_reference_evidence(
            fitz.Rect(candidate.rect),
            reference_rects,
        )
    ]
    return sorted(verified, key=lambda item: (item.rect[1], item.rect[0]))


def _detect_local_dimension_markings(
    page: fitz.Page,
    ocr_page: LocalOcrPage,
    *,
    include_plain_dimensions: bool = False,
    scanned_page: bool = False,
) -> list[_DetectedDimensionMarking]:
    """Parse dimension notation from the shared ONNX OCR page result."""

    detected: list[_DetectedDimensionMarking] = []
    reference_rects = _ocr_reference_rects(ocr_page)
    for line in ocr_page.lines:
        text = unicodedata.normalize("NFKC", line.text)
        if _SURFACE_ROUGHNESS_PATTERN.search(text):
            continue
        if re.search(r"[（(]", text) or re.search(r"[）)]", text):
            continue
        if re.search(r"[ぁ-んァ-ヶ一-龯]", text):
            continue
        if re.search(r"M\d", text, re.IGNORECASE):
            continue
        compact = _normalize_raster_dimension_text(text).lstrip("△▲◆◇")
        # Filled triangular inspection markers are commonly read as A.
        compact = re.sub(r"^A(?=\d)", "", compact)
        if is_tolerance_fragment(text) or is_tolerance_fragment(compact):
            continue
        limit_callout = _MARKING_LIMIT_PATTERN.fullmatch(compact)
        if limit_callout is not None:
            continue
        if not compact or (
            _MARKING_CONTEXT_PATTERN.search(text)
            or _MARKING_NOTE_CONTEXT_PATTERN.search(text)
        ):
            continue
        reference = bool(re.fullmatch(r"[（(].+[）)]", compact))
        working = compact[1:-1] if reference else compact
        working = working.replace("×", "X")
        thread_callout = bool(_MARKING_THREAD_PATTERN.fullmatch(working))
        match = _MARKING_NUMBER_PATTERN.search(working)
        if match is None:
            continue
        if working.endswith(("+", "-")):
            continue
        if working[:1] in {"+", "-", "−", "±"}:
            continue
        try:
            nominal = float(match.group("number").replace(",", "."))
        except (AttributeError, ValueError):
            continue
        if nominal <= 0 or nominal > 4000:
            continue
        if thread_callout:
            continue
        kind = _dimension_marking_kind(match.group("prefix"), match.group("degree"))
        tolerance_range = _explicit_tolerance_range(working, nominal)
        if tolerance_range is not None and not _is_plausible_tolerance_marking(
            text,
            nominal,
            tolerance_range,
        ):
            continue
        if tolerance_range is None and not include_plain_dimensions:
            continue

        rect = fitz.Rect(line.rect) & page.rect
        direction = line.direction
        if _overlaps_reference_evidence(rect, reference_rects):
            continue
        quad_points = [fitz.Point(point) for point in line.quad]
        edge_lengths = (
            math.dist(line.quad[0], line.quad[1]),
            math.dist(line.quad[0], line.quad[3]),
        )
        text_length = max(edge_lengths)
        text_thickness = min(edge_lengths)
        max_text_thickness = max(
            9.0,
            min(page.rect.width, page.rect.height) * 0.018,
        )
        if (
            rect.is_empty
            or rect.y1 < page.rect.height * 0.08
            or rect.y0 > page.rect.height * 0.86
            or rect.width > page.rect.width * 0.34
            or rect.height > page.rect.height * 0.25
            or text_thickness > max_text_thickness
            or text_length > max(page.rect.width, page.rect.height) * 0.18
            or (
                rect.x1 < page.rect.width * 0.22
                and rect.y1 < page.rect.height * 0.32
            )
            or (
                scanned_page
                and tolerance_range is None
                and _is_non_dimension_region(
                    rect,
                    page.rect,
                    bare_only=True,
                )
            )
        ):
            continue
        if not reference:
            if _is_visual_parenthetical(
                ocr_page.image,
                rect,
                direction,
                scale_x=ocr_page.scale_x,
                scale_y=ocr_page.scale_y,
            ):
                reference = True
        if reference:
            continue
        has_line_support = _has_dimension_line_support(
            ocr_page.image,
            rect,
            direction,
            scale_x=ocr_page.scale_x,
            scale_y=ocr_page.scale_y,
            kind=kind,
            strict=False,
        )
        if scanned_page and not has_line_support:
            continue
        if scanned_page and tolerance_range is None and line.score < 0.72:
            continue
        if (
            scanned_page
            and tolerance_range is None
            and kind == "linear"
            and not match.group("prefix")
            and not match.group("degree")
            and nominal < 4.0
            and float(nominal).is_integer()
        ):
            # Isolated 1/2/3 are usually view labels, note numbers, or table
            # cells in scanned drawings rather than actual dimensions.
            continue
        quad = _marking_quad_from_points(
            quad_points,
            direction,
            along_expand=max(1.5, min(5.0, max(rect.width, rect.height) * 0.07)),
            across_inset=max(0.08, min(rect.width, rect.height) * 0.025),
        )
        candidate = _DetectedDimensionMarking(
            rect=_marking_quad_bounds(quad),
            quad=quad,
            direction=direction,
            source_text=working,
            nominal_value=nominal,
            kind=kind,
            tolerance_range=tolerance_range,
            reference=reference,
        )
        candidate_rect = fitz.Rect(candidate.rect)
        if any(
            (candidate_rect & fitz.Rect(existing.rect)).get_area()
            >= 0.45
            * min(candidate_rect.get_area(), fitz.Rect(existing.rect).get_area())
            for existing in detected
        ):
            continue
        detected.append(candidate)
    return sorted(detected, key=lambda item: (item.rect[1], item.rect[0]))


def _resource_path(*parts: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS, "drawing_assist", *parts)
    return Path(__file__).resolve().parent.joinpath(*parts)


def _number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _color(value: Any, default: str = "#fff24d") -> str:
    text = str(value or "")
    return text.lower() if COLOR_PATTERN.fullmatch(text) else default


def _circled_number(value: int) -> str:
    if 1 <= value <= 20:
        return chr(0x2460 + value - 1)
    if 21 <= value <= 35:
        return chr(0x3251 + value - 21)
    if 36 <= value <= 50:
        return chr(0x32B1 + value - 36)
    return f"({value})"


def _point_in_polygon(
    point: tuple[float, float],
    polygon: tuple[tuple[float, float], ...],
) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current[1] > point[1]) != (previous[1] > point[1]):
            intersection_x = (
                (previous[0] - current[0])
                * (point[1] - current[1])
                / (previous[1] - current[1])
                + current[0]
            )
            if point[0] < intersection_x:
                inside = not inside
        previous = current
    return inside


def _region_bbox(
    polygon: tuple[tuple[float, float], ...],
) -> fitz.Rect:
    return fitz.Rect(
        min(point[0] for point in polygon),
        min(point[1] for point in polygon),
        max(point[0] for point in polygon),
        max(point[1] for point in polygon),
    )


def _rect_intersection_area(first: fitz.Rect, second: fitz.Rect) -> float:
    """Fast rectangle intersection area for layout hot paths."""

    width = min(first.x1, second.x1) - max(first.x0, second.x0)
    if width <= 0:
        return 0.0
    height = min(first.y1, second.y1) - max(first.y0, second.y0)
    return width * height if height > 0 else 0.0


def _rects_intersect(first: fitz.Rect, second: fitz.Rect) -> bool:
    return (
        first.x0 < second.x1
        and first.x1 > second.x0
        and first.y0 < second.y1
        and first.y1 > second.y0
    )


def _is_scanned_page(page: fitz.Page) -> bool:
    """Recognize both one-image and tiled-image raster drawing exports."""

    return _is_full_page_image(page) or (
        not page.get_text("words") and bool(page.get_images(full=True))
    )


def _needs_local_ocr(page: fitz.Page) -> bool:
    """RapidOCRの高解像度解析が必要な画像ベース図面かどうか。"""

    if not local_ocr_available():
        return False
    # 抽出可能なテキストがあればネイティブ解析を優先する。
    if page.get_text("words"):
        return False
    # core.pdf のように画像オブジェクトを持たないラスターPDFもOCR対象にする。
    return True


def _page_ink_density(
    image: Image.Image,
    page_rect: fitz.Rect,
    rect: fitz.Rect,
    scale: float,
) -> float:
    """Return the dark-pixel ratio inside a page-space rectangle."""

    clipped = fitz.Rect(rect) & page_rect
    if clipped.is_empty:
        return 0.0
    x0 = max(0, int(math.floor((clipped.x0 - page_rect.x0) * scale)))
    y0 = max(0, int(math.floor((clipped.y0 - page_rect.y0) * scale)))
    x1 = min(image.width, int(math.ceil((clipped.x1 - page_rect.x0) * scale)))
    y1 = min(image.height, int(math.ceil((clipped.y1 - page_rect.y0) * scale)))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    histogram = image.crop((x0, y0, x1, y1)).histogram()
    return sum(histogram[:184]) / max(1, (x1 - x0) * (y1 - y0))


def _same_work_region(
    first: tuple[tuple[float, float], ...],
    second: tuple[tuple[float, float], ...],
) -> bool:
    """Return whether two seed clicks resolved to the same work region."""

    first_rect = _region_bbox(first)
    second_rect = _region_bbox(second)
    intersection = first_rect & second_rect
    smaller_area = min(first_rect.get_area(), second_rect.get_area())
    if smaller_area <= 0:
        return False
    if intersection.get_area() / smaller_area >= 0.88:
        return True
    return (
        len(first) == len(second)
        and all(
            math.dist(first_point, second_point) < 0.5
            for first_point, second_point in zip(first, second)
        )
    )


class DrawingApi:
    def __init__(self) -> None:
        self.window: webview.Window | None = None
        self.document: fitz.Document | None = None
        self.source_path: Path | None = None
        self.display_name: str | None = None
        self.page_index = 0
        self.items: list[DrawingItem] = []
        self.replacement_selection: TextHit | None = None
        self.editable_item_index: int | None = None
        self.editable_tolerance_index: int | None = None
        self.general_tolerance_candidates: list[
            GeneralToleranceCandidate
        ] = []
        self.dimension_marking_candidates: list[
            DimensionMarkingCandidate
        ] = []
        self.last_general_tolerance_batch: list[
            GeneralToleranceCandidate
        ] = []
        self.last_general_tolerance_additions: list[
            ToleranceAddition
        ] = []
        self.general_tolerance_standard = "jis_b_0405"
        self.general_tolerance_grade = "m"
        self.general_tolerance_angle_length = 10.0
        self.last_general_tolerance_marked = False
        self.work_region_candidates: list[
            tuple[tuple[float, float], ...]
        ] = []
        self.work_region_color = "#fff24d"
        self.work_region_opacity = 0.32
        self.word_candidate: Mark | None = None
        self.dimension_style_cache: dict[int, DimensionStyle] = {}
        self.general_tolerance_detection_cache: dict[
            tuple[int, str, str, float],
            tuple[GeneralToleranceCandidate, ...],
        ] = {}
        self.scanned_marking_cache: dict[
            int,
            tuple[_DetectedDimensionMarking, ...],
        ] = {}
        self.local_ocr_cache: dict[int, LocalOcrPage] = {}
        self.scanned_tile_cache: dict[int, tuple[LocalOcrLine, ...]] = {}
        self.lock = RLock()
        self.upload_directory = tempfile.TemporaryDirectory(
            prefix="DrawingAssist-"
        )

    def _clear_review_candidates(self) -> None:
        self.general_tolerance_candidates.clear()
        self.dimension_marking_candidates.clear()

    def set_window(self, window: webview.Window) -> None:
        self.window = window

    def _shared_local_ocr(self, page: fitz.Page) -> LocalOcrPage | None:
        if not _needs_local_ocr(page):
            return None
        cached = self.local_ocr_cache.get(self.page_index)
        if cached is None:
            try:
                cached = analyze_page(page, scanned=True)
            except Exception as exc:
                # Retain the Windows OCR fallback for unusual CPUs or a
                # damaged packaged model instead of blocking the workflow.
                logging.getLogger(__name__).warning(
                    "RapidOCRの初期化または解析に失敗しました: %s",
                    exc,
                )
                return None
            self.local_ocr_cache[self.page_index] = cached
        return cached

    def _shared_scanned_tiles(self, page: fitz.Page) -> tuple[LocalOcrLine, ...] | None:
        return self.scanned_tile_cache.get(self.page_index)

    def _enriched_local_ocr(self, page: fitz.Page) -> LocalOcrPage | None:
        base = self._shared_local_ocr(page)
        if base is None:
            return None
        if not _needs_local_ocr(page):
            return base
        tiles = self.scanned_tile_cache.get(self.page_index)
        if tiles is None:
            try:
                tiles = analyze_scanned_page_tiles(page)
                self.scanned_tile_cache[self.page_index] = tiles
            except Exception:
                tiles = ()
        return enrich_scanned_ocr_page(base, tiles)

    def drawing_assist_command(
        self,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a command received by the local HTTP API."""

        request = request or {}
        action = str(request.get("action") or "")
        arguments = request.get("arguments") or []
        handlers = {
            "get_initial_state": self.get_initial_state,
            "open_pdf": self.open_pdf,
            "previous_page": self.previous_page,
            "next_page": self.next_page,
            "undo": self.undo,
            "clear_page": self.clear_page,
            "clear_all": self.clear_all,
            "apply_action": self.apply_action,
            "detect_work_region": self.detect_work_region,
            "predict_work_shape": self.predict_work_shape,
            "confirm_work_region": self.confirm_work_region,
            "cancel_work_region": self.cancel_work_region,
            "confirm_word_candidate": self.confirm_word_candidate,
            "cancel_word_candidate": self.cancel_word_candidate,
            "select_replacement": self.select_replacement,
            "confirm_replacement": self.confirm_replacement,
            "cancel_replacement": self.cancel_replacement,
            "update_editable_item": self.update_editable_item,
            "select_general_tolerance_addition": self.select_general_tolerance_addition,
            "move_general_tolerance_addition": self.move_general_tolerance_addition,
            "cancel_editable_item_selection": self.cancel_editable_item_selection,
            "scan_general_tolerances": self.scan_general_tolerances,
            "toggle_general_tolerance": self.toggle_general_tolerance,
            "cancel_general_tolerance_candidates": self.cancel_general_tolerance_candidates,
            "apply_general_tolerances": self.apply_general_tolerances,
            "remove_applied_general_tolerance": self.remove_applied_general_tolerance,
            "scan_dimension_markings": self.scan_dimension_markings,
            "toggle_dimension_marking": self.toggle_dimension_marking,
            "cancel_dimension_marking_candidates": (
                self.cancel_dimension_marking_candidates
            ),
            "remove_dimension_marking": self.remove_dimension_marking,
            "apply_dimension_markings": self.apply_dimension_markings,
            "save_pdf": self.save_pdf,
        }
        handler = handlers.get(action)
        if handler is None:
            return self._error("未対応の操作です。")
        if not isinstance(arguments, list):
            return self._error("操作パラメーターが不正です。")
        try:
            return handler(*arguments)
        except TypeError as exc:
            return self._error(f"操作パラメーターが不正です: {exc}")

    def _empty_state(self, message: str = "PDFを開くか、画面へドロップしてください。") -> dict[str, Any]:
        return {
            "ok": True,
            "loaded": False,
            "message": message,
            "today": date.today().strftime("%y.%m.%d"),
            "replacement_selection": None,
            "editable_item_selection": None,
            "general_tolerance_candidate_count": 0,
            "general_tolerance_selected_count": 0,
            "general_tolerance_manual_count": 0,
            "general_tolerance_applied_count": 0,
            "general_tolerance_marked": False,
            "dimension_marking_candidate_count": 0,
            "dimension_marking_selected_count": 0,
            "build_id": APP_BUILD_ID,
            "local_ocr_ready": local_ocr_available(),
        }

    def get_initial_state(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._empty_state()
            return self._state()

    def _editable_item_state(self) -> dict[str, Any] | None:
        index = self.editable_item_index
        if index is None or not 0 <= index < len(self.items):
            self.editable_item_index = None
            return None
        item = self.items[index]
        if item.page_index != self.page_index:
            return None
        if isinstance(item, StampMark):
            rect = stamp_mark_rect(item)
            mode = "quality_stamp" if item.kind == "quality" else "process_stamp"
            size = item.size
        elif isinstance(item, ProcedureNoteMark) and item.kind != "measurement":
            rect = procedure_note_rect(item)
            mode = "procedure_note"
            size = item.font_size
        elif isinstance(item, DimensionMark):
            rect = dimension_label_rect(item)
            mode = "dimension"
            size = item.font_size
        elif isinstance(item, ReplacementMark):
            rect = replacement_content_rect(item)
            mode = "replace"
            size = item.font_size
        elif isinstance(item, GeneralToleranceBatchMark):
            addition_index = self.editable_tolerance_index
            if (
                addition_index is None
                or not 0 <= addition_index < len(item.additions)
            ):
                self.editable_item_index = None
                self.editable_tolerance_index = None
                return None
            addition = item.additions[addition_index]
            rect = self._full_tolerance_addition_rect(addition)
            mode = "general_tolerance"
            size = addition.font_size
        else:
            self.editable_item_index = None
            self.editable_tolerance_index = None
            return None
        state = {
            "index": index,
            "mode": mode,
            "rect": list(rect),
            "size": round(size, 3),
            "move_only": False,
        }
        if isinstance(item, DimensionMark):
            state["target"] = list(item.target)
            state["target_movable"] = item.show_leader
        return state

    def _select_editable_item_at(
        self,
        mode: str,
        point: fitz.Point,
    ) -> bool:
        for index in range(len(self.items) - 1, -1, -1):
            item = self.items[index]
            if item.page_index != self.page_index:
                continue
            if isinstance(item, StampMark):
                item_mode = (
                    "quality_stamp" if item.kind == "quality" else "process_stamp"
                )
                if item_mode != mode:
                    continue
                rect = stamp_mark_rect(item)
            elif (
                isinstance(item, ProcedureNoteMark)
                and item.kind != "measurement"
                and mode == "procedure_note"
            ):
                rect = procedure_note_rect(item)
            elif isinstance(item, DimensionMark) and mode == "dimension":
                rect = dimension_label_rect(item)
            elif isinstance(item, ReplacementMark) and mode == "replace":
                rect = replacement_content_rect(item)
            elif isinstance(item, GeneralToleranceBatchMark) and mode == "general_tolerance":
                for addition_index in range(len(item.additions) - 1, -1, -1):
                    addition = item.additions[addition_index]
                    hit_rect = self._full_tolerance_addition_rect(addition)
                    hit_rect.x0 -= 4
                    hit_rect.y0 -= 4
                    hit_rect.x1 += 4
                    hit_rect.y1 += 4
                    if point in hit_rect:
                        self.editable_item_index = index
                        self.editable_tolerance_index = addition_index
                        return True
                continue
            else:
                continue
            hit_rect = fitz.Rect(rect)
            hit_rect.x0 -= 3
            hit_rect.y0 -= 3
            hit_rect.x1 += 3
            hit_rect.y1 += 3
            if point in hit_rect:
                self.editable_item_index = index
                return True
        self.editable_item_index = None
        self.editable_tolerance_index = None
        return False

    def select_general_tolerance_addition(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            page = self.document[self.page_index]
            point = fitz.Point(
                _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
            )
            if self._select_editable_item_at("general_tolerance", point):
                return self._state("追加公差をドラッグして移動できます。")
            return self._state("移動する追加公差をクリックしてください。")

    def cancel_editable_item_selection(self) -> dict[str, Any]:
        with self.lock:
            self.editable_item_index = None
            self.editable_tolerance_index = None
            return self._state()

    def update_editable_item(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            index = self.editable_item_index
            if index is None or not 0 <= index < len(self.items):
                return self._state("移動する印・注記を選んでください。")
            page = self.document[self.page_index]
            x0 = _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1)
            y0 = _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1)
            x1 = _number(payload.get("x1"), x0, page.rect.x0, page.rect.x1)
            y1 = _number(payload.get("y1"), y0, page.rect.y0, page.rect.y1)
            rect = fitz.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            item = self.items[index]
            if isinstance(item, StampMark):
                size = max(24.0, min(180.0, max(rect.width, rect.height)))
                center = (
                    (rect.x0 + rect.x1) / 2,
                    (rect.y0 + rect.y1) / 2,
                )
                self.items[index] = replace(item, center=center, size=size)
                message = "印の位置とサイズを変更しました。"
            elif isinstance(item, DimensionMark):
                original = dimension_label_rect(item)
                width_scale = rect.width / max(1.0, original.width)
                height_scale = rect.height / max(1.0, original.height)
                scale = max(width_scale, height_scale)
                font_size = max(5.0, min(36.0, item.font_size * scale))
                target = (
                    _number(
                        payload.get("target_x"),
                        item.target[0],
                        page.rect.x0,
                        page.rect.x1,
                    ),
                    _number(
                        payload.get("target_y"),
                        item.target[1],
                        page.rect.y0,
                        page.rect.y1,
                    ),
                )
                self.items[index] = replace(
                    item,
                    label=(rect.x0, rect.y0),
                    target=target,
                    font_size=font_size,
                )
                self._invalidate_dimension_markings()
                message = "追加寸法の位置と大きさを変更しました。矢印は文字位置へ追従します。"
            elif isinstance(item, ReplacementMark):
                original = replacement_content_rect(item)
                width_scale = rect.width / max(1.0, original.width)
                height_scale = rect.height / max(1.0, original.height)
                scale = max(width_scale, height_scale)
                origin = fitz.Point(item.origin or (original.x0, original.y1))
                origin += fitz.Point(
                    rect.x0 - original.x0,
                    rect.y0 - original.y0,
                )
                self.items[index] = replace(
                    item,
                    origin=(origin.x, origin.y),
                    font_size=max(5.0, min(36.0, item.font_size * scale)),
                    tolerance_font_size=max(
                        4.0,
                        min(
                            36.0,
                            (
                                item.tolerance_font_size
                                or item.font_size * 0.8
                            )
                            * scale,
                        ),
                    ),
                )
                self._invalidate_dimension_markings()
                message = "書き直した寸法・公差の位置と大きさを変更しました。"
            elif isinstance(item, ProcedureNoteMark) and item.kind != "measurement":
                original = procedure_note_rect(item)
                width_scale = rect.width / max(1.0, original.width)
                height_scale = rect.height / max(1.0, original.height)
                scale = max(width_scale, height_scale)
                font_size = max(6.0, min(24.0, item.font_size * scale))
                self.items[index] = replace(
                    item,
                    origin=(rect.x0, rect.y0),
                    font_size=font_size,
                )
                message = "注記の位置とサイズを変更しました。"
            else:
                self.editable_item_index = None
                return self._state("この項目はマウス編集の対象外です。")
            return self._state(message)

    def move_general_tolerance_addition(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            item_index = self.editable_item_index
            addition_index = self.editable_tolerance_index
            if (
                item_index is None
                or not 0 <= item_index < len(self.items)
                or addition_index is None
            ):
                return self._state("移動する追加公差を選んでください。")
            item = self.items[item_index]
            if (
                not isinstance(item, GeneralToleranceBatchMark)
                or not 0 <= addition_index < len(item.additions)
            ):
                return self._state("移動する追加公差を選んでください。")
            page = self.document[self.page_index]
            x0 = _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1)
            y0 = _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1)
            x1 = _number(payload.get("x1"), x0, page.rect.x0, page.rect.x1)
            y1 = _number(payload.get("y1"), y0, page.rect.y0, page.rect.y1)
            addition = item.additions[addition_index]
            old_rect = self._full_tolerance_addition_rect(addition)
            delta = fitz.Point(x0 - old_rect.x0, y0 - old_rect.y0)
            requested_rect = fitz.Rect(
                min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
            )
            width_scale = requested_rect.width / max(old_rect.width, 1e-6)
            height_scale = requested_rect.height / max(old_rect.height, 1e-6)
            # 縦横比を保ったまま、選択枠に収まるよう小さい方に合わせる。
            scale = min(width_scale, height_scale)
            font_size = max(4.0, min(24.0, addition.font_size * scale))
            suffix_base = (
                addition.suffix_font_size
                if addition.suffix_font_size is not None
                else addition.font_size
            )
            moved = replace(
                addition,
                origin=(
                    addition.origin[0] + delta.x,
                    addition.origin[1] + delta.y,
                ),
                font_size=font_size,
                suffix_font_size=(
                    max(4.0, min(24.0, suffix_base * scale))
                    if addition.suffix_font_size is not None
                    else None
                ),
            )
            additions = list(item.additions)
            additions[addition_index] = moved
            self.items[item_index] = replace(item, additions=tuple(additions))
            if addition_index < len(self.last_general_tolerance_additions):
                self.last_general_tolerance_additions[addition_index] = moved
            if self.last_general_tolerance_marked:
                self.items = [
                    existing
                    for existing in self.items
                    if not (
                        isinstance(existing, DimensionMarkingBatch)
                        and existing.page_index == self.page_index
                    )
                ]
                self.last_general_tolerance_marked = False
            return self._state(
                "追加公差の位置と大きさを変更しました。色分けにも調整後の状態を使用します。"
            )

    def _state(self, message: str = "") -> dict[str, Any]:
        if self.document is None or self.source_path is None:
            return self._empty_state(message or "PDFを開いてください。")

        page = self.document[self.page_index]
        preview_items = list(self.items)
        if self.replacement_selection is not None:
            selected = self.replacement_selection
            preview_items.append(
                Mark(
                    self.page_index,
                    selected.rect,
                    "#5aa7ff",
                    0.34,
                    selected.quad,
                )
            )
        for candidate in self.general_tolerance_candidates:
            if candidate.manual_required:
                preview_color = "#ff9f1c"
                preview_opacity = 0.38
            elif candidate.selected:
                preview_color = "#42c7df"
                preview_opacity = 0.34
            else:
                preview_color = "#c8cdd4"
                preview_opacity = 0.18
            preview_items.append(
                Mark(
                    self.page_index,
                    candidate.rect,
                    preview_color,
                    preview_opacity,
                    candidate.quad,
                )
            )
        for candidate in self.dimension_marking_candidates:
            if candidate.selected:
                preview_color = candidate.color
                preview_opacity = 0.38
            else:
                preview_color = "#c8cdd4"
                preview_opacity = 0.18
            preview_items.append(
                Mark(
                    self.page_index,
                    candidate.rect,
                    preview_color,
                    preview_opacity,
                    candidate.quad,
                )
            )
        if self.word_candidate is not None:
            preview_items.append(self.word_candidate)
        if self.work_region_candidates:
            preview_items.append(
                WorkRegionMark(
                    self.page_index,
                    tuple(self.work_region_candidates),
                    self.work_region_color,
                    self.work_region_opacity,
                )
            )
        image = render_page_preview(
            self.document,
            self.page_index,
            preview_items,
            zoom=1.8,
        )
        current_items = sum(
            item.page_index == self.page_index for item in self.items
        )
        has_text = bool(page.get_text("words"))
        dimension_style = self.dimension_style_cache.get(self.page_index)
        if dimension_style is None:
            dimension_style = infer_dimension_style(page)
            self.dimension_style_cache[self.page_index] = dimension_style
        replacement_selection = None
        if self.replacement_selection is not None:
            selected = self.replacement_selection
            replacement_selection = {
                "original_text": (
                    (
                        selected.preserved_prefix
                        + selected.nominal_text.strip()
                    )
                    or selected.text.strip()
                    or "画像範囲（文字情報なし）"
                ),
                "original_value": selected.nominal_text.strip(),
                "font_size": round(selected.font_size, 2),
                "font_name": selected.font_name,
                "has_text": bool(selected.nominal_text.strip()),
                "rect": list(selected.rect),
                "whiteout_rect": list(
                    selected.replacement_rect or selected.rect
                ),
                "origin": list(
                    selected.origin
                    or (selected.rect[0] + 1.0, selected.rect[3] - 1.0)
                ),
                "direction": list(selected.direction),
                "selection_key": ":".join(
                    f"{coordinate:.2f}" for coordinate in selected.rect
                ),
            }
        editable_item_selection = self._editable_item_state()
        word_candidate_angle = None
        if self.word_candidate is not None and self.word_candidate.quad:
            first = fitz.Point(self.word_candidate.quad[0])
            second = fitz.Point(self.word_candidate.quad[1])
            edge = second - first
            word_candidate_angle = math.degrees(
                math.atan2(edge.y, edge.x)
            )
            while word_candidate_angle > 90:
                word_candidate_angle -= 180
            while word_candidate_angle <= -90:
                word_candidate_angle += 180
        return {
            "ok": True,
            "loaded": True,
            "message": message,
            "file_name": self.display_name or self.source_path.name,
            "page_index": self.page_index,
            "page_number": self.page_index + 1,
            "page_count": self.document.page_count,
            "pdf_width": page.rect.width,
            "pdf_height": page.rect.height,
            "image": "data:image/png;base64,"
            + base64.b64encode(image).decode("ascii"),
            "item_count": len(self.items),
            "page_item_count": current_items,
            "has_text": has_text,
            "dimension_style": {
                "font_name": dimension_style.font_name,
                "font_size": round(dimension_style.font_size, 2),
                "line_width": round(dimension_style.line_width, 2),
            },
            "today": date.today().strftime("%y.%m.%d"),
            "replacement_selection": replacement_selection,
            "editable_item_selection": editable_item_selection,
            "general_tolerance_candidate_count": len(
                self.general_tolerance_candidates
            ),
            "general_tolerance_selected_count": sum(
                candidate.selected
                for candidate in self.general_tolerance_candidates
            ),
            "general_tolerance_manual_count": sum(
                candidate.manual_required
                for candidate in self.general_tolerance_candidates
            ),
            "general_tolerance_applied_count": len(
                self.last_general_tolerance_batch
            ),
            "general_tolerance_standard": self.general_tolerance_standard,
            "general_tolerance_grade": self.general_tolerance_grade,
            "general_tolerance_angle_length": (
                self.general_tolerance_angle_length
            ),
            "general_tolerance_marked": self.last_general_tolerance_marked,
            "dimension_marking_candidate_count": len(
                self.dimension_marking_candidates
            ),
            "dimension_marking_selected_count": sum(
                candidate.selected
                for candidate in self.dimension_marking_candidates
            ),
            "added_dimension_count": sum(
                isinstance(item, DimensionMark)
                and item.page_index == self.page_index
                for item in self.items
            ),
            "replacement_dimension_count": sum(
                isinstance(item, ReplacementMark)
                and item.page_index == self.page_index
                for item in self.items
            ),
            "struck_dimension_count": sum(
                isinstance(item, StrikeMark)
                and item.page_index == self.page_index
                for item in self.items
            ),
            "work_region_candidate_count": len(
                self.work_region_candidates
            ),
            "word_candidate": self.word_candidate is not None,
            "word_candidate_angle": (
                round(word_candidate_angle, 1)
                if word_candidate_angle is not None
                else None
            ),
            "build_id": APP_BUILD_ID,
            "local_ocr_ready": local_ocr_available(),
            "page_uses_local_ocr": _needs_local_ocr(page),
        }

    def _error(self, message: str) -> dict[str, Any]:
        return {"ok": False, "message": message}

    def open_pdf(self) -> dict[str, Any]:
        if self.window is None:
            return self._error("ウィンドウの準備ができていません。")
        paths = self.window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=PDF_FILE_TYPES,
        )
        if not paths:
            return {"ok": False, "cancelled": True, "message": ""}
        return self.load_pdf(paths[0])

    def load_pdf(self, path_value: str) -> dict[str, Any]:
        with self.lock:
            try:
                path = Path(path_value).expanduser().resolve()
                if path.suffix.lower() != ".pdf":
                    return self._error("PDFファイルを指定してください。")
                if not path.is_file():
                    return self._error("指定したPDFが見つかりません。")
                new_document = fitz.open(path)
                if new_document.needs_pass:
                    new_document.close()
                    return self._error("パスワード付きPDFには対応していません。")
                if new_document.page_count < 1:
                    new_document.close()
                    return self._error("ページのないPDFは開けません。")
            except Exception as exc:
                return self._error(f"PDFを開けませんでした: {exc}")

            if self.document is not None:
                self.document.close()
            self.document = new_document
            self.source_path = path
            self.display_name = path.name
            self.page_index = 0
            self.items.clear()
            self.replacement_selection = None
            self.editable_item_index = None
            self._clear_review_candidates()
            self.last_general_tolerance_batch.clear()
            self.last_general_tolerance_additions.clear()
            self.last_general_tolerance_marked = False
            self.work_region_candidates.clear()
            self.word_candidate = None
            self.dimension_style_cache.clear()
            self.general_tolerance_detection_cache.clear()
            self.scanned_marking_cache.clear()
            self.local_ocr_cache.clear()
            self.scanned_tile_cache.clear()
            return self._state(
                "PDFを読み込みました。使いたいツールを選んで図面をクリックしてください。"
            )

    def load_pdf_bytes(
        self,
        file_name: str,
        content: bytes,
    ) -> dict[str, Any]:
        safe_name = Path(file_name or "document.pdf").name
        if not safe_name.lower().endswith(".pdf"):
            return self._error("PDFファイルを指定してください。")
        if not content:
            return self._error("PDFファイルが空です。")
        upload_path = Path(self.upload_directory.name, "current.pdf")
        try:
            upload_path.write_bytes(content)
        except OSError as exc:
            return self._error(f"PDFを読み込めませんでした: {exc}")
        result = self.load_pdf(str(upload_path))
        if result.get("ok") and result.get("loaded"):
            self.display_name = safe_name
            result["file_name"] = safe_name
        return result

    def previous_page(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if self.page_index > 0:
                self.page_index -= 1
                self.replacement_selection = None
                self.editable_item_index = None
                self._clear_review_candidates()
                self.last_general_tolerance_batch.clear()
                self.last_general_tolerance_additions.clear()
                self.last_general_tolerance_marked = False
                self.work_region_candidates.clear()
                self.word_candidate = None
            return self._state()

    def next_page(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if self.page_index < self.document.page_count - 1:
                self.page_index += 1
                self.replacement_selection = None
                self.editable_item_index = None
                self._clear_review_candidates()
                self.last_general_tolerance_batch.clear()
                self.last_general_tolerance_additions.clear()
                self.last_general_tolerance_marked = False
                self.work_region_candidates.clear()
                self.word_candidate = None
            return self._state()

    def undo(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if self.work_region_candidates:
                self.work_region_candidates.clear()
                return self._state("ワークの候補選択を取り消しました。")
            if self.general_tolerance_candidates:
                self._clear_review_candidates()
                return self._state("一般公差の検出候補を取り消しました。")
            if self.word_candidate is not None:
                self.word_candidate = None
                return self._state(
                    "文字・記号の自動選択候補を取り消しました。"
                )
            if self.items:
                removed_item = self.items.pop()
                self.editable_item_index = None
                if isinstance(removed_item, GeneralToleranceBatchMark):
                    self.last_general_tolerance_batch.clear()
                    self.last_general_tolerance_additions.clear()
                    self.last_general_tolerance_marked = False
                elif isinstance(removed_item, DimensionMarkingBatch):
                    self.last_general_tolerance_marked = False
                return self._state("直前の操作を取り消しました。")
            return self._state("取り消せる操作はありません。")

    def clear_page(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            before = len(self.items)
            self.items = [
                item for item in self.items if item.page_index != self.page_index
            ]
            self.editable_item_index = None
            self.work_region_candidates.clear()
            self.word_candidate = None
            self._clear_review_candidates()
            self.last_general_tolerance_batch.clear()
            self.last_general_tolerance_additions.clear()
            self.last_general_tolerance_marked = False
            removed = before - len(self.items)
            return self._state(f"このページの追加内容を{removed}件消去しました。")

    def clear_all(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            removed = len(self.items)
            self.items.clear()
            self.editable_item_index = None
            self.work_region_candidates.clear()
            self.word_candidate = None
            self._clear_review_candidates()
            self.last_general_tolerance_batch.clear()
            self.last_general_tolerance_additions.clear()
            self.last_general_tolerance_marked = False
            return self._state(f"すべての追加内容を{removed}件消去しました。")

    def confirm_word_candidate(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if self.word_candidate is None:
                return self._state(
                    "確定する文字・記号の候補がありません。"
                )
            self.items.append(self.word_candidate)
            self.word_candidate = None
            return self._state(
                "選択候補をマーキングしました。"
            )

    def cancel_word_candidate(self) -> dict[str, Any]:
        with self.lock:
            self.word_candidate = None
            return self._state(
                "文字・記号の自動選択候補を取り消しました。"
            )

    def detect_work_region(
        self,
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            page = self.document[self.page_index]
            point = fitz.Point(
                _number(
                    payload.get("x"),
                    0,
                    page.rect.x0,
                    page.rect.x1,
                ),
                _number(
                    payload.get("y"),
                    0,
                    page.rect.y0,
                    page.rect.y1,
                ),
            )
            operation = str(payload.get("operation") or "replace")
            if operation == "remove":
                before = len(self.work_region_candidates)
                self.work_region_candidates = [
                    polygon
                    for polygon in self.work_region_candidates
                    if not _point_in_polygon(
                        (point.x, point.y),
                        polygon,
                    )
                ]
                removed = before - len(self.work_region_candidates)
                return self._state(
                    "候補範囲を除外しました。"
                    if removed
                    else "クリック位置に除外できる候補がありません。"
                )
            try:
                polygon = expand_work_region(
                    detect_enclosed_region(page, point),
                    page.rect,
                )
            except ValueError as exc:
                return self._state(str(exc))
            if operation != "add":
                self.work_region_candidates.clear()
            duplicate = any(
                _same_work_region(existing, polygon)
                for existing in self.work_region_candidates
            )
            if not duplicate:
                self.work_region_candidates.append(polygon)
            self.work_region_color = _color(settings.get("color"))
            self.work_region_opacity = _number(
                settings.get("opacity"),
                0.32,
                0.08,
                1.0,
            )
            return self._state(
                "候補を表示しました。範囲を確認して確定してください。"
            )

    def predict_work_shape(
        self,
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a workpiece candidate from ordered outline anchors.

        Two-point interior-seed input is retained for compatibility with
        projects saved before the guided contour workflow was introduced.
        """

        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            raw_points = payload.get("points")
            if not isinstance(raw_points, list) or not 2 <= len(raw_points) <= 32:
                return self._state(
                    "ワーク外形の角・変曲点を順番に3～32点指定してください。"
                )
            page = self.document[self.page_index]
            points: list[fitz.Point] = []
            for raw_point in raw_points:
                if not isinstance(raw_point, dict):
                    continue
                points.append(
                    fitz.Point(
                        _number(
                            raw_point.get("x"),
                            0,
                            page.rect.x0,
                            page.rect.x1,
                        ),
                        _number(
                            raw_point.get("y"),
                            0,
                            page.rect.y0,
                            page.rect.y1,
                        ),
                    )
                )
            if len(points) != len(raw_points):
                return self._state("指定点の形式を確認してください。")

            outline_error: ValueError | None = None
            if len(points) >= 3:
                try:
                    outline = predict_work_outline(
                        page,
                        tuple((point.x, point.y) for point in points),
                    )
                except ValueError as exc:
                    outline_error = exc
                else:
                    self.work_region_candidates = [
                        expand_work_region(outline, page.rect)
                    ]
                    self.work_region_color = _color(settings.get("color"))
                    self.work_region_opacity = _number(
                        settings.get("opacity"),
                        0.32,
                        0.08,
                        1.0,
                    )
                    return self._state(
                        f"{len(points)}点の輪郭からワーク外形を予測しました。"
                        "黄色い候補を確認し、必要な場合だけ修正してください。"
                    )

            # Compatibility fallback for old interior-seed input and for
            # drawings whose contour line cannot be traced reliably.
            predicted: list[tuple[tuple[float, float], ...]] = []
            successful_points = 0
            for point in points:
                try:
                    polygon = detect_enclosed_region(page, point)
                except ValueError:
                    continue
                successful_points += 1
                if any(
                    _same_work_region(existing, polygon)
                    for existing in predicted
                ):
                    continue
                predicted.append(
                    expand_work_region(polygon, page.rect)
                )
            if not predicted:
                self.work_region_candidates.clear()
                detail = (
                    str(outline_error)
                    if outline_error is not None
                    else "指定点から閉じたワーク形状を予測できませんでした。"
                )
                return self._state(
                    f"{detail}"
                    "外形線の角・変曲点を輪郭順にクリックするか、"
                    "手動方式を使用してください。"
                )
            self.work_region_candidates = predicted
            self.work_region_color = _color(settings.get("color"))
            self.work_region_opacity = _number(
                settings.get("opacity"),
                0.32,
                0.08,
                1.0,
            )
            skipped = len(raw_points) - successful_points
            message = (
                f"{len(raw_points)}点から{len(predicted)}か所の"
                "ワーク形状を予測しました。"
                "不足は追加、不要部分は除外してから確定してください。"
            )
            if skipped:
                message += f"（判定できなかった点: {skipped}点）"
            return self._state(message)

    def confirm_work_region(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if not self.work_region_candidates:
                return self._state("確定する候補範囲がありません。")
            self.items.append(
                WorkRegionMark(
                    self.page_index,
                    tuple(self.work_region_candidates),
                    self.work_region_color,
                    self.work_region_opacity,
                )
            )
            count = len(self.work_region_candidates)
            self.work_region_candidates.clear()
            return self._state(
                f"半自動で選択したワーク範囲を{count}か所マーキングしました。"
            )

    def cancel_work_region(self) -> dict[str, Any]:
        with self.lock:
            self.work_region_candidates.clear()
            return self._state("ワークの候補選択を解除しました。")

    def select_replacement(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            page = self.document[self.page_index]

            if all(key in payload for key in ("x0", "y0", "x1", "y1")):
                x0 = _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1)
                y0 = _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1)
                x1 = _number(payload.get("x1"), 0, page.rect.x0, page.rect.x1)
                y1 = _number(payload.get("y1"), 0, page.rect.y0, page.rect.y1)
                rect = fitz.Rect(
                    min(x0, x1),
                    min(y0, y1),
                    max(x0, x1),
                    max(y0, y1),
                )
                if rect.width < 2 or rect.height < 2:
                    return self._state(
                        "修正する元の寸法値を囲んでください。"
                    )
                self.replacement_selection = TextHit(
                    rect=(rect.x0, rect.y0, rect.x1, rect.y1),
                    text="画像範囲（文字情報なし）",
                    direction=(1.0, 0.0),
                    font_size=min(
                        36.0,
                        max(5.0, rect.height * 0.72),
                    ),
                    origin=(rect.x0 + 1.0, rect.y1 - 1.0),
                )
            else:
                point = fitz.Point(
                    _number(
                        payload.get("x"),
                        0,
                        page.rect.x0,
                        page.rect.x1,
                    ),
                    _number(
                        payload.get("y"),
                        0,
                        page.rect.y0,
                        page.rect.y1,
                    ),
                )
                if self._select_editable_item_at("replace", point):
                    self.replacement_selection = None
                    return self._state(
                        "書き直した寸法・公差を選択しました。青枠で移動・サイズ変更できます。"
                    )
                hit = find_text_group(page, point)
                if hit is None:
                    return self._state(
                        "修正する寸法値の中央をクリックしてください。"
                    )
                self.replacement_selection = hit
            return self._state(
                "元の寸法値を選択しました。修正後の値を入力してください。"
            )

    def confirm_replacement(
        self,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            selection = self.replacement_selection
            if selection is None:
                return self._state(
                    "先に修正する元の寸法値を選択してください。"
                )
            value = str(
                settings.get("replacement_value") or ""
            ).strip()
            if not value:
                return self._state("修正後の寸法値を入力してください。")

            font_size = _number(
                settings.get("replacement_size"),
                selection.font_size,
                5.0,
                36.0,
            )
            tolerance_font_size = _number(
                settings.get("replacement_tolerance_size"),
                font_size * 0.8,
                4.0,
                36.0,
            )
            value_offset = (
                _number(settings.get("replacement_value_x"), 0.0, -100.0, 100.0),
                _number(settings.get("replacement_value_y"), 0.0, -100.0, 100.0),
            )
            tolerance_offset = (
                _number(settings.get("replacement_tolerance_x"), 0.0, -100.0, 100.0),
                _number(settings.get("replacement_tolerance_y"), 0.0, -100.0, 100.0),
            )
            self._invalidate_dimension_markings()
            self.items.append(
                ReplacementMark(
                    self.page_index,
                    selection.replacement_rect or selection.rect,
                    selection.direction,
                    value,
                    str(
                        settings.get("upper_tolerance") or ""
                    ).strip(),
                    str(
                        settings.get("lower_tolerance") or ""
                    ).strip(),
                    font_size,
                    tolerance_font_size,
                    value_offset,
                    tolerance_offset,
                    origin=selection.origin,
                    font_name=selection.font_name,
                    font_color=selection.font_color,
                )
            )
            self.editable_item_index = len(self.items) - 1
            self.replacement_selection = None
            return self._state(
                f"寸法値を「{value}」に修正しました。"
            )

    def cancel_replacement(self) -> dict[str, Any]:
        with self.lock:
            self.replacement_selection = None
            return self._state("寸法値の選択を解除しました。")

    def scan_general_tolerances(
        self,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        """OCR the current page and show reviewable tolerance candidates."""

        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            standard = str(
                settings.get("general_tolerance_standard") or "jis_b_0405"
            )
            grade = str(
                settings.get("general_tolerance_grade") or "m"
            )
            try:
                angle_length = float(
                    settings.get("general_tolerance_angle_length") or 10
                )
            except (TypeError, ValueError):
                return self._state("角度寸法の短辺長さ区分を選択してください。")
            if standard not in {"pisco", "jis_b_0405"}:
                return self._state("一般公差規格を選択してください。")
            if grade not in {"f", "m"}:
                return self._state("JISの公差等級を選択してください。")
            if angle_length not in {10.0, 50.0, 120.0, 400.0, 401.0}:
                return self._state("角度寸法の短辺長さ区分を選択してください。")
            self.general_tolerance_standard = standard
            self.general_tolerance_grade = grade
            self.general_tolerance_angle_length = angle_length
            self._clear_review_candidates()
            self.last_general_tolerance_batch.clear()
            self.last_general_tolerance_additions.clear()
            self.last_general_tolerance_marked = False
            cache_key = (self.page_index, standard, grade, angle_length)
            cached_candidates = self.general_tolerance_detection_cache.get(
                cache_key
            )
            try:
                if cached_candidates is None:
                    detected_candidates = detect_general_tolerance_candidates(
                        self.document[self.page_index],
                        self.page_index,
                        standard=standard,
                        grade=grade,
                        ocr_script=_resource_path("windows_ocr.ps1"),
                        angle_shorter_side_length=angle_length,
                        local_ocr_page=self._shared_local_ocr(
                            self.document[self.page_index]
                        ),
                        scanned_tile_lines=self._shared_scanned_tiles(
                            self.document[self.page_index]
                        ),
                        scanned_tile_cache=self.scanned_tile_cache,
                    )
                    self.general_tolerance_detection_cache[cache_key] = tuple(
                        detected_candidates
                    )
                else:
                    detected_candidates = list(cached_candidates)
                self.general_tolerance_candidates = [
                    replace(candidate, selected=not candidate.manual_required)
                    for candidate in detected_candidates
                ]
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                return self._state(
                    "寸法候補を検出できませんでした。"
                    f" Windowsの日本語OCR設定を確認してください: {exc}"
                )
            count = len(self.general_tolerance_candidates)
            OcrPipelineRecorder("scan_general_tolerances").set_count(
                "final_candidates", count
            ).log_summary()
            if not count:
                return self._state(
                    "一般公差を安全に付与できる寸法候補が見つかりませんでした。"
                )
            manual_count = sum(
                candidate.manual_required
                for candidate in self.general_tolerance_candidates
            )
            automatic_count = count - manual_count
            notes = extract_drawing_tolerance_notes(
                self.document[self.page_index].get_text()
            )
            recognized_notes: list[str] = []
            if notes.angle_tolerance is not None:
                recognized_notes.append(
                    f"指示無き角度{notes.angle_tolerance[1]}"
                )
            if notes.unindicated_chamfer_maximum is not None:
                recognized_notes.append(
                    "指示無き角部"
                    f"C{notes.unindicated_chamfer_maximum:g}以下"
                )
            if notes.unindicated_radius_maximum is not None:
                recognized_notes.append(
                    "指示無き隅"
                    f"R{notes.unindicated_radius_maximum:g}以下"
                )
            message = f"候補を{automatic_count}件表示しました。"
            if manual_count:
                message += f" 個別確認が必要な候補が{manual_count}件あります。"
            if recognized_notes:
                message += " 図面注記を優先: " + "、".join(recognized_notes) + "。"
            message += " 水色をクリックすると除外（灰色）できます。"
            return self._state(message)

    def toggle_general_tolerance(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if not self.general_tolerance_candidates:
                return self._state("先に寸法候補を検出してください。")
            page = self.document[self.page_index]
            point = fitz.Point(
                _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
            )
            if any(
                candidate.manual_required
                and point
                in fitz.Rect(candidate.rect) + (-8, -8, 8, 8)
                for candidate in self.general_tolerance_candidates
            ):
                return self._state(
                    "オレンジ色は個別公差の確認候補のため、"
                    "一括反映の対象にはできません。"
                )
            before = sum(
                candidate.selected
                for candidate in self.general_tolerance_candidates
            )
            self.general_tolerance_candidates = toggle_candidate(
                self.general_tolerance_candidates,
                point,
            )
            after = sum(
                candidate.selected
                for candidate in self.general_tolerance_candidates
            )
            if before == after:
                return self._state("水色または灰色の候補をクリックしてください。")
            return self._state(
                f"反映対象 {after}件。水色＝反映、灰色＝除外。"
            )

    def cancel_general_tolerance_candidates(self) -> dict[str, Any]:
        with self.lock:
            self.general_tolerance_candidates.clear()
            return self._state("一般公差の検出候補を閉じました。")

    def _applied_tolerance_hit(
        self,
        point: fitz.Point,
    ) -> tuple[int, int] | None:
        """Return (items_index, addition_index) for an applied tolerance."""

        for index in range(len(self.items) - 1, -1, -1):
            item = self.items[index]
            if (
                not isinstance(item, GeneralToleranceBatchMark)
                or item.page_index != self.page_index
            ):
                continue
            for addition_index, addition in enumerate(item.additions):
                hit_rect = self._full_tolerance_addition_rect(addition)
                hit_rect.x0 -= 6
                hit_rect.y0 -= 6
                hit_rect.x1 += 6
                hit_rect.y1 += 6
                if addition_index < len(self.last_general_tolerance_batch):
                    nominal_rect = fitz.Rect(
                        self.last_general_tolerance_batch[addition_index].rect
                    )
                    hit_rect |= nominal_rect
                if point in hit_rect:
                    return index, addition_index
        return None

    def _dimension_marking_hit(
        self,
        point: fitz.Point,
    ) -> tuple[int, int] | None:
        """Return (items_index, entry_index) for a color marking."""

        for index in range(len(self.items) - 1, -1, -1):
            item = self.items[index]
            if (
                not isinstance(item, DimensionMarkingBatch)
                or item.page_index != self.page_index
            ):
                continue
            for entry_index, entry in enumerate(item.entries):
                hit_rect = fitz.Rect(entry.rect)
                hit_rect.x0 -= 4
                hit_rect.y0 -= 4
                hit_rect.x1 += 4
                hit_rect.y1 += 4
                if point in hit_rect:
                    return index, entry_index
        return None

    def _remove_applied_tolerance_at(
        self,
        item_index: int,
        addition_index: int,
    ) -> bool:
        item = self.items[item_index]
        if not isinstance(item, GeneralToleranceBatchMark):
            return False
        if not 0 <= addition_index < len(item.additions):
            return False
        removed_candidate = None
        if addition_index < len(self.last_general_tolerance_batch):
            removed_candidate = self.last_general_tolerance_batch[addition_index]
        new_additions = tuple(
            addition
            for index, addition in enumerate(item.additions)
            if index != addition_index
        )
        if new_additions:
            self.items[item_index] = GeneralToleranceBatchMark(
                item.page_index,
                new_additions,
            )
        else:
            self.items.pop(item_index)
        if addition_index < len(self.last_general_tolerance_batch):
            self.last_general_tolerance_batch.pop(addition_index)
        if addition_index < len(self.last_general_tolerance_additions):
            self.last_general_tolerance_additions.pop(addition_index)
        if removed_candidate is not None and self.last_general_tolerance_marked:
            self._remove_dimension_marking_for_candidate(removed_candidate)
        self.editable_item_index = None
        self.editable_tolerance_index = None
        return True

    def _remove_dimension_marking_for_candidate(
        self,
        candidate: GeneralToleranceCandidate,
    ) -> None:
        candidate_rect = fitz.Rect(candidate.rect)
        for index in range(len(self.items) - 1, -1, -1):
            item = self.items[index]
            if (
                not isinstance(item, DimensionMarkingBatch)
                or item.page_index != self.page_index
            ):
                continue
            kept_entries = []
            for entry in item.entries:
                entry_rect = fitz.Rect(entry.rect)
                overlap = (candidate_rect & entry_rect).get_area()
                if overlap >= min(
                    candidate_rect.get_area(),
                    entry_rect.get_area(),
                ) * 0.35:
                    continue
                kept_entries.append(entry)
            if kept_entries:
                self.items[index] = DimensionMarkingBatch(
                    item.page_index,
                    tuple(kept_entries),
                )
            else:
                self.items.pop(index)
                self.last_general_tolerance_marked = False

    def remove_applied_general_tolerance(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Remove one applied general-tolerance addition from the drawing."""

        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if not self.last_general_tolerance_batch:
                return self._state("解除できる反映済み公差がありません。")
            page = self.document[self.page_index]
            point = fitz.Point(
                _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
            )
            hit = self._applied_tolerance_hit(point)
            if hit is None:
                return self._state(
                    "解除する公差をクリックしてください。"
                    " 寸法値または追加した公差の近くを選びます。"
                )
            if not self._remove_applied_tolerance_at(*hit):
                return self._state("公差の解除に失敗しました。")
            remaining = len(self.last_general_tolerance_batch)
            if remaining:
                return self._state(
                    f"公差を1件解除しました。残り{remaining}件です。"
                    " 色分け済みの場合は、対応する色も外れます。"
                )
            self.last_general_tolerance_marked = False
            return self._state(
                "反映済み公差をすべて解除しました。"
                " 必要なら再度「対象寸法を検出」からやり直してください。"
            )

    def remove_dimension_marking(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Remove one automatic dimension/tolerance color marking."""

        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            page = self.document[self.page_index]
            point = fitz.Point(
                _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
            )
            hit = self._dimension_marking_hit(point)
            if hit is None:
                return self._state(
                    "解除する色分けをクリックしてください。"
                    " ピンクまたは黄色のマーキング上を選びます。"
                )
            item_index, entry_index = hit
            item = self.items[item_index]
            if not isinstance(item, DimensionMarkingBatch):
                return self._state("色分けの解除に失敗しました。")
            kept_entries = tuple(
                entry
                for index, entry in enumerate(item.entries)
                if index != entry_index
            )
            if kept_entries:
                self.items[item_index] = DimensionMarkingBatch(
                    item.page_index,
                    kept_entries,
                )
            else:
                self.items.pop(item_index)
                self.last_general_tolerance_marked = False
            return self._state(
                f"色分けを1件解除しました。残り{len(kept_entries)}件です。"
            )

    def _tolerance_addition(
        self,
        candidate: GeneralToleranceCandidate,
        occupied_additions: tuple[fitz.Rect, ...] = (),
        layout_context: tuple[
            tuple[fitz.Rect, ...],
            tuple[fitz.Rect, ...],
            Image.Image | None,
            float,
        ] | None = None,
    ) -> ToleranceAddition:
        if self.document is None:
            raise RuntimeError("PDF is not open")
        page = self.document[candidate.page_index]
        rect = fitz.Rect(candidate.rect)
        direction = fitz.Point(candidate.direction)
        direction_length = math.hypot(direction.x, direction.y) or 1.0
        direction /= direction_length
        horizontal = abs(direction.x) >= abs(direction.y)
        source_height = rect.height if horizontal else rect.width
        source_along_length = rect.width if horizontal else rect.height
        if candidate.quad:
            normal = fitz.Point(-direction.y, direction.x)
            along_values = [
                fitz.Point(point).x * direction.x
                + fitz.Point(point).y * direction.y
                for point in candidate.quad
            ]
            across = [
                fitz.Point(point).x * normal.x
                + fitz.Point(point).y * normal.y
                for point in candidate.quad
            ]
            source_along_length = max(along_values) - min(along_values)
            source_height = max(across) - min(across)
        # Keep the tolerance slightly smaller than the nominal, but never so
        # small that it becomes difficult to inspect at normal drawing zoom.
        # Layout movement, rather than excessive shrinking, resolves clashes.
        if candidate.kind in {"angle", "chamfer", "radius"}:
            # Angular and C/R callouts live in the densest parts of a drawing.
            # Match the usual small-tolerance type size instead of making the
            # addition larger than its source nominal.
            font_size = max(5.0, min(8.5, source_height * 0.55))
        else:
            font_size = max(5.0, min(11.0, source_height * 0.76))
        suffix_font_size = max(
            font_size,
            min(11.5, source_height * 0.84),
        )
        gap = (
            max(0.06, font_size * 0.01)
            if candidate.kind in {"angle", "chamfer", "radius"}
            else max(0.55, font_size * 0.075)
        )

        # CAD drawings often keep a descriptor such as ``（溝）`` or
        # ``(二面幅)`` in the same text line after the nominal value. Place
        # the tolerance after that complete line, rather than after the
        # detected number alone, so the descriptor remains visible.
        label_rect = fitz.Rect(rect)
        best_line: tuple[float, fitz.Rect, str] | None = None
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                line_direction_value = line.get("dir") or (1.0, 0.0)
                line_direction = fitz.Point(line_direction_value)
                line_length = math.hypot(
                    line_direction.x,
                    line_direction.y,
                ) or 1.0
                line_direction /= line_length
                direction_dot = (
                    direction.x * line_direction.x
                    + direction.y * line_direction.y
                )
                if abs(direction_dot) < 0.92:
                    continue
                span_rects = [
                    fitz.Rect(span["bbox"])
                    for span in line.get("spans", [])
                    if span.get("text", "").strip()
                ]
                if not span_rects:
                    continue
                line_text = "".join(
                    str(span.get("text") or "")
                    for span in line.get("spans", [])
                )
                line_rect = fitz.Rect(span_rects[0])
                for span_rect in span_rects[1:]:
                    line_rect |= span_rect
                overlap = (line_rect & rect).get_area()
                center_distance = math.dist(
                    (
                        (line_rect.x0 + line_rect.x1) / 2,
                        (line_rect.y0 + line_rect.y1) / 2,
                    ),
                    (
                        (rect.x0 + rect.x1) / 2,
                        (rect.y0 + rect.y1) / 2,
                    ),
                )
                score = overlap * 1000 - center_distance
                if overlap > 0 and (
                    best_line is None or score > best_line[0]
                ):
                    best_line = (score, line_rect, line_text)
        if best_line is not None:
            label_rect = best_line[1]

        suffix_text = ""
        suffix_rect: tuple[float, float, float, float] | None = None
        if best_line is not None:
            line_text = best_line[2]
            nominal_match = None
            for match in re.finditer(r"\d+(?:[.,]\d+)?", line_text):
                try:
                    matched_value = float(
                        match.group(0).replace(",", ".")
                    )
                except ValueError:
                    continue
                if abs(matched_value - candidate.nominal_value) < 1e-9:
                    nominal_match = match
                    break
            if nominal_match is not None:
                suffix_match = re.match(
                    r"\s*[（(]([^）)]+)[）)]",
                    line_text[nominal_match.end() :],
                )
                if suffix_match is not None:
                    descriptor = suffix_match.group(1).strip()
                    # The groove diameter callout is requested as a width
                    # descriptor after the tolerance.
                    if descriptor == "溝":
                        descriptor = "幅"
                    suffix_text = f"({descriptor})"
                    if horizontal and direction.x >= 0:
                        suffix_rect = (
                            rect.x1,
                            label_rect.y0,
                            label_rect.x1,
                            label_rect.y1,
                        )
                    elif horizontal:
                        suffix_rect = (
                            label_rect.x0,
                            label_rect.y0,
                            rect.x0,
                            label_rect.y1,
                        )
                    elif direction.y >= 0:
                        suffix_rect = (
                            label_rect.x0,
                            rect.y1,
                            label_rect.x1,
                            label_rect.y1,
                        )
                    else:
                        suffix_rect = (
                            label_rect.x0,
                            label_rect.y0,
                            label_rect.x1,
                            rect.y0,
                        )

        placement_rect = rect if suffix_text else label_rect

        if candidate.quad and not suffix_text:
            normal = fitz.Point(-direction.y, direction.x)
            quad_points = [fitz.Point(point) for point in candidate.quad]
            along = [
                point.x * direction.x + point.y * direction.y
                for point in quad_points
            ]
            across = [
                point.x * normal.x + point.y * normal.y
                for point in quad_points
            ]
            # Align to the nominal's actual rotated baseline. Axis-aligned
            # bounding-box placement pushed diagonal C/R and angular
            # tolerances into nearby geometry even when their gap was small.
            baseline = max(across) - source_height * 0.12
            origin_point = (
                direction * (max(along) + gap)
                + normal * baseline
            )
            origin = (origin_point.x, origin_point.y)
        elif horizontal and direction.x >= 0:
            origin = (
                placement_rect.x1 + gap,
                rect.y1 - rect.height * 0.10,
            )
        elif horizontal:
            origin = (
                placement_rect.x0 - gap,
                rect.y0 + rect.height * 0.10,
            )
        elif direction.y >= 0:
            origin = (
                rect.x0 + rect.width * 0.12,
                placement_rect.y1 + gap,
            )
        else:
            origin = (
                rect.x1 - rect.width * 0.12,
                placement_rect.y0 - gap,
            )
        base_origin = fitz.Point(origin)

        if layout_context is None:
            layout_context = self._general_tolerance_layout_context(page)
        (
            raw_text_obstacles,
            line_obstacles,
            raster_obstacles,
            raster_scale,
        ) = layout_context
        search_radius = max(34.0, font_size * 6.0)
        search_rect = (
            fitz.Rect(rect)
            | fitz.Rect(
                base_origin.x - search_radius,
                base_origin.y - search_radius,
                base_origin.x + search_radius,
                base_origin.y + search_radius,
            )
        ) & page.rect
        raw_text_obstacles = tuple(
            obstacle
            for obstacle in raw_text_obstacles
            if _rects_intersect(obstacle, search_rect)
        )
        line_obstacles = tuple(
            obstacle
            for obstacle in line_obstacles
            if _rects_intersect(obstacle, search_rect)
        )
        text_obstacles: list[fitz.Rect] = []
        for span_rect in raw_text_obstacles:
            # The source span can contain ``(溝)`` or ``(二面幅)``. That
            # descriptor is intentionally removed and appended after the new
            # tolerance, so keep only the nominal box as an obstacle here.
            intersection_area = _rect_intersection_area(span_rect, rect)
            if (
                intersection_area > 0
                and intersection_area
                >= min(span_rect.get_area(), rect.get_area()) * 0.25
            ):
                text_obstacles.append(fitz.Rect(rect))
            else:
                text_obstacles.append(span_rect)

        best_addition: ToleranceAddition | None = None
        best_score = math.inf
        normal = fitz.Point(-direction.y, direction.x)
        size_factors = (
            (1.0, 0.90, 0.80, 0.70)
            if candidate.kind in {"angle", "chamfer", "radius"}
            else (1.0, 0.94, 0.88)
        )
        size_choices = tuple(
            dict.fromkeys(
                round(
                    max(
                        4.8
                        if candidate.kind in {"angle", "chamfer", "radius"}
                        else 5.0,
                        font_size * factor,
                    ),
                    2,
                )
                for factor in size_factors
            )
        )
        for trial_size in size_choices:
            # A tolerance is part of the dimension callout, not an unrelated
            # label. Angles and ordinary dimensions stay on the same baseline.
            # Dense diagonal C/R leaders may use one nearby parallel baseline
            # when the inline text would cross the product outline.
            if candidate.kind in {"chamfer", "radius"}:
                normal_offsets = (
                    0.0,
                    -trial_size * 0.28,
                    trial_size * 0.28,
                    -trial_size * 0.50,
                    trial_size * 0.50,
                )
                # Keep C/R and its tolerance as one compact inline label.
                along_offsets = (0.0,)
            elif candidate.kind == "angle":
                # Inline is preferred. Small parallel nudges are available
                # only when the degree callout would collide with nearby
                # upper/lower tolerance text (for example M19's 60 degrees).
                normal_offsets = (
                    0.0,
                    -trial_size * 0.42,
                    trial_size * 0.42,
                    -trial_size * 0.72,
                    trial_size * 0.72,
                    -trial_size * 1.05,
                    trial_size * 1.05,
                    -trial_size * 1.35,
                    trial_size * 1.35,
                    -trial_size * 1.75,
                    trial_size * 1.75,
                    -trial_size * 2.15,
                    trial_size * 2.15,
                )
                along_offsets = (0.0,)
            else:
                # Ordinary dimensions may also be dense (stacked limit
                # values, extension lines, adjacent dimensions). Prefer the
                # inline suffix, then search the nearest parallel baseline.
                normal_offsets = (
                    0.0,
                    -trial_size * 0.55,
                    trial_size * 0.55,
                    -trial_size * 0.95,
                    trial_size * 0.95,
                    -trial_size * 1.40,
                    trial_size * 1.40,
                    -trial_size * 1.90,
                    trial_size * 1.90,
                )
                along_offsets = (
                    0.0,
                    trial_size * 0.45,
                    -trial_size * 0.45,
                )
            for normal_offset in normal_offsets:
                for along_offset in along_offsets:
                    if (
                        candidate.kind == "angle"
                        and abs(along_offset) <= 0.1
                        and abs(normal_offset) >= trial_size * 1.30
                    ):
                        # Beyond this distance an inline suffix looks
                        # detached; use the compact parallel alternative.
                        continue
                    if (
                        candidate.kind == "angle"
                        and abs(along_offset) > 0.1
                        and abs(normal_offset) < trial_size * 1.30
                    ):
                        # A parallel line needs a full glyph-height offset;
                        # otherwise it sits on top of the degree value.
                        continue
                    trial_origin = (
                        base_origin
                        + direction * along_offset
                        + normal * normal_offset
                    )
                    trial = ToleranceAddition(
                        origin=(trial_origin.x, trial_origin.y),
                        direction=candidate.direction,
                        text=candidate.tolerance_text,
                        font_size=trial_size,
                        suffix_text=suffix_text,
                        suffix_rect=suffix_rect,
                        suffix_font_size=max(
                            trial_size,
                            suffix_font_size,
                        ),
                    )
                    trial_rect = self._full_tolerance_addition_rect(trial)
                    connection_rect = trial_rect
                    if candidate.kind in {"chamfer", "radius"} and candidate.quad:
                        connection_quad = _marking_quad_from_points(
                            [
                                fitz.Point(point)
                                for point in (
                                    *candidate.quad,
                                    *self._added_tolerance_mark_quad(trial),
                                )
                            ],
                            candidate.direction,
                        )
                        connection_rect = fitz.Rect(
                            _marking_quad_bounds(connection_quad)
                        )
                    outside = trial_rect.get_area() - _rect_intersection_area(
                        trial_rect,
                        page.rect,
                    )
                    score = outside * 1200
                    for obstacle in text_obstacles:
                        source_overlap = _rect_intersection_area(
                            rect,
                            obstacle,
                        )
                        if (
                            candidate.kind == "angle"
                            and abs(along_offset) > 0.1
                            and abs(normal_offset) >= trial_size * 1.30
                            and source_overlap
                            >= min(rect.get_area(), obstacle.get_area()) * 0.25
                        ):
                            # Axis-aligned source boxes overlap a nearby
                            # parallel rotated line even when the glyphs do
                            # not. The offset guard keeps it off the nominal.
                            continue
                        collision_obstacle = obstacle
                        if candidate.kind == "angle":
                            collision_obstacle = fitz.Rect(obstacle)
                            clearance = max(1.2, trial_size * 0.18)
                            collision_obstacle.x0 -= clearance
                            collision_obstacle.y0 -= clearance
                            collision_obstacle.x1 += clearance
                            collision_obstacle.y1 += clearance
                        overlap = _rect_intersection_area(
                            trial_rect,
                            collision_obstacle,
                        )
                        if overlap > 0:
                            score += 1800 + overlap * 180
                        if (
                            candidate.kind in {"chamfer", "radius"}
                            and _rect_intersection_area(rect, obstacle)
                            < min(rect.get_area(), obstacle.get_area()) * 0.25
                        ):
                            connection_overlap = _rect_intersection_area(
                                connection_rect,
                                obstacle,
                            )
                            if connection_overlap > 0:
                                score += 700 + connection_overlap * 90
                    for obstacle in line_obstacles:
                        overlap = _rect_intersection_area(trial_rect, obstacle)
                        if overlap > 0:
                            if candidate.kind in {"angle", "chamfer", "radius"}:
                                score += 900 + overlap * 120
                            else:
                                score += 520 + overlap * 90
                    for obstacle in occupied_additions:
                        overlap = _rect_intersection_area(trial_rect, obstacle)
                        if overlap > 0:
                            score += 2400 + overlap * 220
                    if raster_obstacles is not None:
                        ink_density = _page_ink_density(
                            raster_obstacles,
                            page.rect,
                            trial_rect,
                            raster_scale,
                        )
                        # Image PDFs have no vector/text obstacles. Penalize
                        # both thin drawing lines and dark OCR glyphs so the
                        # first automatic placement already resembles a
                        # careful manual layout.
                        score += ink_density * 13_000
                        if ink_density > 0.10:
                            score += 900
                    score += math.dist(base_origin, trial_origin) * (
                        (
                            4.0
                            if abs(along_offset) > 0.1
                            else 12.0
                        )
                        if candidate.kind == "angle"
                        else 7.0
                        if candidate.kind in {"chamfer", "radius"}
                        else 4.0
                    )
                    score += (font_size - trial_size) * 30
                    if score < best_score:
                        best_score = score
                        best_addition = trial
        if best_addition is None:
            raise RuntimeError("公差の配置を決定できませんでした。")
        return best_addition

    @staticmethod
    def _general_tolerance_layout_context(
        page: fitz.Page,
    ) -> tuple[
        tuple[fitz.Rect, ...],
        tuple[fitz.Rect, ...],
        Image.Image | None,
        float,
    ]:
        """Collect reusable text and thin-line obstacles for one page."""

        text_obstacles: list[fitz.Rect] = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if str(span.get("text") or "").strip():
                        text_obstacles.append(fitz.Rect(span["bbox"]))

        line_obstacles: list[fitz.Rect] = []
        for drawing in page.get_drawings():
            width = float(drawing.get("width") or 0.0)
            if width <= 0 or width > 1.8:
                continue
            padding = max(0.45, width / 2 + 0.25)
            for item in drawing.get("items", []):
                if not item or item[0] not in {"l", "c", "re"}:
                    continue
                if item[0] == "re":
                    item_rect = fitz.Rect(item[1])
                    line_obstacles.append(
                        fitz.Rect(
                            item_rect.x0 - padding,
                            item_rect.y0 - padding,
                            item_rect.x1 + padding,
                            item_rect.y1 + padding,
                        )
                    )
                    continue
                points = [fitz.Point(value) for value in item[1:]]
                line_obstacles.append(
                    fitz.Rect(
                        min(point.x for point in points) - padding,
                        min(point.y for point in points) - padding,
                        max(point.x for point in points) + padding,
                        max(point.y for point in points) + padding,
                    )
                )
        raster_obstacles: Image.Image | None = None
        raster_scale = 1.0
        if _is_full_page_image(page) or not text_obstacles:
            raster_scale = 1.25
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(raster_scale, raster_scale),
                colorspace=fitz.csGRAY,
                alpha=False,
                annots=False,
            )
            raster_obstacles = Image.frombytes(
                "L",
                (pixmap.width, pixmap.height),
                pixmap.samples,
            )
        return (
            tuple(text_obstacles),
            tuple(line_obstacles),
            raster_obstacles,
            raster_scale,
        )

    def apply_general_tolerances(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            selected = [
                candidate
                for candidate in self.general_tolerance_candidates
                if candidate.selected and not candidate.manual_required
            ]
            if not selected:
                return self._state("反映する寸法候補がありません。")
            additions_list: list[ToleranceAddition] = []
            occupied: list[fitz.Rect] = []
            layout_context = self._general_tolerance_layout_context(
                self.document[self.page_index]
            )
            for candidate in selected:
                addition = self._tolerance_addition(
                    candidate,
                    tuple(occupied),
                    layout_context,
                )
                additions_list.append(addition)
                occupied.append(self._full_tolerance_addition_rect(addition))
            additions = tuple(additions_list)
            self.items.append(
                GeneralToleranceBatchMark(self.page_index, additions)
            )
            self.last_general_tolerance_batch = selected
            self.last_general_tolerance_additions = list(additions)
            self.last_general_tolerance_marked = False
            self._clear_review_candidates()
            return self._state(
                f"一般公差を{len(selected)}件、一括反映しました。"
            )

    @staticmethod
    def _added_tolerance_mark_quad(
        addition: ToleranceAddition,
    ) -> tuple[tuple[float, float], ...]:
        """Return an oriented page-space box for added tolerance text."""

        direction = fitz.Point(addition.direction)
        length = math.hypot(direction.x, direction.y) or 1.0
        direction /= length
        normal = fitz.Point(-direction.y, direction.x)
        origin = fitz.Point(addition.origin)
        font_size = max(4.0, min(24.0, addition.font_size))
        text_width = fitz.Font("helv").text_length(
            addition.text,
            fontsize=font_size,
        )
        top = font_size * 0.88
        bottom = font_size * 0.18
        margin = max(0.35, font_size * 0.045)
        return tuple(
            (float(point.x), float(point.y))
            for point in (
                origin - direction * margin - normal * (top + margin),
                origin + direction * (text_width + margin) - normal * (top + margin),
                origin + direction * (text_width + margin) + normal * (bottom + margin),
                origin - direction * margin + normal * (bottom + margin),
            )
        )

    @classmethod
    def _added_tolerance_mark_rect(
        cls,
        addition: ToleranceAddition,
    ) -> tuple[float, float, float, float]:
        """Return the bounds of the oriented added-tolerance marker."""

        corners = [
            fitz.Point(point)
            for point in cls._added_tolerance_mark_quad(addition)
        ]
        return (
            min(point.x for point in corners),
            min(point.y for point in corners),
            max(point.x for point in corners),
            max(point.y for point in corners),
        )

    @classmethod
    def _full_tolerance_addition_quad(
        cls,
        addition: ToleranceAddition,
    ) -> tuple[tuple[float, float], ...]:
        """Return a tight oriented marker for tolerance plus descriptor."""

        points = [
            fitz.Point(point)
            for point in cls._added_tolerance_mark_quad(addition)
        ]
        if addition.suffix_text:
            direction = fitz.Point(addition.direction)
            length = math.hypot(direction.x, direction.y) or 1.0
            direction /= length
            normal = fitz.Point(-direction.y, direction.x)
            font = fitz.Font("helv")
            tolerance_size = max(4.0, min(24.0, addition.font_size))
            suffix_size = max(
                tolerance_size,
                min(
                    24.0,
                    addition.suffix_font_size
                    if addition.suffix_font_size is not None
                    else tolerance_size,
                ),
            )
            tolerance_width = font.text_length(
                addition.text,
                fontsize=tolerance_size,
            )
            suffix_width = font.text_length(
                addition.suffix_text,
                fontsize=suffix_size,
            )
            suffix_origin = (
                fitz.Point(addition.origin)
                + direction
                * (tolerance_width + max(0.25, tolerance_size * 0.045))
            )
            points.extend(
                (
                    suffix_origin - normal * (suffix_size * 0.90),
                    suffix_origin
                    + direction * suffix_width
                    - normal * (suffix_size * 0.90),
                    suffix_origin
                    + direction * suffix_width
                    + normal * (suffix_size * 0.20),
                    suffix_origin + normal * (suffix_size * 0.20),
                )
            )
        return _marking_quad_from_points(
            points,
            addition.direction,
            along_expand=0.04,
            across_inset=0.08,
        )

    @classmethod
    def _full_tolerance_addition_rect(
        cls,
        addition: ToleranceAddition,
    ) -> fitz.Rect:
        """Return bounds of the tolerance and any moved descriptor."""

        rect = fitz.Rect(cls._added_tolerance_mark_rect(addition))
        if not addition.suffix_text:
            return rect
        direction = fitz.Point(addition.direction)
        length = math.hypot(direction.x, direction.y) or 1.0
        direction /= length
        normal = fitz.Point(-direction.y, direction.x)
        font_size = max(4.0, min(24.0, addition.font_size))
        font = fitz.Font("helv")
        tolerance_width = font.text_length(
            addition.text,
            fontsize=font_size,
        )
        suffix_font_size = max(
            font_size,
            min(
                24.0,
                addition.suffix_font_size
                if addition.suffix_font_size is not None
                else font_size,
            ),
        )
        suffix_width = font.text_length(
            addition.suffix_text,
            fontsize=suffix_font_size,
        )
        suffix_origin = (
            fitz.Point(addition.origin)
            + direction * (tolerance_width + max(0.45, font_size * 0.07))
        )
        top = suffix_font_size * 0.92
        bottom = suffix_font_size * 0.24
        points = (
            suffix_origin - normal * top,
            suffix_origin + direction * suffix_width - normal * top,
            suffix_origin + direction * suffix_width + normal * bottom,
            suffix_origin + normal * bottom,
        )
        suffix_bounds = fitz.Rect(
            min(point.x for point in points),
            min(point.y for point in points),
            max(point.x for point in points),
            max(point.y for point in points),
        )
        return rect | suffix_bounds

    def _invalidate_dimension_markings(self) -> None:
        """Remove a stale automatic color batch after dimension edits."""

        self.items = [
            item
            for item in self.items
            if not (
                isinstance(item, DimensionMarkingBatch)
                and item.page_index == self.page_index
            )
        ]
        self.last_general_tolerance_marked = False
        self.dimension_marking_candidates.clear()

    def _build_dimension_marking_entries(
        self,
        page: fitz.Page,
    ) -> list[DimensionMarkingEntry]:
            detected = _detect_dimension_markings(page)
            marking_recorder = OcrPipelineRecorder("dimension_markings")
            marking_recorder.set_count("vector_detected", len(detected))
            if _needs_local_ocr(page):
                try:
                    cached_scanned = self.scanned_marking_cache.get(
                        self.page_index
                    )
                    if cached_scanned is None:
                        shared_ocr = self._enriched_local_ocr(page)
                        if shared_ocr is not None:
                            scanned_detected = _detect_local_dimension_markings(
                                page,
                                shared_ocr,
                                include_plain_dimensions=False,
                                scanned_page=True,
                            )
                            # RapidOCR is strong on small numeric text, while
                            # Windows OCR recovers a different subset of faint
                            # and rotated tolerance callouts. Run both and keep
                            # only geometrically verified, non-overlapping hits.
                            try:
                                windows_detected = _detect_scanned_dimension_markings(
                                    page,
                                    _resource_path("windows_ocr.ps1"),
                                    include_plain_dimensions=False,
                                )
                            except (OSError, subprocess.SubprocessError, ValueError):
                                windows_detected = []
                            for windows_marking in windows_detected:
                                windows_rect = fitz.Rect(windows_marking.rect)
                                if any(
                                    (windows_rect & fitz.Rect(existing.rect)).get_area()
                                    >= 0.45
                                    * min(
                                        windows_rect.get_area(),
                                        fitz.Rect(existing.rect).get_area(),
                                    )
                                    for existing in scanned_detected
                                ):
                                    continue
                                scanned_detected.append(windows_marking)
                        else:
                            scanned_detected = _detect_scanned_dimension_markings(
                                page,
                                _resource_path("windows_ocr.ps1"),
                                include_plain_dimensions=True,
                            )
                        self.scanned_marking_cache[self.page_index] = tuple(
                            scanned_detected
                        )
                    else:
                        scanned_detected = list(cached_scanned)
                except (OSError, subprocess.SubprocessError, ValueError):
                    scanned_detected = []
                for scanned_marking in scanned_detected:
                    scanned_rect = fitz.Rect(scanned_marking.rect)
                    if any(
                        (scanned_rect & fitz.Rect(existing.rect)).get_area()
                        >= 0.45
                        * min(
                            scanned_rect.get_area(),
                            fitz.Rect(existing.rect).get_area(),
                        )
                        for existing in detected
                    ):
                        continue
                    detected.append(scanned_marking)
            marking_recorder.set_count("detected_total", len(detected))
            entries: list[DimensionMarkingEntry] = []
            additions = self.last_general_tolerance_additions
            page_replacements = [
                item
                for item in self.items
                if isinstance(item, ReplacementMark)
                and item.page_index == self.page_index
            ]
            page_added_dimensions = [
                item
                for item in self.items
                if isinstance(item, DimensionMark)
                and item.page_index == self.page_index
            ]
            if len(additions) != len(self.last_general_tolerance_batch):
                additions = [
                    self._tolerance_addition(candidate)
                    for candidate in self.last_general_tolerance_batch
                ]

            def color_for(kind: str, total_range: float | None) -> str:
                if total_range is None:
                    return "#ffff00"
                limit = 1.0 if kind == "angle" else 0.03
                return (
                    "#ff33cc"
                    if total_range <= limit + 1e-9
                    else "#ffff00"
                )

            def matching_general_candidate(
                marking: _DetectedDimensionMarking,
            ) -> GeneralToleranceCandidate | None:
                marking_rect = fitz.Rect(marking.rect)
                marking_center = fitz.Point(
                    (marking_rect.x0 + marking_rect.x1) / 2,
                    (marking_rect.y0 + marking_rect.y1) / 2,
                )
                best: tuple[float, GeneralToleranceCandidate] | None = None
                for candidate in self.last_general_tolerance_batch:
                    if (
                        candidate.page_index != self.page_index
                        or candidate.kind != marking.kind
                        or abs(candidate.nominal_value - marking.nominal_value)
                        > 1e-7
                    ):
                        continue
                    candidate_rect = fitz.Rect(candidate.rect)
                    candidate_center = fitz.Point(
                        (candidate_rect.x0 + candidate_rect.x1) / 2,
                        (candidate_rect.y0 + candidate_rect.y1) / 2,
                    )
                    distance = math.dist(
                        (marking_center.x, marking_center.y),
                        (candidate_center.x, candidate_center.y),
                    )
                    if distance <= max(5.0, marking_rect.height * 0.65):
                        if best is None or distance < best[0]:
                            best = (distance, candidate)
                return best[1] if best is not None else None

            def append_entry(
                rect: tuple[float, float, float, float],
                color: str,
                quad: tuple[tuple[float, float], ...] | None,
            ) -> None:
                new_rect = fitz.Rect(rect)
                if new_rect.is_empty:
                    return
                for existing in entries:
                    existing_rect = fitz.Rect(existing.rect)
                    intersection = new_rect & existing_rect
                    if (
                        existing.color == color
                        and intersection.get_area() >= new_rect.get_area() * 0.82
                        and intersection.get_area() >= existing_rect.get_area() * 0.82
                    ):
                        return
                entries.append(
                    DimensionMarkingEntry(rect, color, 0.42, quad)
                )

            marked_dimension_count = 0
            matched_markings: dict[int, _DetectedDimensionMarking] = {}
            for marking in detected:
                # Reference dimensions in parentheses are informational and
                # must never be part of the automatic marking batch.
                if marking.reference:
                    continue
                marking_rect = fitz.Rect(marking.rect)
                # The original glyphs under a replacement are white-out
                # targets.  Mark only the rewritten value appended below.
                if any(
                    (marking_rect & fitz.Rect(replacement.rect)).get_area()
                    >= min(
                        marking_rect.get_area(),
                        fitz.Rect(replacement.rect).get_area(),
                    )
                    * 0.45
                    for replacement in page_replacements
                ):
                    continue
                matched = matching_general_candidate(marking)
                # Plain drawing numbers which are neither dimensions selected
                # for general tolerance nor explicit/reference callouts are
                # deliberately left alone (for example a surface roughness
                # value or a zone coordinate).
                if (
                    matched is None
                    and marking.tolerance_range is None
                ):
                    continue
                # Batch candidates are appended below as one continuous quad
                # spanning the nominal and newly added tolerance. This also
                # guarantees the nominal is marked if the broader detector
                # could not reconstruct its CAD text group.
                if matched is not None:
                    # Keep the broader read-only marking geometry.  It may
                    # contain an outlined/unmapped diameter glyph which the
                    # conservative general-tolerance candidate omits.
                    matched_markings.setdefault(id(matched), marking)
                    continue
                total_range = marking.tolerance_range
                color = color_for(marking.kind, total_range)
                if marking.tolerance_rect and marking.tolerance_quad:
                    joined_quad = _marking_quad_from_points(
                        [
                            fitz.Point(point)
                            for point in (*marking.quad, *marking.tolerance_quad)
                        ],
                        marking.direction,
                        along_expand=0.04,
                        across_inset=0.08,
                    )
                    append_entry(
                        _marking_quad_bounds(joined_quad),
                        color,
                        joined_quad,
                    )
                else:
                    append_entry(marking.rect, color, marking.quad)
                marked_dimension_count += 1

            def text_kind_and_range(
                text: str,
            ) -> tuple[str, float | None]:
                normalized = unicodedata.normalize("NFKC", text).strip()
                match = _MARKING_NUMBER_PATTERN.search(normalized)
                if match is None:
                    return "linear", None
                try:
                    nominal = float(match.group("number").replace(",", "."))
                except ValueError:
                    return "linear", None
                kind = _dimension_marking_kind(
                    match.group("prefix"),
                    match.group("degree"),
                )
                return kind, _explicit_tolerance_range(normalized, nominal)

            def replacement_marker_quad(
                mark: ReplacementMark,
            ) -> tuple[tuple[float, float], ...]:
                if mark.origin is None:
                    rect = fitz.Rect(mark.rect)
                    return (
                        (rect.x0, rect.y0),
                        (rect.x1, rect.y0),
                        (rect.x1, rect.y1),
                        (rect.x0, rect.y1),
                    )
                direction = fitz.Point(mark.direction)
                direction /= math.hypot(direction.x, direction.y) or 1.0
                normal = fitz.Point(-direction.y, direction.x)
                font = fitz.Font("helv")
                nominal_size = max(5.0, mark.font_size)
                small_size = max(
                    4.0,
                    mark.tolerance_font_size
                    if mark.tolerance_font_size is not None
                    else nominal_size * 0.8,
                )

                def glyph_points(
                    origin: fitz.Point,
                    value: str,
                    size: float,
                ) -> list[fitz.Point]:
                    width = max(1.0, font.text_length(value, fontsize=size))
                    return [
                        origin - normal * size * 0.92,
                        origin + direction * width - normal * size * 0.92,
                        origin + direction * width + normal * size * 0.24,
                        origin + normal * size * 0.24,
                    ]

                base_origin = fitz.Point(mark.origin)
                nominal_origin = base_origin + fitz.Point(mark.value_offset)
                points = glyph_points(
                    nominal_origin,
                    mark.value,
                    nominal_size,
                )
                if mark.upper_tolerance or mark.lower_tolerance:
                    tolerance_origin = (
                        base_origin
                        + direction
                        * (
                            font.text_length(mark.value, fontsize=nominal_size)
                            + 0.5
                        )
                        + fitz.Point(mark.tolerance_offset)
                    )
                    if mark.upper_tolerance:
                        points.extend(
                            glyph_points(
                                tolerance_origin - normal * small_size,
                                mark.upper_tolerance,
                                small_size,
                            )
                        )
                    if mark.lower_tolerance:
                        points.extend(
                            glyph_points(
                                tolerance_origin,
                                mark.lower_tolerance,
                                small_size,
                            )
                        )
                return _marking_quad_from_points(
                    points,
                    (direction.x, direction.y),
                    along_expand=0.04,
                    across_inset=0.08,
                )

            # Dimensions added or rewritten after the tolerance batch are
            # part of the same final color-coding step.
            for dimension in page_added_dimensions:
                kind, total_range = text_kind_and_range(dimension.text)
                label_rect = dimension_label_rect(dimension)
                append_entry(
                    tuple(label_rect),
                    color_for(kind, total_range),
                    None,
                )
                marked_dimension_count += 1

            for replacement in page_replacements:
                tolerance_text = " ".join(
                    value
                    for value in (
                        replacement.value,
                        replacement.upper_tolerance,
                        replacement.lower_tolerance,
                    )
                    if value
                )
                kind, total_range = text_kind_and_range(tolerance_text)
                quad = replacement_marker_quad(replacement)
                append_entry(
                    _marking_quad_bounds(quad),
                    color_for(kind, total_range),
                    quad,
                )
                marked_dimension_count += 1

            # Added general tolerances are separate drawing items, so append
            # their tight rotated marker after marking every original value.
            for candidate, addition in zip(
                self.last_general_tolerance_batch,
                additions,
            ):
                color = color_for(
                    candidate.kind,
                    candidate.tolerance * 2,
                )
                tolerance_quad = self._full_tolerance_addition_quad(addition)
                candidate_rect = fitz.Rect(candidate.rect)
                matched_marking = matched_markings.get(id(candidate))
                nominal_quad = (
                    matched_marking.quad
                    if matched_marking is not None
                    else candidate.quad
                ) or tuple(
                    (float(point.x), float(point.y))
                    for point in (
                        candidate_rect.top_left,
                        candidate_rect.top_right,
                        candidate_rect.bottom_right,
                        candidate_rect.bottom_left,
                    )
                )
                joined_quad = _marking_quad_from_points(
                    [
                        fitz.Point(point)
                        for point in (*nominal_quad, *tolerance_quad)
                    ],
                    candidate.direction,
                    along_expand=0.04,
                    across_inset=0.08,
                )
                append_entry(
                    _marking_quad_bounds(joined_quad),
                    color,
                    joined_quad,
                )
                marked_dimension_count += 1
            marking_recorder.set_count("matched_batch", len(matched_markings))
            marking_recorder.set_count("colored_entries", len(entries))
            marking_recorder.log_summary()
            return entries

    def scan_dimension_markings(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if not self.last_general_tolerance_batch:
                return self._state("先に①で公差を反映してください。")
            if self.last_general_tolerance_marked:
                return self._state("色分けは反映済みです。")
            page = self.document[self.page_index]
            entries = self._build_dimension_marking_entries(page)
            if not entries:
                self.dimension_marking_candidates.clear()
                return self._state("色分けできる寸法が見つかりませんでした。")
            self.dimension_marking_candidates = [
                DimensionMarkingCandidate(
                    self.page_index,
                    entry.rect,
                    entry.color,
                    entry.opacity,
                    entry.quad,
                    selected=True,
                )
                for entry in entries
            ]
            return self._state(
                f"色分け候補を{len(entries)}件表示しました。"
                " ピンク・黄色をクリックすると除外（灰色）できます。"
            )

    def toggle_dimension_marking(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if not self.dimension_marking_candidates:
                return self._state("先に「対象寸法を検出」を押してください。")
            page = self.document[self.page_index]
            point = fitz.Point(
                _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
            )
            before = sum(
                candidate.selected
                for candidate in self.dimension_marking_candidates
            )
            updated: list[DimensionMarkingCandidate] = []
            toggled = False
            for candidate in self.dimension_marking_candidates:
                hit_rect = fitz.Rect(candidate.rect)
                hit_rect.x0 -= 8
                hit_rect.y0 -= 8
                hit_rect.x1 += 8
                hit_rect.y1 += 8
                if point in hit_rect:
                    updated.append(
                        replace(candidate, selected=not candidate.selected)
                    )
                    toggled = True
                else:
                    updated.append(candidate)
            if not toggled:
                return self._state("ピンク・黄色の候補をクリックしてください。")
            self.dimension_marking_candidates = updated
            after = sum(
                candidate.selected
                for candidate in self.dimension_marking_candidates
            )
            if before == after:
                return self._state("ピンク・黄色の候補をクリックしてください。")
            return self._state(
                f"色分け対象 {after}件。ピンク・黄色＝対象、灰色＝除外。"
            )

    def cancel_dimension_marking_candidates(self) -> dict[str, Any]:
        with self.lock:
            self.dimension_marking_candidates.clear()
            return self._state("色分けの検出候補を閉じました。")

    def apply_dimension_markings(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if not self.last_general_tolerance_batch:
                return self._state("先に①で公差を反映してください。")
            if self.last_general_tolerance_marked:
                return self._state("色分けは反映済みです。")
            if not self.dimension_marking_candidates:
                return self._state("先に「対象寸法を検出」を押してください。")
            selected = [
                candidate
                for candidate in self.dimension_marking_candidates
                if candidate.selected
            ]
            if not selected:
                return self._state("色分けする候補がありません。")
            entries = tuple(
                DimensionMarkingEntry(
                    candidate.rect,
                    candidate.color,
                    candidate.opacity,
                    candidate.quad,
                )
                for candidate in selected
            )
            self.items.append(
                DimensionMarkingBatch(self.page_index, entries)
            )
            self.last_general_tolerance_marked = True
            self.dimension_marking_candidates.clear()
            return self._state(
                f"寸法値{len(selected)}件を基準どおり色分けしました。"
            )

    def apply_action(
        self,
        mode: str,
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")

            page = self.document[self.page_index]
            color = _color(settings.get("color"))
            opacity = _number(settings.get("opacity"), 0.42, 0.08, 1.0)
            message = ""
            if (
                mode in {
                    "quality_stamp",
                    "process_stamp",
                    "procedure_note",
                    "dimension",
                }
                and bool(payload.get("select_existing"))
            ):
                select_point = fitz.Point(
                    _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
                )
                if self._select_editable_item_at(mode, select_point):
                    return self._state(
                        "青い枠をドラッグして移動し、右下の丸でサイズを変更できます。"
                    )
                if mode == "dimension":
                    if not (
                        bool(payload.get("create_if_empty"))
                        and all(
                            key in payload
                            for key in ("x0", "y0", "x1", "y1")
                        )
                    ):
                        return self._state(
                            "移動する追加寸法をクリックするか、新しい寸法をドラッグして追加してください。"
                        )
            if (
                mode != "word"
                or all(
                    key in payload
                    for key in ("x0", "y0", "x1", "y1")
                )
            ):
                self.word_candidate = None

            if mode in {"word", "strike"}:
                if mode == "word" and all(
                    key in payload
                    for key in ("x0", "y0", "x1", "y1")
                ):
                    x0 = _number(
                        payload.get("x0"),
                        0,
                        page.rect.x0,
                        page.rect.x1,
                    )
                    y0 = _number(
                        payload.get("y0"),
                        0,
                        page.rect.y0,
                        page.rect.y1,
                    )
                    x1 = _number(
                        payload.get("x1"),
                        0,
                        page.rect.x0,
                        page.rect.x1,
                    )
                    y1 = _number(
                        payload.get("y1"),
                        0,
                        page.rect.y0,
                        page.rect.y1,
                    )
                    mark_style = str(
                        settings.get("mark_style") or "box"
                    )
                    if mark_style == "angled":
                        start = fitz.Point(x0, y0)
                        end = fitz.Point(x1, y1)
                        vector = end - start
                        length = math.hypot(vector.x, vector.y)
                        if length < 2.0:
                            return self._state(
                                "斜め文字に沿ってドラッグしてください。"
                            )
                        direction = vector / length
                        normal = fitz.Point(-direction.y, direction.x)
                        half_width = _number(
                            settings.get("highlight_width"),
                            11.0,
                            3.0,
                            40.0,
                        ) / 2
                        quad_points = (
                            start + normal * half_width,
                            end + normal * half_width,
                            end - normal * half_width,
                            start - normal * half_width,
                        )
                        rect = fitz.Rect(
                            quad_points[0],
                            quad_points[0],
                        )
                        for quad_point in quad_points[1:]:
                            rect.include_point(quad_point)
                        self.items.append(
                            Mark(
                                self.page_index,
                                (
                                    rect.x0,
                                    rect.y0,
                                    rect.x1,
                                    rect.y1,
                                ),
                                color,
                                opacity,
                                tuple(
                                    (point.x, point.y)
                                    for point in quad_points
                                ),
                            )
                        )
                        return self._state(
                            "斜め文字に沿ってマーキングしました。"
                        )
                    rect = fitz.Rect(
                        min(x0, x1),
                        min(y0, y1),
                        max(x0, x1),
                        max(y0, y1),
                    )
                    if rect.width < 1.5 or rect.height < 1.5:
                        return self._state(
                            "マークする文字・記号・範囲をドラッグで囲んでください。"
                        )
                    self.items.append(
                        Mark(
                            self.page_index,
                            (rect.x0, rect.y0, rect.x1, rect.y1),
                            color,
                            opacity,
                        )
                    )
                    return self._state(
                        "選択した範囲をマーキングしました。"
                    )
                point = fitz.Point(
                    _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
                )
                hit = find_text_group(page, point)
                if hit is None:
                    if mode == "word" and not page.get_text("words"):
                        try:
                            visual_hit = detect_visual_text_group(
                                page,
                                point,
                            )
                        except ValueError as exc:
                            self.word_candidate = None
                            return self._state(
                                f"{exc} ドラッグ選択も使用できます。"
                            )
                        self.word_candidate = Mark(
                            self.page_index,
                            visual_hit.rect,
                            color,
                            opacity,
                            visual_hit.quad,
                        )
                        return self._state(
                            "文字・記号の候補を表示しました。"
                            "範囲を確認して確定してください。"
                        )
                    if mode == "word" and not page.get_text("words"):
                        return self._state(
                            "画像化PDFではクリック自動選択ができません。同じツールで文字・記号をドラッグして囲んでください。"
                        )
                    return self._state(
                        "文字が見つかりません。中央をクリックするか、必要な文字・記号をドラッグして囲んでください。"
                    )
                if mode == "word":
                    self.word_candidate = None
                    self.items.append(
                        Mark(
                            self.page_index,
                            hit.rect,
                            color,
                            opacity,
                            hit.quad,
                        )
                    )
                    message = f"「{hit.text.strip() or '寸法値'}」をマーキングしました。"
                else:
                    self._invalidate_dimension_markings()
                    self.items.append(strike_from_hit(self.page_index, hit))
                    message = "選択した寸法に二重取消線を追加しました。"

            elif mode == "angled_rect":
                start = fitz.Point(
                    _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1),
                )
                end = fitz.Point(
                    _number(payload.get("x1"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y1"), 0, page.rect.y0, page.rect.y1),
                )
                vector = end - start
                length = math.hypot(vector.x, vector.y)
                if length < 2.0:
                    return self._state(
                        "寸法文字に沿ってドラッグしてください。"
                    )
                direction = vector / length
                normal = fitz.Point(-direction.y, direction.x)
                half_width = _number(
                    settings.get("highlight_width"),
                    11.0,
                    3.0,
                    40.0,
                ) / 2
                quad_points = (
                    start + normal * half_width,
                    end + normal * half_width,
                    end - normal * half_width,
                    start - normal * half_width,
                )
                rect = fitz.Rect(quad_points[0], quad_points[0])
                for quad_point in quad_points[1:]:
                    rect.include_point(quad_point)
                self.items.append(
                    Mark(
                        self.page_index,
                        (rect.x0, rect.y0, rect.x1, rect.y1),
                        color,
                        opacity,
                        tuple(
                            (point.x, point.y)
                            for point in quad_points
                        ),
                    )
                )
                message = "斜めの範囲をマーキングしました。"

            elif mode == "rect":
                x0 = _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1)
                y0 = _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1)
                x1 = _number(payload.get("x1"), 0, page.rect.x0, page.rect.x1)
                y1 = _number(payload.get("y1"), 0, page.rect.y0, page.rect.y1)
                rect = fitz.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
                if rect.width < 1.5 or rect.height < 1.5:
                    return self._state("マークする範囲をドラッグしてください。")
                self.items.append(
                    Mark(
                        self.page_index,
                        (rect.x0, rect.y0, rect.x1, rect.y1),
                        color,
                        opacity,
                    )
                )
                message = "指定した範囲をマーキングしました。"

            elif mode == "work_shape":
                raw_points = payload.get("points")
                if not isinstance(raw_points, list):
                    return self._state(
                        "ワーク形状に沿って点を指定してください。"
                    )
                points: list[tuple[float, float]] = []
                for raw_point in raw_points[:400]:
                    if (
                        not isinstance(raw_point, dict)
                        or "x" not in raw_point
                        or "y" not in raw_point
                    ):
                        continue
                    points.append(
                        (
                            _number(
                                raw_point.get("x"),
                                0,
                                page.rect.x0,
                                page.rect.x1,
                            ),
                            _number(
                                raw_point.get("y"),
                                0,
                                page.rect.y0,
                                page.rect.y1,
                            ),
                        )
                    )
                style = str(settings.get("work_shape_style") or "fill")
                if style not in {"fill", "line"}:
                    style = "fill"
                minimum_points = 3 if style == "fill" else 2
                if len(points) < minimum_points:
                    return self._state(
                        "面のマーキングは3点以上、実線のマーキングは2点以上を指定してください。"
                    )
                self.items.append(
                    WorkShapeMark(
                        self.page_index,
                        tuple(points),
                        color,
                        opacity,
                        style,
                        _number(
                            settings.get("work_line_width"),
                            6.0,
                            1.0,
                            30.0,
                        ),
                    )
                )
                message = (
                    "ワークの範囲をマーキングしました。"
                    if style == "fill"
                    else "ワークの実線をマーキングしました。"
                )

            elif mode == "dimension":
                text = str(settings.get("dimension_text") or "").strip()
                if not text:
                    return self._state("追加する寸法値を入力してください。")
                target = (
                    _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1),
                )
                label = (
                    _number(payload.get("x1"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y1"), 0, page.rect.y0, page.rect.y1),
                )
                inferred_style = infer_dimension_style(
                    page,
                    target,
                    label,
                )
                automatic_style = settings.get(
                    "dimension_auto_style",
                    True,
                ) is not False
                show_leader = settings.get("dimension_show_leader", True) is not False
                font_size = (
                    inferred_style.font_size
                    if automatic_style
                    else _number(
                        settings.get("font_size"),
                        inferred_style.font_size,
                        5.0,
                        36.0,
                    )
                )
                dimension_mark = avoid_dimension_overlap(
                    page,
                    DimensionMark(
                        self.page_index,
                        target,
                        label,
                        text,
                        color,
                        0.0,
                        font_size,
                        inferred_style.font_name,
                        inferred_style.font_color,
                        inferred_style.line_width,
                        show_leader,
                    ),
                    self.items,
                )
                self._invalidate_dimension_markings()
                self.items.append(dimension_mark)
                self.editable_item_index = len(self.items) - 1
                self.editable_tolerance_index = None
                moved = math.dist(label, dimension_mark.label) > 0.2
                message = (
                    f"寸法「{text}」を原図の書式に合わせて追加しました。"
                    if automatic_style
                    else (
                        f"寸法「{text}」を追加しました。"
                        if not show_leader
                        else f"寸法「{text}」と引出線を追加しました。"
                    )
                )
                if moved:
                    message += " 既存寸法と重ならない位置へ文字を調整しました。"

            elif mode == "replace":
                value = str(settings.get("replacement_value") or "").strip()
                if not value:
                    return self._state("新しい寸法値を入力してください。")
                upper = str(settings.get("upper_tolerance") or "").strip()
                lower = str(settings.get("lower_tolerance") or "").strip()
                replacement_origin: tuple[float, float] | None = None
                replacement_font_name = ""
                replacement_font_color = (0.0, 0.0, 0.0)
                if all(key in payload for key in ("x0", "y0", "x1", "y1")):
                    x0 = _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1)
                    y0 = _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1)
                    x1 = _number(payload.get("x1"), 0, page.rect.x0, page.rect.x1)
                    y1 = _number(payload.get("y1"), 0, page.rect.y0, page.rect.y1)
                    rect = fitz.Rect(
                        min(x0, x1),
                        min(y0, y1),
                        max(x0, x1),
                        max(y0, y1),
                    )
                    if rect.width < 2 or rect.height < 2:
                        return self._state("置き換える元の寸法を囲んでください。")
                    direction = (1.0, 0.0)
                    inferred_size = min(14.0, max(5.0, rect.height * 0.72))
                    replacement_origin = (
                        rect.x0 + 1.0,
                        rect.y1 - 1.0,
                    )
                else:
                    point = fitz.Point(
                        _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                        _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
                    )
                    hit = find_text_group(page, point)
                    if hit is None:
                        return self._state(
                            "元の寸法が見つかりません。文字の中央付近をクリックしてください。"
                        )
                    rect = fitz.Rect(hit.rect)
                    if hit.replacement_rect is not None:
                        rect = fitz.Rect(hit.replacement_rect)
                    direction = hit.direction
                    inferred_size = hit.font_size
                    replacement_origin = hit.origin
                    replacement_font_name = hit.font_name
                    replacement_font_color = hit.font_color
                requested_size = settings.get("replacement_size")
                font_size = (
                    inferred_size
                    if requested_size in (None, "", 0, "0")
                    else _number(requested_size, inferred_size, 5.0, 36.0)
                )
                tolerance_font_size = _number(
                    settings.get("replacement_tolerance_size"),
                    font_size * 0.8,
                    4.0,
                    36.0,
                )
                value_offset = (
                    _number(settings.get("replacement_value_x"), 0.0, -100.0, 100.0),
                    _number(settings.get("replacement_value_y"), 0.0, -100.0, 100.0),
                )
                tolerance_offset = (
                    _number(settings.get("replacement_tolerance_x"), 0.0, -100.0, 100.0),
                    _number(settings.get("replacement_tolerance_y"), 0.0, -100.0, 100.0),
                )
                self._invalidate_dimension_markings()
                self.items.append(
                    ReplacementMark(
                        self.page_index,
                        (rect.x0, rect.y0, rect.x1, rect.y1),
                        direction,
                        value,
                        upper,
                        lower,
                        font_size,
                        tolerance_font_size,
                        value_offset,
                        tolerance_offset,
                        origin=replacement_origin,
                        font_name=replacement_font_name,
                        font_color=replacement_font_color,
                    )
                )
                self.editable_item_index = len(self.items) - 1
                tolerance_text = ""
                if upper or lower:
                    tolerance_text = f"（上:{upper or 'なし'} / 下:{lower or 'なし'}）"
                message = f"寸法を「{value}」{tolerance_text}に修正しました。"

            elif mode == "procedure_note":
                origin = (
                    _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
                )
                note_kind = str(
                    settings.get("procedure_note_type") or "custom"
                )
                allowed_note_kinds = {
                    "confidential",
                    "phase",
                    "post_process",
                    "thread",
                    "borrowed",
                    "cut_split",
                    "surface",
                    "special",
                    "custom",
                }
                if note_kind not in allowed_note_kinds:
                    note_kind = "custom"
                note_text = str(
                    settings.get("procedure_note_text") or ""
                ).strip()[:240]
                if not note_text:
                    return self._state("追加する注記の内容を入力してください。")
                self.items.append(
                    ProcedureNoteMark(
                        self.page_index,
                        origin,
                        note_kind,
                        note_text,
                        _number(
                            settings.get("procedure_note_size"),
                            10.0,
                            6.0,
                            24.0,
                        ),
                    )
                )
                self.editable_item_index = len(self.items) - 1
                message = "手順書の注記を追加しました。"

            elif mode == "measurement":
                origin = (
                    _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
                )
                measurement_type = str(
                    settings.get("measurement_type") or "instrument"
                )
                if measurement_type == "sequence":
                    sequence = round(
                        _number(
                            settings.get("measurement_sequence"),
                            1,
                            1,
                            999,
                        )
                    )
                    measurement_text = _circled_number(sequence)
                else:
                    allowed_codes = {
                        "M", "BM", "DG", "K", "SC",
                        "H", "PG", "SG", "RC", "DM",
                    }
                    measurement_text = str(
                        settings.get("measurement_instrument") or "M"
                    ).upper()
                    if measurement_text not in allowed_codes:
                        measurement_text = "M"
                self.items.append(
                    ProcedureNoteMark(
                        self.page_index,
                        origin,
                        "measurement",
                        measurement_text,
                        _number(
                            settings.get("measurement_size"),
                            9.0,
                            6.0,
                            24.0,
                        ),
                    )
                )
                message = f"測定表示「{measurement_text}」を追加しました。"

            elif mode in {"quality_stamp", "process_stamp"}:
                center = (
                    _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
                )
                kind = "quality" if mode == "quality_stamp" else "process"
                self.items.append(
                    StampMark(
                        self.page_index,
                        center,
                        kind,
                        str(settings.get("stamp_name") or "担当者").strip() or "担当者",
                        str(settings.get("stamp_date") or date.today().strftime("%y.%m.%d")).strip(),
                        _number(settings.get("stamp_size"), 62.0, 30.0, 150.0),
                    )
                )
                self.editable_item_index = len(self.items) - 1
                message = "スタンプを追加しました。"
            else:
                return self._error("未対応のツールです。")

            return self._state(message)

    def save_pdf(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None or self.source_path is None:
                return self._error("PDFを開いてください。")
            if self.window is None:
                return self._error("ウィンドウの準備ができていません。")

            display_stem = Path(
                self.display_name or self.source_path.name
            ).stem
            suggested = f"{display_stem}_編集済.pdf"
            desktop = Path.home() / "Desktop"
            save_directory = desktop if desktop.is_dir() else Path.home()
            paths = self.window.create_file_dialog(
                webview.FileDialog.SAVE,
                directory=str(save_directory),
                save_filename=suggested,
                file_types=PDF_FILE_TYPES,
            )
            if not paths:
                return {"ok": False, "cancelled": True, "message": ""}
            output = Path(paths[0])
            if output.suffix.lower() != ".pdf":
                output = output.with_suffix(".pdf")
            try:
                export_pdf(self.source_path, output, self.items)
            except ValueError:
                return self._error("原本とは別のファイル名で保存してください。")
            except Exception as exc:
                return self._error(f"PDFを保存できませんでした: {exc}")
            state = self._state(f"保存しました: {output}")
            state["saved_path"] = str(output)
            return state

    def close(self) -> None:
        with self.lock:
            if self.document is not None:
                self.document.close()
                self.document = None
            self.upload_directory.cleanup()


class DrawingHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        api: DrawingApi,
        token: str,
        web_root: Path,
    ) -> None:
        self.api = api
        self.token = token
        self.web_root = web_root
        super().__init__(("127.0.0.1", 0), DrawingRequestHandler)


class DrawingRequestHandler(BaseHTTPRequestHandler):
    server: DrawingHttpServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_bytes(
        self,
        status: int,
        content: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        content = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_bytes(status, content, "application/json; charset=utf-8")

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Drawing-Assist-Token", "")
        return secrets.compare_digest(supplied, self.server.token)

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 1 or length > MAX_UPLOAD_BYTES:
            return None
        return self.rfile.read(length)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        file_names = {
            "/": "index.html",
            "/index.html": "index.html",
            "/styles.css": "styles.css",
            "/app.js": "app.js",
        }
        file_name = file_names.get(path)
        if file_name is None:
            self._send_json(404, {"ok": False, "message": "Not found"})
            return
        try:
            content = (self.server.web_root / file_name).read_bytes()
        except OSError as exc:
            self._send_json(
                500,
                {"ok": False, "message": f"画面を読み込めませんでした: {exc}"},
            )
            return
        content_type = mimetypes.guess_type(file_name)[0]
        if file_name.endswith(".js"):
            content_type = "text/javascript"
        self._send_bytes(
            200,
            content,
            f"{content_type or 'application/octet-stream'}; charset=utf-8",
        )

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(403, {"ok": False, "message": "Forbidden"})
            return
        parsed = urlparse(self.path)
        body = self._read_body()
        if body is None:
            self._send_json(
                400,
                {"ok": False, "message": "ファイルまたは要求を読み込めませんでした。"},
            )
            return

        if parsed.path == "/api":
            try:
                request = json.loads(body.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request is not an object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._send_json(
                    400,
                    {"ok": False, "message": "操作要求が不正です。"},
                )
                return
            result = self.server.api.drawing_assist_command(request)
            self._send_json(200, result)
            return

        if parsed.path == "/upload":
            query = parse_qs(parsed.query)
            file_name = unquote((query.get("name") or ["document.pdf"])[0])
            result = self.server.api.load_pdf_bytes(file_name, body)
            self._send_json(200, result)
            return

        self._send_json(404, {"ok": False, "message": "Not found"})


def start_local_server(
    api: DrawingApi,
) -> tuple[DrawingHttpServer, Thread, str]:
    token = secrets.token_urlsafe(32)
    server = DrawingHttpServer(api, token, _resource_path("web"))
    thread = Thread(
        target=server.serve_forever,
        name="DrawingAssistHttp",
        daemon=True,
    )
    thread.start()
    return server, thread, token


def _self_test(
    pdf_path: Path,
    result_path: Path,
    preview_path: Path,
) -> None:
    from urllib.request import Request, urlopen

    api = DrawingApi()
    server, thread, token = start_local_server(api)
    base_url = f"http://127.0.0.1:{server.server_port}"
    annotation_check_path = result_path.with_name(
        f"{result_path.stem}-annotation-check.pdf"
    )
    result: dict[str, Any]
    try:
        with urlopen(f"{base_url}/app.js", timeout=20) as response:
            script = response.read().decode("utf-8")
        with urlopen(f"{base_url}/", timeout=20) as response:
            html = response.read().decode("utf-8")
        with urlopen(f"{base_url}/styles.css", timeout=20) as response:
            styles = response.read().decode("utf-8")
        if "window.pywebview.api" in script:
            raise RuntimeError("packaged app.js still uses pywebview js_api")
        if 'fetch("/api"' not in script:
            raise RuntimeError("packaged app.js does not use the local HTTP API")
        for required_marker in (
            "select_replacement",
            "confirm_replacement",
            "originalReplacementValue",
            "work_shape",
            "detect_work_region",
            "predict_work_shape",
            "confirm_work_region",
            "work_region_candidate_count",
            "markMethod",
            "mark_style",
            "confirm_word_candidate",
            "cancel_word_candidate",
            "wordCandidateBar",
            "word_candidate",
            "guidedPredictionReady",
            "predictWorkShapeButton",
            "dimensionAutoStyle",
            "dimension_auto_style",
            "dimensionStyleStatus",
            "generalToleranceStandard",
            "generalToleranceAngleLength",
            "procedureNoteType",
            "measurementInstrument",
            "scan_general_tolerances",
            "apply_general_tolerances",
            "apply_dimension_markings",
            "select_general_tolerance_addition",
            "move_general_tolerance_addition",
            "procedure_note",
            "measurement",
        ):
            if required_marker not in script:
                raise RuntimeError(
                    f"packaged app.js is missing {required_marker}"
                )
        for required_ui_marker in (
            'id="taskGuide"',
            'id="canvasCoach"',
            'class="more-tools"',
            'class="dimension-fix-tools"',
            'id="dimensionFixStatus"',
            'class="advanced-details"',
            'id="dimensionAutoStyle"',
            'id="dimensionStyleStatus"',
            'id="generalToleranceStandard"',
            'id="generalToleranceAngleLength"',
            'id="scanGeneralToleranceButton"',
            'id="applyDimensionMarkingsButton"',
            'id="processingOverlay"',
            'data-mode="general_tolerance"',
            'data-mode="procedure_note"',
            'data-mode="measurement"',
            "公差をまとめて入れる",
            "印・必要な注記を入れる",
            "寸法・公差を色分けする",
            "測定具・測定順を入れる",
            "寸法・公差を一括で色分け",
            "公差レンジ0.03以内／角度1°以内",
            "角度公差の設定",
            "次の操作",
            "その他の機能",
            "必要な寸法を書き直す",
        ):
            if required_ui_marker not in html:
                raise RuntimeError(
                    f"packaged UI is missing {required_ui_marker}"
                )
        if html.index('data-mode="general_tolerance"') > html.index(
            'data-mode="word"'
        ):
            raise RuntimeError(
                "general-tolerance menu must precede the marking menu"
            )
        if html.index(
            '<option value="jis_b_0405">JIS B 0405</option>'
        ) > html.index(
            '<option value="pisco">日本ピスコ普通公差（加工寸法）</option>'
        ):
            raise RuntimeError(
                "JIS B 0405 must be the first general-tolerance option"
            )
        marking_panel_index = html.index("marking-flow-panel")
        marking_button_index = html.index(
            'id="applyDimensionMarkingsButton"'
        )
        tolerance_panel_index = html.index('data-for="general_tolerance"')
        if not (
            marking_panel_index
            < marking_button_index
            < tolerance_panel_index
        ):
            raise RuntimeError(
                "batch marking is not separated into the marking menu"
            )
        for required_layout_marker in (
            "inset: 56px 0 50px",
            "left: 14px; bottom: 64px",
        ):
            if required_layout_marker not in styles:
                raise RuntimeError(
                    "packaged UI does not reserve separate guide and "
                    f"notification areas: {required_layout_marker}"
                )
        if not _resource_path("windows_ocr.ps1").is_file():
            raise RuntimeError("packaged Windows OCR helper is missing")
        for obsolete_marker in (
            'data-mode="geometric_tolerance"',
            'data-mode="surface_finish"',
            'data-mode="detail_pair"',
            'data-mode="rect"',
            'data-mode="angled_rect"',
            "geometricSymbol1",
            "surfaceValue",
            "detailPairStep",
            'id="replacementValueX"',
            'id="replacementValueY"',
            'id="replacementToleranceX"',
            'id="replacementToleranceY"',
            "位置の微調整",
        ):
            if obsolete_marker in html or obsolete_marker in script:
                raise RuntimeError(
                    f"obsolete separate tool remains: {obsolete_marker}"
                )
        for drag_marker in (
            "青い枠をドラッグ",
            "replacementDrag",
            "replacementOffsets.toleranceX",
        ):
            if drag_marker not in html + script:
                raise RuntimeError(
                    f"replacement drag support is missing: {drag_marker}"
                )
        tool_buttons = re.findall(
            r'<button class="tool-card[\s\S]*?</button>',
            html,
        )
        if any("<kbd>" in button for button in tool_buttons):
            raise RuntimeError("tool shortcut badges are still displayed")
        if (
            "selectTool(modes" in script
            or 'event.key.toLowerCase() === "w"' in script
            or 'event.key.toLowerCase() === "d"' in script
            or "/^[0-9]$/.test(event.key)" in script
        ):
            raise RuntimeError("tool keyboard shortcuts are still enabled")

        request = Request(
            f"{base_url}/upload?name={pdf_path.name}",
            data=pdf_path.read_bytes(),
            method="POST",
            headers={
                "Content-Type": "application/pdf",
                "X-Drawing-Assist-Token": token,
            },
        )
        with urlopen(request, timeout=90) as response:
            state = json.loads(response.read().decode("utf-8"))
        diagonal_angle: float | None = None
        diagonal_target: fitz.Rect | None = None
        fallback_target: fitz.Rect | None = None
        manual_line: dict[str, Any] | None = None
        replacement_line: dict[str, Any] | None = None
        if api.document is not None:
            page = api.document[0]
            for block in page.get_text("rawdict").get("blocks", []):
                for line in block.get("lines", []):
                    text = "".join(
                        character.get("c", "")
                        for span in line.get("spans", [])
                        for character in span.get("chars", [])
                    ).strip()
                    direction = line.get("dir", (1.0, 0.0))
                    if (
                        text == "17.1"
                        and float(line["bbox"][0]) > 400
                    ):
                        replacement_line = line
                    if (
                        text
                        and abs(float(direction[0])) > 0.1
                        and abs(float(direction[1])) > 0.1
                    ):
                        candidate = fitz.Rect(line["bbox"])
                        fallback_target = fallback_target or candidate
                        if text == "C0.3":
                            manual_line = line
                        if text == "C0.15":
                            diagonal_target = candidate
        diagonal_target = diagonal_target or fallback_target
        if diagonal_target is not None:
            marked_state = api.apply_action(
                "word",
                {
                    "x": (
                        diagonal_target.x0 + diagonal_target.x1
                    ) / 2,
                    "y": (
                        diagonal_target.y0 + diagonal_target.y1
                    ) / 2,
                },
                {"color": "#fff24d", "opacity": 0.55},
            )
            if not marked_state.get("ok") or not api.items:
                raise RuntimeError("Diagonal highlight could not be applied")
            mark = api.items[-1]
            if not isinstance(mark, Mark) or not mark.quad:
                raise RuntimeError("Diagonal highlight has no oriented quad")
            edge = fitz.Point(mark.quad[1]) - fitz.Point(mark.quad[0])
            diagonal_angle = math.degrees(math.atan2(edge.y, edge.x))
            if abs(diagonal_angle) < 2.0:
                raise RuntimeError("Diagonal highlight was rendered horizontally")
            state = marked_state
        manual_diagonal_verified = False
        if manual_line is not None:
            recovered = fitz.recover_line_quad(manual_line)
            start = (recovered.ul + recovered.ll) / 2
            end = (recovered.ur + recovered.lr) / 2
            cross = recovered.ll - recovered.ul
            manual_state = api.apply_action(
                "word",
                {
                    "x0": start.x,
                    "y0": start.y,
                    "x1": end.x,
                    "y1": end.y,
                },
                {
                    "color": "#ff76bf",
                    "opacity": 0.55,
                    "mark_style": "angled",
                    "highlight_width": math.hypot(cross.x, cross.y),
                },
            )
            if not manual_state.get("ok") or len(api.items) < 2:
                raise RuntimeError("Manual diagonal highlight could not be applied")
            manual_mark = api.items[-1]
            if not isinstance(manual_mark, Mark) or not manual_mark.quad:
                raise RuntimeError("Manual diagonal highlight has no oriented quad")
            manual_diagonal_verified = True
            state = manual_state
        replacement_workflow_verified = False
        blank_tolerances_omitted = False
        if replacement_line is not None:
            target_rect = fitz.Rect(replacement_line["bbox"])
            selected_state = api.select_replacement(
                {
                    "x": (target_rect.x0 + target_rect.x1) / 2,
                    "y": (target_rect.y0 + target_rect.y1) / 2,
                }
            )
            selection_state = selected_state.get(
                "replacement_selection"
            )
            if (
                not selection_state
                or selection_state.get("original_value") != "17.1"
            ):
                raise RuntimeError(
                    "Original replacement value was not returned"
                )
            replacement_state = api.confirm_replacement(
                {
                    "replacement_value": "17.2",
                    "upper_tolerance": "",
                    "lower_tolerance": "",
                    "replacement_size": 14,
                    "replacement_tolerance_size": 6,
                    "replacement_value_x": 3,
                    "replacement_value_y": -2,
                }
            )
            replacement_mark = api.items[-1]
            if not isinstance(replacement_mark, ReplacementMark):
                raise RuntimeError("Replacement mark was not created")
            expected_span = replacement_line["spans"][0]
            expected_origin = tuple(
                float(value)
                for value in expected_span["origin"]
            )
            expected_direction = tuple(
                float(value)
                for value in replacement_line["dir"]
            )
            expected_size = 14.0
            if (
                replacement_mark.origin is None
                or math.dist(
                    replacement_mark.origin,
                    expected_origin,
                ) > 0.01
                or math.dist(
                    replacement_mark.direction,
                    expected_direction,
                ) > 0.001
                or abs(
                    replacement_mark.font_size - expected_size
                ) > 0.01
                or replacement_mark.tolerance_font_size != 6.0
                or replacement_mark.value_offset != (3.0, -2.0)
            ):
                raise RuntimeError(
                    "Replacement size or position settings were not applied"
                )
            blank_tolerances_omitted = (
                not replacement_mark.upper_tolerance
                and not replacement_mark.lower_tolerance
            )
            if not blank_tolerances_omitted:
                raise RuntimeError("Blank tolerances were retained")
            replacement_workflow_verified = True
            state = replacement_state
        unified_symbol_highlight_verified = False
        unified_detail_highlight_verified = False
        work_shape_verified = False
        work_line_verified = False
        work_auto_verified = False
        work_hatched_verified = False
        work_guided_verified = False
        work_fill_correction_verified = False
        work_outline_prediction_verified = False
        outline_document = fitz.open()
        try:
            outline_page = outline_document.new_page(width=340, height=300)
            expected_outline = (
                (55.0, 245.0),
                (55.0, 60.0),
                (140.0, 60.0),
                (140.0, 85.0),
                (285.0, 85.0),
                (285.0, 245.0),
            )
            outline_page.draw_polyline(
                [
                    fitz.Point(*point)
                    for point in expected_outline + (expected_outline[0],)
                ],
                color=(0, 0, 0),
                width=2.0,
            )
            outline_page.draw_line(
                fitz.Point(90, 155),
                fitz.Point(250, 155),
                color=(0.35, 0.35, 0.35),
                width=0.8,
            )
            predicted_outline = predict_work_outline(
                outline_page,
                (
                    (56.5, 243.5),
                    (53.5, 61.5),
                    (138.5, 58.5),
                    (141.5, 83.5),
                    (283.5, 86.5),
                    (286.5, 243.5),
                ),
            )
            predicted_bounds = _region_bbox(predicted_outline)
            expected_bounds = _region_bbox(expected_outline)
            work_outline_prediction_verified = all(
                abs(actual - expected) <= 3.0
                for actual, expected in zip(
                    tuple(predicted_bounds),
                    tuple(expected_bounds),
                )
            )
            if not work_outline_prediction_verified:
                raise RuntimeError(
                    "Ordered outline prediction bounds are incorrect: "
                    f"{tuple(predicted_bounds)!r}"
                )
        finally:
            outline_document.close()
        symbol_states = [
            api.apply_action(
                "word",
                {"x0": 485, "y0": 505, "x1": 535, "y1": 525},
                {"color": "#fff24d", "opacity": 0.50},
            ),
            api.apply_action(
                "word",
                {"x0": 70, "y0": 75, "x1": 125, "y1": 108},
                {"color": "#ff76bf", "opacity": 0.50},
            ),
        ]
        if (
            not all(item.get("ok") for item in symbol_states)
            or not all(
                isinstance(item, Mark)
                for item in api.items[-2:]
            )
        ):
            raise RuntimeError(
                "Unified geometric/surface symbol highlight failed"
            )
        unified_symbol_highlight_verified = True
        detail_states = [
            api.apply_action(
                "word",
                {"x0": 485, "y0": 475, "x1": 565, "y1": 492},
                {"color": "#ffb347", "opacity": 0.34},
            ),
            api.apply_action(
                "word",
                {"x0": 375, "y0": 330, "x1": 397, "y1": 348},
                {"color": "#ffb347", "opacity": 0.34},
            ),
        ]
        if (
            not all(item.get("ok") for item in detail_states)
            or not all(
                isinstance(item, Mark)
                for item in api.items[-2:]
            )
        ):
            raise RuntimeError("Unified detail highlight failed")
        unified_detail_highlight_verified = True
        candidate_state = api.detect_work_region(
            {"x": 210, "y": 410, "operation": "replace"},
            {"color": "#fff24d", "opacity": 0.32},
        )
        if (
            not candidate_state.get("ok")
            or candidate_state.get(
                "work_region_candidate_count"
            ) != 1
        ):
            raise RuntimeError("Semi-automatic work selection failed")
        confirmed_state = api.confirm_work_region()
        if (
            not confirmed_state.get("ok")
            or confirmed_state.get(
                "work_region_candidate_count"
            ) != 0
            or not isinstance(api.items[-1], WorkRegionMark)
        ):
            raise RuntimeError(
                "Semi-automatic work selection confirmation failed"
            )
        work_auto_verified = True
        hatched_candidate_state = api.detect_work_region(
            {"x": 520, "y": 420, "operation": "replace"},
            {"color": "#fff24d", "opacity": 0.32},
        )
        if (
            not hatched_candidate_state.get("ok")
            or hatched_candidate_state.get(
                "work_region_candidate_count"
            ) != 1
        ):
            raise RuntimeError(
                "Hatched work region selection failed"
            )
        hatched_confirmed_state = api.confirm_work_region()
        if (
            not hatched_confirmed_state.get("ok")
            or not isinstance(api.items[-1], WorkRegionMark)
        ):
            raise RuntimeError(
                "Hatched work region confirmation failed"
            )
        work_hatched_verified = True
        if api.document is None:
            raise RuntimeError("PDF was unexpectedly closed")
        raw_guided_region = detect_enclosed_region(
            api.document[api.page_index],
            fitz.Point(210, 410),
        )
        raw_guided_rect = _region_bbox(raw_guided_region)
        guided_candidate_state = api.predict_work_shape(
            {
                "points": [
                    {"x": 210, "y": 410},
                    {"x": 520, "y": 420},
                ]
            },
            {"color": "#fff24d", "opacity": 0.32},
        )
        if (
            not guided_candidate_state.get("ok")
            or guided_candidate_state.get(
                "work_region_candidate_count"
            ) != 2
        ):
            raise RuntimeError(
                "Guided multi-point work prediction failed"
            )
        corrected_rect = _region_bbox(api.work_region_candidates[0])
        work_fill_correction_verified = (
            corrected_rect.x0 < raw_guided_rect.x0 - 0.7
            and corrected_rect.y0 < raw_guided_rect.y0 - 0.7
            and corrected_rect.x1 > raw_guided_rect.x1 + 0.7
            and corrected_rect.y1 > raw_guided_rect.y1 + 0.7
        )
        if not work_fill_correction_verified:
            raise RuntimeError(
                "Work region edge-gap correction was not applied"
            )
        removed_guided_state = api.detect_work_region(
            {"x": 210, "y": 410, "operation": "remove"},
            {"color": "#fff24d", "opacity": 0.32},
        )
        restored_guided_state = api.detect_work_region(
            {"x": 210, "y": 410, "operation": "add"},
            {"color": "#fff24d", "opacity": 0.32},
        )
        if (
            removed_guided_state.get(
                "work_region_candidate_count"
            ) != 1
            or restored_guided_state.get(
                "work_region_candidate_count"
            ) != 2
        ):
            raise RuntimeError(
                "Guided candidate add/remove correction failed"
            )
        guided_confirmed_state = api.confirm_work_region()
        if (
            not guided_confirmed_state.get("ok")
            or not isinstance(api.items[-1], WorkRegionMark)
            or len(api.items[-1].regions) != 2
        ):
            raise RuntimeError(
                "Guided work prediction confirmation failed"
            )
        work_guided_verified = True
        work_state = api.apply_action(
            "work_shape",
            {
                "points": [
                    {"x": 190, "y": 285},
                    {"x": 250, "y": 285},
                    {"x": 265, "y": 330},
                    {"x": 205, "y": 345},
                    {"x": 180, "y": 315},
                ]
            },
            {
                "color": "#fff24d",
                "opacity": 0.32,
                "work_shape_style": "fill",
                "work_line_width": 6,
            },
        )
        if (
            not work_state.get("ok")
            or not isinstance(api.items[-1], WorkShapeMark)
            or api.items[-1].style != "fill"
        ):
            raise RuntimeError("Workpiece area highlight failed")
        work_shape_verified = True
        work_line_state = api.apply_action(
            "work_shape",
            {
                "points": [
                    {"x": 310, "y": 270},
                    {"x": 330, "y": 290},
                    {"x": 350, "y": 270},
                    {"x": 370, "y": 300},
                ]
            },
            {
                "color": "#72df78",
                "opacity": 0.40,
                "work_shape_style": "line",
                "work_line_width": 5,
            },
        )
        if (
            not work_line_state.get("ok")
            or not isinstance(api.items[-1], WorkShapeMark)
            or api.items[-1].style != "line"
        ):
            raise RuntimeError("Workpiece solid-line highlight failed")
        work_line_verified = True
        state = work_line_state
        result_path.parent.mkdir(parents=True, exist_ok=True)
        export_pdf(pdf_path, annotation_check_path, api.items)
        annotation_document = fitz.open(annotation_check_path)
        editable_marker_count = 0
        transparent_annotation_borders = True
        for annotation_page in annotation_document:
            for annotation in annotation_page.annots() or []:
                if not annotation.colors.get("fill"):
                    continue
                editable_marker_count += 1
                raw_annotation = annotation_document.xref_object(
                    annotation.xref,
                    compressed=False,
                )
                if (
                    not re.search(
                        r"/C\s*\[\s*\]",
                        raw_annotation,
                    )
                    or annotation.border.get("width") != 0
                    or "/AP" not in raw_annotation
                ):
                    transparent_annotation_borders = False
        annotation_document.close()
        if (
            editable_marker_count == 0
            or not transparent_annotation_borders
        ):
            raise RuntimeError(
                "Marker annotations retained a visible fallback border"
            )
        dimension_source_style_verified = False
        dimension_font_face_verified = False
        dimension_overlap_avoidance_verified = False
        dimension_document = fitz.open()
        try:
            dimension_page = dimension_document.new_page(
                width=420,
                height=300,
            )
            source_font = Path("C:/Windows/Fonts/msgothic.ttc")
            for x, y, text in (
                (80, 80, "C0.3"),
                (180, 80, "9.8"),
                (280, 80, "C0.15"),
                (180, 150, "16"),
            ):
                dimension_page.insert_text(
                    (x, y),
                    text,
                    fontname="source_dimension",
                    fontfile=str(source_font),
                    fontsize=9.4,
                )
                dimension_page.draw_line(
                    (x - 15, y + 4),
                    (x + 45, y + 4),
                    color=(0, 0, 0),
                    width=0.24,
                )
            source_style = infer_dimension_style(
                dimension_page,
                (200, 210),
                (180, 72),
            )
            dimension_source_style_verified = (
                "Gothic" in source_style.font_name
                and abs(source_style.font_size - 9.4) < 0.25
                and abs(source_style.line_width - 0.24) < 0.05
            )
            if not dimension_source_style_verified:
                raise RuntimeError(
                    "Dimension source style was not preserved: "
                    f"{source_style!r}"
                )
            preferred_dimension = DimensionMark(
                page_index=0,
                target=(200, 210),
                label=(180, 72),
                text="R0.1以下",
                font_size=source_style.font_size,
                font_name="MS-PGothic",
                font_color=source_style.font_color,
                line_width=source_style.line_width,
            )
            fixed_width_dimension = DimensionMark(
                page_index=0,
                target=preferred_dimension.target,
                label=preferred_dimension.label,
                text=preferred_dimension.text,
                font_size=preferred_dimension.font_size,
                font_name="MS-Gothic",
                font_color=preferred_dimension.font_color,
                line_width=preferred_dimension.line_width,
            )
            proportional_width = dimension_label_rect(
                preferred_dimension
            ).width
            fixed_width = dimension_label_rect(
                fixed_width_dimension
            ).width
            dimension_font_face_verified = (
                proportional_width < fixed_width * 0.98
            )
            if not dimension_font_face_verified:
                raise RuntimeError(
                    "MS PGothic proportional face was not selected: "
                    f"{proportional_width:.2f} / {fixed_width:.2f}"
                )
            adjusted_dimension = avoid_dimension_overlap(
                dimension_page,
                preferred_dimension,
            )
            occupied_dimension = fitz.Rect(178, 68, 205, 84)
            dimension_overlap_avoidance_verified = (
                adjusted_dimension.label != preferred_dimension.label
                and not dimension_label_rect(
                    adjusted_dimension
                ).intersects(occupied_dimension)
            )
            if not dimension_overlap_avoidance_verified:
                raise RuntimeError(
                    "New dimension text was not moved away from "
                    "an existing dimension value"
                )
        finally:
            dimension_document.close()
        rotated_marker_position_verified = False
        rotated_document = fitz.open()
        try:
            rotated_page = rotated_document.new_page(
                width=841.92,
                height=1190.52,
            )
            rotated_page.set_rotation(90)
            apply_item_to_page(
                rotated_page,
                Mark(
                    page_index=0,
                    rect=(250.0, 250.0, 400.0, 320.0),
                    color="#fff24d",
                    opacity=0.55,
                ),
            )
            rotated_pixmap = rotated_page.get_pixmap(
                matrix=fitz.Matrix(2.0, 2.0),
                alpha=False,
                annots=True,
            )
            expected_pixel = rotated_pixmap.pixel(650, 570)
            old_wrong_pixel = rotated_pixmap.pixel(1810, 650)
            rotated_marker_position_verified = (
                expected_pixel[0] > 230
                and expected_pixel[1] > 220
                and expected_pixel[2] < 230
                and old_wrong_pixel == (255, 255, 255)
            )
            if not rotated_marker_position_verified:
                raise RuntimeError(
                    "Rotated PDF marker position is incorrect: "
                    f"expected={expected_pixel}, old={old_wrong_pixel}"
                )
        finally:
            rotated_document.close()
        image_pdf_click_selection_verified = False
        scanned_image = Image.new("RGB", (1200, 500), "white")
        scanned_draw = ImageDraw.Draw(scanned_image)
        scan_font_path = Path("C:/Windows/Fonts/arial.ttf")
        scan_font = (
            ImageFont.truetype(str(scan_font_path), 72)
            if scan_font_path.is_file()
            else ImageFont.load_default()
        )
        scan_text = "C0.3 +0.05 -0.02"
        scan_origin = (140, 170)
        scanned_draw.text(
            scan_origin,
            scan_text,
            font=scan_font,
            fill="black",
        )
        scan_bbox = scanned_draw.textbbox(
            scan_origin,
            scan_text,
            font=scan_font,
        )
        scan_buffer = BytesIO()
        scanned_image.save(scan_buffer, format="PNG")
        scan_document = fitz.open()
        try:
            scan_page = scan_document.new_page(width=600, height=250)
            scan_page.insert_image(
                scan_page.rect,
                stream=scan_buffer.getvalue(),
            )
            scan_hit = detect_visual_text_group(
                scan_page,
                fitz.Point(
                    (scan_bbox[0] + scan_bbox[2]) / 4,
                    (scan_bbox[1] + scan_bbox[3]) / 4,
                ),
            )
            image_pdf_click_selection_verified = (
                scan_hit.rect[2] - scan_hit.rect[0] > 80
                and scan_hit.rect[3] - scan_hit.rect[1] > 12
                and scan_hit.quad is not None
            )
            if not image_pdf_click_selection_verified:
                raise RuntimeError(
                    "Image-only PDF click selection returned "
                    f"an invalid candidate: {scan_hit.rect}"
                )
        finally:
            scan_document.close()
        image_value = str(state.get("image") or "")
        prefix = "data:image/png;base64,"
        if not state.get("ok") or not state.get("loaded"):
            raise RuntimeError(state.get("message") or "PDF did not load")
        if not image_value.startswith(prefix):
            raise RuntimeError("PDF preview was not rendered")
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(base64.b64decode(image_value[len(prefix):]))
        result = {
            "ok": True,
            "transport": "local-http",
            "packaged_assets": True,
            "tool_shortcuts_removed": True,
            "file_name": state.get("file_name"),
            "page_count": state.get("page_count"),
            "preview_bytes": preview_path.stat().st_size,
            "diagonal_highlight": diagonal_angle is not None,
            "manual_diagonal_highlight": manual_diagonal_verified,
            "replacement_workflow": replacement_workflow_verified,
            "blank_tolerances_omitted": blank_tolerances_omitted,
            "unified_symbol_highlight": (
                unified_symbol_highlight_verified
            ),
            "unified_detail_highlight": (
                unified_detail_highlight_verified
            ),
            "work_shape_auto": work_auto_verified,
            "work_shape_hatched": work_hatched_verified,
            "work_shape_guided": work_guided_verified,
            "work_outline_prediction": (
                work_outline_prediction_verified
            ),
            "work_fill_correction": (
                work_fill_correction_verified
            ),
            "work_shape_fill": work_shape_verified,
            "work_shape_line": work_line_verified,
            "transparent_annotation_borders": (
                transparent_annotation_borders
            ),
            "editable_marker_annotations": editable_marker_count > 0,
            "dimension_source_style": (
                dimension_source_style_verified
            ),
            "dimension_font_face": (
                dimension_font_face_verified
            ),
            "dimension_overlap_avoidance": (
                dimension_overlap_avoidance_verified
            ),
            "rotated_marker_position": (
                rotated_marker_position_verified
            ),
            "image_pdf_click_selection": (
                image_pdf_click_selection_verified
            ),
            "diagonal_angle": (
                round(diagonal_angle, 2)
                if diagonal_angle is not None
                else None
            ),
        }
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        raise
    finally:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        api.close()
        annotation_check_path.unlink(missing_ok=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> None:
    api = DrawingApi()
    server, server_thread, token = start_local_server(api)
    window = webview.create_window(
        "加工図面作成支援ツール",
        url=f"http://127.0.0.1:{server.server_port}/?token={token}",
        width=1480,
        height=940,
        min_size=(1080, 680),
        background_color="#111827",
        text_select=False,
    )
    if window is None:
        raise RuntimeError("ウィンドウを作成できませんでした。")
    api.set_window(window)

    def on_closed() -> None:
        api.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    window.events.closed += on_closed
    webview.start(gui="edgechromium", private_mode=False)


if __name__ == "__main__":
    freeze_support()
    if "--self-test" in sys.argv:
        test_index = sys.argv.index("--self-test")
        result_index = sys.argv.index("--result")
        preview_index = sys.argv.index("--preview")
        _self_test(
            Path(sys.argv[test_index + 1]).resolve(),
            Path(sys.argv[result_index + 1]).resolve(),
            Path(sys.argv[preview_index + 1]).resolve(),
        )
    else:
        main()
