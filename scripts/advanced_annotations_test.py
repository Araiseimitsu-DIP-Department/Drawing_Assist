from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.pdf_editor import (
    Mark,
    WorkRegionMark,
    WorkShapeMark,
    export_pdf,
)
from drawing_assist.web_app import DrawingApi


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("preview", type=Path)
    args = parser.parse_args()

    api = DrawingApi()
    loaded = api.load_pdf(str(args.source))
    if not loaded.get("ok"):
        raise RuntimeError(loaded.get("message") or "PDF load failed.")

    unified_results = [
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
    if not all(result.get("ok") for result in unified_results):
        raise RuntimeError(
            f"A unified text/symbol highlight failed: {unified_results}"
        )
    if sum(isinstance(item, Mark) for item in api.items) != 4:
        raise RuntimeError("Expected four unified range highlights.")

    candidate = api.detect_work_region(
        {"x": 210, "y": 410, "operation": "replace"},
        {"color": "#fff24d", "opacity": 0.32},
    )
    if candidate.get("work_region_candidate_count") != 1:
        raise RuntimeError(f"Automatic region detection failed: {candidate}")
    confirmed = api.confirm_work_region()
    if (
        not confirmed.get("ok")
        or not isinstance(api.items[-1], WorkRegionMark)
    ):
        raise RuntimeError(
            f"Automatic region confirmation failed: {confirmed}"
        )

    hatched_candidate = api.detect_work_region(
        {"x": 520, "y": 420, "operation": "replace"},
        {"color": "#fff24d", "opacity": 0.32},
    )
    if hatched_candidate.get("work_region_candidate_count") != 1:
        raise RuntimeError(
            f"Hatched region detection failed: {hatched_candidate}"
        )
    hatched_confirmed = api.confirm_work_region()
    if (
        not hatched_confirmed.get("ok")
        or not isinstance(api.items[-1], WorkRegionMark)
    ):
        raise RuntimeError(
            f"Hatched region confirmation failed: {hatched_confirmed}"
        )

    manual_results = [
        api.apply_action(
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
        ),
        api.apply_action(
            "work_shape",
            {
                "points": [
                    {"x": 560, "y": 240},
                    {"x": 590, "y": 230},
                    {"x": 610, "y": 255},
                    {"x": 575, "y": 270},
                ]
            },
            {
                "color": "#5ee7f2",
                "opacity": 0.30,
                "work_shape_style": "fill",
                "work_line_width": 5,
            },
        ),
    ]
    if not all(result.get("ok") for result in manual_results):
        raise RuntimeError(f"A manual work highlight failed: {manual_results}")
    if sum(isinstance(item, WorkShapeMark) for item in api.items) != 2:
        raise RuntimeError("Expected two manual work shape marks.")

    export_pdf(args.source, args.output, api.items)
    api.close()

    verified = fitz.open(args.output)
    if verified.page_count != 1:
        raise RuntimeError("Unexpected page count.")
    editable_markers = 0
    for page in verified:
        for annotation in page.annots() or []:
            if not annotation.colors.get("fill"):
                continue
            editable_markers += 1
            raw_annotation = verified.xref_object(
                annotation.xref,
                compressed=False,
            )
            if (
                not re.search(r"/C\s*\[\s*\]", raw_annotation)
                or annotation.border.get("width") != 0
                or "/AP" not in raw_annotation
            ):
                raise RuntimeError(
                    "A marker annotation retained a visible border."
                )
    if editable_markers == 0:
        raise RuntimeError("No editable marker annotations were exported.")
    args.preview.parent.mkdir(parents=True, exist_ok=True)
    verified[0].get_pixmap(
        matrix=fitz.Matrix(2.4, 2.4),
        alpha=False,
        annots=True,
    ).save(args.preview)
    verified.close()
    print(
        "PASS: unified range/angled marking, normal and hatched "
        "semi-automatic work selection, and manual work tools were exported"
    )


if __name__ == "__main__":
    main()
