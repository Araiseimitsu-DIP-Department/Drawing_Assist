"""画像PDFのOCR・一般公差・色分け候補の処理時間と件数を計測する。"""

from __future__ import annotations

from pathlib import Path
import json
import logging
import sys
import time

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.general_tolerance import detect_general_tolerance_candidates
from drawing_assist.local_ocr import analyze_page, local_ocr_available
from drawing_assist.web_app import (
    _detect_dimension_markings,
    _detect_local_dimension_markings,
    _is_scanned_page,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
GROUND_TRUTH_DIR = ROOT / "scripts" / "ocr_ground_truth"


def _load_ground_truth(pdf_path: Path) -> list[dict] | None:
    for path in GROUND_TRUTH_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if pdf_path.stem.lower() == str(data.get("name", "")).lower():
            return list(data.get("dimensions") or [])
    return None


def _evaluate_candidates(
    candidates: list,
    ground_truth: list[dict],
) -> dict:
    expected = {
        (item["kind"], float(item["value"]))
        for item in ground_truth
    }
    detected = {
        (item.kind, float(item.nominal_value))
        for item in candidates
    }
    true_positive = expected & detected
    false_positive = detected - expected
    false_negative = expected - detected
    precision = len(true_positive) / len(detected) if detected else 0.0
    recall = len(true_positive) / len(expected) if expected else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall > 0
        else 0.0
    )
    return {
        "ground_truth_count": len(expected),
        "detected_count": len(detected),
        "true_positive": len(true_positive),
        "false_positive": len(false_positive),
        "false_negative": len(false_negative),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "missed": sorted(
            [{"kind": kind, "value": value} for kind, value in false_negative],
            key=lambda item: item["value"],
        ),
        "extra": sorted(
            [{"kind": kind, "value": value} for kind, value in false_positive],
            key=lambda item: item["value"],
        ),
    }


def benchmark_pdf(pdf_path: Path) -> dict:
    ocr_script = ROOT / "src" / "drawing_assist" / "windows_ocr.ps1"
    doc = fitz.open(pdf_path)
    page = doc[0]
    result: dict = {
        "pdf": str(pdf_path),
        "page_size": [round(page.rect.width, 1), round(page.rect.height, 1)],
        "is_scanned": _is_scanned_page(page),
        "local_ocr_available": local_ocr_available(),
    }

    start = time.time()
    local_page = analyze_page(page) if local_ocr_available() else None
    result["ocr_seconds"] = round(time.time() - start, 2)
    result["ocr_raw_lines"] = len(local_page.lines) if local_page else 0

    start = time.time()
    candidates = detect_general_tolerance_candidates(
        page,
        0,
        standard="jis_b_0405",
        grade="m",
        ocr_script=ocr_script,
        local_ocr_page=local_page,
    )
    result["general_tolerance_seconds"] = round(time.time() - start, 2)
    result["general_tolerance_candidates"] = len(candidates)
    result["general_tolerance_items"] = [
        {
            "kind": item.kind,
            "text": item.source_text,
            "value": item.nominal_value,
        }
        for item in candidates
    ]

    ground_truth = _load_ground_truth(pdf_path)
    if ground_truth is not None:
        result["evaluation"] = _evaluate_candidates(candidates, ground_truth)

    start = time.time()
    vector_markings = _detect_dimension_markings(page)
    result["vector_marking_seconds"] = round(time.time() - start, 2)
    result["vector_markings"] = len(vector_markings)

    if local_page is not None:
        start = time.time()
        plain_markings = _detect_local_dimension_markings(
            page,
            local_page,
            include_plain_dimensions=False,
        )
        colored_markings = _detect_local_dimension_markings(
            page,
            local_page,
            include_plain_dimensions=True,
        )
        result["marking_ocr_seconds"] = round(time.time() - start, 2)
        result["markings_with_tolerance_only"] = len(plain_markings)
        result["markings_include_plain"] = len(colored_markings)

        matched = 0
        for marking in colored_markings:
            for candidate in candidates:
                if (
                    candidate.kind == marking.kind
                    and abs(candidate.nominal_value - marking.nominal_value) < 1e-6
                ):
                    matched += 1
                    break
        result["marking_batch_value_matches"] = matched

    doc.close()
    result["total_seconds"] = round(
        result.get("ocr_seconds", 0)
        + result.get("general_tolerance_seconds", 0)
        + result.get("vector_marking_seconds", 0)
        + result.get("marking_ocr_seconds", 0),
        2,
    )
    return result


def main() -> None:
    default_pdf = Path(r"C:\Users\SEIZOU20\Desktop\core.pdf")
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_pdf
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    report = benchmark_pdf(pdf_path)
    out_dir = ROOT / "tmp"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "ocr_benchmark.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"saved: {out_path}")


if __name__ == "__main__":
    main()
