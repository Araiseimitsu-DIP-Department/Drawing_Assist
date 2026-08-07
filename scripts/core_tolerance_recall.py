"""core.pdf 向け: Text Extractor 相当（Windows OCR）と RapidOCR の寸法再現率を測る。"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path

import fitz
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from drawing_assist.drawing_text_normalizer import normalize_drawing_text
from drawing_assist.general_tolerance import (
    _run_windows_ocr_batch,
    detect_general_tolerance_candidates,
)
from drawing_assist.local_ocr import (
    analyze_page,
    analyze_scanned_page_tiles,
    enrich_scanned_ocr_page,
)
from drawing_assist.web_app import _detect_local_dimension_markings

PDF = Path(r"c:\Users\SEIZOU20\Desktop\core.pdf")
OCR_SCRIPT = ROOT / "src" / "drawing_assist" / "windows_ocr.ps1"
OUT_DIR = ROOT / "tmp" / "text_extractor_compare"
GT_PATH = ROOT / "scripts" / "ocr_ground_truth" / "core_tolerance_texts.json"

# 公差付き寸法・角度のゆるい照合用
_TOL_MARK = re.compile(r"[±士土亇干]")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", normalize_drawing_text(text))


def _canon_dim_key(text: str) -> str | None:
    """φ13.7±0.02 / 12.7±0.05 / 60° などを比較キーにする。"""

    compact = _compact(text)
    compact = (
        compact.replace("Ω", "φ")
        .replace("Φ", "φ")
        .replace("Ø", "φ")
        .replace("⌀", "φ")
        .replace("士", "±")
        .replace("土", "±")
        .replace("亇", "±")
        .replace("干", "±")
        .replace("。", "°")
    )
    compact = re.sub(r"^[^\dφCR]+", "", compact)
    # 角度
    angle = re.fullmatch(r"(\d+(?:\.\d+)?)°", compact)
    if angle:
        return f"angle:{float(angle.group(1))}"
    # 公差付き
    if "±" in compact or re.search(r"[+\-＋－]\d", compact):
        prefix = ""
        body = compact
        if body[:1] in {"φ", "C", "R"}:
            prefix = body[0].lower() if body[0] == "φ" else body[0]
            body = body[1:]
        body = body.replace("＋", "+").replace("－", "-")
        m = re.match(
            r"(?P<nom>\d+(?:\.\d+)?)(?:±(?P<tol>\d+(?:\.\d+)?)|"
            r"(?P<sign>[+\-])(?P<unilateral>\d+(?:\.\d+)?))",
            body,
        )
        if not m:
            return None
        nom = float(m.group("nom"))
        if m.group("tol") is not None:
            return f"{prefix}{nom:g}±{float(m.group('tol')):g}"
        return f"{prefix}{nom:g}{m.group('sign')}{float(m.group('unilateral')):g}"
    # 素の直径など（参考）
    plain = re.fullmatch(r"(φ|C|R)?(\d+(?:\.\d+)?)°?", compact)
    if plain:
        prefix = plain.group(1) or ""
        return f"{prefix}{float(plain.group(2)):g}"
    return None


def _match_ground_truth(expected: dict, corpus_keys: set[str]) -> bool:
    key = expected.get("key")
    if key and key in corpus_keys:
        return True
    # aliases
    for alias in expected.get("aliases") or []:
        if alias in corpus_keys:
            return True
    return False


def _collect_keys(texts: list[str]) -> set[str]:
    keys: set[str] = set()
    for text in texts:
        key = _canon_dim_key(text)
        if key:
            keys.add(key)
        # OCRが分断している場合のゆるい連結は別処理
    return keys


def harvest_windows_ocr(page: fitz.Page) -> list[str]:
    """高解像度ページ全体 + 拡大タイルの Windows OCR テキストを集める。"""

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    zoom = 5.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    full_path = OUT_DIR / "core_winocr_z5.png"
    pix.save(str(full_path))
    texts: list[str] = []
    t0 = time.perf_counter()
    full = _run_windows_ocr_batch([full_path], OCR_SCRIPT)[0]
    for line in full.get("lines") or []:
        text = str(line.get("text") or "").strip()
        if text:
            texts.append(text)
    print(f"Windows full z5: lines={len(texts)} time={time.perf_counter()-t0:.1f}s")

    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    tile_w, tile_h = 1400, 1000
    step_x, step_y = int(tile_w * 0.65), int(tile_h * 0.65)
    margin_x, margin_y = int(pix.width * 0.06), int(pix.height * 0.06)
    with tempfile.TemporaryDirectory(prefix="winocr_tiles_") as tmp:
        tmp_dir = Path(tmp)
        paths: list[Path] = []
        for y in range(margin_y, max(margin_y + 1, pix.height - margin_y - tile_h + 1), step_y):
            for x in range(
                margin_x, max(margin_x + 1, pix.width - margin_x - tile_w + 1), step_x
            ):
                x1 = min(pix.width, x + tile_w)
                y1 = min(pix.height, y + tile_h)
                crop = image.crop((x, y, x1, y1))
                enlarged = crop.resize(
                    (crop.width * 2, crop.height * 2),
                    Image.Resampling.LANCZOS,
                )
                path = tmp_dir / f"t{len(paths)}.png"
                enlarged.save(path)
                paths.append(path)
        print(f"Windows tiles: count={len(paths)}")
        t1 = time.perf_counter()
        results = _run_windows_ocr_batch(paths, OCR_SCRIPT)
        print(f"Windows tiles OCR time={time.perf_counter()-t1:.1f}s")
        for result in results:
            for line in result.get("lines") or []:
                text = str(line.get("text") or "").strip()
                if text:
                    texts.append(text)
    return texts


def evaluate_corpus(name: str, texts: list[str], ground_truth: list[dict]) -> dict:
    keys = _collect_keys(texts)
    hits = []
    misses = []
    for item in ground_truth:
        ok = _match_ground_truth(item, keys)
        (hits if ok else misses).append(item)
    recall = len(hits) / len(ground_truth) if ground_truth else 0.0
    print(f"\n[{name}] texts={len(texts)} keys={len(keys)} recall={recall:.1%} "
          f"({len(hits)}/{len(ground_truth)})")
    if misses:
        print("  miss:")
        for item in misses:
            print(f"    {item.get('text') or item.get('key')}")
    if hits:
        print("  hit:")
        for item in hits:
            print(f"    {item.get('text') or item.get('key')}")
    return {
        "name": name,
        "text_count": len(texts),
        "key_count": len(keys),
        "recall": round(recall, 3),
        "hit_count": len(hits),
        "miss_count": len(misses),
        "hits": hits,
        "misses": misses,
        "sample_keys": sorted(keys)[:80],
    }


def default_ground_truth() -> list[dict]:
    """ユーザー提示例 + Text Extractor で取れやすい公差付き寸法の初期正解。"""

    return [
        {
            "text": "φ13.7±0.02",
            "key": "φ13.7±0.02",
            "aliases": ["13.7±0.02", "φ13.7+0.02"],
            "source": "user",
        },
        {
            "text": "12.7±0.05",
            "key": "12.7±0.05",
            "aliases": ["φ12.7±0.05"],
            "source": "user",
        },
        {
            "text": "60°",
            "key": "angle:60",
            "aliases": ["60"],
            "source": "user",
        },
        {
            "text": "7.9±0.05",
            "key": "7.9±0.05",
            "aliases": ["φ7.9±0.05", "7.9±0.01"],
            "source": "drawing_candidate",
        },
        {
            "text": "0.2±0.02",
            "key": "0.2±0.02",
            "aliases": [],
            "source": "drawing_candidate",
        },
        {
            "text": "0.15±0.025",
            "key": "0.15±0.025",
            "aliases": [],
            "source": "drawing_candidate",
        },
        {
            "text": "φ16±0.1",
            "key": "φ16±0.1",
            "aliases": ["16±0.1"],
            "source": "drawing_candidate",
        },
        {
            "text": "φ22±0.1",
            "key": "φ22±0.1",
            "aliases": ["φ22+0.1", "22±0.1"],
            "source": "drawing_candidate",
        },
        {
            "text": "φ2.5±0.1",
            "key": "φ2.5±0.1",
            "aliases": ["2.5±0.1"],
            "source": "drawing_candidate",
        },
        {
            "text": "φ4.5±0.1",
            "key": "φ4.5±0.1",
            "aliases": ["4.5±0.1"],
            "source": "drawing_candidate",
        },
        {
            "text": "R0.2±0.1",
            "key": "R0.2±0.1",
            "aliases": [],
            "source": "drawing_candidate",
        },
        {
            "text": "φ12-0.35",
            "key": "φ12-0.35",
            "aliases": ["12-0.35", "φ12±0.35"],
            "source": "drawing_candidate",
        },
    ]


def main() -> None:
    if not PDF.exists():
        raise SystemExit(f"PDF not found: {PDF}")

    if GT_PATH.exists():
        payload = json.loads(GT_PATH.read_text(encoding="utf-8"))
        ground_truth = list(payload.get("items") or [])
    else:
        ground_truth = default_ground_truth()
        GT_PATH.parent.mkdir(parents=True, exist_ok=True)
        GT_PATH.write_text(
            json.dumps(
                {
                    "name": "core",
                    "description": "公差付き寸法のテキスト再現率用正解（Text Extractor目標）",
                    "items": ground_truth,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote ground truth: {GT_PATH}")

    doc = fitz.open(PDF)
    page = doc[0]

    # 1) RapidOCR page
    t0 = time.perf_counter()
    rapid_page = analyze_page(page, scanned=True)
    print(f"Rapid page: lines={len(rapid_page.lines)} time={time.perf_counter()-t0:.1f}s")
    rapid_texts = [line.text for line in rapid_page.lines]

    # 2) RapidOCR + tiles
    with_tiles = "--no-tiles" not in sys.argv
    if with_tiles:
        t1 = time.perf_counter()
        tile_lines = analyze_scanned_page_tiles(page)
        rapid_merged = enrich_scanned_ocr_page(rapid_page, tile_lines)
        print(
            f"Rapid tiles: tile_lines={len(tile_lines)} merged={len(rapid_merged.lines)} "
            f"time={time.perf_counter()-t1:.1f}s"
        )
        rapid_merged_texts = [line.text for line in rapid_merged.lines]
    else:
        rapid_merged = rapid_page
        rapid_merged_texts = rapid_texts
        print("Rapid tiles: skipped")

    # 3) Windows OCR (Text Extractor 相当)
    win_texts = harvest_windows_ocr(page)

    reports = [
        evaluate_corpus("rapid_page", rapid_texts, ground_truth),
        evaluate_corpus("rapid_merged", rapid_merged_texts, ground_truth),
        evaluate_corpus("windows_ocr", win_texts, ground_truth),
    ]

    # 4) 検出候補（色分け・公差）
    general = detect_general_tolerance_candidates(
        page,
        0,
        standard="jis_b_0405",
        grade="m",
        angle_shorter_side_length=10.0,
        ocr_script=OCR_SCRIPT,
        local_ocr_page=rapid_merged,
        scanned_tile_lines=(),
    )
    markings = _detect_local_dimension_markings(
        page,
        rapid_merged,
        include_plain_dimensions=True,
        scanned_page=True,
    )
    detect_texts = [c.source_text for c in general] + [m.source_text for m in markings]
    reports.append(evaluate_corpus("detection", detect_texts, ground_truth))

    out = {
        "pdf": str(PDF),
        "ground_truth_count": len(ground_truth),
        "reports": reports,
        "windows_sample": win_texts[:120],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "core_recall_report.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved: {out_path}")
    doc.close()


if __name__ == "__main__":
    main()
