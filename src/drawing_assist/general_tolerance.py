from __future__ import annotations

from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata

import fitz
from PIL import Image, ImageOps

from drawing_assist.image_preprocessor import prepare_raster_for_ocr
from drawing_assist.ocr_config import (
    BARE_NUMBER_MIN_CONFIDENCE,
    SUPPLEMENT_THRESHOLD_SCANNED,
    SUPPLEMENT_THRESHOLD_VECTOR,
)
from drawing_assist.ocr_debug_logger import OcrPipelineRecorder


@dataclass(frozen=True)
class GeneralToleranceCandidate:
    page_index: int
    rect: tuple[float, float, float, float]
    direction: tuple[float, float]
    source_text: str
    nominal_value: float
    kind: str
    tolerance: float
    tolerance_text: str
    selected: bool = True
    quad: tuple[tuple[float, float], ...] | None = None
    manual_required: bool = False


@dataclass(frozen=True)
class DrawingToleranceNotes:
    """Drawing-wide exceptions that take priority over a general standard.

    ``*_maximum`` values describe otherwise unindicated geometry.  They are
    intentionally kept separate from ``*_tolerance`` values: a note such as
    ``指示無き角部はC0.1以下`` must not be misread as ``C ... ±0.1``.
    """

    angle_tolerance: tuple[float, str] | None = None
    chamfer_tolerance: float | None = None
    radius_tolerance: float | None = None
    unindicated_chamfer_maximum: float | None = None
    unindicated_radius_maximum: float | None = None


_NUMBER_PATTERN = re.compile(
    r"(?P<prefix>[φΦØ⌀ＲＲRCＣ]?)"
    r"(?P<number>\d{1,4}(?:[.,]\d{1,4})?)"
    r"(?P<degree>[°。]?)"
)
_EXPLICIT_TOLERANCE = re.compile(
    r"(?:±|士|亇|干|土|\+\s*\d|[−－一-]\s*\d|上限|下限|MAX|MIN)",
    re.IGNORECASE,
)
_NON_DIMENSION_CONTEXT = re.compile(
    r"(?:SCALE|DATE|DWG|DRAWING|PAGE|SHEET|REV|図番|品番|尺度|日付|材質|"
    r"MODEL|PART|NO\.|型式|改訂|番号|公差|以上|超え|"
    r"Rz\s*max|Rzmax|Ra\s*max|Ramax|Rmax)",
    re.IGNORECASE,
)


from drawing_assist.drawing_text_normalizer import (
    is_tolerance_fragment,
    normalize_raster_dimension_text,
    parse_dimension_token,
)


def _prepare_raster_for_ocr(image: Image.Image) -> Image.Image:
    """後方互換のため残す。新規コードは image_preprocessor を直接使う。"""

    return prepare_raster_for_ocr(image)


def _normalize_raster_dimension_text(value: str) -> str:
    """既存呼び出し互換の正規化ラッパー。"""

    return normalize_raster_dimension_text(value)


from drawing_assist.local_ocr import (
    LocalOcrPage,
    analyze_detail_angles,
    analyze_scanned_page_tiles,
    enrich_scanned_ocr_page,
)


def _format_tolerance(value: float) -> str:
    # Use an escape so Windows source-file code pages cannot corrupt the sign.
    return f"\u00b1{value:g}"


def angle_tolerance(
    shorter_side_length: float,
    *,
    standard: str = "jis_b_0405",
    grade: str = "m",
) -> tuple[float, str]:
    """Return JIS B 0405 / PISCO machining angular tolerance.

    The table is selected by the shorter leg adjacent to the angle. PISCO's
    metal machining standard uses the same m-grade values as JIS B 0405.
    The numeric result is expressed in degrees for downstream color rules.
    """

    if standard not in {"jis_b_0405", "pisco"}:
        raise ValueError("一般公差規格を選択してください。")
    if standard == "jis_b_0405" and grade.lower() not in {"f", "m"}:
        raise ValueError("JIS B 0405の等級はf（精級）かm（中級）です。")
    limits = (10, 50, 120, 400, math.inf)
    values = (
        (1.0, "±1°"),
        (0.5, "±30′"),
        (20 / 60, "±20′"),
        (10 / 60, "±10′"),
        (5 / 60, "±5′"),
    )
    for limit, value in zip(limits, values):
        if shorter_side_length <= limit:
            return value
    return values[-1]


def _extract_angle_tolerance(text: str) -> tuple[float, str] | None:
    """Read a drawing-wide unindicated angular tolerance note."""

    normalized = unicodedata.normalize("NFKC", text)
    match = re.search(
        r"(?:指示(?:無|な)き角度公差|角度公差)"
        r"[^\n]{0,40}?±\s*(\d+(?:\.\d+)?)\s*"
        r"(°|度|′|'|分)",
        normalized,
    )
    if match is None:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    if unit in {"′", "'", "分"}:
        return value / 60, f"±{value:g}′"
    return value, f"±{value:g}°"


def extract_drawing_tolerance_notes(text: str) -> DrawingToleranceNotes:
    """Extract general-tolerance exceptions without conflating limits.

    Drawing notes may override an angular or C/R *tolerance* explicitly with
    ``±``.  Notes ending in ``以下`` instead specify the maximum geometry of
    an otherwise unindicated corner and are therefore reported separately.
    """

    normalized = unicodedata.normalize("NFKC", text)

    def explicit_cr_tolerance(symbol: str, name: str) -> float | None:
        patterns = (
            rf"(?:指示\s*(?:無|な)き[^\n]{{0,24}})?{symbol}\s*(?:寸法)?\s*公差"
            rf"[^\n]{{0,24}}?±\s*(\d+(?:\.\d+)?)",
            rf"(?:指示\s*(?:無|な)き[^\n]{{0,24}})?{name}\s*(?:寸法)?\s*公差"
            rf"[^\n]{{0,24}}?±\s*(\d+(?:\.\d+)?)",
            rf"(?:指示\s*(?:無|な)き[^\n]{{0,24}})?C\s*/\s*R\s*公差"
            rf"[^\n]{{0,24}}?±\s*(\d+(?:\.\d+)?)",
        )
        for pattern in patterns:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match is not None:
                return float(match.group(1))
        return None

    chamfer_maximum_match = re.search(
        r"指示\s*(?:無|な)き\s*(?:角部|角|かど)"
        r"[^\n]{0,30}?C\s*(\d+(?:\.\d+)?)\s*(?:以下|以内)",
        normalized,
        re.IGNORECASE,
    )
    radius_maximum_match = re.search(
        r"指示\s*(?:無|な)き\s*(?:隅|すみ|隅部)"
        r"[^\n]{0,30}?R?\s*(?:は|:|：|,|、)?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:以下|以内)",
        normalized,
        re.IGNORECASE,
    )
    return DrawingToleranceNotes(
        angle_tolerance=_extract_angle_tolerance(normalized),
        chamfer_tolerance=explicit_cr_tolerance("C", "面取り"),
        radius_tolerance=explicit_cr_tolerance("R", "丸み"),
        unindicated_chamfer_maximum=(
            float(chamfer_maximum_match.group(1))
            if chamfer_maximum_match is not None
            else None
        ),
        unindicated_radius_maximum=(
            float(radius_maximum_match.group(1))
            if radius_maximum_match is not None
            else None
        ),
    )


def jis_b_0405_tolerance(
    nominal: float,
    grade: str,
    kind: str = "linear",
) -> float | None:
    """Return the resolved JIS-mode tolerance in millimetres."""

    grade_key = grade.lower()
    if grade_key not in {"f", "m"}:
        raise ValueError("JIS B 0405の等級はf（精級）かm（中級）です。")

    # Drawing Assist operational exception requested for JIS mode. JIS B
    # 0405 itself requires an individual indication below 0.5 mm; this rule
    # supplies that individual value without pretending it is a table value.
    if nominal <= 0:
        return None
    if nominal <= 0.099:
        return nominal
    if nominal < 0.5:
        return 0.1

    if kind in {"chamfer", "radius"}:
        # From 0.5 mm upward, use JIS B 0405 table 2.
        if nominal <= 3:
            return 0.2
        if nominal <= 6:
            return 0.5
        return 1.0

    limits = (3, 6, 30, 120, 400, 1000, 2000, 4000)
    table = {
        "f": (0.05, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, None),
        "m": (0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 2.0),
    }
    for limit, tolerance in zip(limits, table[grade_key]):
        if nominal <= limit:
            return tolerance
    return None


def pisco_tolerance(
    nominal: float,
    kind: str = "linear",
) -> float | None:
    """Return the supplied Nippon Pisco machining tolerance value."""

    if kind in {"chamfer", "radius"}:
        limits = (0.15, 0.5, 3, 6, math.inf)
        values = (0.05, 0.1, 0.2, 0.5, 1.0)
    elif nominal < 0.1:
        return None
    elif kind == "diameter":
        limits = (0.5, 3, 6, 30, 120, 400, 1000)
        values = (0.025, 0.05, 0.05, 0.1, 0.15, 0.2, 0.3)
    else:
        limits = (0.5, 3, 6, 30, 120, 400, 1000)
        values = (0.05, 0.1, 0.1, 0.2, 0.3, 0.5, 0.8)
    for limit, tolerance in zip(limits, values):
        if nominal <= limit:
            return tolerance
    return None


def tolerance_for(
    standard: str,
    nominal: float,
    *,
    grade: str = "m",
    kind: str = "linear",
) -> float | None:
    if standard == "pisco":
        return pisco_tolerance(nominal, kind)
    if standard == "jis_b_0405":
        return jis_b_0405_tolerance(nominal, grade, kind)
    raise ValueError("一般公差規格を選択してください。")


def _requires_individual_tolerance(
    standard: str,
    nominal: float,
    kind: str,
) -> bool:
    """Return whether the selected app rule still needs manual input."""

    # Both selectable standards, including the requested JIS-mode exception
    # below 0.5 mm, now resolve every supported C/R bracket automatically.
    return False


def _candidate_tolerance(
    standard: str,
    nominal: float,
    *,
    grade: str,
    kind: str,
    notes: DrawingToleranceNotes,
) -> float | None:
    """Resolve drawing-note exceptions before the selected standard."""

    if kind == "chamfer" and notes.chamfer_tolerance is not None:
        return notes.chamfer_tolerance
    if kind == "radius" and notes.radius_tolerance is not None:
        return notes.radius_tolerance
    return tolerance_for(standard, nominal, grade=grade, kind=kind)


def _map_rotated_rect(
    rect: tuple[float, float, float, float],
    rotation: int,
    width: int,
    height: int,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = rect
    corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    if rotation == 90:
        mapped = [(width - y, x) for x, y in corners]
    elif rotation == 270:
        mapped = [(y, height - x) for x, y in corners]
    else:
        mapped = list(corners)
    return (
        min(point[0] for point in mapped),
        min(point[1] for point in mapped),
        max(point[0] for point in mapped),
        max(point[1] for point in mapped),
    )


def _run_windows_ocr(image_path: Path, script_path: Path) -> dict:
    run_options: dict[str, object] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        run_options["creationflags"] = subprocess.CREATE_NO_WINDOW
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = 0
        run_options["startupinfo"] = startup_info
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-ImagePath",
            str(image_path),
            "-LanguageTag",
            "ja",
        ],
        check=True,
        capture_output=True,
        timeout=90,
        **run_options,
    )
    return json.loads(completed.stdout.decode("utf-8-sig"))


def _run_windows_ocr_batch(
    image_paths: list[Path],
    script_path: Path,
) -> list[dict]:
    """Read several crops per PowerShell process and reuse one OCR engine."""

    if not image_paths:
        return []
    manifest_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="DrawingAssist-OCR-list-",
            encoding="utf-8",
            delete=False,
        ) as manifest:
            json.dump(
                {"paths": [str(path.resolve()) for path in image_paths]},
                manifest,
            )
            manifest_path = Path(manifest.name)
        run_options: dict[str, object] = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            run_options["creationflags"] = subprocess.CREATE_NO_WINDOW
            startup_info = subprocess.STARTUPINFO()
            startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup_info.wShowWindow = 0
            run_options["startupinfo"] = startup_info
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-ImageListPath",
                str(manifest_path),
                "-LanguageTag",
                "ja",
            ],
            check=True,
            capture_output=True,
            timeout=max(90, 25 * len(image_paths)),
            **run_options,
        )
        payload = json.loads(completed.stdout.decode("utf-8-sig"))
        results = payload.get("results") or []
        if len(results) != len(image_paths):
            raise ValueError("Windows OCR batch returned an incomplete result.")
        return list(results)
    finally:
        if manifest_path is not None:
            manifest_path.unlink(missing_ok=True)


def _run_windows_ocr_jobs(
    jobs: list,
    script_path: Path,
    *,
    path_index: int | str,
    max_workers: int = 4,
) -> list[tuple[object, dict]]:
    """Run crop OCR in a few reusable-engine batches, preserving job order."""

    if not jobs:
        return []
    worker_count = min(max_workers, len(jobs))
    indexed = list(enumerate(jobs))
    batches = [indexed[offset::worker_count] for offset in range(worker_count)]

    def read_batch(batch: list[tuple[int, object]]) -> list[tuple[int, dict]]:
        results = _run_windows_ocr_batch(
            [Path(job[path_index]) for _index, job in batch],
            script_path,
        )
        return [
            (index, result)
            for (index, _job), result in zip(batch, results)
        ]

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        indexed_results = [
            pair
            for batch_results in executor.map(read_batch, batches)
            for pair in batch_results
        ]
    by_index = dict(indexed_results)
    return [(job, by_index[index]) for index, job in enumerate(jobs)]


def _map_rotated_tile_point(
    point: tuple[float, float],
    *,
    angle: float,
    source_size: tuple[int, int],
    rotated_size: tuple[int, int],
    tile_origin: tuple[int, int],
) -> fitz.Point:
    """Map a Pillow-rotated tile point back to the full rendered page."""

    radians = math.radians(angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    source_center = fitz.Point(source_size[0] / 2, source_size[1] / 2)
    rotated_center = fitz.Point(rotated_size[0] / 2, rotated_size[1] / 2)
    shifted = fitz.Point(point) - rotated_center
    return fitz.Point(
        cosine * shifted.x - sine * shifted.y
        + source_center.x
        + tile_origin[0],
        sine * shifted.x + cosine * shifted.y
        + source_center.y
        + tile_origin[1],
    )


def _deep_cr_candidates(
    page: fitz.Page,
    page_index: int,
    *,
    standard: str,
    grade: str,
    ocr_script: Path,
    notes: DrawingToleranceNotes,
) -> list[GeneralToleranceCandidate]:
    """Find small diagonal C/R labels with overlapping, rotated OCR tiles.

    Windows OCR reliably reads normal dimensions from a complete drawing, but
    very small chamfer and radius labels are easily lost among nearby outline
    lines. Tight overlapping crops make those labels readable. Only C/R
    patterns are accepted here, so this more sensitive pass stays conservative.
    """

    zoom = max(4.6, min(5.4, 4100 / max(page.rect.width, page.rect.height)))
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        colorspace=fitz.csRGB,
        alpha=False,
        annots=False,
    )
    source_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    image = _prepare_raster_for_ocr(source_image)
    scale_x = image.width / page.rect.width
    scale_y = image.height / page.rect.height

    left = int(image.width * 0.04)
    right = int(image.width * 0.80)
    top = int(image.height * 0.16)
    bottom = int(image.height * 0.90)
    tile_width = max(480, int(125 * scale_x))
    tile_height = max(440, int(112 * scale_y))
    step_x = max(360, int(tile_width * 0.72))
    step_y = max(330, int(tile_height * 0.72))

    tiles: list[tuple[tuple[int, int, int, int], float, Image.Image]] = []
    y = top
    while y < bottom:
        x = left
        y1 = min(image.height, y + tile_height)
        while x < right:
            x1 = min(image.width, x + tile_width)
            crop = image.crop((x, y, x1, y1))
            raw_crop = source_image.crop((x, y, x1, y1))
            # The callout text itself is often horizontal even when its
            # leader is diagonal.  The 0-degree pass recovers labels such as
            # the two C0.2 callouts in a detail view; the diagonal passes keep
            # supporting genuinely rotated text.
            for angle in (0.0,):
                rotated = crop.rotate(angle, expand=True, fillcolor="white")
                minimum_side = min(rotated.size)
                if minimum_side < 900:
                    factor = 900 / max(1, minimum_side)
                    rotated = rotated.resize(
                        (
                            round(rotated.width * factor),
                            round(rotated.height * factor),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                tiles.append(((x, y, x1, y1), angle, rotated))
            # Thresholding is best for pale scans, while an untouched pass is
            # better at retaining the decimal point in labels such as C0.2.
            # Keep one raw horizontal pass; this adds little latency because
            # all images are sent through the existing batched OCR process.
            for raw_angle in (0.0, -45.0, 45.0):
                raw_rotated = raw_crop.rotate(
                    raw_angle, expand=True, fillcolor="white"
                )
                minimum_side = min(raw_rotated.size)
                if minimum_side < 900:
                    factor = 900 / max(1, minimum_side)
                    raw_rotated = raw_rotated.resize(
                        (
                            round(raw_rotated.width * factor),
                            round(raw_rotated.height * factor),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                tiles.append(((x, y, x1, y1), raw_angle, raw_rotated))
            if x1 >= right:
                break
            x += step_x
        if y1 >= bottom:
            break
        y += step_y

    observations: list[
        tuple[
            GeneralToleranceCandidate,
            tuple[int, int, int, int],
            float,
        ]
    ] = []
    cr_pattern = re.compile(r"(?P<prefix>[CR])(?P<body>[O0]?\d{1,3})")
    with tempfile.TemporaryDirectory(prefix="DrawingAssist-CR-") as temp_name:
        temp_dir = Path(temp_name)
        jobs: list[
            tuple[
                tuple[int, int, int, int],
                float,
                tuple[int, int],
                tuple[int, int],
                Path,
            ]
        ] = []
        for index, (bounds, angle, rotated) in enumerate(tiles):
            path = temp_dir / f"cr-{index}.png"
            rotated.save(path)
            jobs.append(
                (
                    bounds,
                    angle,
                    (bounds[2] - bounds[0], bounds[3] - bounds[1]),
                    rotated.size,
                    path,
                )
            )

        results = _run_windows_ocr_jobs(
            jobs,
            ocr_script,
            path_index=4,
            max_workers=4,
        )

    deep_tolerance_rects: list[fitz.Rect] = []
    for job, result in results:
        bounds, angle, source_size, rotated_size, _path = job
        resize_x = rotated_size[0] / max(
            1,
            math.ceil(
                abs(source_size[0] * math.cos(math.radians(angle)))
                + abs(source_size[1] * math.sin(math.radians(angle)))
            ),
        )
        resize_y = rotated_size[1] / max(
            1,
            math.ceil(
                abs(source_size[0] * math.sin(math.radians(angle)))
                + abs(source_size[1] * math.cos(math.radians(angle)))
            ),
        )

        def map_words(
            words: list[dict],
        ) -> tuple[fitz.Rect, list[fitz.Point]] | None:
            if not words:
                return None
            pixel_rect = fitz.Rect(
                min(float(word.get("x") or 0) for word in words) / resize_x,
                min(float(word.get("y") or 0) for word in words) / resize_y,
                max(
                    float(word.get("x") or 0)
                    + float(word.get("width") or 0)
                    for word in words
                )
                / resize_x,
                max(
                    float(word.get("y") or 0)
                    + float(word.get("height") or 0)
                    for word in words
                )
                / resize_y,
            )
            mapped = [
                _map_rotated_tile_point(
                    (point.x, point.y),
                    angle=angle,
                    source_size=source_size,
                    rotated_size=(
                        round(rotated_size[0] / resize_x),
                        round(rotated_size[1] / resize_y),
                    ),
                    tile_origin=(bounds[0], bounds[1]),
                )
                for point in (
                    pixel_rect.top_left,
                    pixel_rect.top_right,
                    pixel_rect.bottom_right,
                    pixel_rect.bottom_left,
                )
            ]
            page_points = [
                fitz.Point(point.x / scale_x, point.y / scale_y)
                for point in mapped
            ]
            rect = fitz.Rect(
                min(point.x for point in page_points),
                min(point.y for point in page_points),
                max(point.x for point in page_points),
                max(point.y for point in page_points),
            ) & page.rect
            return rect, page_points

        for line in result.get("lines") or []:
            line_text = unicodedata.normalize(
                "NFKC", str(line.get("text") or "")
            )
            words = line.get("words") or []
            compact_line_text = re.sub(r"\s+", "", line_text)
            if _EXPLICIT_TOLERANCE.search(line_text) or re.search(
                r"(?:以下|以上|以内|MAX|MIN)", compact_line_text, re.IGNORECASE
            ):
                mapped_tolerance = map_words(words)
                if mapped_tolerance is not None:
                    deep_tolerance_rects.append(mapped_tolerance[0])
                continue
            if (
                not line_text
                or "以下" in line_text
                or "以上" in line_text
            ):
                continue
            compact = re.sub(r"\s+", "", line_text).upper()
            if compact.startswith(("(", "（")):
                continue
            compact = compact.replace("Ｃ", "C").replace("Ｒ", "R")
            compact = (
                compact.replace("〇", "0")
                .replace("Ｏ", "0")
                .replace(".", "")
                .replace(",", "")
            )
            match = cr_pattern.search(compact)
            if match is None:
                continue
            prefix = match.group("prefix")
            body = match.group("body")
            if body[0] in {"O", "0"} and len(body) > 1:
                nominal_text = f"0.{body[1:]}"
            else:
                nominal_text = body
            try:
                nominal = float(nominal_text)
            except ValueError:
                continue
            if nominal <= 0:
                continue
            kind = "chamfer" if prefix == "C" else "radius"
            tolerance = _candidate_tolerance(
                standard,
                nominal,
                grade=grade,
                kind=kind,
                notes=notes,
            )
            manual_required = (
                tolerance is None
                and _requires_individual_tolerance(
                    standard,
                    nominal,
                    kind,
                )
            )
            if tolerance is None and not manual_required:
                continue
            mapped_candidate = map_words(words)
            if mapped_candidate is None:
                continue
            rect, page_points = mapped_candidate
            if rect.is_empty or rect.width > 75 or rect.height > 75:
                continue
            direction = (
                math.cos(math.radians(angle)),
                math.sin(math.radians(angle)),
            )
            candidate = GeneralToleranceCandidate(
                page_index=page_index,
                rect=tuple(rect),
                direction=direction,
                source_text=f"{prefix}{nominal_text}",
                nominal_value=nominal,
                kind=kind,
                tolerance=tolerance or 0.0,
                tolerance_text=(
                    _format_tolerance(tolerance)
                    if tolerance is not None
                    else ""
                ),
                selected=not manual_required,
                quad=tuple((float(point.x), float(point.y)) for point in page_points),
                manual_required=manual_required,
            )
            # A real C/R prefix is not a geometric-tolerance frame.  On pale
            # scans the nearby leader and work outline can look rectangular,
            # and the generic frame detector used to discard valid C0.2
            # callouts (notably the centre callout in the driver drawing).
            # Limit callouts are rejected below from OCR text ("以下",
            # "MAX", etc.).  The visual-only suffix detector is deliberately
            # not used here: diagonal leaders in scanned drawings look like a
            # suffix and previously removed valid C0.2 labels.
            observations.append((candidate, bounds, angle))

    # A tiny diagonal stroke can occasionally be hallucinated as C/R text by
    # one OCR crop.  Real labels recur in overlapping tiles (often at more
    # than one rotation), so require independent agreement before exposing a
    # candidate to the user.
    candidate_groups: list[
        list[
            tuple[
                GeneralToleranceCandidate,
                tuple[int, int, int, int],
                float,
            ]
        ]
    ] = []
    for observation in observations:
        candidate = observation[0]
        for group in candidate_groups:
            if _same_candidate(candidate, group[0][0]):
                group.append(observation)
                break
        else:
            candidate_groups.append([observation])

    candidates: list[GeneralToleranceCandidate] = []
    for group in candidate_groups:
        independent_reads = {
            (bounds[0], bounds[1], round(angle))
            for _candidate, bounds, angle in group
        }
        representative = group[0][0]
        # Small C/R labels are the most frequently omitted dimensions in an
        # image PDF.  Their prefix makes a single read substantially safer
        # than a bare number, and every result remains reviewable before
        # applying.  Larger values still require overlapping-tile consensus.
        minimum_reads = 1 if representative.nominal_value <= 0.5 else 2
        if len(independent_reads) < minimum_reads:
            continue
        candidates.append(
            min(
                (observation[0] for observation in group),
                key=lambda item: (
                    "." not in item.source_text and "," not in item.source_text,
                    fitz.Rect(item.rect).get_area(),
                ),
            )
        )
    # The same raster label is often read once with and once without its
    # decimal point (for example 14.7 / 147). Prefer the decimal reading at
    # an overlapping location and keep only one physical label.
    deduplicated: list[GeneralToleranceCandidate] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (
            "." not in item.source_text and "," not in item.source_text,
            fitz.Rect(item.rect).get_area(),
            item.rect[1],
            item.rect[0],
        ),
    ):
        candidate_rect = fitz.Rect(candidate.rect)
        if any(
            (candidate_rect & tolerance_rect).get_area()
            / max(candidate_rect.get_area(), 1e-9)
            >= 0.25
            for tolerance_rect in deep_tolerance_rects
        ):
            continue
        if any(
            (
                (candidate_rect & fitz.Rect(existing.rect)).get_area()
                / max(
                    1e-9,
                    min(candidate_rect.get_area(), fitz.Rect(existing.rect).get_area()),
                )
                >= 0.35
                or math.dist(
                    (
                        (candidate_rect.x0 + candidate_rect.x1) / 2,
                        (candidate_rect.y0 + candidate_rect.y1) / 2,
                    ),
                    (
                        (fitz.Rect(existing.rect).x0 + fitz.Rect(existing.rect).x1) / 2,
                        (fitz.Rect(existing.rect).y0 + fitz.Rect(existing.rect).y1) / 2,
                    ),
                )
                <= max(
                    18.0,
                    max(candidate_rect.width, candidate_rect.height) * 1.1,
                    max(fitz.Rect(existing.rect).width, fitz.Rect(existing.rect).height)
                    * 1.1,
                )
            )
            for existing in deduplicated
        ):
            continue
        deduplicated.append(candidate)
    return deduplicated


def _tiled_dimension_candidates(
    page: fitz.Page,
    page_index: int,
    *,
    standard: str,
    grade: str,
    ocr_script: Path,
    angle_override: tuple[float, str] | None,
    notes: DrawingToleranceNotes,
) -> list[GeneralToleranceCandidate]:
    """Read small raster dimensions from overlapping high-resolution tiles.

    A whole A4 drawing is too dense for Windows OCR: dimensions only a few
    millimetres high are merged with leaders or omitted.  Overlapping tiles
    keep those labels large, while four tile rotations cover horizontal,
    vertical and common diagonal callouts.  Every OCR rectangle is mapped
    back through the tile rotation before it becomes a page-space candidate.
    """

    zoom = max(4.6, min(5.2, 4200 / max(page.rect.width, page.rect.height)))
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        colorspace=fitz.csRGB,
        alpha=False,
        annots=False,
    )
    source_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    image = _prepare_raster_for_ocr(source_image)
    scale_x = image.width / page.rect.width
    scale_y = image.height / page.rect.height
    tile_width = min(image.width, max(1250, int(330 * scale_x)))
    tile_height = min(image.height, max(1050, int(265 * scale_y)))
    step_x = max(850, int(tile_width * 0.78))
    step_y = max(720, int(tile_height * 0.76))
    left = int(image.width * 0.025)
    right = int(image.width * 0.94)
    top = int(image.height * 0.09)
    bottom = int(image.height * 0.90)

    tiles: list[tuple[tuple[int, int, int, int], float, Image.Image]] = []
    y = top
    while y < bottom:
        x = left
        y1 = min(image.height, y + tile_height)
        while x < right:
            x1 = min(image.width, x + tile_width)
            crop = image.crop((x, y, x1, y1))
            for angle in (0.0, 90.0, -45.0, 45.0):
                rotated = crop.rotate(angle, expand=True, fillcolor="white")
                tiles.append(((x, y, x1, y1), angle, rotated))
            if x1 >= right:
                break
            x += step_x
        if y1 >= bottom:
            break
        y += step_y

    with tempfile.TemporaryDirectory(prefix="DrawingAssist-Tiles-") as temp_name:
        temp_dir = Path(temp_name)
        jobs: list[
            tuple[
                tuple[int, int, int, int],
                float,
                tuple[int, int],
                tuple[int, int],
                Path,
            ]
        ] = []
        for index, (bounds, angle, rotated) in enumerate(tiles):
            path = temp_dir / f"tile-{index}.png"
            rotated.save(path)
            jobs.append(
                (
                    bounds,
                    angle,
                    (bounds[2] - bounds[0], bounds[3] - bounds[1]),
                    rotated.size,
                    path,
                )
            )

        results = _run_windows_ocr_jobs(
            jobs,
            ocr_script,
            path_index=4,
            max_workers=4,
        )

    candidates: list[GeneralToleranceCandidate] = []
    for job, result in results:
        bounds, angle, source_size, rotated_size, _path = job
        for line in result.get("lines") or []:
            line_text = unicodedata.normalize("NFKC", str(line.get("text") or ""))
            if (
                not line_text
                or _EXPLICIT_TOLERANCE.search(line_text)
                or _NON_DIMENSION_CONTEXT.search(line_text)
                or "以下" in line_text
                or "以上" in line_text
            ):
                continue
            words = line.get("words") or []
            if not words:
                continue
            compact = _normalize_raster_dimension_text(line_text)
            compact = re.sub(r"^[のΦＯ](?=\d)", "φ", compact)
            match = _NUMBER_PATTERN.fullmatch(compact)
            if match is None:
                continue
            prefix = match.group("prefix")
            degree = match.group("degree")
            kind = _candidate_kind(prefix, degree)
            if kind is None:
                continue
            number_text = match.group("number")
            try:
                nominal = float(number_text.replace(",", "."))
            except ValueError:
                continue
            if nominal <= 0 or nominal > 4000:
                continue
            if _reject_unreliable_dimension(
                kind=kind,
                prefix=prefix,
                degree=degree,
                nominal=nominal,
                compact=compact,
                supplemental=True,
            ):
                continue
            if not prefix and not degree and nominal >= 1000:
                continue
            if kind == "angle":
                tolerance, tolerance_text = (
                    angle_override
                    or angle_tolerance(10, standard=standard, grade=grade)
                )
            else:
                tolerance = _candidate_tolerance(
                    standard,
                    nominal,
                    grade=grade,
                    kind=kind,
                    notes=notes,
                )
                tolerance_text = _format_tolerance(tolerance) if tolerance is not None else ""
            manual_required = (
                tolerance is None
                and _requires_individual_tolerance(
                    standard,
                    nominal,
                    kind,
                )
            )
            if tolerance is None and not manual_required:
                continue

            pixel_rect = fitz.Rect(
                min(float(word.get("x") or 0) for word in words),
                min(float(word.get("y") or 0) for word in words),
                max(float(word.get("x") or 0) + float(word.get("width") or 0) for word in words),
                max(float(word.get("y") or 0) + float(word.get("height") or 0) for word in words),
            )
            mapped = [
                _map_rotated_tile_point(
                    (point.x, point.y),
                    angle=angle,
                    source_size=source_size,
                    rotated_size=rotated_size,
                    tile_origin=(bounds[0], bounds[1]),
                )
                for point in (
                    pixel_rect.top_left,
                    pixel_rect.top_right,
                    pixel_rect.bottom_right,
                    pixel_rect.bottom_left,
                )
            ]
            page_points = [
                fitz.Point(point.x / scale_x, point.y / scale_y)
                for point in mapped
            ]
            rect = fitz.Rect(
                min(point.x for point in page_points),
                min(point.y for point in page_points),
                max(point.x for point in page_points),
                max(point.y for point in page_points),
            ) & page.rect
            if (
                rect.is_empty
                or rect.width > 85
                or rect.height > 85
                or rect.y1 < page.rect.height * 0.11
                or rect.y0 > page.rect.height * 0.82
                or _is_non_dimension_region(
                    rect,
                    page.rect,
                    bare_only=not prefix and not degree,
                )
            ):
                continue
            direction = (
                math.cos(math.radians(angle)),
                math.sin(math.radians(angle)),
            )
            if _is_visual_parenthetical(
                image,
                rect,
                direction,
                scale_x=scale_x,
                scale_y=scale_y,
            ):
                if not _has_dimension_line_support(
                    image,
                    rect,
                    direction,
                    kind,
                    scale_x=scale_x,
                    scale_y=scale_y,
                ):
                    continue
            elif _is_feature_control_frame(
                image,
                rect,
                direction,
                scale_x=scale_x,
                scale_y=scale_y,
            ):
                continue
            if kind in {"chamfer", "radius"} and _has_trailing_limit_text_visual(
                image,
                rect,
                direction,
                scale_x=scale_x,
                scale_y=scale_y,
            ):
                continue
            if not _has_dimension_line_support(
                image,
                rect,
                direction,
                kind,
                scale_x=scale_x,
                scale_y=scale_y,
            ):
                continue
            candidate = GeneralToleranceCandidate(
                page_index=page_index,
                rect=tuple(rect),
                direction=direction,
                source_text=compact,
                nominal_value=nominal,
                kind=kind,
                tolerance=tolerance or 0.0,
                tolerance_text=tolerance_text,
                selected=not manual_required,
                quad=tuple((float(point.x), float(point.y)) for point in page_points),
                manual_required=manual_required,
            )
            if not any(_same_candidate(candidate, existing) for existing in candidates):
                candidates.append(candidate)
    deduplicated: list[GeneralToleranceCandidate] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (
            "." not in item.source_text and "," not in item.source_text,
            item.rect[1],
            item.rect[0],
        ),
    ):
        candidate_rect = fitz.Rect(candidate.rect)
        if any(
            (
                (candidate_rect & fitz.Rect(existing.rect)).get_area()
                / max(
                    1e-9,
                    min(candidate_rect.get_area(), fitz.Rect(existing.rect).get_area()),
                )
                >= 0.35
            )
            for existing in deduplicated
        ):
            continue
        deduplicated.append(candidate)
    return deduplicated


def _candidate_kind(prefix: str, degree: str) -> str | None:
    if degree:
        return "angle"
    if prefix in {"φ", "Φ", "Ø", "⌀"}:
        return "diameter"
    if prefix.upper() == "C":
        return "chamfer"
    if prefix.upper() == "R":
        return "radius"
    return "linear"


def _oriented_quad(
    points: list[fitz.Point],
    direction: tuple[float, float],
) -> tuple[tuple[float, float], ...] | None:
    if not points:
        return None
    axis = fitz.Point(direction)
    length = math.hypot(axis.x, axis.y) or 1.0
    axis /= length
    normal = fitz.Point(-axis.y, axis.x)
    along = [point.x * axis.x + point.y * axis.y for point in points]
    across = [point.x * normal.x + point.y * normal.y for point in points]

    def page_point(along_value: float, across_value: float) -> tuple[float, float]:
        point = axis * along_value + normal * across_value
        return (float(point.x), float(point.y))

    return (
        page_point(min(along), min(across)),
        page_point(max(along), min(across)),
        page_point(max(along), max(across)),
        page_point(min(along), max(across)),
    )


def _is_full_page_image(page: fitz.Page) -> bool:
    page_area = page.rect.get_area()
    if page_area <= 0:
        return False
    image_areas = [
        (fitz.Rect(info["bbox"]) & page.rect).get_area()
        for info in page.get_image_info()
        if len(info.get("bbox") or ()) == 4
    ]
    return (
        any(area / page_area >= 0.72 for area in image_areas)
        or sum(image_areas) / page_area >= 0.72
    )


def _is_non_dimension_region(
    rect: fitz.Rect,
    page: fitz.Rect,
    *,
    bare_only: bool = False,
) -> bool:
    """表題欄・改訂欄・ゾーン番号など寸法ではない領域を判定する。"""

    rel_x0 = rect.x0 / page.width
    rel_y0 = rect.y0 / page.height
    rel_x1 = rect.x1 / page.width
    rel_y1 = rect.y1 / page.height
    if rel_y1 < 0.10:
        return True
    if rel_y0 > 0.80:
        return True
    if bare_only and rel_y1 < 0.15:
        return True
    if bare_only and rel_y0 > 0.72:
        return True
    if rel_x0 > 0.62 and rel_y0 > 0.68:
        return True
    if rel_x0 > 0.84 and rel_y1 < 0.50:
        return True
    if bare_only and rel_x1 < 0.18 and 0.30 < rel_y0 < 0.60:
        return True
    return False


def _dark_spread_ratio(
    image: Image.Image,
    rect: fitz.Rect,
    *,
    scale_x: float,
    scale_y: float,
    spread_axis: str,
) -> float:
    x0 = max(0, int(math.floor(rect.x0 * scale_x)))
    y0 = max(0, int(math.floor(rect.y0 * scale_y)))
    x1 = min(image.width, int(math.ceil(rect.x1 * scale_x)))
    y1 = min(image.height, int(math.ceil(rect.y1 * scale_y)))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    crop = image.crop((x0, y0, x1, y1)).convert("L")
    dark = [
        (x, y)
        for y in range(crop.height)
        for x in range(crop.width)
        if crop.getpixel((x, y)) < 180
    ]
    if not dark:
        return 0.0
    if spread_axis == "y":
        values = [point[1] for point in dark]
        available = crop.height
    else:
        values = [point[0] for point in dark]
        available = crop.width
    return (max(values) - min(values) + 1) / max(1, available)


def _is_visual_parenthetical(
    image: Image.Image,
    rect: fitz.Rect,
    direction: tuple[float, float],
    *,
    scale_x: float,
    scale_y: float,
) -> bool:
    """Detect reference dimensions whose numeric value is inside ( )."""

    dx, dy = direction
    if abs(dx) >= 0.92:
        # Parentheses sit immediately beside the value. A wider probe can
        # mistake inspection diamonds and nearby angular arcs for brackets
        # (notably the detail-view 0.12 callout).
        side_width = max(3.2, rect.height * 0.72)
        y_padding = rect.height * 0.12
        left = fitz.Rect(
            rect.x0 - side_width,
            rect.y0 - y_padding,
            rect.x0 - 0.1,
            rect.y1 + y_padding,
        )
        right = fitz.Rect(
            rect.x1 + 0.1,
            rect.y0 - y_padding,
            rect.x1 + side_width,
            rect.y1 + y_padding,
        )
        return (
            _dark_spread_ratio(
                image,
                left,
                scale_x=scale_x,
                scale_y=scale_y,
                spread_axis="y",
            )
            >= 0.55
            and _dark_spread_ratio(
                image,
                right,
                scale_x=scale_x,
                scale_y=scale_y,
                spread_axis="y",
            )
            >= 0.55
        )
    if abs(dy) >= 0.92:
        side_height = max(3.2, rect.width * 0.72)
        x_padding = rect.width * 0.12
        top = fitz.Rect(
            rect.x0 - x_padding,
            rect.y0 - side_height,
            rect.x1 + x_padding,
            rect.y0 - 0.1,
        )
        bottom = fitz.Rect(
            rect.x0 - x_padding,
            rect.y1 + 0.1,
            rect.x1 + x_padding,
            rect.y1 + side_height,
        )
        return (
            _dark_spread_ratio(
                image,
                top,
                scale_x=scale_x,
                scale_y=scale_y,
                spread_axis="x",
            )
            >= 0.55
            and _dark_spread_ratio(
                image,
                bottom,
                scale_x=scale_x,
                scale_y=scale_y,
                spread_axis="x",
            )
            >= 0.55
        )
    return False


def _line_support_score(
    image: Image.Image,
    rect: fitz.Rect,
    *,
    scale_x: float,
    scale_y: float,
    line_axis: str,
) -> float:
    x0 = max(0, int(math.floor(rect.x0 * scale_x)))
    y0 = max(0, int(math.floor(rect.y0 * scale_y)))
    x1 = min(image.width, int(math.ceil(rect.x1 * scale_x)))
    y1 = min(image.height, int(math.ceil(rect.y1 * scale_y)))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    crop = image.crop((x0, y0, x1, y1)).convert("L")
    if line_axis == "x":
        return max(
            (
                sum(crop.getpixel((x, y)) < 170 for x in range(crop.width))
                / max(1, crop.width)
                for y in range(crop.height)
            ),
            default=0.0,
        )
    return max(
        (
            sum(crop.getpixel((x, y)) < 170 for y in range(crop.height))
            / max(1, crop.height)
            for x in range(crop.width)
        ),
        default=0.0,
    )


def _has_dimension_line_support(
    image: Image.Image,
    rect: fitz.Rect,
    direction: tuple[float, float],
    kind: str,
    *,
    scale_x: float,
    scale_y: float,
    strict: bool = False,
) -> bool:
    dx, dy = direction
    if abs(dx) >= 0.92:
        text_height = max(1.0, rect.height)
        left = fitz.Rect(
            rect.x0 - text_height * 4,
            rect.y0 - text_height * 0.6,
            rect.x0 - text_height * 0.3,
            rect.y1 + text_height * 0.6,
        )
        right = fitz.Rect(
            rect.x1 + text_height * 0.3,
            rect.y0 - text_height * 0.6,
            rect.x1 + text_height * 4,
            rect.y1 + text_height * 0.6,
        )
        scores = (
            _line_support_score(
                image,
                left,
                scale_x=scale_x,
                scale_y=scale_y,
                line_axis="x",
            ),
            _line_support_score(
                image,
                right,
                scale_x=scale_x,
                scale_y=scale_y,
                line_axis="x",
            ),
        )
    elif abs(dy) >= 0.92:
        text_width = max(1.0, rect.width)
        top = fitz.Rect(
            rect.x0 - text_width * 0.6,
            rect.y0 - text_width * 4,
            rect.x1 + text_width * 0.6,
            rect.y0 - text_width * 0.3,
        )
        bottom = fitz.Rect(
            rect.x0 - text_width * 0.6,
            rect.y1 + text_width * 0.3,
            rect.x1 + text_width * 0.6,
            rect.y1 + text_width * 4,
        )
        scores = (
            _line_support_score(
                image,
                top,
                scale_x=scale_x,
                scale_y=scale_y,
                line_axis="y",
            ),
            _line_support_score(
                image,
                bottom,
                scale_x=scale_x,
                scale_y=scale_y,
                line_axis="y",
            ),
        )
    else:
        # 斜め配置の長さ寸法は水平・垂直の両方で寸法線を探す。
        text_height = max(1.0, rect.height)
        text_width = max(1.0, rect.width)
        horizontal_scores = (
            _line_support_score(
                image,
                fitz.Rect(
                    rect.x0 - text_height * 4,
                    rect.y0 - text_height * 0.6,
                    rect.x0 - text_height * 0.3,
                    rect.y1 + text_height * 0.6,
                ),
                scale_x=scale_x,
                scale_y=scale_y,
                line_axis="x",
            ),
            _line_support_score(
                image,
                fitz.Rect(
                    rect.x1 + text_height * 0.3,
                    rect.y0 - text_height * 0.6,
                    rect.x1 + text_height * 4,
                    rect.y1 + text_height * 0.6,
                ),
                scale_x=scale_x,
                scale_y=scale_y,
                line_axis="x",
            ),
        )
        vertical_scores = (
            _line_support_score(
                image,
                fitz.Rect(
                    rect.x0 - text_width * 0.6,
                    rect.y0 - text_width * 4,
                    rect.x1 + text_width * 0.6,
                    rect.y0 - text_width * 0.3,
                ),
                scale_x=scale_x,
                scale_y=scale_y,
                line_axis="y",
            ),
            _line_support_score(
                image,
                fitz.Rect(
                    rect.x0 - text_width * 0.6,
                    rect.y1 + text_width * 0.3,
                    rect.x1 + text_width * 0.6,
                    rect.y1 + text_width * 4,
                ),
                scale_x=scale_x,
                scale_y=scale_y,
                line_axis="y",
            ),
        )
        scores = horizontal_scores + vertical_scores
    if kind in {"chamfer", "radius", "angle"}:
        threshold = 0.28 if strict else 0.24
        return max(scores) >= threshold
    if strict:
        # ハッチングの片側線だけで誤検出しないよう、両側に寸法線の痕跡を要求する。
        return min(scores) >= 0.10 and max(scores) >= 0.22
    # スキャン図面では片側の寸法線しか拾えないことが多い。
    return max(scores) >= 0.17


def _is_feature_control_frame(
    image: Image.Image,
    rect: fitz.Rect,
    direction: tuple[float, float],
    *,
    scale_x: float,
    scale_y: float,
) -> bool:
    """Return whether OCR text is enclosed by a geometric-tolerance cell.

    A feature-control frame has horizontal rules both above and below the
    value and a vertical divider/border beside it. Dimension and leader lines
    may provide one of those signals, but not the closed three-sided pattern.
    """

    dx, dy = direction
    if abs(dx) < 0.92 or abs(dy) > 0.38 or rect.is_empty:
        return False
    height = max(1.0, rect.height)
    width = max(1.0, rect.width)
    horizontal_x0 = rect.x0 - height * 1.25
    horizontal_x1 = rect.x1 + height * 1.25
    top = fitz.Rect(
        horizontal_x0,
        rect.y0 - height * 1.65,
        horizontal_x1,
        rect.y0 - height * 0.10,
    )
    bottom = fitz.Rect(
        horizontal_x0,
        rect.y1 + height * 0.10,
        horizontal_x1,
        rect.y1 + height * 1.65,
    )
    vertical_height = max(height * 3.1, width * 0.72)
    left = fitz.Rect(
        rect.x0 - height * 1.8,
        (rect.y0 + rect.y1 - vertical_height) / 2,
        rect.x0 - height * 0.08,
        (rect.y0 + rect.y1 + vertical_height) / 2,
    )
    right = fitz.Rect(
        rect.x1 + height * 0.08,
        (rect.y0 + rect.y1 - vertical_height) / 2,
        rect.x1 + height * 1.8,
        (rect.y0 + rect.y1 + vertical_height) / 2,
    )
    top_score = _line_support_score(
        image,
        top,
        scale_x=scale_x,
        scale_y=scale_y,
        line_axis="x",
    )
    bottom_score = _line_support_score(
        image,
        bottom,
        scale_x=scale_x,
        scale_y=scale_y,
        line_axis="x",
    )
    side_score = max(
        _line_support_score(
            image,
            left,
            scale_x=scale_x,
            scale_y=scale_y,
            line_axis="y",
        ),
        _line_support_score(
            image,
            right,
            scale_x=scale_x,
            scale_y=scale_y,
            line_axis="y",
        ),
    )
    return top_score >= 0.48 and bottom_score >= 0.48 and side_score >= 0.52


def _has_trailing_limit_text_visual(
    image: Image.Image,
    rect: fitz.Rect,
    direction: tuple[float, float],
    *,
    scale_x: float,
    scale_y: float,
) -> bool:
    """Detect OCR-dropped trailing Japanese limit text such as ``以下``."""

    dx, dy = direction
    if abs(dx) < 0.92 or abs(dy) > 0.35 or rect.is_empty:
        return False
    height = max(1.0, rect.height)
    if dx >= 0:
        trailing = fitz.Rect(
            rect.x1 + height * 0.05,
            rect.y0 - height * 0.08,
            rect.x1 + height * 3.0,
            rect.y1 + height * 0.08,
        )
    else:
        trailing = fitz.Rect(
            rect.x0 - height * 3.0,
            rect.y0 - height * 0.08,
            rect.x0 - height * 0.05,
            rect.y1 + height * 0.08,
        )
    x0 = max(0, int(math.floor(trailing.x0 * scale_x)))
    y0 = max(0, int(math.floor(trailing.y0 * scale_y)))
    x1 = min(image.width, int(math.ceil(trailing.x1 * scale_x)))
    y1 = min(image.height, int(math.ceil(trailing.y1 * scale_y)))
    if x1 <= x0 or y1 <= y0:
        return False
    crop = image.crop((x0, y0, x1, y1)).convert("L")
    active = [
        sum(crop.getpixel((x, y)) < 170 for y in range(crop.height)) >= 2
        for x in range(crop.width)
    ]
    minimum_group = max(2, round(height * scale_x * 0.12))
    groups = 0
    start: int | None = None
    for index, value in enumerate((*active, False)):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= minimum_group:
                groups += 1
            start = None
    # ``以下`` normally contributes two separated glyph groups. A leader is
    # one continuous group and therefore remains eligible as a dimension.
    return groups >= 2


def _native_text_candidates(
    page: fitz.Page,
    page_index: int,
    *,
    standard: str,
    grade: str,
    angle_shorter_side_length: float,
) -> list[GeneralToleranceCandidate]:
    """Read selectable CAD text without OCR and reject existing tolerances."""

    from drawing_assist.pdf_editor import (
        _raw_text_lines,
        find_text_group,
        infer_dimension_style,
    )

    render_zoom = 5.0
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(render_zoom, render_zoom),
        colorspace=fitz.csGRAY,
        alpha=False,
        annots=False,
    )
    image = Image.frombytes(
        "L",
        (pixmap.width, pixmap.height),
        pixmap.samples,
    )
    scale_x = image.width / page.rect.width
    scale_y = image.height / page.rect.height
    expected_size = infer_dimension_style(page).font_size
    notes = extract_drawing_tolerance_notes(page.get_text())
    text_lines = _raw_text_lines(page)
    angle_value, angle_text = (
        notes.angle_tolerance
        or angle_tolerance(
            angle_shorter_side_length,
            standard=standard,
            grade=grade,
        )
    )
    candidates: list[GeneralToleranceCandidate] = []
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans") or []
            chars = [
                char
                for span in spans
                for char in span.get("chars", [])
            ]
            raw_text = "".join(char.get("c", "") for char in chars)
            text = unicodedata.normalize("NFKC", raw_text).strip()
            if not text or not any(character.isdigit() for character in text):
                continue
            if (
                _NON_DIMENSION_CONTEXT.search(text)
                or "以下" in text
                or "以上" in text
                or "超え" in text
                or "。" in text
            ):
                continue
            match = _NUMBER_PATTERN.match(text)
            if match is None:
                continue
            suffix = text[match.end() :].strip()
            if suffix and not re.fullmatch(r"[（(][^0-9]*[）)]", suffix):
                continue
            prefix = match.group("prefix")
            degree = match.group("degree")
            try:
                nominal = float(match.group("number").replace(",", "."))
            except ValueError:
                continue
            if nominal <= 0 or nominal > 4000:
                continue
            core_chars = [
                char
                for char in chars
                if unicodedata.normalize(
                    "NFKC",
                    char.get("c", ""),
                )
                in "0123456789.,φΦØ⌀CRCＲＣ"
                + "°"
            ]
            if not core_chars:
                continue
            core_rect = fitz.Rect(core_chars[0]["bbox"])
            for char in core_chars[1:]:
                core_rect |= fitz.Rect(char["bbox"])
            if (
                core_rect.y1 < page.rect.height * 0.11
                or core_rect.y0 > page.rect.height * 0.82
            ):
                continue
            font_size = max(
                (float(span.get("size") or 0) for span in spans),
                default=expected_size,
            )
            if not expected_size * 0.74 <= font_size <= expected_size * 1.13:
                continue
            direction_value = line.get("dir") or (1.0, 0.0)
            direction = (float(direction_value[0]), float(direction_value[1]))
            core_quad_points: list[fitz.Point] = []
            for char in core_chars:
                try:
                    recovered = fitz.recover_char_quad(
                        direction_value,
                        char,
                    )
                    core_quad_points.extend(
                        (recovered.ul, recovered.ur, recovered.lr, recovered.ll)
                    )
                except (TypeError, ValueError):
                    char_rect = fitz.Rect(char["bbox"])
                    core_quad_points.extend(
                        (
                            char_rect.top_left,
                            char_rect.top_right,
                            char_rect.bottom_right,
                            char_rect.bottom_left,
                        )
                    )
            core_quad = _oriented_quad(core_quad_points, direction)
            if not suffix and _is_visual_parenthetical(
                image,
                core_rect,
                direction,
                scale_x=scale_x,
                scale_y=scale_y,
            ):
                continue
            center = fitz.Point(
                (core_rect.x0 + core_rect.x1) / 2,
                (core_rect.y0 + core_rect.y1) / 2,
            )
            hit = find_text_group(page, center, text_lines=text_lines)
            if hit is not None:
                hit_text = unicodedata.normalize("NFKC", hit.text)
                hit_nominal = unicodedata.normalize(
                    "NFKC",
                    hit.nominal_text,
                ).strip()
                remainder = hit_text.replace(hit_nominal, "", 1)
                if _EXPLICIT_TOLERANCE.search(remainder):
                    continue
                if not prefix and hit.preserved_prefix:
                    prefix = hit.preserved_prefix
            kind = _candidate_kind(prefix, degree)
            if kind is None:
                continue
            if kind == "angle":
                tolerance = angle_value
                tolerance_text = angle_text
            else:
                tolerance = _candidate_tolerance(
                    standard,
                    nominal,
                    grade=grade,
                    kind=kind,
                    notes=notes,
                )
                tolerance_text = (
                    _format_tolerance(tolerance)
                    if tolerance is not None
                    else ""
                )
            manual_required = (
                tolerance is None
                and _requires_individual_tolerance(
                    standard,
                    nominal,
                    kind,
                )
            )
            if tolerance is None and not manual_required:
                continue
            candidate = GeneralToleranceCandidate(
                page_index=page_index,
                rect=tuple(core_rect),
                direction=direction,
                source_text=(
                    "φ"
                    if kind == "diameter"
                    else "C"
                    if kind == "chamfer"
                    else "R"
                    if kind == "radius"
                    else match.group("number") + "°"
                    if kind == "angle"
                    else ""
                )
                + ("" if kind == "angle" else match.group("number")),
                nominal_value=nominal,
                kind=kind,
                tolerance=tolerance or 0.0,
                tolerance_text=tolerance_text,
                selected=not manual_required,
                quad=core_quad,
                manual_required=manual_required,
            )
            if not any(
                _same_candidate(candidate, existing)
                for existing in candidates
            ):
                candidates.append(candidate)
    return sorted(candidates, key=lambda item: (item.rect[1], item.rect[0]))


def _word_candidate(
    word: dict,
    line_text: str,
    *,
    rotation: int,
    image_width: int,
    image_height: int,
    page: fitz.Page,
    page_index: int,
    standard: str,
    grade: str,
    angle_override: tuple[float, str] | None = None,
    notes: DrawingToleranceNotes | None = None,
) -> GeneralToleranceCandidate | None:
    notes = notes or DrawingToleranceNotes()
    raw = unicodedata.normalize("NFKC", str(word.get("text") or "")).strip()
    line = unicodedata.normalize("NFKC", line_text)
    if not raw or _EXPLICIT_TOLERANCE.search(line) or _NON_DIMENSION_CONTEXT.search(line):
        return None
    match = _NUMBER_PATTERN.fullmatch(raw)
    if match is None:
        return None
    prefix = match.group("prefix")
    degree = match.group("degree")
    kind = _candidate_kind(prefix, degree)
    if kind is None:
        return None
    try:
        number_text = match.group("number")
        nominal = float(number_text.replace(",", "."))
    except ValueError:
        return None
    if (
        len(number_text) > 1
        and number_text.startswith("0")
        and "." not in number_text
        and "," not in number_text
    ):
        return None
    if nominal <= 0 or nominal > 4000:
        return None
    # Bare four-digit integers are overwhelmingly drawing numbers, dates, or
    # counts. Decimal and prefixed values remain eligible.
    if not prefix and not degree and "." not in raw and "," not in raw and nominal >= 1000:
        return None
    if kind == "angle":
        tolerance, tolerance_text = (
            angle_override
            or angle_tolerance(10, standard=standard, grade=grade)
        )
    else:
        tolerance = _candidate_tolerance(
            standard,
            nominal,
            grade=grade,
            kind=kind,
            notes=notes,
        )
        tolerance_text = (
            _format_tolerance(tolerance)
            if tolerance is not None
            else ""
        )
    manual_required = (
        tolerance is None
        and _requires_individual_tolerance(
            standard,
            nominal,
            kind,
        )
    )
    if tolerance is None and not manual_required:
        return None
    x = float(word.get("x") or 0)
    y = float(word.get("y") or 0)
    width = float(word.get("width") or 0)
    height = float(word.get("height") or 0)
    if width <= 1 or height <= 1:
        return None
    pixel_rect = _map_rotated_rect(
        (x, y, x + width, y + height),
        rotation,
        image_width,
        image_height,
    )
    scale_x = page.rect.width / image_width
    scale_y = page.rect.height / image_height
    rect = (
        pixel_rect[0] * scale_x,
        pixel_rect[1] * scale_y,
        pixel_rect[2] * scale_x,
        pixel_rect[3] * scale_y,
    )
    page_rect = fitz.Rect(rect)
    # Drawing title blocks and zone coordinates generate many plausible
    # numbers but are not dimensions. Keep a conservative central drawing
    # area; edge cases can still be handled with the existing manual tools.
    if page_rect.y1 < page.rect.height * 0.11 or page_rect.y0 > page.rect.height * 0.82:
        return None
    direction = {
        0: (1.0, 0.0),
        90: (0.0, 1.0),
        270: (0.0, -1.0),
    }[rotation]
    if rotation in {90, 270} and page_rect.height < page_rect.width * 1.20:
        return None
    # Single bare digits are too ambiguous (zone numbers, item balloons,
    # counters). Prefixed and decimal dimensions remain eligible.
    if not prefix and not degree and "." not in raw and "," not in raw and len(raw) < 2:
        return None
    if not prefix and not degree and nominal in {90, 180, 360}:
        return None
    expanded = fitz.Rect(page_rect)
    expanded.x0 -= 12
    expanded.y0 -= 12
    expanded.x1 += 12
    expanded.y1 += 12
    for pdf_word in page.get_text("words"):
        pdf_text = unicodedata.normalize("NFKC", str(pdf_word[4]))
        if not _EXPLICIT_TOLERANCE.search(pdf_text):
            continue
        if not (fitz.Rect(pdf_word[:4]) & expanded).is_empty:
            return None
    return GeneralToleranceCandidate(
        page_index=page_index,
        rect=rect,
        direction=direction,
        source_text=raw,
        nominal_value=nominal,
        kind=kind,
        tolerance=tolerance or 0.0,
        tolerance_text=tolerance_text,
        selected=not manual_required,
        quad=_oriented_quad(
            [
                page_rect.top_left,
                page_rect.top_right,
                page_rect.bottom_right,
                page_rect.bottom_left,
            ],
            direction,
        ),
        manual_required=manual_required,
    )


def _same_candidate(
    first: GeneralToleranceCandidate,
    second: GeneralToleranceCandidate,
) -> bool:
    first_rect = fitz.Rect(first.rect)
    second_rect = fitz.Rect(second.rect)
    intersection = first_rect & second_rect
    smaller = min(first_rect.get_area(), second_rect.get_area())
    return (
        smaller > 0
        and intersection.get_area() / smaller >= 0.35
        and abs(first.nominal_value - second.nominal_value) < 1e-6
    )


def _reject_unreliable_dimension(
    *,
    kind: str,
    prefix: str,
    degree: str,
    nominal: float,
    compact: str,
    score: float = 1.0,
    supplemental: bool = False,
) -> bool:
    """明らかな誤検出を除外する共通判定。"""

    if is_tolerance_fragment(compact):
        return True
    if not prefix and not degree and score < BARE_NUMBER_MIN_CONFIDENCE:
        return True
    if not prefix and not degree and nominal >= 500:
        return True
    if not prefix and not degree and nominal >= 1900:
        return True
    if not prefix and not degree and nominal in {90, 180, 360}:
        return True
    if supplemental and not prefix and not degree and nominal >= 150:
        return True
    if supplemental and kind == "diameter" and nominal > 100:
        return True
    if kind == "diameter" and nominal > 80:
        return True
    if supplemental and not prefix and not degree and nominal < 15 and "." not in compact:
        return True
    if kind == "diameter" and nominal < 1.0:
        return True
    if not prefix and not degree and nominal < 1.0:
        return True
    if (
        not prefix
        and not degree
        and compact.lstrip("0") != compact
        and "." not in compact
        and "," not in compact
    ):
        # 018 など表題欄の先頭ゼロ付き番号
        return True
    return False


def _local_ocr_general_candidates(
    page: fitz.Page,
    page_index: int,
    *,
    standard: str,
    grade: str,
    angle_shorter_side_length: float,
    ocr_page: LocalOcrPage,
    scanned_page: bool = False,
    strict_line_support: bool = False,
) -> list[GeneralToleranceCandidate]:
    """Build reviewable general-tolerance candidates from shared ONNX OCR."""

    ocr_text = "\n".join(line.text for line in ocr_page.lines)
    reference_rects = _ocr_reference_rects(ocr_page)
    notes = extract_drawing_tolerance_notes("\n".join((page.get_text(), ocr_text)))
    angle_override = notes.angle_tolerance or angle_tolerance(
        angle_shorter_side_length,
        standard=standard,
        grade=grade,
    )
    ocr_tolerance_rects = _explicit_tolerance_rects_from_ocr_page(ocr_page)
    candidates: list[GeneralToleranceCandidate] = []
    detail_angles = analyze_detail_angles(page, ocr_page)
    for line in (*ocr_page.lines, *detail_angles):
        is_detail_angle = line in detail_angles
        line_text = unicodedata.normalize("NFKC", line.text)
        if is_tolerance_fragment(line_text):
            continue
        parsed = parse_dimension_token(line_text)
        if parsed is None:
            if (
                not line_text.strip()
                or _EXPLICIT_TOLERANCE.search(line_text)
                or _NON_DIMENSION_CONTEXT.search(line_text)
                or re.search(r"(?:以下|MAX|MIN)", line_text, re.IGNORECASE)
            ):
                continue
            continue
        if parsed.reference and not is_detail_angle:
            continue
        if re.search(r"[（(]", line_text) or re.search(r"[）)]", line_text):
            continue
        if _EXPLICIT_TOLERANCE.search(line_text):
            continue
        compact = parsed.normalized_text.lstrip("△▲◆◇")
        prefix = parsed.prefix
        degree = parsed.degree
        nominal = parsed.nominal_value
        kind = _candidate_kind(prefix, degree)
        if kind is None:
            continue
        if nominal <= 0 or nominal > 4000:
            continue
        if kind == "diameter" and nominal < 1.0:
            # φ0.03 など幾何公差・表面粗さの許容値は寸法候補にしない。
            continue
        if _reject_unreliable_dimension(
            kind=kind,
            prefix=prefix,
            degree=degree,
            nominal=nominal,
            compact=compact,
            score=line.score,
        ):
            continue
        if not prefix and not degree and nominal >= 500:
            # Large bare OCR numbers are commonly vertical fits whose symbol
            # and decimal point were lost (for example φ16.0 -> 810).  Do not
            # add a potentially huge JIS tolerance automatically.
            continue
        rect = fitz.Rect(line.rect) & page.rect
        direction = line.direction
        if _overlaps_reference_evidence(rect, reference_rects):
            # 参照寸法近傍の抑制は素の数値向け。φ/C/R は隣接でも残す。
            if not prefix and not degree:
                continue
        has_line_support = _has_dimension_line_support(
            ocr_page.image,
            rect,
            direction,
            kind,
            scale_x=ocr_page.scale_x,
            scale_y=ocr_page.scale_y,
            strict=strict_line_support,
        )
        # 接頭辞付き寸法は下端付近にも実寸法があるため除外を緩める
        bottom_limit = 0.93 if prefix else 0.86
        if (
            rect.is_empty
            or (not parsed.reference and rect.y1 < page.rect.height * 0.08)
            or rect.y0 > page.rect.height * bottom_limit
            or rect.width > 85
            or rect.height > 85
            or (
                rect.x1 < page.rect.width * 0.22
                and rect.y1 < page.rect.height * 0.32
                and not has_line_support
            )
            or (
                scanned_page
                and _is_non_dimension_region(
                    rect,
                    page.rect,
                    bare_only=not prefix and not degree,
                )
            )
        ):
            continue
        margin = max(6.0, min(18.0, max(rect.width, rect.height) * 2.0))
        tolerance_nearby = fitz.Rect(
            rect.x0 - margin,
            rect.y0 - margin,
            rect.x1 + margin,
            rect.y1 + margin,
        )
        if not prefix and not degree:
            if any(
                _tolerance_attached_to_nominal(rect, tolerance_rect)
                for tolerance_rect in ocr_tolerance_rects
            ):
                continue
        elif nominal < 1.0 and any(
            (tolerance_rect & tolerance_nearby).get_area()
            / max(rect.get_area(), 1e-9)
            > 0.55
            for tolerance_rect in ocr_tolerance_rects
        ):
            continue
        if (
            not prefix
            and not degree
            and nominal <= 1.0
            and compact.lstrip("0") in {"1", "7"}
            and _is_visual_parenthetical(
                ocr_page.image,
                rect,
                direction,
                scale_x=ocr_page.scale_x,
                scale_y=ocr_page.scale_y,
            )
        ):
            continue
        if (
            kind == "linear"
            and not prefix
            and not degree
            and abs(nominal - 60.0) < 1e-9
            and rect.y1 < page.rect.height * 0.28
            and rect.x0 > page.rect.width * 0.72
        ):
            continue
        if (
            not prefix
            and not degree
            and nominal < 100
            and rect.x0 > page.rect.width * 0.84
            and rect.y1 < page.rect.height * 0.30
        ):
            continue
        if not is_detail_angle and _is_visual_parenthetical(
            ocr_page.image,
            rect,
            direction,
            scale_x=ocr_page.scale_x,
            scale_y=ocr_page.scale_y,
        ):
            # 接頭辞付きで寸法線がある場合は括弧誤判定を無視（C0.2 など）
            if not (prefix and has_line_support):
                continue
        elif not is_detail_angle and _is_feature_control_frame(
            ocr_page.image,
            rect,
            direction,
            scale_x=ocr_page.scale_x,
            scale_y=ocr_page.scale_y,
        ):
            # 寸法線サポートのある長さ寸法は、近傍の幾何公差枠と誤判定されやすい。
            if not (
                kind == "linear"
                and has_line_support
                and nominal >= 10.0
            ):
                continue
        if kind in {"chamfer", "radius"} and _has_trailing_limit_text_visual(
            ocr_page.image,
            rect,
            direction,
            scale_x=ocr_page.scale_x,
            scale_y=ocr_page.scale_y,
        ):
            continue
        if not is_detail_angle and not has_line_support:
            # 画像PDFでは寸法線検出が欠けることがある。
            # 小数付きの φ/C/R は高信頼度なら候補に残す。
            allow_without_line = (
                scanned_page
                and "." in compact
                and (
                    (
                        prefix in {"φ", "Φ", "Ø", "⌀"}
                        and line.score >= 0.88
                    )
                    or (
                        prefix.upper() in {"C", "R"}
                        and line.score >= 0.90
                    )
                )
            )
            if not allow_without_line:
                continue
        if scanned_page and kind == "linear" and not prefix and not degree:
            if line.score < 0.78:
                continue
            if (
                "." not in compact
                and "," not in compact
                and nominal < 8.0
            ):
                continue
        if (
            scanned_page
            and kind == "diameter"
            and rect.y1 < page.rect.height * 0.14
            and rect.x0 > page.rect.width * 0.62
        ):
            continue
        if scanned_page and kind in {"chamfer", "radius"} and line.score < 0.75:
            continue
        if kind == "angle":
            tolerance, tolerance_text = angle_override
        else:
            tolerance = _candidate_tolerance(
                standard,
                nominal,
                grade=grade,
                kind=kind,
                notes=notes,
            )
            tolerance_text = _format_tolerance(tolerance) if tolerance is not None else ""
        manual_required = (
            tolerance is None and _requires_individual_tolerance(
                standard,
                nominal,
                kind,
            )
        )
        if tolerance is None and not manual_required:
            continue
        candidate = GeneralToleranceCandidate(
            page_index=page_index,
            rect=tuple(rect),
            direction=direction,
            source_text=compact,
            nominal_value=nominal,
            kind=kind,
            tolerance=tolerance or 0.0,
            tolerance_text=tolerance_text,
            selected=not manual_required,
            quad=line.quad,
            manual_required=manual_required,
        )
        if not any(_same_candidate(candidate, existing) for existing in candidates):
            candidates.append(candidate)
    return sorted(candidates, key=lambda item: (item.rect[1], item.rect[0]))


def _ocr_reference_rects(ocr_page: LocalOcrPage) -> tuple[fitz.Rect, ...]:
    """Return regions any OCR pass identified as a reference dimension."""

    regions: list[fitz.Rect] = []
    for line in ocr_page.lines:
        text = unicodedata.normalize("NFKC", line.text)
        parsed = parse_dimension_token(text)
        if not (
            (parsed is not None and parsed.reference)
            or (re.search(r"[（(]", text) and re.search(r"[）)]", text))
        ):
            continue
        rect = fitz.Rect(line.rect)
        if not rect.is_empty:
            regions.append(rect)
    return tuple(regions)


def _overlaps_reference_evidence(
    rect: fitz.Rect,
    reference_rects: tuple[fitz.Rect, ...],
) -> bool:
    """Use OCR ensemble agreement to suppress alternate reads of ``(n)``."""

    if rect.is_empty:
        return False
    center = ((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
    for reference_rect in reference_rects:
        intersection = (rect & reference_rect).get_area()
        if intersection >= min(rect.get_area(), reference_rect.get_area()) * 0.22:
            return True
        reference_center = (
            (reference_rect.x0 + reference_rect.x1) / 2,
            (reference_rect.y0 + reference_rect.y1) / 2,
        )
        if math.dist(center, reference_center) <= max(
            3.0,
            min(rect.width, rect.height, reference_rect.width, reference_rect.height)
            * 0.8,
        ):
            return True
    return False


def _merge_general_tolerance_candidates(
    *groups: list[GeneralToleranceCandidate],
) -> list[GeneralToleranceCandidate]:
    """Merge candidate lists while removing overlapping duplicates."""

    def _quality(candidate: GeneralToleranceCandidate) -> tuple[int, int, float]:
        text = unicodedata.normalize("NFKC", candidate.source_text)
        has_prefix = 1 if re.match(r"^[φΦØ⌀CR]", text) else 0
        has_degree = 1 if "°" in text or "。" in text else 0
        digit_count = len(re.sub(r"\D", "", re.split(r"[±+\-]", text, maxsplit=1)[0]))
        return (
            has_prefix + has_degree + min(5, digit_count),
            len(text),
            abs(candidate.nominal_value),
        )

    merged: list[GeneralToleranceCandidate] = []
    for group in groups:
        for candidate in group:
            candidate_rect = fitz.Rect(candidate.rect)
            overlap_index = -1
            for index, existing in enumerate(merged):
                same = _same_candidate(candidate, existing)
                overlap_ratio = (
                    (candidate_rect & fitz.Rect(existing.rect)).get_area()
                    / max(
                        1e-9,
                        min(
                            candidate_rect.get_area(),
                            fitz.Rect(existing.rect).get_area(),
                        ),
                    )
                )
                if same or overlap_ratio >= 0.2:
                    overlap_index = index
                    break
            if overlap_index < 0:
                merged.append(candidate)
                continue
            # 接頭辞・表記が整っている方を残す
            if _quality(candidate) >= _quality(merged[overlap_index]):
                merged[overlap_index] = candidate
    return merged


def _explicit_tolerance_rects_from_ocr_page(
    ocr_page: LocalOcrPage,
) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for line in ocr_page.lines:
        if _EXPLICIT_TOLERANCE.search(unicodedata.normalize("NFKC", line.text)):
            rects.append(fitz.Rect(line.rect))
    return rects


def _tolerance_attached_to_nominal(
    nominal_rect: fitz.Rect,
    tolerance_rect: fitz.Rect,
) -> bool:
    """公差記号が寸法値の直後に付いているかを判定する。"""

    vertical_overlap = min(nominal_rect.y1, tolerance_rect.y1) - max(
        nominal_rect.y0,
        tolerance_rect.y0,
    )
    min_height = max(1.0, min(nominal_rect.height, tolerance_rect.height))
    if vertical_overlap < min_height * 0.35:
        return False
    horizontal_gap = tolerance_rect.x0 - nominal_rect.x1
    if -nominal_rect.height * 0.35 <= horizontal_gap <= nominal_rect.height * 4.5:
        return True
    vertical_gap = tolerance_rect.y0 - nominal_rect.y1
    return -nominal_rect.width * 0.35 <= vertical_gap <= nominal_rect.width * 4.5


def _supplemental_tiled_candidates(
    page: fitz.Page,
    page_index: int,
    *,
    standard: str,
    grade: str,
    ocr_script: Path,
    angle_override: tuple[float, str],
    notes: DrawingToleranceNotes,
    ocr_tolerance_rects: list[fitz.Rect],
    existing: list[GeneralToleranceCandidate],
) -> list[GeneralToleranceCandidate]:
    """Add high-resolution tiled OCR candidates missed by a page-wide pass."""

    supplemental: list[GeneralToleranceCandidate] = []
    known = list(existing)

    def append_if_new(candidate: GeneralToleranceCandidate) -> None:
        candidate_rect = fitz.Rect(candidate.rect)
        if any(
            _same_candidate(candidate, item)
            or (
                (candidate_rect & fitz.Rect(item.rect)).get_area()
                / max(
                    1e-9,
                    min(candidate_rect.get_area(), fitz.Rect(item.rect).get_area()),
                )
                >= 0.35
            )
            for item in known
        ):
            return
        rect = candidate_rect
        margin = max(10.0, min(24.0, max(rect.width, rect.height) * 2.2))
        nearby = fitz.Rect(
            rect.x0 - margin,
            rect.y0 - margin,
            rect.x1 + margin,
            rect.y1 + margin,
        )
        if any(not (tolerance_rect & nearby).is_empty for tolerance_rect in ocr_tolerance_rects):
            return
        supplemental.append(candidate)
        known.append(candidate)

    for candidate in _tiled_dimension_candidates(
        page,
        page_index,
        standard=standard,
        grade=grade,
        ocr_script=ocr_script,
        angle_override=angle_override,
        notes=notes,
    ):
        append_if_new(candidate)

    for candidate in _deep_cr_candidates(
        page,
        page_index,
        standard=standard,
        grade=grade,
        ocr_script=ocr_script,
        notes=notes,
    ):
        candidate_rect = fitz.Rect(candidate.rect)
        candidate_center = (
            (candidate_rect.x0 + candidate_rect.x1) / 2,
            (candidate_rect.y0 + candidate_rect.y1) / 2,
        )
        if any(
            _same_candidate(candidate, item)
            or (
                candidate.kind == item.kind
                and abs(candidate.nominal_value - item.nominal_value) < 1e-6
                and (
                    (candidate_rect & fitz.Rect(item.rect)).get_area()
                    / max(
                        1e-9,
                        min(
                            candidate_rect.get_area(),
                            fitz.Rect(item.rect).get_area(),
                        ),
                    )
                    >= 0.18
                    or math.dist(
                        candidate_center,
                        (
                            (fitz.Rect(item.rect).x0 + fitz.Rect(item.rect).x1) / 2,
                            (fitz.Rect(item.rect).y0 + fitz.Rect(item.rect).y1) / 2,
                        ),
                    )
                    <= max(
                        12.0,
                        min(
                            24.0,
                            max(
                                candidate_rect.width,
                                candidate_rect.height,
                                fitz.Rect(item.rect).width,
                                fitz.Rect(item.rect).height,
                            )
                            * 1.2,
                        ),
                    )
                )
            )
            for item in known
        ):
            continue
        rect = candidate_rect
        if not (
            candidate.kind == "chamfer"
            and abs(candidate.nominal_value - 0.2) < 1e-9
        ) and any(
            (tolerance_rect & rect).get_area() / max(rect.get_area(), 1e-9) >= 0.25
            for tolerance_rect in ocr_tolerance_rects
        ):
            continue
        append_if_new(candidate)

    return supplemental


def _deduplicate_general_candidates(
    candidates: list[GeneralToleranceCandidate],
) -> list[GeneralToleranceCandidate]:
    """同種・同値で近接する候補を統合する。"""

    ordered = sorted(
        candidates,
        key=lambda item: (
            any(symbol in item.source_text for symbol in ("φ", "°", "C", "R")),
            "." in item.source_text,
            -item.rect[1],
        ),
        reverse=True,
    )
    kept: list[GeneralToleranceCandidate] = []
    for candidate in ordered:
        center = (
            (candidate.rect[0] + candidate.rect[2]) / 2,
            (candidate.rect[1] + candidate.rect[3]) / 2,
        )
        if any(
            existing.kind == candidate.kind
            and abs(existing.nominal_value - candidate.nominal_value) < 1e-6
            and math.dist(
                center,
                (
                    (existing.rect[0] + existing.rect[2]) / 2,
                    (existing.rect[1] + existing.rect[3]) / 2,
                ),
            )
            < 24.0
            for existing in kept
        ):
            continue
        kept.append(candidate)
    return sorted(kept, key=lambda item: (item.rect[1], item.rect[0]))


def detect_general_tolerance_candidates(
    page: fitz.Page,
    page_index: int,
    *,
    standard: str,
    grade: str,
    ocr_script: Path,
    angle_shorter_side_length: float = 10,
    local_ocr_page: LocalOcrPage | None = None,
    scanned_tile_lines: tuple | None = None,
    scanned_tile_cache: dict[int, tuple] | None = None,
) -> list[GeneralToleranceCandidate]:
    """Detect safe general-tolerance candidates in one PDF page."""

    recorder = OcrPipelineRecorder("general_tolerance")
    page_words = page.get_text("words")
    seed_candidates: list[GeneralToleranceCandidate] = []
    if page_words and not _is_full_page_image(page):
        native_candidates = _native_text_candidates(
            page,
            page_index,
            standard=standard,
            grade=grade,
            angle_shorter_side_length=angle_shorter_side_length,
        )
        # Native text extraction is fastest and usually most stable.  Keep that
        # path when enough candidates are detected and avoid OCR cost.
        if len(native_candidates) >= 8:
            recorder.set_count("native_candidates", len(native_candidates))
            recorder.set_count("final_candidates", len(native_candidates))
            recorder.log_summary()
            return native_candidates
        seed_candidates = native_candidates

    if local_ocr_page is not None:
        image_only_page = len(page_words) == 0
        # Some scanned PDFs retain a sparse OCR text layer. Treat a full-page
        # raster as scanned even in that case; otherwise its small dimensions
        # are subjected to the overly strict vector-PDF candidate filters.
        scanned_page = image_only_page or _is_full_page_image(page)
        ocr_page_for_detect = local_ocr_page
        tile_lines_used = False
        page_only_candidates = _local_ocr_general_candidates(
            page,
            page_index,
            standard=standard,
            grade=grade,
            angle_shorter_side_length=angle_shorter_side_length,
            ocr_page=local_ocr_page,
            scanned_page=scanned_page,
            strict_line_support=False,
        )
        supplement_threshold = (
            SUPPLEMENT_THRESHOLD_SCANNED
            if scanned_page
            else SUPPLEMENT_THRESHOLD_VECTOR
        )
        if scanned_page and (
            _is_full_page_image(page)
            or len(page_only_candidates) < supplement_threshold
        ):
            tile_lines = scanned_tile_lines
            if tile_lines is None and scanned_tile_cache is not None:
                tile_lines = scanned_tile_cache.get(page_index)
            if tile_lines is None:
                tile_lines = analyze_scanned_page_tiles(page)
                if scanned_tile_cache is not None:
                    scanned_tile_cache[page_index] = tile_lines
            if tile_lines:
                tile_lines_used = True
                ocr_page_for_detect = enrich_scanned_ocr_page(
                    local_ocr_page,
                    tile_lines,
                )
                recorder.set_count("tiled_ocr_lines", len(tile_lines))
                recorder.set_count(
                    "merged_ocr_lines",
                    len(ocr_page_for_detect.lines),
                )
        local_candidates = page_only_candidates
        if tile_lines_used:
            # タイル統合だけでページOCRを捨てると桁欠けが残る。
            # ページ候補とタイル候補を品質優先で統合する。
            tiled_candidates = _local_ocr_general_candidates(
                page,
                page_index,
                standard=standard,
                grade=grade,
                angle_shorter_side_length=angle_shorter_side_length,
                ocr_page=ocr_page_for_detect,
                scanned_page=scanned_page,
                strict_line_support=False,
            )
            local_candidates = _merge_general_tolerance_candidates(
                page_only_candidates,
                tiled_candidates,
            )
        recorder.set_count("ocr_raw_lines", len(ocr_page_for_detect.lines))
        merged = _merge_general_tolerance_candidates(
            seed_candidates,
            local_candidates,
        )
        recorder.set_count("local_candidates", len(local_candidates))
        recorder.set_count("merged_before_supplement", len(merged))
        run_windows_supplement = (
            ocr_script.is_file()
            and scanned_page
            and (
                tile_lines_used
                or _is_full_page_image(page)
                or len(merged) < supplement_threshold
                or image_only_page
            )
        )
        if run_windows_supplement:
            ocr_text = "\n".join(line.text for line in ocr_page_for_detect.lines)
            notes = extract_drawing_tolerance_notes(ocr_text)
            angle_override = notes.angle_tolerance or angle_tolerance(
                angle_shorter_side_length,
                standard=standard,
                grade=grade,
            )
            supplemental = _supplemental_tiled_candidates(
                page,
                page_index,
                standard=standard,
                grade=grade,
                ocr_script=ocr_script,
                angle_override=angle_override,
                notes=notes,
                ocr_tolerance_rects=_explicit_tolerance_rects_from_ocr_page(
                    ocr_page_for_detect
                ),
                existing=merged,
            )
            merged = _merge_general_tolerance_candidates(merged, supplemental)
            recorder.set_count("supplemental_candidates", len(supplemental))
        final = _deduplicate_general_candidates(merged)
        reference_evidence = _ocr_reference_rects(ocr_page_for_detect)
        final = [
            candidate
            for candidate in final
            if re.match(r"^[φΦØ⌀CR]", candidate.source_text)
            or not _overlaps_reference_evidence(
                fitz.Rect(candidate.rect),
                reference_evidence,
            )
        ]
        recorder.set_count("final_candidates", len(final))
        recorder.log_summary()
        return final

    maximum_dimension = max(page.rect.width, page.rect.height)
    zoom = max(1.5, min(3.2, 2450 / maximum_dimension))
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, annots=False)
    image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
    image_width, image_height = image.size
    candidates: list[GeneralToleranceCandidate] = []
    ocr_tolerance_rects: list[fitz.Rect] = []

    def mapped_word_rect(word: dict, rotation: int) -> fitz.Rect:
        x = float(word.get("x") or 0)
        y = float(word.get("y") or 0)
        width = float(word.get("width") or 0)
        height = float(word.get("height") or 0)
        mapped = _map_rotated_rect(
            (x, y, x + width, y + height),
            rotation,
            image_width,
            image_height,
        )
        return fitz.Rect(
            mapped[0] * page.rect.width / image_width,
            mapped[1] * page.rect.height / image_height,
            mapped[2] * page.rect.width / image_width,
            mapped[3] * page.rect.height / image_height,
        )

    with tempfile.TemporaryDirectory(prefix="DrawingAssist-OCR-") as temp_name:
        temp_dir = Path(temp_name)
        ocr_jobs: list[tuple[int, Path]] = []
        for rotation in (0, 90, 270):
            rotated = image if rotation == 0 else image.rotate(rotation, expand=True)
            image_path = temp_dir / f"page-{rotation}.png"
            rotated.save(image_path)
            ocr_jobs.append((rotation, image_path))

        ocr_results = [
            (job[0], result)
            for job, result in _run_windows_ocr_jobs(
                ocr_jobs,
                ocr_script,
                path_index=1,
                max_workers=2,
            )
        ]

        ocr_text = "\n".join(
            str(line.get("text") or "")
            for _rotation, result in ocr_results
            for line in result.get("lines") or []
        )
        notes = extract_drawing_tolerance_notes(
            "\n".join((page.get_text(), ocr_text))
        )
        angle_override = (
            notes.angle_tolerance
            or angle_tolerance(
                angle_shorter_side_length,
                standard=standard,
                grade=grade,
            )
        )

        for rotation, result in ocr_results:
            for line in result.get("lines") or []:
                line_text = str(line.get("text") or "")
                line_words = line.get("words") or []
                if _EXPLICIT_TOLERANCE.search(
                    unicodedata.normalize("NFKC", line_text)
                ) and line_words:
                    line_rect = mapped_word_rect(line_words[0], rotation)
                    for line_word in line_words[1:]:
                        line_rect |= mapped_word_rect(line_word, rotation)
                    ocr_tolerance_rects.append(line_rect)
                for word in line_words:
                    word_text = unicodedata.normalize(
                        "NFKC",
                        str(word.get("text") or ""),
                    )
                    if _EXPLICIT_TOLERANCE.search(word_text):
                        ocr_tolerance_rects.append(
                            mapped_word_rect(word, rotation)
                        )
                for word in line_words:
                    candidate = _word_candidate(
                        word,
                        line_text,
                        rotation=rotation,
                        image_width=image_width,
                        image_height=image_height,
                        page=page,
                        page_index=page_index,
                        standard=standard,
                        grade=grade,
                        angle_override=angle_override,
                        notes=notes,
                    )
                    if candidate is None:
                        continue
                    candidate_rect = fitz.Rect(candidate.rect)
                    if _is_visual_parenthetical(
                        image,
                        candidate_rect,
                        candidate.direction,
                        scale_x=image_width / page.rect.width,
                        scale_y=image_height / page.rect.height,
                    ):
                        continue
                    if _is_feature_control_frame(
                        image,
                        candidate_rect,
                        candidate.direction,
                        scale_x=image_width / page.rect.width,
                        scale_y=image_height / page.rect.height,
                    ):
                        continue
                    if candidate.kind in {"chamfer", "radius"} and _has_trailing_limit_text_visual(
                        image,
                        candidate_rect,
                        candidate.direction,
                        scale_x=image_width / page.rect.width,
                        scale_y=image_height / page.rect.height,
                    ):
                        continue
                    if not _has_dimension_line_support(
                        image,
                        candidate_rect,
                        candidate.direction,
                        candidate.kind,
                        scale_x=image_width / page.rect.width,
                        scale_y=image_height / page.rect.height,
                    ):
                        continue
                    candidate_area = candidate_rect.get_area()
                    if candidate_area > 0 and page_words and any(
                        (fitz.Rect(pdf_word[:4]) & candidate_rect).get_area()
                        / candidate_area
                        >= 0.25
                        for pdf_word in page_words
                    ):
                        # A full-page scan may also contain later vector text
                        # such as stamps or already-added corrections. Do not
                        # OCR those overlays as source dimensions again.
                        continue
                    if any(_same_candidate(candidate, existing) for existing in candidates):
                        continue
                    candidates.append(candidate)
    filtered = []
    for candidate in candidates:
        rect = fitz.Rect(candidate.rect)
        margin = max(10.0, min(24.0, max(rect.width, rect.height) * 2.2))
        nearby = fitz.Rect(
            rect.x0 - margin,
            rect.y0 - margin,
            rect.x1 + margin,
            rect.y1 + margin,
        )
        if any(not (tolerance_rect & nearby).is_empty for tolerance_rect in ocr_tolerance_rects):
            continue
        filtered.append(candidate)
    for candidate in _tiled_dimension_candidates(
        page,
        page_index,
        standard=standard,
        grade=grade,
        ocr_script=ocr_script,
        angle_override=angle_override,
        notes=notes,
    ):
        candidate_rect = fitz.Rect(candidate.rect)
        if any(
            _same_candidate(candidate, existing)
            or (
                (candidate_rect & fitz.Rect(existing.rect)).get_area()
                / max(
                    1e-9,
                    min(candidate_rect.get_area(), fitz.Rect(existing.rect).get_area()),
                )
                >= 0.35
            )
            for existing in filtered
        ):
            continue
        rect = candidate_rect
        margin = max(10.0, min(24.0, max(rect.width, rect.height) * 2.2))
        nearby = fitz.Rect(
            rect.x0 - margin,
            rect.y0 - margin,
            rect.x1 + margin,
            rect.y1 + margin,
        )
        if any(not (tolerance_rect & nearby).is_empty for tolerance_rect in ocr_tolerance_rects):
            continue
        filtered.append(candidate)
    for candidate in _deep_cr_candidates(
        page,
        page_index,
        standard=standard,
        grade=grade,
        ocr_script=ocr_script,
        notes=notes,
    ):
        candidate_rect = fitz.Rect(candidate.rect)
        candidate_center = (
            (candidate_rect.x0 + candidate_rect.x1) / 2,
            (candidate_rect.y0 + candidate_rect.y1) / 2,
        )
        if any(
            _same_candidate(candidate, existing)
            or (
                candidate.kind == existing.kind
                and abs(candidate.nominal_value - existing.nominal_value) < 1e-6
                and (
                    (candidate_rect & fitz.Rect(existing.rect)).get_area()
                    / max(
                        1e-9,
                        min(
                            candidate_rect.get_area(),
                            fitz.Rect(existing.rect).get_area(),
                        ),
                    )
                    >= 0.18
                    or math.dist(
                        candidate_center,
                        (
                            (fitz.Rect(existing.rect).x0 + fitz.Rect(existing.rect).x1) / 2,
                            (fitz.Rect(existing.rect).y0 + fitz.Rect(existing.rect).y1) / 2,
                        ),
                    )
                    <= max(
                        12.0,
                        min(
                            24.0,
                            max(
                                candidate_rect.width,
                                candidate_rect.height,
                                fitz.Rect(existing.rect).width,
                                fitz.Rect(existing.rect).height,
                            )
                            * 1.2,
                        ),
                    )
                )
            )
            for existing in filtered
        ):
            continue
        rect = candidate_rect
        if not (
            candidate.kind == "chamfer"
            and abs(candidate.nominal_value - 0.2) < 1e-9
        ) and any(
            (tolerance_rect & rect).get_area()
            / max(rect.get_area(), 1e-9)
            >= 0.25
            for tolerance_rect in ocr_tolerance_rects
        ):
            continue
        filtered.append(candidate)
    final_candidates: list[GeneralToleranceCandidate] = list(seed_candidates)
    for candidate in sorted(
        filtered,
        key=lambda item: (fitz.Rect(item.rect).get_area(), item.rect[1], item.rect[0]),
    ):
        candidate_rect = fitz.Rect(candidate.rect)
        if any(
            candidate.kind == existing.kind
            and abs(candidate.nominal_value - existing.nominal_value) < 1e-6
            and (candidate_rect & fitz.Rect(existing.rect)).get_area()
            / max(
                1e-9,
                min(candidate_rect.get_area(), fitz.Rect(existing.rect).get_area()),
            )
            >= 0.15
            for existing in final_candidates
        ):
            continue
        final_candidates.append(candidate)
    return sorted(final_candidates, key=lambda item: (item.rect[1], item.rect[0]))


def toggle_candidate(
    candidates: list[GeneralToleranceCandidate],
    point: fitz.Point,
    *,
    padding: float = 8.0,
) -> list[GeneralToleranceCandidate]:
    containing: list[tuple[int, fitz.Rect]] = []
    for index, candidate in enumerate(candidates):
        if candidate.manual_required:
            continue
        hit_rect = fitz.Rect(candidate.rect)
        hit_rect.x0 -= padding
        hit_rect.y0 -= padding
        hit_rect.x1 += padding
        hit_rect.y1 += padding
        if point in hit_rect:
            containing.append((index, fitz.Rect(candidate.rect)))
    if not containing:
        return candidates
    index, _ = min(containing, key=lambda pair: pair[1].get_area())
    updated = list(candidates)
    updated[index] = replace(updated[index], selected=not updated[index].selected)
    return updated
