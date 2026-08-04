from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import statistics

import fitz


def family_key(stem: str) -> str:
    normalized = stem.upper()
    normalized = re.sub(r"(?:[#_-](?:0?\d{1,3}|[A-Z]))+$", "", normalized)
    return normalized or stem.upper()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--sample", type=int, default=120)
    args = parser.parse_args()

    files = sorted(
        args.root.glob("*.pdf"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    sample = files[: max(1, args.sample)]
    pages = 0
    text_pages = 0
    image_pages = 0
    annotated_pages = 0
    annotations = 0
    annotation_types: Counter[str] = Counter()
    failures = 0
    page_counts: list[int] = []
    for path in sample:
        try:
            document = fitz.open(path)
            page_counts.append(document.page_count)
            for page in document:
                pages += 1
                if len(page.get_text().strip()) >= 40:
                    text_pages += 1
                page_area = max(1.0, page.rect.get_area())
                if any(
                    (fitz.Rect(info.get("bbox")) & page.rect).get_area()
                    / page_area
                    >= 0.72
                    for info in page.get_image_info()
                    if len(info.get("bbox") or ()) == 4
                ):
                    image_pages += 1
                page_annotations = list(page.annots() or [])
                if page_annotations:
                    annotated_pages += 1
                annotations += len(page_annotations)
                annotation_types.update(
                    annotation.type[1] for annotation in page_annotations
                )
            document.close()
        except Exception:
            failures += 1

    families = Counter(family_key(path.stem) for path in files)
    repeated_files = sum(count for count in families.values() if count > 1)
    print(
        json.dumps(
            {
                "pdf_count": len(files),
                "sampled": len(sample),
                "sample_failures": failures,
                "pages": pages,
                "text_page_ratio": round(text_pages / max(1, pages), 3),
                "image_page_ratio": round(image_pages / max(1, pages), 3),
                "annotated_page_ratio": round(
                    annotated_pages / max(1, pages), 3
                ),
                "annotations": annotations,
                "annotation_types": annotation_types,
                "median_pages": statistics.median(page_counts) if page_counts else 0,
                "repeated_family_file_ratio": round(
                    repeated_files / max(1, len(files)), 3
                ),
                "repeated_family_count": sum(
                    count > 1 for count in families.values()
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
