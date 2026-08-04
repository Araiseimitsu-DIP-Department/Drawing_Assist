param(
    [string]$ImagePath,
    [string]$ImageListPath,
    [string]$LanguageTag = "ja"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime

[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

$asTaskMethods = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
        $_.Name -eq "AsTask" -and
        $_.IsGenericMethod -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1"
    }

function Await-WinRtOperation {
    param(
        [Parameter(Mandatory = $true)]$Operation,
        [Parameter(Mandatory = $true)][Type]$ResultType
    )

    $method = $asTaskMethods[0].MakeGenericMethod($ResultType)
    $task = $method.Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}

$language = [Windows.Globalization.Language]::new($LanguageTag)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) {
    throw "Windows OCR language is not installed: $LanguageTag"
}

function Read-OcrImage {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $file = Await-WinRtOperation (
        [Windows.Storage.StorageFile]::GetFileFromPathAsync($resolvedPath)
    ) ([Windows.Storage.StorageFile])
    $stream = Await-WinRtOperation (
        $file.OpenAsync([Windows.Storage.FileAccessMode]::Read)
    ) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await-WinRtOperation (
            [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
        ) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-WinRtOperation (
            $decoder.GetSoftwareBitmapAsync()
        ) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await-WinRtOperation (
            $engine.RecognizeAsync($bitmap)
        ) ([Windows.Media.Ocr.OcrResult])
    } finally {
        $stream.Dispose()
    }

    $lines = foreach ($line in $result.Lines) {
        $words = foreach ($word in $line.Words) {
            [ordered]@{
                text = $word.Text
                x = [Math]::Round($word.BoundingRect.X, 3)
                y = [Math]::Round($word.BoundingRect.Y, 3)
                width = [Math]::Round($word.BoundingRect.Width, 3)
                height = [Math]::Round($word.BoundingRect.Height, 3)
            }
        }
        [ordered]@{
            text = $line.Text
            words = @($words)
        }
    }
    [ordered]@{
        text_angle = if ($null -eq $result.TextAngle) {
            $null
        } else {
            [Math]::Round($result.TextAngle, 3)
        }
        lines = @($lines)
    }
}

if ($ImageListPath) {
    $batch = Get-Content -Raw -LiteralPath $ImageListPath -Encoding UTF8 |
        ConvertFrom-Json
    $results = foreach ($path in @($batch.paths)) {
        Read-OcrImage -Path ([string]$path)
    }
    [ordered]@{ results = @($results) } |
        ConvertTo-Json -Depth 7 -Compress
} elseif ($ImagePath) {
    Read-OcrImage -Path $ImagePath | ConvertTo-Json -Depth 6 -Compress
} else {
    throw "ImagePath or ImageListPath is required."
}
