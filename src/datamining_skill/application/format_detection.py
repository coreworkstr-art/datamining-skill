"""Picks the data format by comparing the confidence of pluggable handlers."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from datamining_skill.application.config import ProfilerConfig
from datamining_skill.domain.exceptions import UnsupportedDataFormatException
from datamining_skill.domain.models import DataFormat, EncodingInfo, StructureAnalysis
from datamining_skill.domain.ports import FormatHandler, StreamReader


@dataclass(frozen=True, slots=True)
class FormatMatch:
    """The winning handler's verdict for a file."""

    data_format: DataFormat
    analysis: StructureAnalysis


class FormatDetector:
    """Selects the best-fitting ``FormatHandler``.

    Every handler analyses the same bounded head sample through its own stream. The highest
    confidence wins; ties go to extension agreement, then registration order.
    """

    def __init__(
        self,
        handlers: Sequence[FormatHandler],
        streams: StreamReader,
        config: ProfilerConfig,
    ) -> None:
        if not handlers:
            raise ValueError("at least one format handler is required")
        self._handlers = tuple(handlers)
        self._streams = streams
        self._config = config

    def detect(self, path: Path, encoding: EncodingInfo) -> FormatMatch:
        extension = path.suffix.lower()
        scored: list[tuple[float, bool, int, FormatHandler, StructureAnalysis]] = []
        confidence_report: list[str] = []

        for priority, handler in enumerate(self._handlers):
            with closing(
                self._streams.lines(
                    path,
                    encoding,
                    encoding.bom_length,
                    self._config.head_sample_bytes,
                )
            ) as lines:
                analysis = handler.analyze(lines, self._config.structure_sample_records)
            confidence = analysis.confidence if analysis is not None else 0.0
            confidence_report.append(f"{handler.data_format.value}={confidence:.2f}")
            if analysis is not None and confidence >= self._config.min_format_confidence:
                scored.append(
                    (confidence, extension in handler.extensions, -priority, handler, analysis)
                )

        if not scored:
            raise UnsupportedDataFormatException(
                path.name,
                "content matches no supported format "
                f"(minimum confidence {self._config.min_format_confidence:.2f}; "
                f"{', '.join(confidence_report)})",
            )

        _, _, _, handler, analysis = max(scored, key=lambda item: item[:3])
        return FormatMatch(handler.data_format, analysis)
