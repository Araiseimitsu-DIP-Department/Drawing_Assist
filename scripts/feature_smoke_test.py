from __future__ import annotations

import argparse
from pathlib import Path
import sys

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.pdf_editor import (
    DimensionMark,
    Mark,
    StampMark,
    export_pdf,
    find_text_group,
    strike_from_hit,
)


def _center(rect: fitz.Rect) -> fitz.Point:
    return fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("preview", type=Path)
    args = parser.parse_args()

    document = fitz.open(args.source)
    page = document[0]

    hit_147 = find_text_group(page, fitz.Point(279, 88))
    hit_diameter = find_text_group(page, fitz.Point(86, 290))
    note_rects = page.search_for("ナシジ処理範囲")
    if hit_147 is None or hit_diameter is None or not note_rects:
        raise SystemExit("Expected drawing text was not found.")
    strike_hit = find_text_group(page, _center(note_rects[0]))
    if strike_hit is None:
        raise SystemExit("Strike-through target was not found.")
    document.close()

    items = [
        Mark(0, hit_147.rect, "#fff24d", 0.42),
        Mark(0, hit_diameter.rect, "#ff8fe5", 0.42),
        strike_from_hit(0, strike_hit),
        DimensionMark(0, (260, 405), (205, 386), "R0.1以下", "#fff24d", 0.42, 10),
        StampMark(0, (620, 125), "quality", "櫻井", "'26.07.27", 58),
        StampMark(0, (695, 125), "process", "品証課", "'26.07.27", 58),
    ]
    export_pdf(args.source, args.output, items)

    verified = fitz.open(args.output)
    if verified.page_count != 1:
        raise SystemExit("Unexpected output page count.")
    args.preview.parent.mkdir(parents=True, exist_ok=True)
    pixmap = verified[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    pixmap.save(args.preview)
    verified.close()
    print(f"OK: {args.output}")


if __name__ == "__main__":
    main()
