from __future__ import annotations

import argparse
from pathlib import Path
import sys

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.pdf_editor import Mark, export_pdf, find_word_rect


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()

    source = fitz.open(args.source)
    page = source[0]
    words = page.get_text("words")
    if not words:
        raise SystemExit("No selectable PDF text was found.")

    target_word = next(
        (word for word in words if str(word[4]).strip() == "14.7"),
        next(
            (word for word in words if any(character.isdigit() for character in str(word[4]))),
            words[0],
        ),
    )
    target_rect = fitz.Rect(target_word[:4])
    center = fitz.Point(
        (target_rect.x0 + target_rect.x1) / 2,
        (target_rect.y0 + target_rect.y1) / 2,
    )
    result = find_word_rect(page, center)
    if result is None:
        raise SystemExit("Automatic click selection did not find the target word.")
    rect, text = result
    source.close()

    mark = Mark(0, tuple(rect), "#fff24d", 0.42)
    export_pdf(args.source, args.output, [mark])

    verified = fitz.open(args.output)
    if verified.page_count < 1:
        raise SystemExit("The exported PDF has no pages.")
    if args.preview:
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        pixmap = verified[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        pixmap.save(args.preview)
    verified.close()
    print(f"OK: selected={text!r} output={args.output}")


if __name__ == "__main__":
    main()
