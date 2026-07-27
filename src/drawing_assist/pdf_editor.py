from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
import os
from pathlib import Path
from typing import Iterable, TypeAlias

import fitz
from PIL import Image, ImageChops, ImageDraw, ImageFilter


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
class ReplacementMark:
    """A white-out and replacement for an existing dimension value."""

    page_index: int
    rect: tuple[float, float, float, float]
    direction: tuple[float, float]
    value: str
    upper_tolerance: str = ""
    lower_tolerance: str = ""
    font_size: float = 9.0
    origin: tuple[float, float] | None = None
    font_name: str = ""
    font_color: tuple[float, float, float] = (0.0, 0.0, 0.0)


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


DrawingItem: TypeAlias = (
    Mark
    | StrikeMark
    | DimensionMark
    | StampMark
    | ReplacementMark
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
) -> TextHit | None:
    """Select one dimension group, including prefixes and tolerance symbols.

    CAD PDFs often split ``φ``, ``+`` and ``-`` into separate text lines. Some
    diameter glyphs are even mapped to a blank character. Nearby collinear
    lines are therefore merged around the clicked text.
    """

    lines = _raw_text_lines(page)
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


def find_japanese_font() -> Path:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windows / "Fonts" / "meiryo.ttc",
        windows / "Fonts" / "YuGothM.ttc",
        windows / "Fonts" / "msgothic.ttc",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("A Japanese Windows font could not be found.")


@lru_cache(maxsize=1)
def _pdf_font() -> fitz.Font:
    return fitz.Font(fontfile=str(find_japanese_font()))


def dimension_label_rect(mark: DimensionMark) -> fitz.Rect:
    width = _pdf_font().text_length(mark.text, fontsize=mark.font_size)
    return fitz.Rect(
        mark.label[0],
        mark.label[1],
        mark.label[0] + width + 5.0,
        mark.label[1] + mark.font_size * 1.55,
    )


def _leader_anchor(target: fitz.Point, label_rect: fitz.Rect) -> fitz.Point:
    if target.x < label_rect.x0:
        return fitz.Point(label_rect.x0, label_rect.y0 + label_rect.height / 2)
    if target.x > label_rect.x1:
        return fitz.Point(label_rect.x1, label_rect.y0 + label_rect.height / 2)
    if target.y < label_rect.y0:
        return fitz.Point(label_rect.x0 + label_rect.width / 2, label_rect.y0)
    return fitz.Point(label_rect.x0 + label_rect.width / 2, label_rect.y1)


def _draw_dimension(page: fitz.Page, mark: DimensionMark, font_file: Path) -> None:
    target = fitz.Point(mark.target)
    label_rect = dimension_label_rect(mark)
    anchor = _leader_anchor(target, label_rect)
    vector = anchor - target
    length = math.hypot(vector.x, vector.y)
    if length > 0.1:
        unit = vector / length
        normal = fitz.Point(-unit.y, unit.x)
        arrow_length = max(5.0, mark.font_size * 0.7)
        arrow_width = max(2.0, mark.font_size * 0.25)
        base = target + unit * arrow_length
        page.draw_line(base, anchor, color=(0, 0, 0), width=0.9, overlay=True)
        page.draw_polyline(
            [target, base + normal * arrow_width, base - normal * arrow_width],
            color=(0, 0, 0),
            fill=(0, 0, 0),
            width=0.6,
            closePath=True,
            overlay=True,
        )

    page.draw_rect(
        label_rect,
        color=None,
        fill=hex_to_rgb(mark.color),
        fill_opacity=max(0.05, min(1.0, mark.opacity)),
        overlay=True,
    )
    font = _pdf_font()
    text_height = (font.ascender - font.descender) * mark.font_size
    baseline = (
        label_rect.y0
        + (label_rect.height - text_height) / 2
        + font.ascender * mark.font_size
    )
    page.insert_text(
        (label_rect.x0 + 2.0, baseline),
        mark.text,
        fontname="jpfont",
        fontfile=str(font_file),
        fontsize=mark.font_size,
        color=(0, 0, 0),
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

    center = fitz.Point(mark.center)
    radius = mark.size / 2
    rect = fitz.Rect(
        center.x - radius,
        center.y - radius,
        center.x + radius,
        center.y + radius,
    )
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


def _insert_rotated_text(
    page: fitz.Page,
    origin: fitz.Point,
    text: str,
    *,
    direction: fitz.Point,
    font_file: Path,
    font_size: float,
    color: tuple[float, float, float] = (0, 0, 0),
) -> None:
    if not text:
        return
    # PyMuPDF's morph matrix rotates counter to the page text direction.
    # Negating the PDF direction angle preserves vertical and oblique text.
    angle = -math.degrees(math.atan2(direction.y, direction.x))
    page.insert_text(
        origin,
        text,
        fontname="jpfont",
        fontfile=str(font_file),
        fontsize=font_size,
        color=color,
        morph=(origin, fitz.Matrix(angle)),
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
    page.draw_rect(
        rect,
        color=None,
        fill=(1, 1, 1),
        overlay=True,
    )

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
    small_size = max(4.0, nominal_size * 0.80)

    if mark.origin is not None:
        nominal_origin = fitz.Point(mark.origin)
        _insert_rotated_text(
            page,
            nominal_origin,
            mark.value,
            direction=direction,
            font_file=replacement_font_file,
            font_size=nominal_size,
            color=mark.font_color,
        )
        if mark.upper_tolerance or mark.lower_tolerance:
            tolerance_origin = (
                nominal_origin
                + direction
                * (
                    replacement_font.text_length(
                        mark.value,
                        fontsize=nominal_size,
                    )
                    + 0.5
                )
            )
            if mark.upper_tolerance:
                _insert_rotated_text(
                    page,
                    tolerance_origin - normal * small_size,
                    mark.upper_tolerance,
                    direction=direction,
                    font_file=replacement_font_file,
                    font_size=small_size,
                    color=mark.font_color,
                )
            if mark.lower_tolerance:
                _insert_rotated_text(
                    page,
                    tolerance_origin,
                    mark.lower_tolerance,
                    direction=direction,
                    font_file=replacement_font_file,
                    font_size=small_size,
                    color=mark.font_color,
                )
        return

    along = _projection_interval(rect, (direction.x, direction.y))
    across = _projection_interval(rect, (normal.x, normal.y))
    font = _pdf_font()
    nominal_baseline = across[1] + font.descender * nominal_size
    nominal_origin = (
        direction * (along[0] + 1.0) + normal * nominal_baseline
    )
    _insert_rotated_text(
        page,
        nominal_origin,
        mark.value,
        direction=direction,
        font_file=replacement_font_file,
        font_size=nominal_size,
        color=mark.font_color,
    )

    if mark.upper_tolerance or mark.lower_tolerance:
        tolerance_along = (
            along[0]
            + 1.0
            + font.text_length(mark.value, fontsize=nominal_size)
            + 1.0
        )
        if mark.upper_tolerance:
            upper_baseline = across[0] + font.ascender * small_size
            _insert_rotated_text(
                page,
                direction * tolerance_along + normal * upper_baseline,
                mark.upper_tolerance,
                direction=direction,
                font_file=replacement_font_file,
                font_size=small_size,
                color=mark.font_color,
            )
        if mark.lower_tolerance:
            lower_baseline = across[1] + font.descender * small_size
            _insert_rotated_text(
                page,
                direction * tolerance_along + normal * lower_baseline,
                mark.lower_tolerance,
                direction=direction,
                font_file=replacement_font_file,
                font_size=small_size,
                color=mark.font_color,
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


def apply_item_to_page(
    page: fitz.Page,
    item: DrawingItem,
    *,
    font_file: Path | None = None,
) -> None:
    """Apply one drawing item to a page.

    Highlights use a Multiply-blended annotation. Multiplication keeps black
    source text black while coloring the white paper around it.
    """

    if font_file is None:
        font_file = find_japanese_font()

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

    if isinstance(item, Mark):
        rect = fitz.Rect(item.rect) & page.rect
        if not rect.is_empty and rect.get_area() > 0:
            if item.quad:
                points = [fitz.Point(point) for point in item.quad]
                annotation = page.add_polygon_annot(points)
            else:
                annotation = page.add_rect_annot(rect)
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
    elif isinstance(item, ReplacementMark):
        _draw_replacement(page, item, font_file)
    elif isinstance(item, GeometricToleranceMark):
        _draw_geometric_tolerance(page, item, font_file)
    elif isinstance(item, SurfaceFinishMark):
        _draw_surface_finish(page, item, font_file)
    elif isinstance(item, WorkShapeMark):
        points = [fitz.Point(point) for point in item.points]
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
            points = [fitz.Point(point) for point in region]
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
            annotation = page.add_rect_annot(rect)
            finalize_fill_annotation(
                annotation,
                item.color,
                item.opacity,
            )
    else:
        raise TypeError(f"Unsupported drawing item: {type(item)!r}")


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
        for item in items:
            if item.page_index == page_index:
                apply_item_to_page(preview_page, item, font_file=font_file)
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
        for item in items:
            if not 0 <= item.page_index < document.page_count:
                raise IndexError(f"Invalid page index: {item.page_index}")
            page = document[item.page_index]
            apply_item_to_page(page, item, font_file=font_file)

        output.parent.mkdir(parents=True, exist_ok=True)
        document.save(output, garbage=4, deflate=True)
    finally:
        document.close()
