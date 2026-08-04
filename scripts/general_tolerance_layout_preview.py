from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

import fitz


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.pdf_editor import export_pdf
from drawing_assist.web_app import DrawingApi


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("preview", type=Path)
    args = parser.parse_args()

    api = DrawingApi()
    try:
        started = time.perf_counter()
        state = api.load_pdf(str(args.source.resolve()))
        loaded_at = time.perf_counter()
        if not state.get("ok"):
            raise SystemExit(state.get("message") or "PDF load failed")
        state = api.scan_general_tolerances(
            {
                "general_tolerance_standard": "jis_b_0405",
                "general_tolerance_grade": "m",
                "general_tolerance_angle_length": 10,
            }
        )
        scanned_at = time.perf_counter()
        if not state.get("general_tolerance_candidate_count"):
            raise SystemExit("No general-tolerance candidates detected")
        api.apply_general_tolerances()
        applied_at = time.perf_counter()
        api.apply_dimension_markings()
        marked_at = time.perf_counter()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        export_pdf(args.source, args.output, api.items)
        exported_at = time.perf_counter()
        rendered = fitz.open(args.output)
        try:
            pixmap = rendered[0].get_pixmap(
                matrix=fitz.Matrix(3.0, 3.0),
                alpha=False,
            )
            pixmap.save(args.preview)
        finally:
            rendered.close()
        print(
            "PASS: collision-aware tolerance layout and batch marking "
            f"({len(api.last_general_tolerance_batch)} candidates)"
        )
        print(
            "TIMING: "
            f"load={loaded_at - started:.2f}s "
            f"scan={scanned_at - loaded_at:.2f}s "
            f"layout={applied_at - scanned_at:.2f}s "
            f"mark={marked_at - applied_at:.2f}s "
            f"export={exported_at - marked_at:.2f}s"
        )
    finally:
        api.close()


if __name__ == "__main__":
    main()
