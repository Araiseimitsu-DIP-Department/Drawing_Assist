from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import heapq
from io import BytesIO
import math
import os
from pathlib import Path
import re
from typing import Iterable, TypeAlias
import unicodedata

import fitz
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont


@dataclass(frozen=True)
class Mark:
    """A translucent highlight in PDF page coordinates."""

    page_index: int
    rect: tuple[float, float, float, float]
    color: str
    opacity: float = 0.42
    quad: tuple[tuple[float, float], ...] | None = None


@dataclass(frozen=True)
class StrikeMark:
    """Two parallel strike-through lines."""

    page_index: int
    start: tuple[float, float]
    end: tuple[float, float]
    normal: tuple[float, float]
    gap: float = 1.35
    width: float = 0.8


@dataclass(frozen=True)
class DimensionMark:
    """A leader arrow with an editable dimension/note label."""

    page_index: int
    target: tuple[float, float]
    label: tuple[float, float]
    text: str
    color: str = "#fff24d"
    opacity: float = 0.42
    font_size: float = 10.0
    font_name: str = ""
    font_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    line_width: float = 0.45
    show_leader: bool = True


@dataclass(frozen=True)
class DimensionStyle:
    """Drawing-derived typography and line weight for a new dimension."""

    font_size: float
    font_name: str
    font_color: tuple[float, float, float]
    line_width: float


@dataclass(frozen=True)
class StampMark:
    """A circular quality or process stamp."""

    page_index: int
    center: tuple[float, float]
    kind: str
    name: str
    date: str
    size: float = 62.0


@dataclass(frozen=True)
class ProcedureNoteMark:
    """A procedure-required note placed directly on the drawing."""

    page_index: int
    origin: tuple[float, float]
    kind: str
    text: str
    font_size: float = 10.0


def stamp_mark_rect(mark: StampMark) -> fitz.Rect:
    """Return the exact square occupied by a stamp."""

    center = fitz.Point(mark.center)
    radius = max(1.0, mark.size / 2)
    return fitz.Rect(
        center.x - radius,
        center.y - radius,
        center.x + radius,
        center.y + radius,
    )


def procedure_note_rect(mark: ProcedureNoteMark) -> fitz.Rect:
    """Return a practical selection box matching the rendered note."""

    origin = fitz.Point(mark.origin)
    font_size = max(6.0, min(24.0, mark.font_size))
    font = _pdf_font()
    lines = [line.strip() for line in mark.text.splitlines() if line.strip()]
    if not lines:
        return fitz.Rect(origin.x, origin.y, origin.x + font_size, origin.y + font_size)

    def text_width(text: str, size: float = font_size) -> float:
        return font.text_length(text, fontsize=size)

    if mark.kind == "phase":
        width = text_width(lines[0]) + font_size * 1.3
        height = font_size * 1.75
    elif mark.kind == "post_process":
        content_width = max((text_width(line) for line in lines), default=0.0)
        width = max(150.0, content_width + font_size * 1.2)
        header_height = font_size * 2.0
        body_height = max(
            font_size * 2.3,
            len(lines) * font_size * 1.3 + font_size,
        )
        height = header_height + body_height
    else:
        width = max((text_width(line) for line in lines), default=font_size)
        height = font_size + max(0, len(lines) - 1) * font_size * 1.28
        width += max(1.5, font_size * 0.12)
        height += max(1.5, font_size * 0.18)
    return fitz.Rect(origin.x, origin.y, origin.x + width, origin.y + height)


@dataclass(frozen=True)
class ReplacementMark:
    """A white-out and replacement for an existing dimension value."""

    page_index: int
    rect: tuple[float, float, float, float]
    direction: tuple[float, float]
    value: str
    upper_tolerance: str = ""
    lower_tolerance: str = ""
    font_size: float = 9.0
    tolerance_font_size: float | None = None
    value_offset: tuple[float, float] = (0.0, 0.0)
    tolerance_offset: tuple[float, float] = (0.0, 0.0)
    origin: tuple[float, float] | None = None
    font_name: str = ""
    font_color: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class ToleranceAddition:
    """One general-tolerance label positioned after an OCR dimension."""

    origin: tuple[float, float]
    direction: tuple[float, float]
    text: str
    font_size: float
    suffix_text: str = ""
    suffix_rect: tuple[float, float, float, float] | None = None
    suffix_font_size: float | None = None


@dataclass(frozen=True)
class GeneralToleranceBatchMark:
    """A single undoable batch of automatically added tolerances."""

    page_index: int
    additions: tuple[ToleranceAddition, ...]


@dataclass(frozen=True)
class DimensionMarkingEntry:
    rect: tuple[float, float, float, float]
    color: str
    opacity: float = 0.42
    quad: tuple[tuple[float, float], ...] | None = None
    kind: str = ""


@dataclass
class DimensionMarkingCandidate:
    """Preview candidate before committing automatic color markings."""

    page_index: int
    rect: tuple[float, float, float, float]
    color: str
    opacity: float = 0.42
    quad: tuple[tuple[float, float], ...] | None = None
    kind: str = ""
    selected: bool = True


@dataclass(frozen=True)
class DimensionMarkingBatch:
    """A single undoable batch of dimension and tolerance highlights."""

    page_index: int
    entries: tuple[DimensionMarkingEntry, ...]


@dataclass(frozen=True)
class GeometricToleranceMark:
    """One or two feature-control frame rows, optionally with a leader."""

    page_index: int
    target: tuple[float, float]
    label: tuple[float, float]
    rows: tuple[tuple[str, str, str], ...]
    color: str = "#fff24d"
    opacity: float = 0.42
    font_size: float = 9.0
    leader: bool = False


@dataclass(frozen=True)
class SurfaceFinishMark:
    """A configurable triangular surface-finish callout."""

    page_index: int
    anchor: tuple[float, float]
    value: str
    triangle_count: int = 3
    orientation: str = "horizontal"
    value_position: str = "right"
    parenthesized: bool = False
    color: str = "#ff76bf"
    opacity: float = 0.42
    font_size: float = 10.0


@dataclass(frozen=True)
class WorkShapeMark:
    """A user-defined workpiece fill or traced solid-line path."""

    page_index: int
    points: tuple[tuple[float, float], ...]
    color: str = "#fff24d"
    opacity: float = 0.32
    style: str = "fill"
    line_width: float = 6.0


@dataclass(frozen=True)
class WorkRegionMark:
    """One confirmed semi-automatic workpiece selection."""

    page_index: int
    regions: tuple[tuple[tuple[float, float], ...], ...]
    color: str = "#fff24d"
    opacity: float = 0.32


@dataclass(frozen=True)
class DetailPairMark:
    """Two linked highlights for a detail caption and its drawing callout."""

    page_index: int
    areas: tuple[tuple[float, float, float, float], ...]
    color: str = "#ffb347"
    opacity: float = 0.32


@dataclass(frozen=True)
class TextHit:
    rect: tuple[float, float, float, float]
    text: str
    direction: tuple[float, float]
    font_size: float = 9.0
    quad: tuple[tuple[float, float], ...] | None = None
    origin: tuple[float, float] | None = None
    nominal_text: str = ""
    font_name: str = ""
    font_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    replacement_rect: tuple[float, float, float, float] | None = None
    preserved_prefix: str = ""


@dataclass(frozen=True)
class _InkComponent:
    """One connected dark component in a rendered PDF crop."""

    bbox: tuple[int, int, int, int]
    area: int
    center: tuple[float, float]
    major_span: float
    elongation: float


DrawingItem: TypeAlias = (
    Mark
    | StrikeMark
    | DimensionMark
    | StampMark
    | ProcedureNoteMark
    | ReplacementMark
    | GeneralToleranceBatchMark
    | DimensionMarkingBatch
    | GeometricToleranceMark
    | SurfaceFinishMark
    | WorkShapeMark
    | WorkRegionMark
    | DetailPairMark
)


def hex_to_rgb(color: str) -> tuple[float, float, float]:
    value = color.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Unsupported color: {color}")
    return tuple(int(value[index : index + 2], 16) / 255 for index in (0, 2, 4))


def _polygon_area(points: list[tuple[float, float]]) -> float:
    return sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    ) / 2


def _rdp(
    points: list[tuple[float, float]],
    tolerance: float,
) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    start = points[0]
    end = points[-1]
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    farthest_index = 0
    farthest_distance = 0.0
    for index, point in enumerate(points[1:-1], start=1):
        if length:
            distance = abs(
                dy * point[0]
                - dx * point[1]
                + end[0] * start[1]
                - end[1] * start[0]
            ) / length
        else:
            distance = math.dist(start, point)
        if distance > farthest_distance:
            farthest_distance = distance
            farthest_index = index
    if farthest_distance <= tolerance:
        return [start, end]
    left = _rdp(points[: farthest_index + 1], tolerance)
    right = _rdp(points[farthest_index:], tolerance)
    return left[:-1] + right


def _simplify_closed_polygon(
    points: list[tuple[float, float]],
    tolerance: float,
) -> list[tuple[float, float]]:
    if len(points) < 4:
        return points
    clean: list[tuple[float, float]] = []
    for point in points:
        if not clean or point != clean[-1]:
            clean.append(point)
    if clean and clean[0] == clean[-1]:
        clean.pop()
    if len(clean) < 4:
        return clean
    split_index = max(
        range(1, len(clean)),
        key=lambda index: math.dist(clean[0], clean[index]),
    )
    first = _rdp(clean[: split_index + 1], tolerance)
    second = _rdp(clean[split_index:] + [clean[0]], tolerance)
    simplified = first[:-1] + second[:-1]
    return simplified if len(simplified) >= 3 else clean


def _largest_mask_boundary(
    mask: Image.Image,
) -> list[tuple[float, float]]:
    width, height = mask.size
    data = mask.tobytes()
    bbox = mask.getbbox()
    if bbox is None:
        return []
    x0, y0, x1, y1 = bbox
    edges: dict[
        tuple[int, int],
        list[tuple[int, int]],
    ] = {}

    def selected(x: int, y: int) -> bool:
        return (
            0 <= x < width
            and 0 <= y < height
            and data[y * width + x] == 255
        )

    def add_edge(
        start: tuple[int, int],
        end: tuple[int, int],
    ) -> None:
        edges.setdefault(start, []).append(end)

    for y in range(y0, y1):
        for x in range(x0, x1):
            if not selected(x, y):
                continue
            if not selected(x, y - 1):
                add_edge((x, y), (x + 1, y))
            if not selected(x + 1, y):
                add_edge((x + 1, y), (x + 1, y + 1))
            if not selected(x, y + 1):
                add_edge((x + 1, y + 1), (x, y + 1))
            if not selected(x - 1, y):
                add_edge((x, y + 1), (x, y))

    unused = {
        (start, end)
        for start, destinations in edges.items()
        for end in destinations
    }
    loops: list[list[tuple[float, float]]] = []
    while unused:
        start_edge = next(iter(unused))
        start, end = start_edge
        unused.remove(start_edge)
        loop: list[tuple[float, float]] = [start, end]
        current = end
        guard = len(unused) + 2
        while current != start and guard > 0:
            candidates = [
                candidate
                for candidate in edges.get(current, [])
                if (current, candidate) in unused
            ]
            if not candidates:
                break
            next_point = candidates[0]
            unused.remove((current, next_point))
            loop.append(next_point)
            current = next_point
            guard -= 1
        if current == start and len(loop) >= 4:
            loops.append(loop[:-1])
    if not loops:
        return []
    return max(loops, key=lambda loop: abs(_polygon_area(loop)))


def _flood_component(
    barrier: Image.Image,
    seed: tuple[int, int],
) -> Image.Image:
    filled = barrier.copy()
    ImageDraw.floodfill(filled, seed, 128, border=255)
    return filled.point(
        lambda value: 255 if value == 128 else 0,
        mode="L",
    )


def _first_mask_pixel(mask: Image.Image) -> tuple[int, int] | None:
    bbox = mask.getbbox()
    if bbox is None:
        return None
    pixels = mask.load()
    for y in range(bbox[1], bbox[3]):
        for x in range(bbox[0], bbox[2]):
            if pixels[x, y]:
                return x, y
    return None


def _merge_hatched_components(
    barrier: Image.Image,
    initial: Image.Image,
    *,
    page_pixels: int,
) -> Image.Image:
    """Join small cells separated by thin hatch lines.

    A very large neighboring component is the page background and is rejected.
    Tall, narrow neighbors are normally holes or slots and are also rejected.
    """

    initial_bbox = initial.getbbox()
    if initial_bbox is None:
        return initial
    initial_width = max(1, initial_bbox[2] - initial_bbox[0])
    initial_height = max(1, initial_bbox[3] - initial_bbox[1])
    merged = initial
    rejected = Image.new("L", barrier.size, 0)
    probable_hole_boxes: list[tuple[int, int, int, int]] = []
    background = barrier.point(
        lambda value: 255 if value == 0 else 0,
        mode="L",
    )
    bridge_size = 7
    for _ in range(48):
        nearby = merged.filter(ImageFilter.MaxFilter(bridge_size))
        candidates = ImageChops.multiply(nearby, background)
        candidates = ImageChops.multiply(
            candidates,
            ImageChops.invert(merged),
        )
        candidates = ImageChops.multiply(
            candidates,
            ImageChops.invert(rejected),
        )
        candidate_seed = _first_mask_pixel(candidates)
        if candidate_seed is None:
            break
        component = _flood_component(barrier, candidate_seed)
        component_bbox = component.getbbox()
        if component_bbox is None:
            break
        component_pixels = component.histogram()[255]
        component_width = component_bbox[2] - component_bbox[0]
        component_height = component_bbox[3] - component_bbox[1]
        is_page_background = component_pixels > page_pixels * 0.05
        is_probable_hole = (
            component_height > initial_height * 1.4
            and component_width < initial_width * 0.7
        )
        is_inside_probable_hole = any(
            component_bbox[0] >= hole_bbox[0] - 2
            and component_bbox[1] >= hole_bbox[1] - 2
            and component_bbox[2] <= hole_bbox[2] + 2
            and component_bbox[3] <= hole_bbox[3] + 2
            for hole_bbox in probable_hole_boxes
        )
        if (
            is_page_background
            or is_probable_hole
            or is_inside_probable_hole
        ):
            rejected = ImageChops.lighter(rejected, component)
            if is_probable_hole:
                probable_hole_boxes.append(component_bbox)
        else:
            merged = ImageChops.lighter(merged, component)
    merged = merged.filter(
        ImageFilter.MaxFilter(bridge_size)
    ).filter(
        ImageFilter.MinFilter(bridge_size)
    )
    return ImageChops.subtract(merged, rejected)


def detect_enclosed_region(
    page: fitz.Page,
    point: fitz.Point,
    *,
    render_scale: float = 1.8,
) -> tuple[tuple[float, float], ...]:
    """Detect a closed drawing region around a user-selected seed point.

    Multiple darkness thresholds are tried because scanned drawings often have
    pale outer lines. Small cells separated by hatch lines are joined while the
    page background and likely holes remain excluded. The result is only a
    candidate and must be confirmed by the user.
    """

    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(render_scale, render_scale),
        colorspace=fitz.csGRAY,
        alpha=False,
        annots=True,
    )
    full_grayscale = Image.frombytes(
        "L",
        (pixmap.width, pixmap.height),
        pixmap.samples,
    )
    full_seed = (
        int(round((point.x - page.rect.x0) * render_scale)),
        int(round((point.y - page.rect.y0) * render_scale)),
    )
    full_seed = (
        max(2, min(full_grayscale.width - 3, full_seed[0])),
        max(2, min(full_grayscale.height - 3, full_seed[1])),
    )
    half_width = int(round(260 * render_scale))
    half_height = int(round(190 * render_scale))
    crop_box = (
        max(0, full_seed[0] - half_width),
        max(0, full_seed[1] - half_height),
        min(full_grayscale.width, full_seed[0] + half_width),
        min(full_grayscale.height, full_seed[1] + half_height),
    )
    grayscale = full_grayscale.crop(crop_box)
    initial_seed = (
        full_seed[0] - crop_box[0],
        full_seed[1] - crop_box[1],
    )
    page_pixels = full_grayscale.width * full_grayscale.height
    mask: Image.Image | None = None
    for threshold in (82, 110, 140, 170, 200, 225):
        barrier = grayscale.point(
            lambda value, limit=threshold: (
                255 if value < limit else 0
            ),
            mode="L",
        )
        barrier = barrier.filter(ImageFilter.MaxFilter(3))
        barrier = barrier.filter(ImageFilter.MinFilter(3))
        draw = ImageDraw.Draw(barrier)
        draw.rectangle(
            (0, 0, barrier.width - 1, barrier.height - 1),
            outline=255,
            width=2,
        )
        seed = initial_seed
        if barrier.getpixel(seed) == 255:
            candidates: list[tuple[float, int, int]] = []
            for radius in range(1, 15):
                for y in range(
                    seed[1] - radius,
                    seed[1] + radius + 1,
                ):
                    for x in range(
                        seed[0] - radius,
                        seed[0] + radius + 1,
                    ):
                        if (
                            1 <= x < barrier.width - 1
                            and 1 <= y < barrier.height - 1
                            and barrier.getpixel((x, y)) != 255
                        ):
                            candidates.append(
                                (
                                    math.hypot(
                                        x - seed[0],
                                        y - seed[1],
                                    ),
                                    x,
                                    y,
                                )
                            )
                if candidates:
                    _, seed_x, seed_y = min(candidates)
                    seed = (seed_x, seed_y)
                    break
        if barrier.getpixel(seed) == 255:
            continue
        candidate = _flood_component(barrier, seed)
        selected_pixels = candidate.histogram()[255]
        candidate_bbox = candidate.getbbox()
        if candidate_bbox is None or selected_pixels < 80:
            continue
        touches_crop_border = (
            candidate_bbox[0] <= 2
            or candidate_bbox[1] <= 2
            or candidate_bbox[2] >= candidate.width - 2
            or candidate_bbox[3] >= candidate.height - 2
        )
        if (
            touches_crop_border
            or selected_pixels > candidate.width * candidate.height * 0.45
        ):
            continue
        if (
            threshold >= 140
            and selected_pixels < page_pixels * 0.003
        ):
            candidate = _merge_hatched_components(
                barrier,
                candidate,
                page_pixels=page_pixels,
            )
        if candidate.histogram()[255] > page_pixels * 0.08:
            continue
        mask = candidate
        break
    if mask is None:
        raise ValueError(
            "閉じたワーク範囲を検出できませんでした。手動点指定をご使用ください。"
        )
    boundary = _largest_mask_boundary(mask)
    if len(boundary) < 3:
        raise ValueError("ワークの外形を検出できませんでした。")
    boundary = _simplify_closed_polygon(
        boundary,
        tolerance=max(1.4, render_scale * 0.9),
    )
    points = tuple(
        (
            page.rect.x0 + (x + crop_box[0]) / render_scale,
            page.rect.y0 + (y + crop_box[1]) / render_scale,
        )
        for x, y in boundary
    )
    if len(points) < 3:
        raise ValueError("ワークの外形を検出できませんでした。")
    return points


def expand_work_region(
    points: tuple[tuple[float, float], ...],
    page_rect: fitz.Rect,
    *,
    padding: float = 1.1,
    raster_scale: float = 4.0,
) -> tuple[tuple[float, float], ...]:
    """Expand a detected region slightly to cover anti-aliased edge gaps.

    The expansion is intentionally small and raster based.  This handles
    concave workpiece outlines more safely than moving vertices away from a
    centroid and keeps the correction visually consistent in every direction.
    """

    if len(points) < 3 or padding <= 0:
        return points
    source_rect = fitz.Rect(
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )
    margin = padding + 2.0
    crop = fitz.Rect(
        source_rect.x0 - margin,
        source_rect.y0 - margin,
        source_rect.x1 + margin,
        source_rect.y1 + margin,
    ) & page_rect
    if crop.is_empty:
        return points
    width = max(3, int(math.ceil(crop.width * raster_scale)) + 1)
    height = max(3, int(math.ceil(crop.height * raster_scale)) + 1)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).polygon(
        [
            (
                (point[0] - crop.x0) * raster_scale,
                (point[1] - crop.y0) * raster_scale,
            )
            for point in points
        ],
        fill=255,
    )
    radius = max(1, int(math.ceil(padding * raster_scale)))
    expanded = mask.filter(ImageFilter.MaxFilter(radius * 2 + 1))
    boundary = _largest_mask_boundary(expanded)
    if len(boundary) < 3:
        return points
    simplified = _simplify_closed_polygon(
        boundary,
        tolerance=max(1.0, raster_scale * 0.25),
    )
    corrected = tuple(
        (
            min(page_rect.x1, max(page_rect.x0, crop.x0 + x / raster_scale)),
            min(page_rect.y1, max(page_rect.y0, crop.y0 + y / raster_scale)),
        )
        for x, y in simplified
    )
    return corrected if len(corrected) >= 3 else points


def _nearest_ink_pixel(
    image: Image.Image,
    point: tuple[float, float],
    *,
    radius: int,
    threshold: int,
) -> tuple[int, int] | None:
    """Snap a user anchor to the nearest strong drawing line."""

    center_x = int(round(point[0]))
    center_y = int(round(point[1]))
    pixels = image.load()
    best: tuple[float, int, int] | None = None
    for y in range(
        max(0, center_y - radius),
        min(image.height, center_y + radius + 1),
    ):
        for x in range(
            max(0, center_x - radius),
            min(image.width, center_x + radius + 1),
        ):
            distance = math.hypot(x - point[0], y - point[1])
            if distance > radius:
                continue
            value = pixels[x, y]
            if value >= threshold:
                continue
            # Prefer a solid black contour over a closer pale dimension line.
            score = distance + value / 255 * radius * 0.75
            candidate = (score, x, y)
            if best is None or candidate < best:
                best = candidate
    return (best[1], best[2]) if best is not None else None


def _point_segment_distance(
    point: tuple[int, int],
    start: tuple[int, int],
    end: tuple[int, int],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= 0:
        return math.dist(point, start)
    ratio = max(
        0.0,
        min(
            1.0,
            (
                (point[0] - start[0]) * dx
                + (point[1] - start[1]) * dy
            )
            / length_squared,
        ),
    )
    projection = (
        start[0] + ratio * dx,
        start[1] + ratio * dy,
    )
    return math.dist(point, projection)


def _trace_ink_segment(
    image: Image.Image,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    threshold: int,
    render_scale: float,
) -> list[tuple[int, int]]:
    """Trace the darkest reasonable route between two outline anchors."""

    direct_length = max(1.0, math.dist(start, end))
    margin = int(
        round(
            max(
                render_scale * 9,
                min(render_scale * 28, direct_length * 0.22),
            )
        )
    )
    x0 = max(0, min(start[0], end[0]) - margin)
    y0 = max(0, min(start[1], end[1]) - margin)
    x1 = min(image.width - 1, max(start[0], end[0]) + margin)
    y1 = min(image.height - 1, max(start[1], end[1]) + margin)
    width = x1 - x0 + 1
    height = y1 - y0 + 1
    if width <= 0 or height <= 0:
        return [start, end]
    local_start = (start[0] - x0, start[1] - y0)
    local_end = (end[0] - x0, end[1] - y0)
    crop = image.crop((x0, y0, x1 + 1, y1 + 1))
    pixels = crop.tobytes()

    def index(point: tuple[int, int]) -> int:
        return point[1] * width + point[0]

    start_index = index(local_start)
    end_index = index(local_end)
    distances = {start_index: 0.0}
    previous: dict[int, int] = {}
    queue: list[tuple[float, float, int]] = [
        (direct_length, 0.0, start_index)
    ]
    visited = 0
    maximum_visited = min(280_000, width * height)
    neighbor_steps = (
        (-1, -1, math.sqrt(2)),
        (0, -1, 1.0),
        (1, -1, math.sqrt(2)),
        (-1, 0, 1.0),
        (1, 0, 1.0),
        (-1, 1, math.sqrt(2)),
        (0, 1, 1.0),
        (1, 1, math.sqrt(2)),
    )
    while queue and visited < maximum_visited:
        _, current_distance, current_index = heapq.heappop(queue)
        if current_distance != distances.get(current_index):
            continue
        if current_index == end_index:
            break
        visited += 1
        current_y, current_x = divmod(current_index, width)
        for step_x, step_y, step_length in neighbor_steps:
            next_x = current_x + step_x
            next_y = current_y + step_y
            if not (0 <= next_x < width and 0 <= next_y < height):
                continue
            next_index = next_y * width + next_x
            value = pixels[next_index]
            darkness_cost = 1.0 + (value / 255) ** 3 * 30.0
            page_pixel = (next_x + x0, next_y + y0)
            corridor_cost = (
                _point_segment_distance(page_pixel, start, end)
                / max(1.0, margin)
            ) ** 2 * 2.0
            candidate_distance = current_distance + step_length * (
                darkness_cost + corridor_cost
            )
            if candidate_distance >= distances.get(next_index, math.inf):
                continue
            distances[next_index] = candidate_distance
            previous[next_index] = current_index
            heuristic = math.hypot(
                local_end[0] - next_x,
                local_end[1] - next_y,
            )
            heapq.heappush(
                queue,
                (
                    candidate_distance + heuristic,
                    candidate_distance,
                    next_index,
                ),
            )
    if end_index not in distances:
        return [start, end]
    path_indices = [end_index]
    while path_indices[-1] != start_index:
        predecessor = previous.get(path_indices[-1])
        if predecessor is None:
            return [start, end]
        path_indices.append(predecessor)
    path_indices.reverse()
    path = [
        (
            path_index % width + x0,
            path_index // width + y0,
        )
        for path_index in path_indices
    ]
    dark_pixels = sum(
        image.getpixel(point) < threshold
        for point in path
    )
    if (
        len(path) > direct_length * 2.8 + render_scale * 20
        or dark_pixels / max(1, len(path)) < 0.48
    ):
        return [start, end]
    return path


def predict_work_outline(
    page: fitz.Page,
    points: tuple[tuple[float, float], ...],
    *,
    render_scale: float = 2.2,
) -> tuple[tuple[float, float], ...]:
    """Predict one closed work outline from ordered contour anchor points."""

    if not 3 <= len(points) <= 32:
        raise ValueError("輪郭の角・変曲点を3～32点指定してください。")
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(render_scale, render_scale),
        colorspace=fitz.csGRAY,
        alpha=False,
        annots=False,
    )
    grayscale = Image.frombytes(
        "L",
        (pixmap.width, pixmap.height),
        pixmap.samples,
    )
    threshold = min(185, _otsu_threshold(grayscale))
    snap_radius = max(4, int(round(render_scale * 7)))
    snapped: list[tuple[int, int]] = []
    for point in points:
        pixel_point = (
            (point[0] - page.rect.x0) * render_scale,
            (point[1] - page.rect.y0) * render_scale,
        )
        nearest = _nearest_ink_pixel(
            grayscale,
            pixel_point,
            radius=snap_radius,
            threshold=threshold,
        )
        if nearest is None:
            raise ValueError(
                "輪郭線から離れた点があります。"
                "黒い外形線の角または線上をクリックしてください。"
            )
        snapped.append(nearest)
    traced: list[tuple[int, int]] = []
    for index, start in enumerate(snapped):
        end = snapped[(index + 1) % len(snapped)]
        segment = _trace_ink_segment(
            grayscale,
            start,
            end,
            threshold=threshold,
            render_scale=render_scale,
        )
        traced.extend(segment[:-1])
    traced = [
        (int(point[0]), int(point[1]))
        for point in _simplify_closed_polygon(
            [(float(x), float(y)) for x, y in traced],
            tolerance=max(1.4, render_scale * 0.8),
        )
    ]
    coarse_area = abs(
        _polygon_area(
            [(float(x), float(y)) for x, y in snapped]
        )
    )
    traced_area = abs(
        _polygon_area(
            [(float(x), float(y)) for x, y in traced]
        )
    )
    if (
        len(traced) < 3
        or coarse_area < render_scale * render_scale * 8
        or traced_area < coarse_area * 0.52
        or traced_area > coarse_area * 1.65
    ):
        traced = snapped
    predicted = tuple(
        (
            page.rect.x0 + x / render_scale,
            page.rect.y0 + y / render_scale,
        )
        for x, y in traced
    )
    if len(predicted) < 3:
        raise ValueError("指定点から閉じたワーク外形を予測できませんでした。")
    return predicted


def _distance_to_rect(point: fitz.Point, rect: fitz.Rect) -> float:
    dx = max(rect.x0 - point.x, 0.0, point.x - rect.x1)
    dy = max(rect.y0 - point.y, 0.0, point.y - rect.y1)
    return (dx * dx + dy * dy) ** 0.5


def _projection_interval(
    rect: fitz.Rect, axis: tuple[float, float]
) -> tuple[float, float]:
    values = [
        corner.x * axis[0] + corner.y * axis[1]
        for corner in (rect.top_left, rect.top_right, rect.bottom_left, rect.bottom_right)
    ]
    return min(values), max(values)


def _interval_gap(first: tuple[float, float], second: tuple[float, float]) -> float:
    return max(first[0] - second[1], second[0] - first[1], 0.0)


def _otsu_threshold(image: Image.Image) -> int:
    """Return a stable dark/bright split for a grayscale drawing crop."""

    histogram = image.histogram()
    total = image.width * image.height
    weighted_total = sum(index * count for index, count in enumerate(histogram))
    background_weight = 0
    background_sum = 0
    best_variance = -1.0
    best_threshold = 180
    for threshold, count in enumerate(histogram):
        background_weight += count
        if background_weight == 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight == 0:
            break
        background_sum += threshold * count
        background_mean = background_sum / background_weight
        foreground_mean = (
            weighted_total - background_sum
        ) / foreground_weight
        variance = (
            background_weight
            * foreground_weight
            * (background_mean - foreground_mean) ** 2
        )
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return max(95, min(225, best_threshold + 16))


def _connected_ink_components(
    image: Image.Image,
    threshold: int,
) -> list[_InkComponent]:
    """Extract 8-connected dark components without an OpenCV dependency."""

    width, height = image.size
    pixels = image.tobytes()
    ink = bytearray(value < threshold for value in pixels)
    visited = bytearray(width * height)
    components: list[_InkComponent] = []
    for seed_index, is_ink in enumerate(ink):
        if not is_ink or visited[seed_index]:
            continue
        visited[seed_index] = 1
        stack = [seed_index]
        area = 0
        x0 = width
        y0 = height
        x1 = 0
        y1 = 0
        sum_x = 0.0
        sum_y = 0.0
        sum_xx = 0.0
        sum_yy = 0.0
        sum_xy = 0.0
        while stack:
            index = stack.pop()
            y, x = divmod(index, width)
            area += 1
            x0 = min(x0, x)
            y0 = min(y0, y)
            x1 = max(x1, x + 1)
            y1 = max(y1, y + 1)
            sum_x += x
            sum_y += y
            sum_xx += x * x
            sum_yy += y * y
            sum_xy += x * y
            for neighbor_y in range(max(0, y - 1), min(height, y + 2)):
                row = neighbor_y * width
                for neighbor_x in range(max(0, x - 1), min(width, x + 2)):
                    neighbor = row + neighbor_x
                    if ink[neighbor] and not visited[neighbor]:
                        visited[neighbor] = 1
                        stack.append(neighbor)
        if area < 2:
            continue
        center_x = sum_x / area
        center_y = sum_y / area
        variance_x = max(0.0, sum_xx / area - center_x * center_x)
        variance_y = max(0.0, sum_yy / area - center_y * center_y)
        covariance = sum_xy / area - center_x * center_y
        trace = variance_x + variance_y
        discriminant = math.sqrt(
            max(
                0.0,
                (variance_x - variance_y) ** 2
                + 4 * covariance * covariance,
            )
        )
        major_variance = max(0.0, (trace + discriminant) / 2)
        minor_variance = max(0.0, (trace - discriminant) / 2)
        components.append(
            _InkComponent(
                bbox=(x0, y0, x1, y1),
                area=area,
                center=(center_x, center_y),
                major_span=4 * math.sqrt(major_variance),
                elongation=(major_variance + 1.0) / (minor_variance + 1.0),
            )
        )
    return components


def _component_distance(
    point: tuple[float, float],
    component: _InkComponent,
) -> float:
    x0, y0, x1, y1 = component.bbox
    dx = max(x0 - point[0], 0.0, point[0] - x1)
    dy = max(y0 - point[1], 0.0, point[1] - y1)
    return math.hypot(dx, dy)


def _component_projection(
    component: _InkComponent,
    axis: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    normal = (-axis[1], axis[0])
    x0, y0, x1, y1 = component.bbox
    corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    along = tuple(x * axis[0] + y * axis[1] for x, y in corners)
    across = tuple(x * normal[0] + y * normal[1] for x, y in corners)
    return (min(along), max(along)), (min(across), max(across))


def detect_visual_text_group(
    page: fitz.Page,
    point: fitz.Point,
    *,
    render_scale: float = 3.2,
) -> TextHit:
    """Detect a likely text / symbol group near a click on a visual-only PDF.

    This intentionally recognizes geometry rather than characters. It works
    for both scanned pages and CAD PDFs whose text has been converted to
    outlines, while keeping OCR and external runtime dependencies optional.
    """

    clip = fitz.Rect(
        point.x - 110,
        point.y - 72,
        point.x + 110,
        point.y + 72,
    ) & page.rect
    if clip.is_empty:
        raise ValueError("クリック位置の周辺を解析できませんでした。")
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(render_scale, render_scale),
        clip=clip,
        colorspace=fitz.csGRAY,
        alpha=False,
        annots=False,
    )
    grayscale = Image.frombytes(
        "L",
        (pixmap.width, pixmap.height),
        pixmap.samples,
    )
    components = _connected_ink_components(
        grayscale,
        _otsu_threshold(grayscale),
    )
    maximum_component_span = render_scale * 26
    usable = [
        component
        for component in components
        if component.area >= 2
        and (
            component.major_span <= maximum_component_span
            or component.elongation < 18
        )
        and component.area < pixmap.width * pixmap.height * 0.035
    ]
    if not usable:
        raise ValueError(
            "クリック位置の近くに文字・記号の候補が見つかりませんでした。"
        )
    local_point = (
        (point.x - clip.x0) * render_scale,
        (point.y - clip.y0) * render_scale,
    )
    seed_candidates = [
        component
        for component in usable
        if _component_distance(local_point, component) <= render_scale * 7
    ]
    if not seed_candidates:
        raise ValueError(
            "文字・記号の線に近い位置をクリックしてください。"
        )
    seed = min(
        seed_candidates,
        key=lambda component: (
            _component_distance(local_point, component),
            component.area,
        ),
    )

    best_group: list[_InkComponent] | None = None
    best_axis = (1.0, 0.0)
    best_score = -math.inf
    for degrees in range(0, 180, 15):
        radians = math.radians(degrees)
        axis = (math.cos(radians), math.sin(radians))
        projections = {
            component: _component_projection(component, axis)
            for component in usable
        }
        group = [seed]
        remaining = set(usable)
        remaining.discard(seed)
        changed = True
        while changed:
            changed = False
            for candidate in tuple(remaining):
                candidate_along, candidate_across = projections[candidate]
                candidate_across_size = (
                    candidate_across[1] - candidate_across[0]
                )
                connected = False
                for selected in group:
                    selected_along, selected_across = projections[selected]
                    selected_across_size = (
                        selected_across[1] - selected_across[0]
                    )
                    along_limit = max(
                        render_scale * 5.5,
                        min(
                            render_scale * 20,
                            max(
                                selected_across_size,
                                candidate_across_size,
                            ) * 0.75,
                        ),
                    )
                    across_limit = max(
                        render_scale * 1.8,
                        min(
                            selected_across_size,
                            candidate_across_size,
                        ) * 0.45,
                    )
                    if (
                        _interval_gap(
                            selected_along,
                            candidate_along,
                        ) <= along_limit
                        and _interval_gap(
                            selected_across,
                            candidate_across,
                        ) <= across_limit
                        and abs(
                            sum(selected_across) / 2
                            - sum(candidate_across) / 2
                        ) <= render_scale * 10
                    ):
                        connected = True
                        break
                if connected:
                    group.append(candidate)
                    remaining.remove(candidate)
                    changed = True
        group_along = [
            value
            for component in group
            for value in projections[component][0]
        ]
        group_across = [
            value
            for component in group
            for value in projections[component][1]
        ]
        along_span = max(group_along) - min(group_along)
        across_span = max(group_across) - min(group_across)
        if (
            along_span > render_scale * 240
            or across_span > render_scale * 55
        ):
            continue
        ratio = along_span / max(across_span, render_scale)
        density = sum(component.area for component in group) / max(
            1.0,
            along_span * across_span,
        )
        score = (
            len(group) * 2.3
            + min(12.0, ratio) * 1.4
            + min(80.0, along_span / render_scale) * 0.05
            - max(0.0, across_span / render_scale - 18) * 0.5
            - abs(density - 0.22) * 2
        )
        if score > best_score:
            best_score = score
            best_group = group
            best_axis = axis
    if not best_group:
        raise ValueError(
            "文字・記号のまとまりを判定できませんでした。"
        )

    # Two-character dimensions such as "R3" are common. Their component
    # centers are already enough to refine the coarse 15-degree search, while
    # the alignment guard below prevents an unrelated pair from rotating the
    # result away from the selected baseline.
    if len(best_group) >= 2:
        center_x = sum(component.center[0] for component in best_group) / len(
            best_group
        )
        center_y = sum(component.center[1] for component in best_group) / len(
            best_group
        )
        variance_x = sum(
            (component.center[0] - center_x) ** 2
            for component in best_group
        )
        variance_y = sum(
            (component.center[1] - center_y) ** 2
            for component in best_group
        )
        covariance = sum(
            (component.center[0] - center_x)
            * (component.center[1] - center_y)
            for component in best_group
        )
        refined_angle = 0.5 * math.atan2(
            2 * covariance,
            variance_x - variance_y,
        )
        refined_axis = (
            math.cos(refined_angle),
            math.sin(refined_angle),
        )
        alignment = abs(
            refined_axis[0] * best_axis[0]
            + refined_axis[1] * best_axis[1]
        )
        if alignment >= math.cos(math.radians(20)):
            if (
                refined_axis[0] * best_axis[0]
                + refined_axis[1] * best_axis[1]
            ) < 0:
                refined_axis = (-refined_axis[0], -refined_axis[1])
            best_axis = refined_axis

    best_projections = [
        _component_projection(component, best_axis)
        for component in best_group
    ]
    along_min = min(value[0][0] for value in best_projections)
    along_max = max(value[0][1] for value in best_projections)
    across_min = min(value[1][0] for value in best_projections)
    across_max = max(value[1][1] for value in best_projections)
    along_padding = render_scale * 1.5
    across_padding = render_scale * 1.15
    along_min -= along_padding
    along_max += along_padding
    across_min -= across_padding
    across_max += across_padding
    normal = (-best_axis[1], best_axis[0])

    def page_point(along: float, across: float) -> tuple[float, float]:
        pixel_x = best_axis[0] * along + normal[0] * across
        pixel_y = best_axis[1] * along + normal[1] * across
        return (
            clip.x0 + pixel_x / render_scale,
            clip.y0 + pixel_y / render_scale,
        )

    quad = (
        page_point(along_min, across_min),
        page_point(along_max, across_min),
        page_point(along_max, across_max),
        page_point(along_min, across_max),
    )
    rect = fitz.Rect(quad[0], quad[0])
    for quad_point in quad[1:]:
        rect.include_point(fitz.Point(quad_point))
    rect &= page.rect
    if rect.is_empty or rect.get_area() < 4:
        raise ValueError("候補範囲が小さすぎます。")
    return TextHit(
        rect=(rect.x0, rect.y0, rect.x1, rect.y1),
        text="",
        direction=best_axis,
        font_size=(across_max - across_min) / render_scale,
        quad=quad,
    )


def _raw_text_lines(page: fitz.Page) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            rect = fitz.Rect(line.get("bbox"))
            if rect.is_empty or rect.get_area() <= 0:
                continue
            text = "".join(
                character.get("c", "")
                for span in line.get("spans", [])
                for character in span.get("chars", [])
            )
            visible_sizes = [
                float(span.get("size", 0.0))
                for span in line.get("spans", [])
                if any(
                    character.get("c", "").strip()
                    for character in span.get("chars", [])
                )
            ]
            raw_size = max(
                (
                    float(span.get("size", 0.0))
                    for span in line.get("spans", [])
                ),
                default=0.0,
            )
            visible_spans = [
                span
                for span in line.get("spans", [])
                if any(
                    character.get("c", "").strip()
                    for character in span.get("chars", [])
                )
            ]
            primary_span = (
                max(
                    visible_spans,
                    key=lambda span: float(span.get("size", 0.0)),
                )
                if visible_spans
                else {}
            )
            origin_value = primary_span.get(
                "origin",
                (rect.x0, rect.y1),
            )
            color_value = fitz.sRGB_to_rgb(
                int(primary_span.get("color", 0))
            )
            direction = tuple(float(value) for value in line.get("dir", (1.0, 0.0)))
            length = math.hypot(direction[0], direction[1]) or 1.0
            try:
                recovered = fitz.recover_line_quad(line)
                quad = tuple(
                    (float(point.x), float(point.y))
                    for point in (
                        recovered.ul,
                        recovered.ur,
                        recovered.lr,
                        recovered.ll,
                    )
                )
            except (TypeError, ValueError):
                quad = tuple(
                    (float(point.x), float(point.y))
                    for point in (
                        rect.top_left,
                        rect.top_right,
                        rect.bottom_right,
                        rect.bottom_left,
                    )
                )
            lines.append(
                {
                    "rect": rect,
                    "text": text,
                    "size": max(visible_sizes, default=0.0),
                    "raw_size": raw_size,
                    "direction": (direction[0] / length, direction[1] / length),
                    "quad": quad,
                    "origin": (
                        float(origin_value[0]),
                        float(origin_value[1]),
                    ),
                    "font_name": str(primary_span.get("font", "")),
                    "font_color": tuple(
                        float(channel) / 255.0
                        for channel in color_value
                    ),
                }
            )
    return lines


def find_text_group(
    page: fitz.Page,
    point: fitz.Point,
    *,
    padding: float = 1.5,
    hit_slop: float = 5.0,
    along_gap: float = 10.0,
    normal_gap: float = 1.75,
    text_lines: list[dict[str, object]] | None = None,
    include_stacked_tolerance: bool = False,
) -> TextHit | None:
    """Select one dimension group, including prefixes and tolerance symbols.

    CAD PDFs often split ``φ``, ``+`` and ``-`` into separate text lines. Some
    diameter glyphs are even mapped to a blank character. Nearby collinear
    lines are therefore merged around the clicked text.
    """

    # Batch operations may resolve dozens of dimensions on the same page.
    # Reusing the parsed lines avoids rebuilding every recovered text quad for
    # each dimension while preserving the single-click API's old behaviour.
    lines = text_lines if text_lines is not None else _raw_text_lines(page)
    candidates: list[tuple[float, float, int]] = []
    for index, line in enumerate(lines):
        text = str(line["text"])
        if not text.strip():
            continue
        rect = line["rect"]
        assert isinstance(rect, fitz.Rect)
        distance = _distance_to_rect(point, rect)
        if distance <= hit_slop:
            candidates.append((distance, rect.get_area(), index))
    if not candidates:
        return None

    _, _, selected_index = min(candidates, key=lambda item: (item[0], item[1]))

    # If the pointer landed on a small tolerance character, first resolve it
    # back to the nearest nominal-size value. Normal-axis distance is weighted
    # heavily so a tolerance above / below an adjacent dimension is not stolen.
    selected_size = float(lines[selected_index]["size"])
    larger_anchors: list[tuple[float, float, int]] = []
    selected_rect_for_anchor = lines[selected_index]["rect"]
    selected_direction_for_anchor = lines[selected_index]["direction"]
    assert isinstance(selected_rect_for_anchor, fitz.Rect)
    assert isinstance(selected_direction_for_anchor, tuple)
    selected_normal_for_anchor = (
        -selected_direction_for_anchor[1],
        selected_direction_for_anchor[0],
    )
    for index, candidate in enumerate(lines):
        if float(candidate["size"]) <= selected_size * 1.12:
            continue
        candidate_direction = candidate["direction"]
        assert isinstance(candidate_direction, tuple)
        if (
            abs(
                candidate_direction[0] * selected_direction_for_anchor[0]
                + candidate_direction[1] * selected_direction_for_anchor[1]
            )
            < 0.985
        ):
            continue
        candidate_rect = candidate["rect"]
        assert isinstance(candidate_rect, fitz.Rect)
        along = _interval_gap(
            _projection_interval(candidate_rect, selected_direction_for_anchor),
            _projection_interval(
                selected_rect_for_anchor, selected_direction_for_anchor
            ),
        )
        across = _interval_gap(
            _projection_interval(candidate_rect, selected_normal_for_anchor),
            _projection_interval(
                selected_rect_for_anchor, selected_normal_for_anchor
            ),
        )
        if along <= along_gap + 2.0 and across <= normal_gap + 3.0:
            larger_anchors.append((across * 3.0 + along, along, index))
    if larger_anchors:
        selected_index = min(larger_anchors)[2]

    selected = lines[selected_index]
    direction = selected["direction"]
    assert isinstance(direction, tuple)
    normal = (-direction[1], direction[0])
    group_indexes = {selected_index}
    selected_rect = selected["rect"]
    assert isinstance(selected_rect, fitz.Rect)
    selected_along = _projection_interval(selected_rect, direction)
    selected_normal = _projection_interval(selected_rect, normal)
    selected_size = float(selected["size"])
    selected_midpoint = (selected_along[0] + selected_along[1]) / 2

    # Merge only lines directly adjacent to the clicked line. Deliberately do
    # not grow the group transitively: a chain of tolerances and nearby values
    # can otherwise bridge three independent dimensions into one selection.
    for index, candidate in enumerate(lines):
        if index == selected_index:
            continue
        candidate_direction = candidate["direction"]
        assert isinstance(candidate_direction, tuple)
        if (
            abs(
                candidate_direction[0] * direction[0]
                + candidate_direction[1] * direction[1]
            )
            < 0.985
        ):
            continue
        candidate_rect = candidate["rect"]
        assert isinstance(candidate_rect, fitz.Rect)
        candidate_text = str(candidate["text"]).strip()
        candidate_size = float(candidate["size"])
        candidate_along = _projection_interval(candidate_rect, direction)
        is_blank_or_prefix = not candidate_text or candidate_text in {
            "φ",
            "Φ",
            "Ø",
            "⌀",
            "R",
            "C",
            "M",
            "S",
            "□",
        }
        # Other nominal-size values are independent dimensions. Tolerance
        # fragments are smaller and follow the value along the text direction.
        if (
            not is_blank_or_prefix
            and (
                candidate_size >= selected_size * 0.92
                or (candidate_along[0] + candidate_along[1]) / 2
                < selected_midpoint
            )
        ):
            continue
        if (
            _interval_gap(
                candidate_along, selected_along
            )
            <= along_gap
            and _interval_gap(
                _projection_interval(candidate_rect, normal), selected_normal
            )
            <= normal_gap
        ):
            group_indexes.add(index)

    if include_stacked_tolerance:
        # CAD の片側・上下公差は、公称値と同じ行ではなく、末尾の上下へ
        # 小さい文字で分かれて配置されることがある。編集時の通常選択には
        # 影響させず、寸法検出だけが明示指定した場合に限って結合する。
        signed_indexes: list[int] = []
        tolerance_pattern = re.compile(
            r"^[+\-−－±](?:0(?:[.,]\d+)?|[.,]\d+)$"
        )
        zero_pattern = re.compile(r"^0(?:[.,]0+)?$")
        for index, candidate in enumerate(lines):
            if index in group_indexes:
                continue
            candidate_direction = candidate["direction"]
            assert isinstance(candidate_direction, tuple)
            if abs(
                candidate_direction[0] * direction[0]
                + candidate_direction[1] * direction[1]
            ) < 0.985:
                continue
            candidate_text = unicodedata.normalize(
                "NFKC", str(candidate["text"])
            ).replace(" ", "")
            if not tolerance_pattern.fullmatch(candidate_text):
                continue
            candidate_size = float(candidate["size"])
            if candidate_size > selected_size * 0.82:
                continue
            candidate_rect = candidate["rect"]
            assert isinstance(candidate_rect, fitz.Rect)
            candidate_along = _projection_interval(candidate_rect, direction)
            candidate_normal = _projection_interval(candidate_rect, normal)
            if (
                candidate_along[1] < selected_midpoint - selected_size * 0.25
                or candidate_along[0] > selected_along[1] + selected_size * 1.9
                or _interval_gap(candidate_normal, selected_normal)
                > selected_size * 2.8 + 3.0
            ):
                continue
            group_indexes.add(index)
            signed_indexes.append(index)

        # 下側の 0 は、それ単体では通常の別寸法と区別できない。上側または
        # 下側の符号付き公差を同じ位置に確認できたときだけ追加する。
        for index, candidate in enumerate(lines):
            if not signed_indexes or index in group_indexes:
                continue
            candidate_text = unicodedata.normalize(
                "NFKC", str(candidate["text"])
            ).replace(" ", "")
            if not zero_pattern.fullmatch(candidate_text):
                continue
            candidate_direction = candidate["direction"]
            assert isinstance(candidate_direction, tuple)
            if abs(
                candidate_direction[0] * direction[0]
                + candidate_direction[1] * direction[1]
            ) < 0.985 or float(candidate["size"]) > selected_size * 0.82:
                continue
            candidate_rect = candidate["rect"]
            assert isinstance(candidate_rect, fitz.Rect)
            candidate_along = _projection_interval(candidate_rect, direction)
            candidate_normal = _projection_interval(candidate_rect, normal)
            for signed_index in signed_indexes:
                signed_rect = lines[signed_index]["rect"]
                assert isinstance(signed_rect, fitz.Rect)
                signed_along = _projection_interval(signed_rect, direction)
                signed_normal = _projection_interval(signed_rect, normal)
                if (
                    _interval_gap(candidate_along, signed_along)
                    <= selected_size * 1.35 + 2.0
                    and _interval_gap(candidate_normal, signed_normal)
                    <= selected_size * 2.6 + 3.0
                ):
                    group_indexes.add(index)
                    break

    group_rect = fitz.Rect(lines[selected_index]["rect"])
    visible_group_rect = fitz.Rect(lines[selected_index]["rect"])
    group_text: list[str] = []
    for index in sorted(group_indexes):
        line = lines[index]
        group_rect |= line["rect"]
        text = str(line["text"]).strip()
        if text:
            group_text.append(text)
            visible_group_rect |= line["rect"]

    padded = fitz.Rect(
        max(page.rect.x0, group_rect.x0 - padding),
        max(page.rect.y0, group_rect.y0 - padding),
        min(page.rect.x1, group_rect.x1 + padding),
        min(page.rect.y1, group_rect.y1 + padding),
    )
    replacement_padded = fitz.Rect(
        max(page.rect.x0, visible_group_rect.x0 - padding),
        max(page.rect.y0, visible_group_rect.y0 - padding),
        min(page.rect.x1, visible_group_rect.x1 + padding),
        min(page.rect.y1, visible_group_rect.y1 + padding),
    )
    preserved_prefix = ""
    for index in group_indexes:
        line = lines[index]
        if str(line["text"]).strip():
            continue
        if float(line["raw_size"]) < selected_size * 0.8:
            continue
        blank_along = _projection_interval(line["rect"], direction)
        if blank_along[1] < selected_along[0]:
            preserved_prefix = "φ"
            break
    quad_points = [
        fitz.Point(point)
        for index in group_indexes
        for point in lines[index]["quad"]
    ]
    along_values = [
        point.x * direction[0] + point.y * direction[1]
        for point in quad_points
    ]
    normal_values = [
        point.x * normal[0] + point.y * normal[1]
        for point in quad_points
    ]
    along_min = min(along_values) - padding
    along_max = max(along_values) + padding
    normal_min = min(normal_values) - padding
    normal_max = max(normal_values) + padding

    def oriented_point(along: float, across: float) -> tuple[float, float]:
        return (
            direction[0] * along + normal[0] * across,
            direction[1] * along + normal[1] * across,
        )

    oriented_quad = (
        oriented_point(along_min, normal_min),
        oriented_point(along_max, normal_min),
        oriented_point(along_max, normal_max),
        oriented_point(along_min, normal_max),
    )
    return TextHit(
        rect=(padded.x0, padded.y0, padded.x1, padded.y1),
        text=" ".join(group_text),
        direction=(float(direction[0]), float(direction[1])),
        font_size=selected_size,
        quad=oriented_quad,
        origin=selected["origin"],
        nominal_text=str(selected["text"]).strip(),
        font_name=str(selected["font_name"]),
        font_color=selected["font_color"],
        replacement_rect=(
            replacement_padded.x0,
            replacement_padded.y0,
            replacement_padded.x1,
            replacement_padded.y1,
        ),
        preserved_prefix=preserved_prefix,
    )


def find_word_rect(
    page: fitz.Page,
    point: fitz.Point,
    *,
    padding: float = 1.5,
    hit_slop: float = 5.0,
) -> tuple[fitz.Rect, str] | None:
    """Backward-compatible wrapper returning the improved text group."""

    hit = find_text_group(
        page, point, padding=padding, hit_slop=hit_slop
    )
    if hit is None:
        return None
    return fitz.Rect(hit.rect), hit.text


def strike_from_hit(page_index: int, hit: TextHit) -> StrikeMark:
    rect = fitz.Rect(hit.rect)
    direction = fitz.Point(hit.direction)
    normal = fitz.Point(-direction.y, direction.x)
    along = _projection_interval(rect, (direction.x, direction.y))
    across = _projection_interval(rect, (normal.x, normal.y))
    center_across = (across[0] + across[1]) / 2
    start = direction * along[0] + normal * center_across
    end = direction * along[1] + normal * center_across
    return StrikeMark(
        page_index=page_index,
        start=(start.x, start.y),
        end=(end.x, end.y),
        normal=(normal.x, normal.y),
        gap=max(1.0, min(2.2, (across[1] - across[0]) * 0.14)),
    )


_DIMENSION_TEXT_PATTERN = re.compile(
    r"^[\s0-9０-９A-Za-zφΦØ∅⌀RrCcM＋+\-−±°′″.,()（）/以下最大最小]+$"
)


def _looks_like_dimension_text(text: str) -> bool:
    compact = "".join(text.split())
    return (
        0 < len(compact) <= 24
        and any(character.isdigit() for character in compact)
        and bool(_DIMENSION_TEXT_PATTERN.fullmatch(compact))
    )


def _dimension_text_spans(
    page: fitz.Page,
) -> list[dict[str, object]]:
    spans: list[dict[str, object]] = []
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = "".join(
                    character.get("c", "")
                    for character in span.get("chars", [])
                ).strip()
                size = float(span.get("size", 0.0))
                if not _looks_like_dimension_text(text) or size <= 0:
                    continue
                color = fitz.sRGB_to_rgb(
                    int(span.get("color", 0))
                )
                spans.append(
                    {
                        "text": text,
                        "rect": fitz.Rect(span["bbox"]),
                        "font_size": size,
                        "font_name": str(span.get("font", "")),
                        "font_color": tuple(
                            float(channel) / 255.0
                            for channel in color
                        ),
                    }
                )
    return spans


def _drawing_line_width(page: fitz.Page) -> float:
    counts: dict[float, int] = {}
    for drawing in page.get_drawings():
        width = float(drawing.get("width") or 0.0)
        if not 0.08 <= width <= 0.75:
            continue
        rounded = round(width, 2)
        counts[rounded] = counts.get(rounded, 0) + 1
    if not counts:
        return 0.35
    return max(counts, key=lambda width: (counts[width], -width))


def infer_dimension_style(
    page: fitz.Page,
    target: tuple[float, float] | None = None,
    label: tuple[float, float] | None = None,
) -> DimensionStyle:
    """Infer the typical dimension text and thin-line style on a page."""

    spans = _dimension_text_spans(page)
    selected: dict[str, object] | None = None
    if spans and target is not None:
        font_counts: dict[str, int] = {}
        for span in spans:
            font_name = str(span["font_name"])
            font_counts[font_name] = font_counts.get(font_name, 0) + 1
        maximum_font_count = max(font_counts.values())
        minimum_font_count = (
            1
            if maximum_font_count < 4
            else max(2, math.ceil(maximum_font_count * 0.08))
        )
        nearby_pool = [
            span
            for span in spans
            if font_counts[str(span["font_name"])] >= minimum_font_count
        ]
        target_point = fitz.Point(target)
        label_point = fitz.Point(label or target)
        selected = min(
            nearby_pool or spans,
            key=lambda span: (
                _distance_to_rect(target_point, span["rect"]) * 0.72
                + _distance_to_rect(label_point, span["rect"]) * 0.28
            ),
        )
        nearest_distance = min(
            _distance_to_rect(target_point, selected["rect"]),
            _distance_to_rect(label_point, selected["rect"]),
        )
        if nearest_distance > max(page.rect.width, page.rect.height) * 0.28:
            selected = None
    if spans and selected is None:
        style_counts: dict[tuple[str, float], int] = {}
        for span in spans:
            key = (
                str(span["font_name"]),
                round(float(span["font_size"]) * 2) / 2,
            )
            style_counts[key] = style_counts.get(key, 0) + 1
        preferred = max(
            style_counts,
            key=lambda key: (
                style_counts[key],
                "gothic" in key[0].lower(),
                key[1],
            ),
        )
        selected = min(
            (
                span
                for span in spans
                if str(span["font_name"]) == preferred[0]
                and abs(float(span["font_size"]) - preferred[1]) <= 0.3
            ),
            key=lambda span: abs(float(span["font_size"]) - preferred[1]),
        )
    if selected is None:
        return DimensionStyle(
            font_size=max(6.5, min(10.5, page.rect.width / 90.0)),
            font_name="MS-PGothic",
            font_color=(0.0, 0.0, 0.0),
            line_width=_drawing_line_width(page),
        )
    return DimensionStyle(
        font_size=max(5.0, min(18.0, float(selected["font_size"]))),
        font_name=str(selected["font_name"]),
        font_color=selected["font_color"],
        line_width=_drawing_line_width(page),
    )


def _segment_intersects_rect(
    start: fitz.Point,
    end: fitz.Point,
    rect: fitz.Rect,
) -> bool:
    """Return whether a finite line segment crosses a rectangle."""

    dx = end.x - start.x
    dy = end.y - start.y
    lower = 0.0
    upper = 1.0
    for origin, delta, minimum, maximum in (
        (start.x, dx, rect.x0, rect.x1),
        (start.y, dy, rect.y0, rect.y1),
    ):
        if abs(delta) < 1e-9:
            if origin < minimum or origin > maximum:
                return False
            continue
        first = (minimum - origin) / delta
        second = (maximum - origin) / delta
        if first > second:
            first, second = second, first
        lower = max(lower, first)
        upper = min(upper, second)
        if lower > upper:
            return False
    return True


def _dimension_occupied_rects(
    page: fitz.Page,
    existing_items: Iterable[DrawingItem],
) -> list[fitz.Rect]:
    occupied = [
        fitz.Rect(span["rect"]) + (-2.0, -2.0, 2.0, 2.0)
        for span in _dimension_text_spans(page)
    ]
    for item in existing_items:
        if item.page_index != page.number:
            continue
        if isinstance(item, DimensionMark):
            occupied.append(
                dimension_label_rect(item) + (-3.0, -3.0, 3.0, 3.0)
            )
        elif isinstance(item, ReplacementMark):
            source_rect = fitz.Rect(item.rect)
            occupied.append(source_rect + (-3.0, -3.0, 3.0, 3.0))
            for offset in (item.value_offset, item.tolerance_offset):
                moved_rect = source_rect + (
                    offset[0],
                    offset[1],
                    offset[0],
                    offset[1],
                )
                padding = max(
                    item.font_size,
                    item.tolerance_font_size or item.font_size * 0.8,
                )
                occupied.append(
                    moved_rect + (-padding, -padding, padding, padding)
                )
    return occupied


def _rendered_ink_density(
    image: Image.Image,
    page_rect: fitz.Rect,
    rect: fitz.Rect,
    scale: float,
) -> float:
    x0 = max(
        0,
        int(math.floor((rect.x0 - page_rect.x0) * scale)),
    )
    y0 = max(
        0,
        int(math.floor((rect.y0 - page_rect.y0) * scale)),
    )
    x1 = min(
        image.width,
        int(math.ceil((rect.x1 - page_rect.x0) * scale)),
    )
    y1 = min(
        image.height,
        int(math.ceil((rect.y1 - page_rect.y0) * scale)),
    )
    if x1 <= x0 or y1 <= y0:
        return 1.0
    crop = image.crop((x0, y0, x1, y1))
    histogram = crop.histogram()
    dark = sum(histogram[:176])
    return dark / max(1, crop.width * crop.height)


def avoid_dimension_overlap(
    page: fitz.Page,
    mark: DimensionMark,
    existing_items: Iterable[DrawingItem] = (),
) -> DimensionMark:
    """Move a new label to the nearest clear area when text would overlap."""

    preferred = fitz.Point(mark.label)
    preferred_rect = dimension_label_rect(mark)
    step_x = max(16.0, preferred_rect.width * 0.58)
    step_y = max(14.0, preferred_rect.height + 6.0)
    offsets: list[tuple[float, float]] = [(0.0, 0.0)]
    for ring in range(1, 6):
        offsets.extend(
            (
                (0.0, -step_y * ring),
                (0.0, step_y * ring),
                (-step_x * ring, 0.0),
                (step_x * ring, 0.0),
                (-step_x * ring, -step_y * ring),
                (step_x * ring, -step_y * ring),
                (-step_x * ring, step_y * ring),
                (step_x * ring, step_y * ring),
            )
        )
    occupied = _dimension_occupied_rects(page, existing_items)
    raster_scale = 1.5
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(raster_scale, raster_scale),
        colorspace=fitz.csGRAY,
        alpha=False,
        annots=False,
    )
    grayscale = Image.frombytes(
        "L",
        (pixmap.width, pixmap.height),
        pixmap.samples,
    )
    target = fitz.Point(mark.target)
    page_margin = 3.0
    best_mark = mark
    best_score = math.inf
    for offset_x, offset_y in offsets:
        candidate_label = (
            preferred.x + offset_x,
            preferred.y + offset_y,
        )
        candidate = replace(mark, label=candidate_label)
        label_rect = dimension_label_rect(candidate)
        if (
            label_rect.x0 < page.rect.x0 + page_margin
            or label_rect.y0 < page.rect.y0 + page_margin
            or label_rect.x1 > page.rect.x1 - page_margin
            or label_rect.y1 > page.rect.y1 - page_margin
        ):
            continue
        anchor = _leader_anchor(target, label_rect)
        label_collisions = sum(
            label_rect.intersects(rect)
            for rect in occupied
        )
        leader_collisions = sum(
            not rect.contains(target)
            and _segment_intersects_rect(target, anchor, rect)
            for rect in occupied
        )
        ink_density = _rendered_ink_density(
            grayscale,
            page.rect,
            label_rect + (-2.0, -2.0, 2.0, 2.0),
            raster_scale,
        )
        distance = math.hypot(offset_x, offset_y)
        score = (
            distance
            + label_collisions * 10_000
            + leader_collisions * 1_500
            + ink_density * 8_000
        )
        if score < best_score:
            best_score = score
            best_mark = candidate
        if (
            label_collisions == 0
            and leader_collisions == 0
            and ink_density < 0.012
        ):
            return candidate
    return best_mark


def find_japanese_font() -> Path:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windows / "Fonts" / "meiryo.ttc",
        windows / "Fonts" / "YuGothM.ttc",
        windows / "Fonts" / "msgothic.ttc",
        windows / "Fonts" / "msmincho.ttc",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("A Japanese Windows font could not be found.")


@lru_cache(maxsize=1)
def _pdf_font() -> fitz.Font:
    return fitz.Font(fontfile=str(find_japanese_font()))


@lru_cache(maxsize=24)
def _dimension_font_file(font_name: str) -> Path:
    windows_fonts = Path(
        os.environ.get("WINDIR", r"C:\Windows"),
        "Fonts",
    )
    normalized = font_name.lower().replace(" ", "").replace("-", "")
    if "mincho" in normalized or "明朝" in font_name:
        candidates = ("msmincho.ttc", "YuMincho.ttc")
    elif "gothic" in normalized or "ゴシック" in font_name:
        candidates = ("msgothic.ttc", "YuGothM.ttc", "meiryo.ttc")
    elif "meiryo" in normalized:
        candidates = ("meiryo.ttc", "msgothic.ttc")
    else:
        candidates = ("msgothic.ttc", "YuGothM.ttc", "meiryo.ttc")
    for file_name in candidates:
        candidate = windows_fonts / file_name
        if candidate.is_file():
            return candidate
    return find_japanese_font()


def _dimension_font_index(font_name: str) -> int:
    """Return the matching face index inside common Windows TTC files."""

    normalized = font_name.lower().replace(" ", "").replace("-", "")
    font_file = _dimension_font_file(font_name).name.lower()
    if font_file == "msgothic.ttc":
        if "pgothic" in normalized:
            return 2
        if "uigothic" in normalized:
            return 1
    if font_file == "msmincho.ttc" and "pmincho" in normalized:
        return 1
    if font_file == "meiryo.ttc" and "meiryoui" in normalized:
        return 2
    return 0


@lru_cache(maxsize=64)
def _dimension_pillow_font(
    font_name: str,
    pixel_size: int,
) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        str(_dimension_font_file(font_name)),
        max(1, pixel_size),
        index=_dimension_font_index(font_name),
    )


def dimension_label_rect(mark: DimensionMark) -> fitz.Rect:
    measurement_scale = 4.0
    font = _dimension_pillow_font(
        mark.font_name,
        round(mark.font_size * measurement_scale),
    )
    width = font.getlength(mark.text) / measurement_scale
    return fitz.Rect(
        mark.label[0],
        mark.label[1],
        mark.label[0] + width + 5.0,
        mark.label[1] + mark.font_size * 1.55,
    )


def replacement_content_rect(mark: ReplacementMark) -> fitz.Rect:
    """Return the visible replacement text bounds for direct manipulation."""

    if mark.origin is None:
        return fitz.Rect(mark.rect)
    direction = fitz.Point(mark.direction)
    length = math.hypot(direction.x, direction.y) or 1.0
    direction /= length
    normal = fitz.Point(-direction.y, direction.x)
    font = _pdf_font()
    nominal_size = max(5.0, mark.font_size)
    tolerance_size = max(
        4.0,
        mark.tolerance_font_size
        if mark.tolerance_font_size is not None
        else nominal_size * 0.8,
    )
    base = fitz.Point(mark.origin)
    nominal_origin = base + fitz.Point(mark.value_offset)
    entries: list[tuple[fitz.Point, str, float]] = [
        (nominal_origin, mark.value, nominal_size)
    ]
    if mark.upper_tolerance or mark.lower_tolerance:
        tolerance_origin = (
            base
            + direction
            * (font.text_length(mark.value, fontsize=nominal_size) + 0.5)
            + fitz.Point(mark.tolerance_offset)
        )
        if mark.upper_tolerance:
            entries.append(
                (
                    tolerance_origin - normal * tolerance_size,
                    mark.upper_tolerance,
                    tolerance_size,
                )
            )
        if mark.lower_tolerance:
            entries.append(
                (tolerance_origin, mark.lower_tolerance, tolerance_size)
            )
    points: list[fitz.Point] = []
    for origin, text, size in entries:
        width = max(size * 0.5, font.text_length(text, fontsize=size))
        points.extend(
            origin + direction * x + normal * y
            for x, y in (
                (-2.0, -size * 1.15),
                (width + 2.0, -size * 1.15),
                (width + 2.0, size * 0.35),
                (-2.0, size * 0.35),
            )
        )
    return fitz.Rect(
        min(point.x for point in points),
        min(point.y for point in points),
        max(point.x for point in points),
        max(point.y for point in points),
    )
def _leader_anchor(target: fitz.Point, label_rect: fitz.Rect) -> fitz.Point:
    if target.x < label_rect.x0:
        return fitz.Point(label_rect.x0, label_rect.y0 + label_rect.height / 2)
    if target.x > label_rect.x1:
        return fitz.Point(label_rect.x1, label_rect.y0 + label_rect.height / 2)
    if target.y < label_rect.y0:
        return fitz.Point(label_rect.x0 + label_rect.width / 2, label_rect.y0)
    return fitz.Point(label_rect.x0 + label_rect.width / 2, label_rect.y1)


def _draw_dimension(page: fitz.Page, mark: DimensionMark, _font_file: Path) -> None:
    target = fitz.Point(mark.target)
    label_rect = dimension_label_rect(mark)
    anchor = _leader_anchor(target, label_rect)
    vector = anchor - target
    length = math.hypot(vector.x, vector.y)
    stroke_color = mark.font_color
    line_width = max(0.18, min(1.2, mark.line_width))
    if mark.show_leader and length > 0.1:
        unit = vector / length
        normal = fitz.Point(-unit.y, unit.x)
        arrow_length = max(3.2, mark.font_size * 0.48)
        arrow_width = max(1.2, mark.font_size * 0.16)
        base = target + unit * arrow_length
        page.draw_line(
            base,
            anchor,
            color=stroke_color,
            width=line_width,
            overlay=True,
        )
        page.draw_polyline(
            [target, base + normal * arrow_width, base - normal * arrow_width],
            color=stroke_color,
            fill=stroke_color,
            width=max(0.18, line_width),
            closePath=True,
            overlay=True,
        )

    # Clear any source line behind the label before applying the translucent
    # marker color. This keeps both the dimension value and its leader legible.
    page.draw_rect(
        label_rect,
        color=None,
        fill=(1, 1, 1),
        overlay=True,
    )
    if mark.opacity > 0:
        page.draw_rect(
            label_rect,
            color=None,
            fill=hex_to_rgb(mark.color),
            fill_opacity=min(1.0, mark.opacity),
            overlay=True,
        )
    # Render from the matching face inside Windows TTC collections. PyMuPDF
    # otherwise always selects face 0 (for example MS Gothic instead of
    # MS PGothic), which visibly changes CAD dimension spacing.
    text_scale = 4.0
    pixel_size = max(1, round(mark.font_size * text_scale))
    text_font = _dimension_pillow_font(mark.font_name, pixel_size)
    image_width = max(1, math.ceil(label_rect.width * text_scale))
    image_height = max(1, math.ceil(label_rect.height * text_scale))
    text_image = Image.new(
        "RGBA",
        (image_width, image_height),
        (255, 255, 255, 0),
    )
    text_draw = ImageDraw.Draw(text_image)
    text_bbox = text_draw.textbbox((0, 0), mark.text, font=text_font)
    text_y = (
        (image_height - (text_bbox[3] - text_bbox[1])) / 2
        - text_bbox[1]
    )
    text_draw.text(
        (2.0 * text_scale, text_y),
        mark.text,
        font=text_font,
        fill=tuple(
            round(max(0.0, min(1.0, component)) * 255)
            for component in stroke_color
        )
        + (255,),
    )
    text_stream = BytesIO()
    text_image.save(text_stream, format="PNG")
    page.insert_image(
        label_rect,
        stream=text_stream.getvalue(),
        keep_proportion=False,
        overlay=True,
    )


def _insert_centered_text(
    page: fitz.Page,
    rect: fitz.Rect,
    text: str,
    *,
    font_file: Path,
    font_size: float,
    color: tuple[float, float, float],
) -> None:
    font = _pdf_font()
    width = font.text_length(text, fontsize=font_size)
    text_height = (font.ascender - font.descender) * font_size
    x = rect.x0 + max(0.0, (rect.width - width) / 2)
    baseline = rect.y0 + (rect.height - text_height) / 2 + font.ascender * font_size
    page.insert_text(
        (x, baseline),
        text,
        fontname="jpfont",
        fontfile=str(font_file),
        fontsize=font_size,
        color=color,
        overlay=True,
    )


def _draw_stamp(page: fitz.Page, mark: StampMark, font_file: Path) -> None:
    if mark.kind == "quality":
        title = "品質保証"
        color = hex_to_rgb("#e31b23")
    elif mark.kind == "process":
        title = "加工図"
        color = hex_to_rgb("#1f2a7a")
    else:
        raise ValueError(f"Unsupported stamp kind: {mark.kind}")

    rect = stamp_mark_rect(mark)
    first_y = rect.y0 + rect.height * 0.34
    second_y = rect.y0 + rect.height * 0.67
    page.draw_oval(rect, color=color, width=max(1.1, mark.size * 0.022), overlay=True)
    page.draw_line(
        (rect.x0 + 2, first_y),
        (rect.x1 - 2, first_y),
        color=color,
        width=max(0.9, mark.size * 0.018),
        overlay=True,
    )
    page.draw_line(
        (rect.x0 + 2, second_y),
        (rect.x1 - 2, second_y),
        color=color,
        width=max(0.9, mark.size * 0.018),
        overlay=True,
    )
    _insert_centered_text(
        page,
        fitz.Rect(rect.x0, rect.y0 + 2, rect.x1, first_y + 1),
        title,
        font_file=font_file,
        font_size=mark.size * 0.17,
        color=color,
    )
    _insert_centered_text(
        page,
        fitz.Rect(rect.x0, first_y + 2, rect.x1, second_y + 1),
        mark.date,
        font_file=font_file,
        font_size=mark.size * 0.18,
        color=color,
    )
    _insert_centered_text(
        page,
        fitz.Rect(rect.x0, second_y + 2, rect.x1, rect.y1),
        mark.name,
        font_file=font_file,
        font_size=mark.size * 0.17,
        color=color,
    )


def _draw_procedure_note(
    page: fitz.Page,
    mark: ProcedureNoteMark,
    font_file: Path,
) -> None:
    """Draw one simple, readable note style from the in-app procedure."""

    origin = fitz.Point(mark.origin)
    font_size = max(6.0, min(24.0, mark.font_size))
    font = fitz.Font(fontfile=str(font_file))
    red = hex_to_rgb("#e31b23")
    green = hex_to_rgb("#6ee76e")
    black = (0.0, 0.0, 0.0)

    def text_width(text: str, size: float = font_size) -> float:
        return font.text_length(text, fontsize=size)

    def insert_lines(
        lines: list[str],
        point: fitz.Point,
        *,
        color: tuple[float, float, float] = black,
        size: float = font_size,
    ) -> None:
        line_height = size * 1.28
        for index, line in enumerate(lines):
            if not line:
                continue
            page.insert_text(
                (point.x, point.y + size + index * line_height),
                line,
                fontname="jpfont",
                fontfile=str(font_file),
                fontsize=size,
                color=color,
                overlay=True,
            )

    lines = [line.strip() for line in mark.text.splitlines() if line.strip()]
    if not lines:
        return
    if mark.kind == "phase":
        label = lines[0]
        width = text_width(label) + font_size * 1.3
        height = font_size * 1.75
        rect = fitz.Rect(origin.x, origin.y, origin.x + width, origin.y + height)
        page.draw_rect(rect, color=red, width=max(1.0, font_size * 0.12), overlay=True)
        insert_lines([label], origin + fitz.Point(font_size * 0.55, font_size * 0.10), color=red)
        return
    if mark.kind == "post_process":
        content_width = max((text_width(line) for line in lines), default=0.0)
        width = max(150.0, content_width + font_size * 1.2)
        header_height = font_size * 2.0
        body_height = max(font_size * 2.3, len(lines) * font_size * 1.3 + font_size)
        outer = fitz.Rect(origin.x, origin.y, origin.x + width, origin.y + header_height + body_height)
        header = fitz.Rect(origin.x, origin.y, origin.x + width, origin.y + header_height)
        page.draw_rect(outer, color=black, width=max(1.0, font_size * 0.11), overlay=True)
        page.draw_rect(header, color=black, fill=green, width=max(1.0, font_size * 0.11), overlay=True)
        insert_lines(["後処理あり"], origin + fitz.Point(font_size * 0.55, font_size * 0.2), size=font_size * 1.08)
        insert_lines(lines, fitz.Point(origin.x + font_size * 0.55, origin.y + header_height + font_size * 0.15), size=font_size * 0.90)
        return
    if mark.kind in {"confidential", "borrowed"}:
        insert_lines(lines, origin, color=red)
        return
    insert_lines(lines, origin, color=black)


def _insert_rotated_text(
    page: fitz.Page,
    origin: fitz.Point,
    text: str,
    *,
    direction: fitz.Point,
    font_file: Path,
    font_size: float,
    color: tuple[float, float, float] = (0, 0, 0),
    halo_width: float = 0.0,
) -> None:
    if not text:
        return
    # PyMuPDF's morph matrix rotates counter to the page text direction.
    # Negating the PDF direction angle preserves vertical and oblique text.
    angle = -math.degrees(math.atan2(direction.y, direction.x))
    insert_options = {}
    if halo_width > 0:
        # A narrow white outline keeps a drawing line from running through
        # the glyphs without blanking a rectangular section of that line.
        insert_options = {
            "fill": color,
            "color": (1, 1, 1),
            "border_width": halo_width,
            "render_mode": 2,
        }
    else:
        insert_options = {"color": color}
    # Built-in Helvetica renders compact Latin engineering notation more
    # reliably than a TTC subfont (notably the ± glyph in rotated text).
    latin_text = all(ord(character) <= 255 for character in text)
    font_options = (
        {"fontname": "helv"}
        if latin_text
        else {"fontname": "jpfont", "fontfile": str(font_file)}
    )
    page.insert_text(
        origin,
        text,
        fontsize=font_size,
        morph=(origin, fitz.Matrix(angle)),
        overlay=True,
        **font_options,
        **insert_options,
    )


def _clip_line_to_rect(
    start: fitz.Point,
    end: fitz.Point,
    rect: fitz.Rect,
) -> tuple[fitz.Point, fitz.Point] | None:
    """Return the portion of a line segment inside ``rect``."""

    dx = end.x - start.x
    dy = end.y - start.y
    lower = 0.0
    upper = 1.0
    for coefficient, distance in (
        (-dx, start.x - rect.x0),
        (dx, rect.x1 - start.x),
        (-dy, start.y - rect.y0),
        (dy, rect.y1 - start.y),
    ):
        if abs(coefficient) < 1e-9:
            if distance < 0:
                return None
            continue
        ratio = distance / coefficient
        if coefficient < 0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return None
    return (
        fitz.Point(start.x + lower * dx, start.y + lower * dy),
        fitz.Point(start.x + upper * dx, start.y + upper * dy),
    )


def _whiteout_preserving_lines(
    page: fitz.Page,
    rect: fitz.Rect,
) -> None:
    """Clear source text while restoring thin vector dimension lines.

    A PDF text replacement still needs to cover the old glyphs. Capturing the
    thin source line segments first and drawing only their clipped portions
    back prevents that whiteout from creating a visible gap in a dimension
    line.
    """

    preserved: list[
        tuple[fitz.Point, fitz.Point, tuple[float, ...], float]
    ] = []
    for drawing in page.get_drawings():
        color = drawing.get("color")
        width = float(drawing.get("width") or 0.0)
        if color is None or width <= 0 or width > 1.25:
            continue
        for item in drawing.get("items", []):
            if not item or item[0] != "l":
                continue
            clipped = _clip_line_to_rect(
                fitz.Point(item[1]),
                fitz.Point(item[2]),
                rect,
            )
            if clipped is not None and math.dist(*clipped) > 0.05:
                preserved.append(
                    (clipped[0], clipped[1], tuple(color), width)
                )
    page.draw_rect(
        rect,
        color=None,
        fill=(1, 1, 1),
        overlay=True,
    )
    for start, end, color, width in preserved:
        page.draw_line(
            start,
            end,
            color=color,
            width=width,
            overlay=True,
        )


def _draw_replacement(
    page: fitz.Page,
    mark: ReplacementMark,
    font_file: Path,
) -> None:
    rect = fitz.Rect(mark.rect) & page.rect
    if rect.is_empty:
        return
    _whiteout_preserving_lines(page, rect)

    direction = fitz.Point(mark.direction)
    length = math.hypot(direction.x, direction.y) or 1.0
    direction /= length
    normal = fitz.Point(-direction.y, direction.x)
    replacement_font_file = font_file
    if "gothic" in mark.font_name.lower():
        gothic = Path(
            os.environ.get("WINDIR", r"C:\Windows"),
            "Fonts",
            "msgothic.ttc",
        )
        if gothic.is_file():
            replacement_font_file = gothic
    replacement_font = fitz.Font(
        fontfile=str(replacement_font_file)
    )
    nominal_size = max(5.0, mark.font_size)
    small_size = max(
        4.0,
        min(
            36.0,
            mark.tolerance_font_size
            if mark.tolerance_font_size is not None
            else nominal_size * 0.80,
        ),
    )

    def offset_point(
        origin: fitz.Point,
        offset: tuple[float, float],
    ) -> fitz.Point:
        return origin + fitz.Point(offset)

    def render_text_entries(
        entries: list[tuple[fitz.Point, str, float]],
    ) -> None:
        for origin, text, font_size in entries:
            _insert_rotated_text(
                page,
                origin,
                text,
                direction=direction,
                font_file=replacement_font_file,
                font_size=font_size,
                color=mark.font_color,
                halo_width=0.025,
            )

    if mark.origin is not None:
        base_origin = fitz.Point(mark.origin)
        nominal_origin = offset_point(base_origin, mark.value_offset)
        entries = [(nominal_origin, mark.value, nominal_size)]
        if mark.upper_tolerance or mark.lower_tolerance:
            tolerance_origin = offset_point(
                base_origin
                + direction
                * (
                    replacement_font.text_length(
                        mark.value,
                        fontsize=nominal_size,
                    )
                    + 0.5
                ),
                mark.tolerance_offset,
            )
            if mark.upper_tolerance:
                entries.append(
                    (
                        tolerance_origin - normal * small_size,
                        mark.upper_tolerance,
                        small_size,
                    )
                )
            if mark.lower_tolerance:
                entries.append(
                    (tolerance_origin, mark.lower_tolerance, small_size)
                )
        render_text_entries(entries)
        return

    along = _projection_interval(rect, (direction.x, direction.y))
    across = _projection_interval(rect, (normal.x, normal.y))
    font = _pdf_font()
    nominal_baseline = across[1] + font.descender * nominal_size
    base_origin = (
        direction * (along[0] + 1.0) + normal * nominal_baseline
    )
    nominal_origin = offset_point(base_origin, mark.value_offset)
    entries = [(nominal_origin, mark.value, nominal_size)]

    if mark.upper_tolerance or mark.lower_tolerance:
        tolerance_along = (
            + font.text_length(mark.value, fontsize=nominal_size)
            + 1.0
        )
        if mark.upper_tolerance:
            upper_baseline = across[0] + font.ascender * small_size
            entries.append(
                (
                    offset_point(
                        direction * (along[0] + 1.0 + tolerance_along)
                        + normal * upper_baseline,
                        mark.tolerance_offset,
                    ),
                    mark.upper_tolerance,
                    small_size,
                )
            )
        if mark.lower_tolerance:
            lower_baseline = across[1] + font.descender * small_size
            entries.append(
                (
                    offset_point(
                        direction * (along[0] + 1.0 + tolerance_along)
                        + normal * lower_baseline,
                        mark.tolerance_offset,
                    ),
                    mark.lower_tolerance,
                    small_size,
                )
            )
    render_text_entries(entries)


def _draw_general_tolerance_batch(
    page: fitz.Page,
    mark: GeneralToleranceBatchMark,
    font_file: Path,
) -> None:
    font = fitz.Font(fontfile=str(font_file))
    for addition in mark.additions:
        direction = fitz.Point(addition.direction)
        length = math.hypot(direction.x, direction.y) or 1.0
        direction /= length
        origin = fitz.Point(addition.origin)
        font_size = max(4.0, min(24.0, addition.font_size))
        if addition.suffix_rect is not None:
            suffix_rect = fitz.Rect(addition.suffix_rect) & page.rect
            if not suffix_rect.is_empty:
                _whiteout_preserving_lines(page, suffix_rect)
        if addition.text.startswith("\u00b1"):
            # PyMuPDF can retain ± in extraction while producing no visible
            # glyph with some Windows TTC / subset combinations. Draw the
            # compact engineering symbol as vectors, then render ASCII digits.
            normal = fitz.Point(-direction.y, direction.x)
            width = max(0.55, font_size * 0.055)
            symbol_center = (
                origin + direction * font_size * 0.27
                - normal * font_size * 0.39
            )
            half = font_size * 0.20
            page.draw_line(
                symbol_center - direction * half,
                symbol_center + direction * half,
                color=(0, 0, 0),
                width=width,
                overlay=True,
            )
            page.draw_line(
                symbol_center - normal * half,
                symbol_center + normal * half,
                color=(0, 0, 0),
                width=width,
                overlay=True,
            )
            minus_center = symbol_center + normal * font_size * 0.42
            page.draw_line(
                minus_center - direction * half,
                minus_center + direction * half,
                color=(0, 0, 0),
                width=width,
                overlay=True,
            )
            _insert_rotated_text(
                page,
                origin + direction * font_size * 0.62,
                addition.text[1:],
                direction=direction,
                font_file=font_file,
                font_size=font_size,
                color=(0, 0, 0),
                halo_width=0.0,
            )
        else:
            _insert_rotated_text(
                page,
                origin,
                addition.text,
                direction=direction,
                font_file=font_file,
                font_size=font_size,
                color=(0, 0, 0),
                halo_width=0.0,
            )
        if addition.suffix_text:
            suffix_font_size = max(
                font_size,
                min(
                    24.0,
                    addition.suffix_font_size
                    if addition.suffix_font_size is not None
                    else font_size,
                ),
            )
            suffix_origin = origin + direction * (
                font.text_length(addition.text, fontsize=font_size)
                + max(0.45, font_size * 0.07)
            )
            _insert_rotated_text(
                page,
                suffix_origin,
                addition.suffix_text,
                direction=direction,
                font_file=font_file,
                font_size=suffix_font_size,
                color=(0, 0, 0),
                halo_width=0.0,
            )

def _draw_geometric_symbol(
    page: fitz.Page,
    rect: fitz.Rect,
    symbol: str,
) -> None:
    center = fitz.Point(
        (rect.x0 + rect.x1) / 2,
        (rect.y0 + rect.y1) / 2,
    )
    size = min(rect.width, rect.height) * 0.52
    half = size / 2
    width = max(0.65, rect.height * 0.045)
    color = (0, 0, 0)
    if symbol == "parallelism":
        for offset in (-size * 0.18, size * 0.18):
            page.draw_line(
                (center.x - half + offset, center.y + half),
                (center.x + half + offset, center.y - half),
                color=color,
                width=width,
                overlay=True,
            )
    elif symbol == "flatness":
        slant = size * 0.22
        page.draw_polyline(
            [
                (center.x - half + slant, center.y - half),
                (center.x + half, center.y - half),
                (center.x + half - slant, center.y + half),
                (center.x - half, center.y + half),
            ],
            color=color,
            width=width,
            closePath=True,
            overlay=True,
        )
    elif symbol == "perpendicularity":
        page.draw_line(
            (center.x - half, center.y + half),
            (center.x + half, center.y + half),
            color=color,
            width=width,
            overlay=True,
        )
        page.draw_line(
            (center.x, center.y - half),
            (center.x, center.y + half),
            color=color,
            width=width,
            overlay=True,
        )
    elif symbol == "concentricity":
        page.draw_circle(
            center,
            half,
            color=color,
            width=width,
            overlay=True,
        )
        page.draw_circle(
            center,
            half * 0.48,
            color=color,
            width=width,
            overlay=True,
        )
    elif symbol == "position":
        page.draw_circle(
            center,
            half * 0.72,
            color=color,
            width=width,
            overlay=True,
        )
        page.draw_line(
            (center.x - half, center.y),
            (center.x + half, center.y),
            color=color,
            width=width,
            overlay=True,
        )
        page.draw_line(
            (center.x, center.y - half),
            (center.x, center.y + half),
            color=color,
            width=width,
            overlay=True,
        )
    elif symbol == "angularity":
        page.draw_line(
            (center.x - half, center.y + half),
            (center.x + half, center.y + half),
            color=color,
            width=width,
            overlay=True,
        )
        page.draw_line(
            (center.x - half * 0.35, center.y + half),
            (center.x + half * 0.65, center.y - half),
            color=color,
            width=width,
            overlay=True,
        )
    elif symbol == "circularity":
        page.draw_circle(
            center,
            half,
            color=color,
            width=width,
            overlay=True,
        )
    elif symbol == "cylindricity":
        page.draw_circle(
            center,
            half * 0.72,
            color=color,
            width=width,
            overlay=True,
        )
        page.draw_line(
            (center.x - half, center.y - half),
            (center.x + half, center.y - half),
            color=color,
            width=width,
            overlay=True,
        )
        page.draw_line(
            (center.x - half, center.y + half),
            (center.x + half, center.y + half),
            color=color,
            width=width,
            overlay=True,
        )
    elif symbol == "profile":
        points = [
            (
                center.x - half + size * index / 8,
                center.y
                - math.sin(math.pi * index / 8) * half,
            )
            for index in range(9)
        ]
        page.draw_polyline(
            points,
            color=color,
            width=width,
            overlay=True,
        )
    else:
        page.draw_line(
            (center.x - half, center.y),
            (center.x + half, center.y),
            color=color,
            width=width,
            overlay=True,
        )


def _draw_geometric_tolerance(
    page: fitz.Page,
    mark: GeometricToleranceMark,
    font_file: Path,
) -> None:
    font = _pdf_font()
    row_height = max(13.0, mark.font_size * 1.55)
    symbol_width = row_height
    row_specs: list[tuple[float, float]] = []
    for _symbol, value, datum in mark.rows:
        value_width = max(
            row_height * 1.7,
            font.text_length(value, fontsize=mark.font_size) + 8.0,
        )
        datum_width = (
            max(
                row_height,
                font.text_length(datum, fontsize=mark.font_size) + 8.0,
            )
            if datum
            else 0.0
        )
        row_specs.append((value_width, datum_width))
    total_width = max(
        symbol_width + value_width + datum_width
        for value_width, datum_width in row_specs
    )
    total_height = row_height * len(mark.rows)
    frame_rect = fitz.Rect(
        mark.label[0],
        mark.label[1],
        mark.label[0] + total_width,
        mark.label[1] + total_height,
    )

    if mark.leader:
        target = fitz.Point(mark.target)
        anchor = _leader_anchor(target, frame_rect)
        vector = anchor - target
        length = math.hypot(vector.x, vector.y)
        if length > 0.1:
            unit = vector / length
            normal = fitz.Point(-unit.y, unit.x)
            arrow_length = max(5.0, mark.font_size * 0.65)
            arrow_width = max(2.0, mark.font_size * 0.24)
            base = target + unit * arrow_length
            page.draw_line(
                base,
                anchor,
                color=(0, 0, 0),
                width=0.9,
                overlay=True,
            )
            page.draw_polyline(
                [
                    target,
                    base + normal * arrow_width,
                    base - normal * arrow_width,
                ],
                color=(0, 0, 0),
                fill=(0, 0, 0),
                width=0.6,
                closePath=True,
                overlay=True,
            )

    fill = hex_to_rgb(mark.color)
    for index, ((symbol, value, datum), spec) in enumerate(
        zip(mark.rows, row_specs)
    ):
        value_width, datum_width = spec
        y0 = mark.label[1] + index * row_height
        cells = [
            fitz.Rect(
                mark.label[0],
                y0,
                mark.label[0] + symbol_width,
                y0 + row_height,
            ),
            fitz.Rect(
                mark.label[0] + symbol_width,
                y0,
                mark.label[0] + symbol_width + value_width,
                y0 + row_height,
            ),
        ]
        if datum:
            cells.append(
                fitz.Rect(
                    cells[-1].x1,
                    y0,
                    cells[-1].x1 + datum_width,
                    y0 + row_height,
                )
            )
        for cell in cells:
            page.draw_rect(
                cell,
                color=(0, 0, 0),
                fill=fill,
                width=0.8,
                fill_opacity=max(
                    0.05,
                    min(1.0, mark.opacity),
                ),
                overlay=True,
            )
        _draw_geometric_symbol(page, cells[0], symbol)
        _insert_centered_text(
            page,
            cells[1],
            value,
            font_file=font_file,
            font_size=mark.font_size,
            color=(0, 0, 0),
        )
        if datum:
            _insert_centered_text(
                page,
                cells[2],
                datum,
                font_file=font_file,
                font_size=mark.font_size,
                color=(0, 0, 0),
            )


def _draw_surface_finish(
    page: fitz.Page,
    mark: SurfaceFinishMark,
    font_file: Path,
) -> None:
    direction = (
        fitz.Point(0, -1)
        if mark.orientation == "vertical"
        else fitz.Point(1, 0)
    )
    normal = fitz.Point(-direction.y, direction.x)
    anchor = fitz.Point(mark.anchor)

    def local(x: float, y: float) -> fitz.Point:
        return anchor + direction * x + normal * y

    color = hex_to_rgb(mark.color)
    count = max(1, min(6, int(mark.triangle_count)))
    triangle_size = max(7.0, mark.font_size * 1.05)
    font = _pdf_font()
    text_width = font.text_length(
        mark.value,
        fontsize=mark.font_size,
    )
    paren_space = mark.font_size * 0.75 if mark.parenthesized else 0.0

    if mark.value_position == "above":
        triangle_x = paren_space
        triangle_y = mark.font_size + 3.0
        text_x = paren_space + max(
            0.0,
            (count * triangle_size - text_width) / 2,
        )
        text_baseline = mark.font_size
        total_width = paren_space * 2 + max(
            count * triangle_size,
            text_width,
        )
    else:
        triangle_x = paren_space
        triangle_y = 0.0
        text_x = (
            paren_space + count * triangle_size + 4.0
        )
        text_baseline = triangle_size * 0.76
        total_width = (
            paren_space * 2
            + count * triangle_size
            + 4.0
            + text_width
        )

    text_quad = [
        local(text_x - 1.5, text_baseline - mark.font_size),
        local(text_x + text_width + 1.5, text_baseline - mark.font_size),
        local(text_x + text_width + 1.5, text_baseline + 1.5),
        local(text_x - 1.5, text_baseline + 1.5),
    ]
    page.draw_polyline(
        text_quad,
        color=None,
        fill=color,
        fill_opacity=max(0.05, min(1.0, mark.opacity)),
        closePath=True,
        overlay=True,
    )
    _insert_rotated_text(
        page,
        local(text_x, text_baseline),
        mark.value,
        direction=direction,
        font_file=font_file,
        font_size=mark.font_size,
        color=color,
    )

    for index in range(count):
        x0 = triangle_x + index * triangle_size
        points = [
            local(x0, triangle_y),
            local(x0 + triangle_size, triangle_y),
            local(x0 + triangle_size / 2, triangle_y + triangle_size),
        ]
        page.draw_polyline(
            points,
            color=color,
            fill=color,
            width=max(0.8, mark.font_size * 0.08),
            fill_opacity=max(
                0.05,
                min(1.0, mark.opacity),
            ),
            closePath=True,
            overlay=True,
        )

    if mark.parenthesized:
        paren_size = mark.font_size * 1.5
        paren_baseline = (
            triangle_y + triangle_size * 0.78
            if mark.value_position == "right"
            else triangle_y + triangle_size * 0.7
        )
        _insert_rotated_text(
            page,
            local(0, paren_baseline),
            "(",
            direction=direction,
            font_file=font_file,
            font_size=paren_size,
            color=(0, 0, 0),
        )
        _insert_rotated_text(
            page,
            local(total_width - paren_space * 0.55, paren_baseline),
            ")",
            direction=direction,
            font_file=font_file,
            font_size=paren_size,
            color=(0, 0, 0),
        )


def _point_in_polygon(
    point: fitz.Point,
    polygon: tuple[tuple[float, float], ...],
) -> bool:
    """Return whether a point is inside a page-space polygon."""

    inside = False
    previous = fitz.Point(polygon[-1])
    for raw_point in polygon:
        current = fitz.Point(raw_point)
        if (current.y > point.y) != (previous.y > point.y):
            crossing_x = (
                (previous.x - current.x)
                * (point.y - current.y)
                / (previous.y - current.y)
                + current.x
            )
            if point.x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _rect_intersects_polygon(
    rect: fitz.Rect,
    polygon: tuple[tuple[float, float], ...],
) -> bool:
    if len(polygon) < 3:
        return False
    polygon_rect = fitz.Rect(
        min(point[0] for point in polygon),
        min(point[1] for point in polygon),
        max(point[0] for point in polygon),
        max(point[1] for point in polygon),
    )
    if (rect & polygon_rect).is_empty:
        return False
    rect_corners = (
        fitz.Point(rect.x0, rect.y0),
        fitz.Point(rect.x1, rect.y0),
        fitz.Point(rect.x1, rect.y1),
        fitz.Point(rect.x0, rect.y1),
    )
    if any(_point_in_polygon(corner, polygon) for corner in rect_corners):
        return True
    if any(fitz.Point(point) in rect for point in polygon):
        return True
    previous = fitz.Point(polygon[-1])
    for raw_point in polygon:
        current = fitz.Point(raw_point)
        if _segment_intersects_rect(previous, current, rect):
            return True
        previous = current
    return False


def _needs_white_separation_border(
    rect: fitz.Rect,
    work_fill_items: Iterable[DrawingItem],
) -> bool:
    """Only separate markers that actually cross a product fill."""

    for fill_item in work_fill_items:
        if isinstance(fill_item, WorkShapeMark) and fill_item.style != "line":
            if _rect_intersects_polygon(rect, fill_item.points):
                return True
        elif isinstance(fill_item, WorkRegionMark):
            if any(
                _rect_intersects_polygon(rect, region)
                for region in fill_item.regions
            ):
                return True
    return False


def apply_item_to_page(
    page: fitz.Page,
    item: DrawingItem,
    *,
    font_file: Path | None = None,
    work_fill_items: Iterable[DrawingItem] = (),
) -> None:
    """Apply one drawing item to a page.

    Highlights use a Multiply-blended annotation. Multiplication keeps black
    source text black while coloring the white paper around it.
    """

    if font_file is None:
        font_file = find_japanese_font()

    def annotation_point(
        point: tuple[float, float] | fitz.Point,
    ) -> fitz.Point:
        """Convert a visible-page point to annotation coordinates.

        PyMuPDF reports ``page.rect`` in the page's displayed orientation,
        while annotation constructors expect coordinates for the unrotated
        page.  Without this conversion, markers on 90 / 180 / 270 degree
        PDFs are written to a different visible location.
        """

        value = fitz.Point(point)
        if page.rotation:
            value *= page.derotation_matrix
        return value

    def annotation_rect(rect: fitz.Rect) -> fitz.Rect:
        """Convert a visible-page rectangle to annotation coordinates."""

        corners = (
            (rect.x0, rect.y0),
            (rect.x1, rect.y0),
            (rect.x1, rect.y1),
            (rect.x0, rect.y1),
        )
        converted = [annotation_point(point) for point in corners]
        return fitz.Rect(
            min(point.x for point in converted),
            min(point.y for point in converted),
            max(point.x for point in converted),
            max(point.y for point in converted),
        )

    def finalize_fill_annotation(
        annotation: fitz.Annot,
        color: str,
        opacity: float,
    ) -> None:
        """Create a borderless, editable marker annotation.

        PyMuPDF gives Square / Polygon annotations a red `/C` stroke by
        default even when the visible border width is zero. Some PDF viewers
        still display that fallback color as an outline, so replace `/C` with
        the standard transparent color after generating the appearance stream.
        `/IC` keeps the marker fill.
        """

        annotation.set_border(width=0)
        annotation.set_colors(
            stroke=None,
            fill=hex_to_rgb(color),
        )
        annotation.set_opacity(max(0.05, min(1.0, opacity)))
        annotation.set_blendmode("Multiply")
        annotation.update()
        document = page.parent
        # An empty `/C` array is the PDF-standard transparent annotation
        # color. It avoids the red default used when `/C` is absent or null.
        document.xref_set_key(annotation.xref, "C", "[]")
        document.xref_set_key(
            annotation.xref,
            "Border",
            "[0 0 0]",
        )

    def add_white_separation_border(
        rect: fitz.Rect,
        quad: tuple[tuple[float, float], ...] | None,
    ) -> None:
        """Separate a dimension marker from a workpiece fill.

        Product regions are deliberately rendered first.  A normal-blended
        white stroke then cuts a narrow, clean gap around every dimension
        marker without hiding the source glyphs inside the marker.
        """

        if quad:
            annotation = page.add_polygon_annot(
                [annotation_point(point) for point in quad]
            )
        else:
            annotation = page.add_rect_annot(annotation_rect(rect))
        annotation.set_border(width=1.65)
        annotation.set_colors(stroke=(1.0, 1.0, 1.0), fill=None)
        annotation.set_opacity(1.0)
        annotation.set_blendmode("Normal")
        annotation.update()

    if isinstance(item, Mark):
        rect = fitz.Rect(item.rect) & page.rect
        if not rect.is_empty and rect.get_area() > 0:
            if _needs_white_separation_border(rect, work_fill_items):
                add_white_separation_border(rect, item.quad)
            if item.quad:
                points = [
                    annotation_point(point)
                    for point in item.quad
                ]
                annotation = page.add_polygon_annot(points)
            else:
                annotation = page.add_rect_annot(annotation_rect(rect))
            finalize_fill_annotation(
                annotation,
                item.color,
                item.opacity,
            )
    elif isinstance(item, StrikeMark):
        normal = fitz.Point(item.normal)
        start = fitz.Point(item.start)
        end = fitz.Point(item.end)
        for offset in (-item.gap, item.gap):
            page.draw_line(
                start + normal * offset,
                end + normal * offset,
                color=(0, 0, 0),
                width=item.width,
                overlay=True,
            )
    elif isinstance(item, DimensionMark):
        _draw_dimension(page, item, font_file)
    elif isinstance(item, StampMark):
        _draw_stamp(page, item, font_file)
    elif isinstance(item, ProcedureNoteMark):
        _draw_procedure_note(page, item, font_file)
    elif isinstance(item, ReplacementMark):
        _draw_replacement(page, item, font_file)
    elif isinstance(item, GeneralToleranceBatchMark):
        if page.rotation:
            converted_additions: list[ToleranceAddition] = []
            for addition in item.additions:
                visible_origin = fitz.Point(addition.origin)
                visible_end = visible_origin + fitz.Point(addition.direction)
                origin = visible_origin * page.derotation_matrix
                end = visible_end * page.derotation_matrix
                direction = end - origin
                direction /= math.hypot(direction.x, direction.y) or 1.0
                suffix_rect = (
                    tuple(annotation_rect(fitz.Rect(addition.suffix_rect)))
                    if addition.suffix_rect is not None
                    else None
                )
                converted_additions.append(
                    replace(
                        addition,
                        origin=(origin.x, origin.y),
                        direction=(direction.x, direction.y),
                        suffix_rect=suffix_rect,
                    )
                )
            _draw_general_tolerance_batch(
                page,
                GeneralToleranceBatchMark(
                    item.page_index, tuple(converted_additions)
                ),
                font_file,
            )
        else:
            _draw_general_tolerance_batch(page, item, font_file)
    elif isinstance(item, DimensionMarkingBatch):
        for entry in item.entries:
            rect = fitz.Rect(entry.rect) & page.rect
            if rect.is_empty or rect.get_area() <= 0:
                continue
            if _needs_white_separation_border(rect, work_fill_items):
                add_white_separation_border(rect, entry.quad)
            if entry.quad:
                annotation = page.add_polygon_annot(
                    [annotation_point(point) for point in entry.quad]
                )
            else:
                annotation = page.add_rect_annot(annotation_rect(rect))
            finalize_fill_annotation(
                annotation,
                entry.color,
                entry.opacity,
            )
    elif isinstance(item, GeometricToleranceMark):
        _draw_geometric_tolerance(page, item, font_file)
    elif isinstance(item, SurfaceFinishMark):
        _draw_surface_finish(page, item, font_file)
    elif isinstance(item, WorkShapeMark):
        points = [annotation_point(point) for point in item.points]
        if item.style == "line":
            if len(points) >= 2:
                annotation = page.add_polyline_annot(points)
                annotation.set_border(width=max(0.5, item.line_width))
                annotation.set_colors(stroke=hex_to_rgb(item.color))
                annotation.set_opacity(max(0.05, min(1.0, item.opacity)))
                annotation.set_blendmode("Multiply")
                annotation.update()
        elif len(points) >= 3:
            annotation = page.add_polygon_annot(points)
            finalize_fill_annotation(
                annotation,
                item.color,
                item.opacity,
            )
    elif isinstance(item, WorkRegionMark):
        for region in item.regions:
            points = [annotation_point(point) for point in region]
            if len(points) < 3:
                continue
            annotation = page.add_polygon_annot(points)
            finalize_fill_annotation(
                annotation,
                item.color,
                item.opacity,
            )
    elif isinstance(item, DetailPairMark):
        for area in item.areas:
            rect = fitz.Rect(area) & page.rect
            if rect.is_empty or rect.get_area() <= 0:
                continue
            annotation = page.add_rect_annot(annotation_rect(rect))
            finalize_fill_annotation(
                annotation,
                item.color,
                item.opacity,
            )
    else:
        raise TypeError(f"Unsupported drawing item: {type(item)!r}")


def _items_in_visual_order(
    items: Iterable[DrawingItem],
    page_index: int,
) -> list[DrawingItem]:
    """Render product fills below dimensions regardless of operation order."""

    page_items = [item for item in items if item.page_index == page_index]
    fills: list[DrawingItem] = []
    foreground: list[DrawingItem] = []
    for item in page_items:
        if isinstance(item, WorkRegionMark) or (
            isinstance(item, WorkShapeMark) and item.style != "line"
        ):
            fills.append(item)
        else:
            foreground.append(item)
    return fills + foreground


def render_page_preview(
    document: fitz.Document,
    page_index: int,
    items: Iterable[DrawingItem],
    *,
    zoom: float = 1.7,
) -> bytes:
    """Render one source page plus pending items to PNG bytes."""

    preview = fitz.open()
    try:
        preview.insert_pdf(document, from_page=page_index, to_page=page_index)
        preview_page = preview[0]
        font_file = find_japanese_font()
        ordered_items = _items_in_visual_order(items, page_index)
        work_fill_items = tuple(
            item
            for item in ordered_items
            if isinstance(item, WorkRegionMark)
            or (isinstance(item, WorkShapeMark) and item.style != "line")
        )
        for item in ordered_items:
            apply_item_to_page(
                preview_page,
                item,
                font_file=font_file,
                work_fill_items=work_fill_items,
            )
        pixmap = preview_page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            alpha=False,
            annots=True,
        )
        return pixmap.tobytes("png")
    finally:
        preview.close()


def export_pdf(
    source_path: str | Path,
    output_path: str | Path,
    items: Iterable[DrawingItem],
) -> None:
    """Write a flattened PDF containing all drawing-assist additions."""

    source = Path(source_path)
    output = Path(output_path)
    if source.resolve() == output.resolve():
        raise ValueError("The output path must be different from the source PDF.")

    document = fitz.open(source)
    font_file = find_japanese_font()
    try:
        all_items = list(items)
        for item in all_items:
            if not 0 <= item.page_index < document.page_count:
                raise IndexError(f"Invalid page index: {item.page_index}")
        for page_index in range(document.page_count):
            page = document[page_index]
            ordered_items = _items_in_visual_order(all_items, page_index)
            work_fill_items = tuple(
                item
                for item in ordered_items
                if isinstance(item, WorkRegionMark)
                or (isinstance(item, WorkShapeMark) and item.style != "line")
            )
            for item in ordered_items:
                apply_item_to_page(
                    page,
                    item,
                    font_file=font_file,
                    work_fill_items=work_fill_items,
                )

        output.parent.mkdir(parents=True, exist_ok=True)
        document.save(output, garbage=4, deflate=True)
    finally:
        document.close()
