from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.pdf_editor import Mark, export_pdf, find_text_group
from drawing_assist.web_app import DrawingApi


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("preview", type=Path)
    parser.add_argument("--text", default="C0.15")
    args = parser.parse_args()

    document = fitz.open(args.source)
    page = document[0]
    target_rect: fitz.Rect | None = None
    manual_rect: fitz.Rect | None = None
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(
                character.get("c", "")
                for span in line.get("spans", [])
                for character in span.get("chars", [])
            ).strip()
            if text == args.text:
                target_rect = fitz.Rect(line["bbox"])
            elif text == "C0.3":
                manual_rect = fitz.Rect(line["bbox"])
    if target_rect is None or manual_rect is None:
        raise SystemExit(f"Target text was not found: {args.text}")

    hit = find_text_group(
        page,
        fitz.Point(
            (target_rect.x0 + target_rect.x1) / 2,
            (target_rect.y0 + target_rect.y1) / 2,
        ),
    )
    document.close()
    if hit is None or hit.quad is None:
        raise SystemExit("The diagonal text did not produce an oriented quad.")
    angle = math.degrees(math.atan2(hit.direction[1], hit.direction[0]))
    if abs(angle) < 2:
        raise SystemExit(f"Expected a diagonal direction, got {angle:.2f} degrees.")

    automatic_mark = Mark(0, hit.rect, "#fff24d", 0.55, hit.quad)

    api = DrawingApi()
    loaded = api.load_pdf(str(args.source))
    if not loaded.get("ok") or api.document is None:
        raise SystemExit(loaded.get("message") or "Could not load the PDF.")
    manual_hit = find_text_group(
        api.document[0],
        fitz.Point(
            (manual_rect.x0 + manual_rect.x1) / 2,
            (manual_rect.y0 + manual_rect.y1) / 2,
        ),
    )
    if manual_hit is None or manual_hit.quad is None:
        raise SystemExit("Manual diagonal target was not detected.")
    quad = [fitz.Point(point) for point in manual_hit.quad]
    start = (quad[0] + quad[3]) / 2
    end = (quad[1] + quad[2]) / 2
    cross_vector = quad[3] - quad[0]
    width = math.hypot(cross_vector.x, cross_vector.y)
    manual_state = api.apply_action(
        "angled_rect",
        {"x0": start.x, "y0": start.y, "x1": end.x, "y1": end.y},
        {"color": "#ff76bf", "opacity": 0.55, "highlight_width": width},
    )
    if not manual_state.get("ok") or not api.items:
        raise SystemExit("Manual diagonal highlight failed.")
    manual_mark = api.items[-1]
    if not isinstance(manual_mark, Mark) or not manual_mark.quad:
        raise SystemExit("Manual diagonal highlight has no oriented quad.")
    api.close()

    export_pdf(args.source, args.output, [automatic_mark, manual_mark])

    verified = fitz.open(args.output)
    if verified.page_count != 1:
        raise SystemExit("Unexpected output page count.")
    args.preview.parent.mkdir(parents=True, exist_ok=True)
    pixmap = verified[0].get_pixmap(
        matrix=fitz.Matrix(3, 3),
        clip=fitz.Rect(
            min(target_rect.x0, manual_rect.x0) - 25,
            min(target_rect.y0, manual_rect.y0) - 25,
            target_rect.x1 + 25,
            target_rect.y1 + 25,
        ),
        alpha=False,
        annots=True,
    )
    pixmap.save(args.preview)
    verified.close()
    print(
        f"PASS: automatic {args.text!r} and manual 'C0.3' highlighted "
        f"at {angle:.2f} degrees"
    )


if __name__ == "__main__":
    main()
