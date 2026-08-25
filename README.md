# 加工図面作成支援ツール

PDF図面に公差・色分け・印・注記などを追加し、別名で保存する Windows 向けツールです。  
原本は変更しません。処理はローカル PC 内で完結します。

## セキュリティ

- 図面データは **外部クラウドへ送信しません**
- 生成 AI は **使用しません**
- OCR・画像処理は **ローカル PC 上の RapidOCR / Windows OCR / OpenCV** で実行します

## 使い方

1. `dist\加工図面作成支援ツール.exe` を起動する
2. PDF を開く（ドラッグ＆ドロップ可）
3. 左の「やりたいこと」を上から順に進める

| 作業 | 操作 | 流れ |
|------|------|------|
| 公差を自動で入れる | 規格を選び **公差未記載寸法を検出** | 水色＝反映、灰色＝除外（画像PDFではオレンジ＝個別確認）→ **一括反映** |
| 寸法を追加・修正 | 必要なときだけ | 寸法追加・書き直し・二重線消し。修正後に色分けへ |
| 寸法を色分け | **1. 自動で対象寸法を探す** | ピンク・黄色を確認 → **3. この内容で色分けする**。合わない所だけ手動で直す |
| 製品を塗る | 断面・加工部を見やすく | — |

公差未記載寸法の検出で候補が0件の場合でも、右の色分けパネルから次工程に進めます。

4. **別名で保存** で書き出す

### 解除操作

| タイミング | 操作 |
|-----------|------|
| 公差の候補確認中 | 候補をクリック → 水色／灰色で切替（オレンジは一括反映対象外） |
| 公差反映後 | 「不要な公差を解除」→ クリック |
| 色分けの候補確認中 | 候補をクリック → 灰色で除外 |
| 色分け反映後 | 「不要な色を解除」→ クリック |

### 動作確認

画面右下に **ビルド識別子** が表示されます。

| 表示例 | 意味 |
|--------|------|
| `2026-08-25-color77 / OCR有効` | タイルOCRの小R/C・斜め角度を局所高精細OCRへ渡し、上下公差・縦g6の塗り範囲と画面デザインを調整 |
| `OCR無効` | RapidOCR が起動できていない |

画像 PDF を開いたとき、ファイル情報に **「画像PDF（高解像度OCR）」** と出れば、新しい OCR 経路で動作しています。  
初回検出は数分かかる場合があります。

### 候補色の意味（公差未記載寸法）

| 色 | 意味 |
|----|------|
| 水色 | 一括反映の対象 |
| 灰色 | 除外 |
| オレンジ | 画像PDFで OCR 信頼度が低い候補。目視確認用で、一括反映には含まれません |

### 候補色の意味（寸法色分け）

| 色 | 意味 |
|----|------|
| ピンク | 厳しい公差（0.03 mm 以内・角度 1° 以内） |
| 黄色 | それ以外の寸法・角度 |
| 灰色 | 除外 |

## 対応 PDF

| 種類 | 検出方式 |
|------|----------|
| テキスト PDF | PDF 内の文字情報を優先 |
| 画像 PDF | ページ全体 OCR + タイル OCR + 縦寸法領域 OCR + Windows OCR 補完 + 寸法線の幾何情報（OpenCV） |

画像 PDF では、公差未記載寸法と色分け検出で OCR 結果を共有・キャッシュします。  
主な後処理:

- 公称値と公差が別行になった場合の結合（上下公差・縦書きはめあい）
- 縦書き `g6` を OCR が `96` と読んだ場合の復元（隣接偏差があるときのみ）
- 小数点欠落の正規化、複数 OCR 経路の一致判定
- 寸法線に近い候補を優先する幾何スコア（誤検出の抑制。候補そのものは棄却しない）
- 誤関連付けによる過大な塗り矩形の抑制

## 開発・ビルド

### 前提

- Windows 10 / 11
- Python 3.12 推奨

### 初回セットアップ

```powershell
.\build_exe.ps1
```

`build_exe.ps1` は次を自動実行します。

- 仮想環境の作成（`.venv`）
- 依存パッケージのインストール
- PyInstaller による EXE 生成（RapidOCR モデルを `--collect-all rapidocr` で同梱）
- デスクトップへの EXE コピー

### 開発実行（EXE なし）

```powershell
.\run.ps1 "C:\path\to\drawing.pdf"
```

### アイコン設定

`assets\app_icon.ico` が必要です。PNG から生成する例:

```powershell
.\.venv\Scripts\python.exe -c "from PIL import Image; img=Image.open(r'assets\ChatGPT Image 2026年8月4日 16_47_43.png').convert('RGBA'); img.save(r'assets\app_icon.ico', format='ICO', sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])"
```

再ビルド後は次も更新してください。

- `src\drawing_assist\ocr_config.py` の `APP_BUILD_ID`
- `src\drawing_assist\web\index.html` の CSS / JS 参照クエリ（`?v=`）

## テスト

```powershell
.\.venv\Scripts\python.exe scripts\general_tolerance_test.py
.\.venv\Scripts\python.exe scripts\local_ocr_pipeline_test.py
.\.venv\Scripts\python.exe scripts\image_preprocessor_test.py
.\.venv\Scripts\python.exe scripts\drawing_text_normalizer_test.py
```

OCR 検出の確認用（オーバーレイ画像を `tmp\pdfs\` に出力）:

```powershell
.\.venv\Scripts\python.exe scripts\ocr_detection_smoke_test.py --pipeline
```

`general_tolerance_test.py` には、上下公差のレーン帰属・縦 g6 復元・過大塗り矩形の抑制などの回帰テストも含みます。

## プロジェクト構成

```
Drawing_Assist/
├── assets/              # アイコン元画像
├── dist/                # ビルド成果物（EXE）
├── scripts/             # 検証・デバッグ用スクリプト
├── src/drawing_assist/  # アプリ本体
│   ├── web/             # UI（HTML / CSS / JS）
│   ├── local_ocr.py     # RapidOCR・タイル OCR・縦寸法 OCR
│   ├── drawing_structure.py  # 寸法線の幾何情報（OpenCV）
│   ├── general_tolerance.py
│   ├── drawing_text_normalizer.py  # OCR 文字列の正規化
│   ├── image_preprocessor.py
│   ├── ocr_config.py    # 閾値・ビルド ID
│   ├── pdf_editor.py    # PDF 描画・候補データ
│   └── web_app.py       # エントリポイント
├── build_exe.ps1
├── run.ps1
└── requirements.txt
```

## 注意

- テキスト PDF・画像 PDF の両方に対応
- 一般公差・色分けの検出は、画像 PDF では RapidOCR を中心に Windows OCR で補完
- 画像 PDF の低信頼度候補はオレンジ色で目視確認用に表示されます
- パスワード付き PDF には非対応
- 検出結果は候補として提示され、最終確認はユーザーが行う前提です
