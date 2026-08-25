"""Lightweight local drawing-structure evidence for OCR candidates.

This module deliberately does not decide what a dimension says.  OCR remains
the text reader; OpenCV provides independent geometric evidence so duplicate
OCR readings can prefer the coordinate that is actually near a dimension
line.  It is local-only and runs once per rendered page.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import fitz

from drawing_assist.local_ocr import LocalOcrPage


@dataclass(frozen=True)
class StructureSegment:
    """A horizontal or vertical drafting segment in PDF coordinates."""

    x0: float
    y0: float
    x1: float
    y1: float
    horizontal: bool


def extract_structure_segments(ocr_page: LocalOcrPage) -> tuple[StructureSegment, ...]:
    """Extract long axis-aligned drafting lines once from a page raster.

    A reduced image caps OpenCV work on high-DPI scans.  Coordinates are
    immediately transformed back to PDF space, so later scoring is invariant
    to the OCR rendering scale.
    """

    try:
        import cv2
        import numpy as np
    except ImportError:
        return ()

    gray = np.asarray(ocr_page.image.convert("L"))
    if gray.size == 0:
        return ()
    original_height, original_width = gray.shape[:2]
    maximum = max(original_width, original_height)
    resize = min(1.0, 1800.0 / maximum)
    if resize < 1.0:
        gray = cv2.resize(
            gray,
            (round(original_width * resize), round(original_height * resize)),
            interpolation=cv2.INTER_AREA,
        )
    edges = cv2.Canny(gray, 50, 145, apertureSize=3, L2gradient=True)
    minimum_length = max(22, min(gray.shape[:2]) // 42)
    raw_lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=math.pi / 180,
        threshold=max(14, minimum_length // 2),
        minLineLength=minimum_length,
        maxLineGap=max(3, minimum_length // 7),
    )
    if raw_lines is None:
        return ()
    scale_x = ocr_page.scale_x * resize
    scale_y = ocr_page.scale_y * resize
    segments: list[StructureSegment] = []
    for x0, y0, x1, y1 in raw_lines[:, 0, :]:
        dx, dy = float(x1 - x0), float(y1 - y0)
        length = math.hypot(dx, dy)
        if length < minimum_length:
            continue
        horizontal = abs(dx) >= abs(dy) * 4.0
        vertical = abs(dy) >= abs(dx) * 4.0
        if not horizontal and not vertical:
            continue
        segments.append(
            StructureSegment(
                float(x0) / scale_x,
                float(y0) / scale_y,
                float(x1) / scale_x,
                float(y1) / scale_y,
                horizontal,
            )
        )
    # Hough can return each physical line twice.  The score only needs nearby
    # evidence, so retaining a bounded set keeps subsequent candidate scoring
    # predictable and fast.
    return tuple(segments[:700])


def dimension_line_score(
    rect: tuple[float, float, float, float],
    direction: tuple[float, float],
    segments: tuple[StructureSegment, ...],
) -> float:
    """Return geometric support for an OCR candidate without rejecting it.

    ``0.5`` is intentionally neutral when a scan has no usable line evidence;
    lack of a detected line must not turn into an OCR false negative.  Scores
    above it mean one or more compatible dimension/extension lines occur near
    the text in the expected orientation.
    """

    if not segments:
        return 0.5
    box = fitz.Rect(rect)
    if box.is_empty:
        return 0.5
    horizontal_text = abs(direction[0]) >= abs(direction[1])
    text_length = box.width if horizontal_text else box.height
    text_thickness = box.height if horizontal_text else box.width
    perpendicular_limit = max(12.0, text_thickness * 3.2)
    along_padding = max(20.0, text_length * 2.2)
    matches = 0
    for segment in segments:
        if segment.horizontal != horizontal_text:
            continue
        if horizontal_text:
            perpendicular = abs((segment.y0 + segment.y1) / 2 - (box.y0 + box.y1) / 2)
            segment_start, segment_end = sorted((segment.x0, segment.x1))
            text_start, text_end = box.x0 - along_padding, box.x1 + along_padding
        else:
            perpendicular = abs((segment.x0 + segment.x1) / 2 - (box.x0 + box.x1) / 2)
            segment_start, segment_end = sorted((segment.y0, segment.y1))
            text_start, text_end = box.y0 - along_padding, box.y1 + along_padding
        if perpendicular <= perpendicular_limit and segment_end >= text_start and segment_start <= text_end:
            matches += 1
            if matches >= 2:
                return 0.95
    return 0.74 if matches else 0.5
