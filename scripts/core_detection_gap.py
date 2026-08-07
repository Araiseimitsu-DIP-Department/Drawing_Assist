"""正規化修正後の検出再現率と落ち理由を短時間で確認する。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from drawing_assist.drawing_text_normalizer import normalize_drawing_text
from drawing_assist.local_ocr import analyze_page
from drawing_assist.web_app import _detect_local_dimension_markings
from core_tolerance_recall import (
    GT_PATH,
    _canon_dim_key,
    _collect_keys,
    _match_ground_truth,
    evaluate_corpus,
)

PDF = Path(r"c:\Users\SEIZOU20\Desktop\core.pdf")


def main() -> None:
    ground_truth = json.loads(GT_PATH.read_text(encoding="utf-8"))["items"]
    doc = fitz.open(PDF)
    page = doc[0]
    ocr = analyze_page(page, scanned=True)
    texts = [line.text for line in ocr.lines]
    print("--- OCR raw (normalized key) ---")
    evaluate_corpus("rapid_page", texts, ground_truth)

    # 正規化後テキストでも評価
    normalized_texts = [normalize_drawing_text(t) for t in texts]
    evaluate_corpus("rapid_normalized", normalized_texts, ground_truth)

    markings = _detect_local_dimension_markings(
        page, ocr, include_plain_dimensions=True, scanned_page=True
    )
    detect_texts = [m.source_text for m in markings]
    print("\n検出ソーステキスト（公差あり）:")
    for m in markings:
        if m.tolerance_range is not None or "°" in m.source_text:
            print(f"  {m.source_text!r} val={m.nominal_value} tol={m.tolerance_range}")
    evaluate_corpus("detection_page", detect_texts, ground_truth)

    # 正解キーがOCRにはあるが検出にないものを列挙
    ocr_keys = _collect_keys(normalized_texts)
    det_keys = _collect_keys(detect_texts)
    print("\nOCRにあるが検出にない:")
    for item in ground_truth:
        if _match_ground_truth(item, ocr_keys) and not _match_ground_truth(item, det_keys):
            # OCR生テキストを探す
            key = item["key"]
            samples = []
            for line in ocr.lines:
                ck = _canon_dim_key(line.text) or _canon_dim_key(normalize_drawing_text(line.text))
                if ck == key or ck in (item.get("aliases") or []):
                    samples.append(f"{line.text!r} score={line.score:.2f}")
            print(f"  {item['text']}: {samples[:5]}")
    doc.close()


if __name__ == "__main__":
    main()
