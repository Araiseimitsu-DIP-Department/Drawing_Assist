"""Text Extractor 相当（Windows OCR）とアプリ OCR/検出の差分を診断する。"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from drawing_assist.drawing_text_normalizer import parse_dimension_token
from drawing_assist.general_tolerance import (
    detect_general_tolerance_candidates,
    _run_windows_ocr,
)
from drawing_assist.local_ocr import (
    analyze_page,
    analyze_scanned_page_tiles,
    enrich_scanned_ocr_page,
)
from drawing_assist.web_app import _detect_local_dimension_markings

OCR_SCRIPT = ROOT / "src" / "drawing_assist" / "windows_ocr.ps1"
OUT_DIR = ROOT / "tmp" / "text_extractor_compare"
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _dim_like_texts(texts: list[str]) -> list[str]:
    hits: list[str] = []
    for text in texts:
        compact = text.replace(" ", "")
        if parse_dimension_token(text) is not None or NUMBER_RE.search(compact):
            hits.append(text)
    return hits


def _unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def diagnose(pdf_path: Path, *, with_tiles: bool) -> None:
    print("=" * 72)
    print(f"PDF: {pdf_path.name}")
    print("=" * 72)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem

    doc = fitz.open(pdf_path)
    page = doc[0]

    t0 = time.perf_counter()
    ocr_page = analyze_page(page, scanned=True)
    print(f"[1] RapidOCR page: lines={len(ocr_page.lines)} time={time.perf_counter()-t0:.1f}s")

    if with_tiles:
        t1 = time.perf_counter()
        tile_lines = analyze_scanned_page_tiles(page)
        ocr_page = enrich_scanned_ocr_page(ocr_page, tile_lines)
        print(
            f"[2] RapidOCR tiles: tile_lines={len(tile_lines)} "
            f"merged={len(ocr_page.lines)} time={time.perf_counter()-t1:.1f}s"
        )
    else:
        print("[2] RapidOCR tiles: skipped")

    # Text Extractor 相当: ページ全体を Windows OCR
    render = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), alpha=False)
    image_path = OUT_DIR / f"{stem}_winocr.png"
    render.save(str(image_path))
    t2 = time.perf_counter()
    win = _run_windows_ocr(image_path, OCR_SCRIPT)
    win_lines = []
    for line in win.get("lines", []) if isinstance(win, dict) else []:
        text = str(line.get("text", "")).strip()
        if text:
            win_lines.append(text)
    # 戻り形式の揺れに対応
    if not win_lines and isinstance(win, dict):
        for key in ("text", "raw_text"):
            if key in win and isinstance(win[key], str) and win[key].strip():
                win_lines = [part.strip() for part in win[key].splitlines() if part.strip()]
                break
    print(f"[3] Windows OCR full-page: lines={len(win_lines)} time={time.perf_counter()-t2:.1f}s")

    rapid_texts = [line.text for line in ocr_page.lines]
    rapid_dims = _unique_keep_order(_dim_like_texts(rapid_texts))
    win_dims = _unique_keep_order(_dim_like_texts(win_lines))

    rapid_set = {t.replace(" ", "") for t in rapid_dims}
    win_set = {t.replace(" ", "") for t in win_dims}
    only_win = [t for t in win_dims if t.replace(" ", "") not in rapid_set]
    only_rapid = [t for t in rapid_dims if t.replace(" ", "") not in win_set]

    print(f"\n寸法っぽい文字列: RapidOCR={len(rapid_dims)} / WindowsOCR={len(win_dims)}")
    print(f"Windowsのみ: {len(only_win)}")
    for text in only_win[:40]:
        print(f"  WIN_ONLY {text!r}")
    if len(only_win) > 40:
        print(f"  ... {len(only_win)-40} more")
    print(f"Rapidのみ: {len(only_rapid)}")
    for text in only_rapid[:20]:
        print(f"  RAPID_ONLY {text!r}")

    t3 = time.perf_counter()
    general = detect_general_tolerance_candidates(
        page,
        0,
        standard="jis_b_0405",
        grade="m",
        angle_shorter_side_length=10.0,
        ocr_script=OCR_SCRIPT,
        local_ocr_page=ocr_page,
        scanned_tile_lines=(),
    )
    markings = _detect_local_dimension_markings(
        page,
        ocr_page,
        include_plain_dimensions=True,
        scanned_page=True,
    )
    print(
        f"\n[4] 検出: general={len(general)} color(plain)={len(markings)} "
        f"time={time.perf_counter()-t3:.1f}s"
    )
    general_texts = [c.source_text for c in general]
    marking_texts = [m.source_text for m in markings]
    detected_set = {t.replace(" ", "") for t in general_texts + marking_texts}

    # OCRでは読めたが検出で落ちたもの
    rapid_but_dropped = [
        t for t in rapid_dims if t.replace(" ", "") not in detected_set and parse_dimension_token(t)
    ]
    print(f"\nRapidで寸法トークン解析できたが検出落ち: {len(rapid_but_dropped)}")
    for text in rapid_but_dropped[:40]:
        print(f"  DROPPED {text!r}")
    if len(rapid_but_dropped) > 40:
        print(f"  ... {len(rapid_but_dropped)-40} more")

    print("\n検出候補(公差):")
    for c in general[:30]:
        print(f"  TOL {c.source_text!r} val={c.nominal_value}")
    print("\n検出候補(色分け plain):")
    for m in markings[:30]:
        print(f"  CLR {m.source_text!r} val={m.nominal_value} tol={m.tolerance_range}")

    dump = {
        "pdf": str(pdf_path),
        "rapid_lines": rapid_texts,
        "windows_lines": win_lines,
        "only_windows_dimlike": only_win,
        "only_rapid_dimlike": only_rapid,
        "rapid_but_dropped": [t for t in rapid_but_dropped],
        "general": general_texts,
        "markings": marking_texts,
    }
    dump_path = OUT_DIR / f"{stem}_compare.json"
    dump_path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved: {dump_path}")
    doc.close()


def main() -> None:
    with_tiles = "--tiles" in sys.argv
    targets = [
        Path(r"c:\Users\SEIZOU20\Desktop\原図.pdf"),
        Path(r"c:\Users\SEIZOU20\Desktop\core.pdf"),
    ]
    for path in targets:
        diagnose(path, with_tiles=with_tiles)


if __name__ == "__main__":
    main()
