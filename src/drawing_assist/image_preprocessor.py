"""Raster preprocessing shared by the local OCR paths."""

from __future__ import annotations

from PIL import Image, ImageOps


def _legacy_rapidocr_raster(image: Image.Image) -> Image.Image:
    """Return the previous, conservative RapidOCR input image."""

    gray = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    return Image.merge("RGB", (gray, gray, gray))


def prepare_raster_for_structure(image: Image.Image) -> Image.Image:
    """Keep all drafting lines for geometric candidate verification."""

    return _legacy_rapidocr_raster(image)


def prepare_raster_for_rapidocr(image: Image.Image) -> Image.Image:
    """Prepare a raster drawing for RapidOCR using OpenCV.

    Scanned drawings commonly contain uneven paper shading and long drafting
    lines that join to small glyphs. Local contrast normalization, adaptive
    binarization, and conservative long-line suppression make glyphs
    separable. The caller keeps a separate structural raster for geometric
    candidate verification, so this OCR-only cleanup cannot remove the
    dimension-line evidence used after recognition. If OpenCV is unavailable,
    retain the former conservative input.
    """

    try:
        import cv2
        import numpy as np
    except ImportError:
        return _legacy_rapidocr_raster(image)

    gray = np.asarray(image.convert("L"))
    # CLAHE handles uneven/aged scan backgrounds while keeping faint text.
    contrast = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(24, 24)).apply(gray)
    denoised = cv2.medianBlur(contrast, 3)
    ink = cv2.adaptiveThreshold(
        denoised,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        9,
    )
    # Suppress only strokes much longer than an OCR character. This removes
    # extension lines, center lines, and title-block rules that commonly join
    # to the small dimension glyphs in raster drawings.
    minimum_line = max(80, min(gray.shape) // 55)
    horizontal = cv2.morphologyEx(
        ink,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (minimum_line, 1)),
    )
    vertical = cv2.morphologyEx(
        ink,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, minimum_line)),
    )
    line_mask = cv2.bitwise_or(horizontal, vertical)
    cleaned = cv2.inpaint(denoised, line_mask, 2, cv2.INPAINT_TELEA)
    binary = cv2.adaptiveThreshold(
        cleaned,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        9,
    )
    # Retain anti-aliasing from the contrast image around thin characters.
    prepared = cv2.min(cleaned, binary)
    return Image.fromarray(prepared, mode="L").convert("RGB")


def prepare_raster_for_ocr(image: Image.Image) -> Image.Image:
    """Prepare a high-contrast raster for Windows OCR crops."""

    gray = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    return gray.point(lambda value: 255 if value > 205 else 0, mode="1").convert("L")


def to_ocr_rgb(image: Image.Image) -> Image.Image:
    """Return an RGB image suitable for an OCR engine."""

    if image.mode == "RGB":
        return image
    if image.mode == "L":
        return Image.merge("RGB", (image, image, image))
    return image.convert("RGB")
