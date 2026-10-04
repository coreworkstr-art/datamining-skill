"""Record-count estimation in constant memory and (near-)constant time."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from datamining_skill.application.config import ProfilerConfig
from datamining_skill.domain.models import EncodingInfo, RecordEstimate
from datamining_skill.domain.ports import StreamReader


@dataclass(frozen=True, slots=True)
class _Window:
    offset: int
    length: int
    align: bool


class RecordEstimator:
    """Counts or extrapolates a file's line-delimited records; cost is at most
    ``estimation_windows * window_bytes`` whatever the size.

    * ``exact-scan``: data up to ``exact_count_max_bytes`` is streamed completely.
    * ``stratified-window-extrapolation``: evenly spaced windows give the mean line length and
      the count is ``data_bytes / mean_line_bytes``; each window skips the length-biased line
      it lands in.
    * ``head-window-extrapolation``: multi-byte encodings (UTF-16), where offsets are not line
      aligned, sample only the head.

    A record is a physical line, so quoted CSV fields with line breaks and blank lines make
    the figure approximate.
    """

    EXACT = "exact-scan"
    STRATIFIED = "stratified-window-extrapolation"
    HEAD_ONLY = "head-window-extrapolation"

    def __init__(self, streams: StreamReader, config: ProfilerConfig) -> None:
        self._streams = streams
        self._config = config

    def estimate(
        self,
        path: Path,
        size_bytes: int,
        data_offset: int,
        encoding: EncodingInfo,
    ) -> RecordEstimate:
        """Estimate the records at or after ``data_offset``, which must start a record (past any BOM and header)."""
        data_bytes = size_bytes - data_offset
        if data_bytes <= 0:
            return RecordEstimate(0, True, self.EXACT, 0, 0)

        config = self._config
        if data_bytes <= config.exact_count_max_bytes:
            method = self.EXACT
            windows = [_Window(data_offset, data_bytes, align=False)]
        elif not encoding.ascii_compatible:
            method = self.HEAD_ONLY
            head = config.window_bytes * config.estimation_windows
            windows = [_Window(data_offset, head, align=False)]
        else:
            method = self.STRATIFIED
            windows = self._spread_windows(data_offset, data_bytes)

        sampled_lines = 0
        sampled_bytes = 0
        for window in windows:
            with closing(
                self._streams.lines(
                    path, encoding, window.offset, window.length, align=window.align
                )
            ) as lines:
                for line in lines:
                    sampled_lines += 1
                    sampled_bytes += line.byte_length

        if method == self.EXACT:
            return RecordEstimate(sampled_lines, True, method, sampled_lines, sampled_bytes)
        if sampled_lines == 0 or sampled_bytes == 0:
            return RecordEstimate(0, False, method, 0, 0)
        estimated = round(data_bytes * sampled_lines / sampled_bytes)
        return RecordEstimate(estimated, False, method, sampled_lines, sampled_bytes)

    def _spread_windows(self, data_offset: int, data_bytes: int) -> list[_Window]:
        count = self._config.estimation_windows
        length = self._config.window_bytes
        span = max(data_bytes - length, 0)
        windows: list[_Window] = []
        for index in range(count):
            offset = data_offset + (span * index) // (count - 1)
            # the first window starts on a record boundary by contract
            windows.append(_Window(offset, length, align=index > 0))
        return windows
