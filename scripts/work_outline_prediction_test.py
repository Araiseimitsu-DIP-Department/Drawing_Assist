from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.pdf_editor import predict_work_outline


def _bbox(
    points: tuple[tuple[float, float], ...],
) -> tuple[float, float, float, float]:
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def main() -> None:
    document = fitz.open()
    page = document.new_page(width=340, height=300)
    expected = (
        (55.0, 245.0),
        (55.0, 60.0),
        (140.0, 60.0),
        (140.0, 85.0),
        (285.0, 85.0),
        (285.0, 245.0),
    )
    page.draw_polyline(
        [fitz.Point(*point) for point in expected + (expected[0],)],
        color=(0, 0, 0),
        width=2.0,
    )
    # Distracting drawing lines make sure the predictor follows the ordered
    # outline anchors instead of flooding an arbitrary white compartment.
    page.draw_line(
        fitz.Point(90, 155),
        fitz.Point(250, 155),
        color=(0.35, 0.35, 0.35),
        width=0.8,
    )
    page.draw_line(
        fitz.Point(200, 35),
        fitz.Point(200, 265),
        color=(0.55, 0.55, 0.55),
        width=0.6,
    )
    anchors = (
        (56.5, 243.5),
        (53.5, 61.5),
        (138.5, 58.5),
        (141.5, 83.5),
        (283.5, 86.5),
        (286.5, 243.5),
    )
    predicted = predict_work_outline(page, anchors)
    bounds = _bbox(predicted)
    expected_bounds = _bbox(expected)
    if any(
        abs(actual - target) > 3.0
        for actual, target in zip(bounds, expected_bounds)
    ):
        raise RuntimeError(
            f"Predicted outline bounds differ: {bounds!r}"
        )
    if not all(
        min(math.dist(point, corner) for point in predicted) <= 4.0
        for corner in expected
    ):
        raise RuntimeError(
            "Predicted outline did not retain every stepped corner"
        )
    print(
        json.dumps(
            {
                "ok": True,
                "anchor_count": len(anchors),
                "predicted_point_count": len(predicted),
                "bounds": [round(value, 2) for value in bounds],
            },
            ensure_ascii=False,
        )
    )
    document.close()


if __name__ == "__main__":
    main()
