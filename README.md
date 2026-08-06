# 加工図面作成支援ツール

PDF図面に公差・色分け・印・注記などを追加し、別名で保存する Windows 向けツールです。  
原本は変更しません。処理はローカル PC 内で完結します。

## セキュリティ

- 図面データは **外部クラウドへ送信しません**
- 生成 AI は **使用しません**
- OCR・画像処理は **ローカル PC 上の RapidOCR / Windows OCR** で実行します

## 使い方

1. `dist\加工図面作成支援ツール.exe` を起動する
2. PDF を開く（ドラッグ＆ドロップ可）
3. 左の作業メニューを上から順に進める

| 作業 | 操作 | 流れ |
|------|------|------|
| 公差未記載の寸法 | 規格を選び **公差未記載寸法を検出** | 水色＝反映、灰色＝除外 → **一括反映** |
| 寸法を直す | 必要なときだけ | 修正後に色分けへ |
| 寸法を色分け | **対象寸法を検出** | ピンク・黄色を確認 → **一括反映** |
| 製品を塗る | 断面・加工部の範囲 | — |

4. **別名で保存** で書き出す

### 解除操作

| タイミング | 操作 |
|-----------|------|
| 公差の候補確認中 | 候補をクリック → 灰色で除外 |
| 公差反映後 | 「不要な公差を解除」→ クリック |
| 色分けの候補確認中 | 候補をクリック → 灰色で除外 |
| 色分け反映後 | 「不要な色を解除」→ クリック |

### 動作確認

画面右下に **ビルド識別子** が表示されます。

| 表示例 | 意味 |
|--------|------|
| `2026-08-06-ocr6b / OCR有効` | 最新ビルドで高解像度 OCR が利用可能 |
| `OCR無効` | RapidOCR が起動できていない |

画像 PDF を開いたとき、ファイル情報に **「画像PDF（高解像度OCR）」** と出れば、新しい OCR 経路で動作しています。  
初回検出は数分かかる場合があります。

## 対応 PDF

| 種類 | 検出方式 |
|------|----------|
| テキスト PDF | PDF 内の文字情報を優先 |
| 画像 PDF | RapidOCR（高解像度）+ タイル OCR + Windows OCR 補完 |

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
- PyInstaller による EXE 生成
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

再ビルド後は `src\drawing_assist\ocr_config.py` の `APP_BUILD_ID` も更新してください。

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

## プロジェクト構成

```
Drawing_Assist/
├── assets/              # アイコン元画像
├── dist/                # ビルド成果物（EXE）
├── scripts/             # 検証・デバッグ用スクリプト
├── src/drawing_assist/  # アプリ本体
│   ├── web/             # UI（HTML / CSS / JS）
│   ├── local_ocr.py     # RapidOCR 処理
│   ├── general_tolerance.py
│   ├── image_preprocessor.py
│   ├── ocr_config.py    # 閾値・ビルド ID
│   └── web_app.py       # エントリポイント
├── build_exe.ps1
├── run.ps1
└── requirements.txt
```

## 注意

- テキスト PDF・画像 PDF の両方に対応
- 一般公差・色分けの検出は、画像 PDF では RapidOCR を中心に Windows OCR で補完
- パスワード付き PDF には非対応
- 検出結果は候補として提示され、最終確認はユーザーが行う前提です
