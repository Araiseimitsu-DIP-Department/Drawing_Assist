from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.pdf_editor import ReplacementMark, export_pdf
from drawing_assist.web_app import DrawingApi


def line_details(
    page: fitz.Page,
    text_to_find: str,
    *,
    minimum_x: float = 0,
) -> tuple[fitz.Rect, tuple[float, float], float, tuple[float, float]]:
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(
                character.get("c", "")
                for span in line.get("spans", [])
                for character in span.get("chars", [])
            ).strip()
            if text != text_to_find or float(line["bbox"][0]) < minimum_x:
                continue
            span = line["spans"][0]
            return (
                fitz.Rect(line["bbox"]),
                tuple(float(value) for value in line["dir"]),
                float(span["size"]),
                tuple(float(value) for value in span["origin"]),
            )
    raise RuntimeError(f"Text was not found: {text_to_find!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("source_preview", type=Path)
    parser.add_argument("output_preview", type=Path)
    args = parser.parse_args()

    source_document = fitz.open(args.source)
    source_page = source_document[0]
    rect, direction, font_size, origin = line_details(
        source_page,
        "17.1",
        minimum_x=400,
    )
    source_document.close()

    api = DrawingApi()
    loaded = api.load_pdf(str(args.source))
    if not loaded.get("ok"):
        raise RuntimeError(loaded.get("message") or "PDF load failed.")
    selected = api.select_replacement(
        {
            "x": (rect.x0 + rect.x1) / 2,
            "y": (rect.y0 + rect.y1) / 2,
        }
    )
    selection_state = selected.get("replacement_selection")
    if (
        not selection_state
        or selection_state["original_text"] != "φ17.1"
        or selection_state["original_value"] != "17.1"
        or not selection_state.get("selection_key")
        or selected.get("image") == loaded.get("image")
    ):
        raise RuntimeError(
            "The original value or highlighted selection was not returned to the UI."
        )

    requested_size = 14.0
    value_offset = (4.0, -3.0)
    confirmed = api.confirm_replacement(
        {
            "replacement_value": "17.2",
            "upper_tolerance": "",
            "lower_tolerance": "",
            "replacement_size": requested_size,
            "replacement_tolerance_size": 6.0,
            "replacement_value_x": value_offset[0],
            "replacement_value_y": value_offset[1],
            "replacement_tolerance_x": 2.0,
            "replacement_tolerance_y": 1.0,
        }
    )
    if not confirmed.get("ok") or confirmed.get("replacement_selection"):
        raise RuntimeError("The two-step replacement workflow did not finish.")
    if len(api.items) != 1 or not isinstance(api.items[0], ReplacementMark):
        raise RuntimeError("The replacement mark was not created.")
    mark = api.items[0]
    if mark.upper_tolerance or mark.lower_tolerance:
        raise RuntimeError("Blank tolerances were unexpectedly retained.")
    if mark.origin is None:
        raise RuntimeError("The original baseline was not retained.")
    if math.dist(mark.origin, origin) > 0.01:
        raise RuntimeError(f"Base origin changed: {origin} -> {mark.origin}")
    if math.dist(mark.direction, direction) > 0.001:
        raise RuntimeError(
            f"Direction changed: {direction} -> {mark.direction}"
        )
    if abs(mark.font_size - requested_size) > 0.01:
        raise RuntimeError(
            f"Requested font size was ignored: {requested_size} -> {mark.font_size}"
        )
    if mark.tolerance_font_size != 6.0:
        raise RuntimeError("The independent tolerance size was not retained.")
    if mark.value_offset != value_offset or mark.tolerance_offset != (2.0, 1.0):
        raise RuntimeError("Independent text offsets were not retained.")

    export_pdf(args.source, args.output, api.items)
    api.close()

    output_document = fitz.open(args.output)
    output_page = output_document[0]
    _, new_direction, new_size, new_origin = line_details(
        output_page,
        "17.2",
        minimum_x=400,
    )
    expected_origin = (
        origin[0] + value_offset[0],
        origin[1] + value_offset[1],
    )
    if math.dist(new_origin, expected_origin) > 0.05:
        raise RuntimeError(
            f"Rendered origin differs: {expected_origin} -> {new_origin}"
        )
    if math.dist(new_direction, direction) > 0.001:
        raise RuntimeError(
            f"Rendered direction changed: {direction} -> {new_direction}"
        )
    if abs(new_size - requested_size) > 0.05:
        raise RuntimeError(
            f"Rendered font size differs: {requested_size} -> {new_size}"
        )

    clip = fitz.Rect(rect.x0 - 25, rect.y0 - 35, rect.x1 + 25, rect.y1 + 20)
    args.source_preview.parent.mkdir(parents=True, exist_ok=True)
    original_document = fitz.open(args.source)
    original_document[0].get_pixmap(
        matrix=fitz.Matrix(4, 4),
        clip=clip,
        alpha=False,
        annots=True,
    ).save(args.source_preview)
    original_document.close()
    output_page.get_pixmap(
        matrix=fitz.Matrix(4, 4),
        clip=clip,
        alpha=False,
        annots=True,
    ).save(args.output_preview)
    output_document.close()

    print(
        "PASS: two-step replacement highlighted the selection and applied "
        f"size={requested_size:.1f}pt, offset={value_offset}; "
        "blank tolerances were omitted and independent tolerance settings were retained"
    )


if __name__ == "__main__":
    main()
