"""OCR向けの画像前処理。"""

from __future__ import annotations

from PIL import Image, ImageOps


def prepare_raster_for_rapidocr(image: Image.Image) -> Image.Image:
    """RapidOCR向けの軽いコントラスト補正。

    二値化は細い文字の誤認識を増やすため、autocontrast のみ適用する。
  """

    gray = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    return Image.merge("RGB", (gray, gray, gray))


def prepare_raster_for_ocr(image: Image.Image) -> Image.Image:
    """スキャン図面向けにコントラストを上げ、薄い文字を残す二値化を行う。

    Windows OCR 用に general_tolerance で使っていた処理を共通化したもの。
    RapidOCR にも同じ前処理を適用し、エンジン間の取りこぼし差を減らす。
    """

    gray = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    # 薄い小数点・直径記号・公差記号を残すため、閾値は高めに設定する。
    return gray.point(lambda value: 255 if value > 205 else 0, mode="1").convert("L")


def to_ocr_rgb(image: Image.Image) -> Image.Image:
    """OCRエンジンへ渡す3チャンネル画像へ変換する。"""

    if image.mode == "RGB":
        return image
    if image.mode == "L":
        return Image.merge("RGB", (image, image, image))
    return image.convert("RGB")
