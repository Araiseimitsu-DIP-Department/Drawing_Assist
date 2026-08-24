"""OCR処理で使う解像度・閾値の定数。"""

from __future__ import annotations

# 画面表示・ビルド確認用（再ビルドのたびに更新する）
APP_BUILD_ID = "2026-08-24-color63"

# RapidOCR（ページ全体）のレンダリング倍率
LOCAL_OCR_ZOOM_MIN = 2.4
LOCAL_OCR_ZOOM_MAX = 3.2
LOCAL_OCR_ZOOM_NUMERATOR = 3200.0

# 画像PDF向けページ全体OCR（高解像度）
SCANNED_OCR_ZOOM_MIN = 4.0
SCANNED_OCR_ZOOM_MAX = 4.8
SCANNED_OCR_ZOOM_NUMERATOR = 4800.0

# 画像PDF向けタイルOCR（小寸法の読み取り用）
SCANNED_TILE_ZOOM_MIN = 6.0
SCANNED_TILE_ZOOM_MAX = 7.2
SCANNED_TILE_ZOOM_NUMERATOR = 7600.0
SCANNED_TILE_MIN_CONFIDENCE = 0.34

# RapidOCR 行の最低信頼度
LOCAL_OCR_MIN_CONFIDENCE = 0.42

# 一般公差の素の数値に要求する最低信頼度
BARE_NUMBER_MIN_CONFIDENCE = 0.82

# Scanned drawings often produce a correct bare dimension at a lower score
# than normal text.  Keep the stricter vector/native threshold unchanged and
# use this lower floor only when the scanned-page geometry checks also pass.
SCANNED_BARE_NUMBER_MIN_CONFIDENCE = 0.68

# A low-confidence OCR result can still be useful when independent page/tile/
# rotation passes agree on the same reading.
SCANNED_AGREED_BARE_NUMBER_MIN_CONFIDENCE = 0.58

# Below this score a scanned candidate is shown for review instead of being
# selected for automatic application.
SCANNED_REVIEW_CONFIDENCE = 0.72

# 画像PDFでタイルOCR補完を行う候補数の閾値
SUPPLEMENT_THRESHOLD_VECTOR = 12
SUPPLEMENT_THRESHOLD_SCANNED = 20

# 詳細図角度の切り出し倍率
DETAIL_ANGLE_ZOOM = 10.0
