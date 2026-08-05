"""OCRパイプラインの段階別件数を記録する診断ログ。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import logging
from typing import Any

logger = logging.getLogger("drawing_assist.ocr")


@dataclass
class OcrPipelineRecorder:
    """処理段階ごとの件数と除外理由を集計する。"""

    pipeline: str
    counts: dict[str, int] = field(default_factory=dict)
    rejects: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def set_count(self, stage: str, value: int) -> OcrPipelineRecorder:
        self.counts[stage] = value
        return self

    def add_count(self, stage: str, delta: int = 1) -> None:
        self.counts[stage] = self.counts.get(stage, 0) + delta

    def reject(self, reason: str, delta: int = 1) -> None:
        self.rejects[reason] += delta

    def summary(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "counts": dict(self.counts),
            "rejects": dict(self.rejects),
        }

    def log_summary(self) -> None:
        parts = [f"{key}={value}" for key, value in sorted(self.counts.items())]
        reject_parts = [
            f"{key}={value}" for key, value in sorted(self.rejects.items())
        ]
        logger.info(
            "[%s] counts: %s",
            self.pipeline,
            ", ".join(parts) if parts else "(none)",
        )
        if reject_parts:
            logger.info(
                "[%s] rejects: %s",
                self.pipeline,
                ", ".join(reject_parts),
            )
