"""Mining runs that continue in the background while the MCP server answers other requests.

A client whose tool calls time out after a minute or so cannot wait for a run over a very large
file, so ``mine_dataset`` can start one here instead and poll ``mining_status``. A run is the
same ``run_mining`` call as in the foreground, on its own thread; if the server stops, the run
stops at its last checkpoint and resumes when the same call is made again.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from datamining_skill.application.orchestrator import MiningProgress, MiningSummary
from datamining_skill.application.support import describe_failure
from datamining_skill.domain.exceptions import DataMiningException, MiningCancelledException

RunFn = Callable[[Callable[[MiningProgress], None]], dict[str, Any]]
_MAX_RUNNING = 4
_KEEP_FINISHED = 20


class JobAlreadyRunningError(Exception):
    """A run for the same source, output and settings is in progress in this server."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"this mining job is already running as job '{job_id}'")
        self.job_id = job_id


class TooManyJobsError(Exception):
    """The server runs as many background jobs as it allows."""


class _Job:
    def __init__(self, job_id: str, label: str, key: str) -> None:
        self.job_id = job_id
        self.label = label
        self.key = key
        self.status = "running"
        self.chunks_done = 0
        self.chunks_total = 0
        self.records_written = 0
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.started_at = _now()
        self.finished_at: str | None = None
        self.cancel_requested = threading.Event()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job": self.label,
            "status": self.status,
            "chunks_done": self.chunks_done,
            "chunks_total": self.chunks_total,
            "records_written": self.records_written,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MiningJobs:
    """Starts, tracks and cancels background mining runs. Safe to call from any thread."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, _Job] = OrderedDict()

    def start(self, label: str, key: str, run: RunFn) -> dict[str, Any]:
        """Run ``run`` in the background and return the new job's description.

        ``run`` receives a progress callback and returns the result dictionary. ``key``
        identifies the job (source, output, settings): a second start while it runs is refused.
        """
        with self._lock:
            running = [job for job in self._jobs.values() if job.status == "running"]
            for job in running:
                if job.key == key:
                    raise JobAlreadyRunningError(job.job_id)
            if len(running) >= _MAX_RUNNING:
                raise TooManyJobsError(
                    f"{_MAX_RUNNING} mining jobs are already running; wait for one to finish "
                    "or cancel it"
                )
            job = _Job(os.urandom(6).hex(), label, key)
            self._jobs[job.job_id] = job
            self._evict_finished()
            description = job.to_dict()
        threading.Thread(
            target=self._work, args=(job, run), name=f"mining-{job.job_id}", daemon=True
        ).start()
        return description

    def status(self, job_id: str | None) -> list[dict[str, Any]] | None:
        """One job (a list of one), every job if ``job_id`` is ``None``, ``None`` if unknown."""
        with self._lock:
            if job_id is None:
                return [job.to_dict() for job in self._jobs.values()]
            job = self._jobs.get(job_id)
            return None if job is None else [job.to_dict()]

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        """Ask a running job to stop after its current chunk; ``None`` if the id is unknown."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status == "running":
                job.cancel_requested.set()
            return job.to_dict()

    def _evict_finished(self) -> None:
        finished = [job_id for job_id, job in self._jobs.items() if job.status != "running"]
        for job_id in finished[: max(len(finished) - _KEEP_FINISHED, 0)]:
            del self._jobs[job_id]

    def _work(self, job: _Job, run: RunFn) -> None:
        def on_progress(update: MiningProgress) -> None:
            with self._lock:
                job.chunks_done = update.chunks_done
                job.chunks_total = update.chunks_total
                job.records_written = update.records_written
            if job.cancel_requested.is_set():
                raise MiningCancelledException("the run was cancelled")

        status = "failed"
        result: dict[str, Any] | None = None
        error: str | None = None
        try:
            result = run(on_progress)
            status = "succeeded" if result.get("succeeded") else "failed"
            if status == "failed":
                error = f"{result.get('chunks_failed')} chunk(s) failed; the result is incomplete"
                if result.get("first_error"):
                    error += f". First failure: {result['first_error']}"
        except MiningCancelledException:
            status = "cancelled"
            error = "cancelled; call mine_dataset again with the same arguments to resume"
        except DataMiningException as exc:
            error = f"{type(exc).__name__}: {exc}"
        except OSError as exc:
            error = f"file system error: {describe_failure(exc)}"
        except Exception as exc:
            self._logger.exception("mining job %s failed unexpectedly", job.job_id)
            error = f"internal error ({type(exc).__name__}); see the server log"
        with self._lock:
            job.status = status
            job.result = result
            job.error = error
            job.finished_at = _now()


def summary_data(summary: MiningSummary, output_display: str) -> dict[str, Any]:
    """A mining summary as the dictionary tools and jobs report."""
    data = summary.to_dict()
    data["output_path"] = output_display
    data["succeeded"] = summary.succeeded
    return data
