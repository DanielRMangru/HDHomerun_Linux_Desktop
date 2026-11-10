"""Local DVR Recording Manager and background timer scheduler."""

from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

from ..config import AppConfig, ScheduledRecording


def _sanitize_filename(text: str) -> str:
    clean = re.sub(r"[^\w\s.-]", "", text).strip()
    clean = re.sub(r"\s+", "_", clean)
    return clean[:80] or "Recording"


class ActiveRecordingWorker:
    """Handles streaming a single channel to local storage."""

    def __init__(
        self,
        job: ScheduledRecording,
        use_ffmpeg: bool = False,
        rec_format: str = "ts",
        on_update: Optional[Callable[[ScheduledRecording], None]] = None,
    ) -> None:
        self.job = job
        self.use_ffmpeg = use_ffmpeg and bool(shutil.which("ffmpeg"))
        self.rec_format = rec_format if self.use_ffmpeg else "ts"
        self.on_update = on_update
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self.started_at: float = time.time()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.5)

    def _run(self) -> None:
        self.job.status = "recording"
        self.job.error_message = ""
        if self.on_update:
            self.on_update(self.job)

        out_path = Path(self.job.output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if self.use_ffmpeg:
            self._run_ffmpeg(out_path)
        else:
            self._run_http_stream(out_path)

        if out_path.exists():
            self.job.bytes_written = out_path.stat().st_size

        if self.job.bytes_written > 0 and not self.job.error_message:
            self.job.status = "completed"
        elif self._stop_event.is_set() and self.job.bytes_written > 0:
            self.job.status = "completed"
        elif self._stop_event.is_set():
            self.job.status = "cancelled"
        else:
            self.job.status = "failed"
            if not self.job.error_message:
                self.job.error_message = "Zero bytes received from tuner stream."

        if self.on_update:
            self.on_update(self.job)

    def _run_http_stream(self, out_path: Path) -> None:
        last_notify = time.time()
        try:
            with requests.get(
                self.job.stream_url,
                stream=True,
                timeout=(5.0, 5.0),
                headers={"User-Agent": "HDHomeRun-Linux-Desktop-DVR/1.0"},
            ) as resp:
                resp.raise_for_status()
                with open(out_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if self._stop_event.is_set() or time.time() >= self.job.effective_end:
                            break
                        if chunk:
                            fh.write(chunk)
                            self.job.bytes_written += len(chunk)
                            now = time.time()
                            if now - last_notify >= 1.0:
                                last_notify = now
                                if self.on_update:
                                    self.on_update(self.job)
        except Exception as exc:
            if not self._stop_event.is_set() and self.job.bytes_written == 0:
                self.job.error_message = str(exc)

    def _run_ffmpeg(self, out_path: Path) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            self.job.stream_url,
            "-c",
            "copy",
            str(out_path),
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            while self._proc.poll() is None:
                if self._stop_event.is_set() or time.time() >= self.job.effective_end:
                    self._proc.terminate()
                    break
                if out_path.exists():
                    self.job.bytes_written = out_path.stat().st_size
                if self.on_update:
                    self.on_update(self.job)
                time.sleep(1.0)
            self._proc.wait(timeout=3.0)
        except Exception as exc:
            if not self._stop_event.is_set() and self.job.bytes_written == 0:
                self.job.error_message = str(exc)


class DVRManager:
    """Manages scheduled and active recordings and runs a background timer thread."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._active_workers: Dict[str, ActiveRecordingWorker] = {}
        self._stop_scheduler = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None
        self._listeners: List[Callable[[], None]] = []

        # Clean up any stale "recording" status left over from an abrupt exit
        now = time.time()
        changed = False
        for rec in self.config.recordings:
            if rec.status == "recording" and rec.effective_end < now:
                if rec.output_path and Path(rec.output_path).exists():
                    rec.bytes_written = Path(rec.output_path).stat().st_size
                    rec.status = "completed" if rec.bytes_written > 0 else "failed"
                else:
                    rec.status = "failed"
                changed = True
        if changed:
            self.config.save()

    def add_listener(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def _notify_listeners(self) -> None:
        with self._lock:
            callbacks = list(self._listeners)
        for cb in callbacks:
            try:
                cb()
            except Exception:
                pass

    def start_scheduler(self) -> None:
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._stop_scheduler.clear()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        self._stop_scheduler.set()
        with self._lock:
            workers = list(self._active_workers.values())
        for w in workers:
            w.stop()

    def _scheduler_loop(self) -> None:
        while not self._stop_scheduler.is_set():
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(1.5)

    def _tick(self) -> None:
        now = time.time()
        with self._lock:
            # Clean up finished workers
            finished_ids = [
                rid for rid, w in self._active_workers.items() if not w.is_alive
            ]
            for rid in finished_ids:
                self._active_workers.pop(rid, None)

            for rec in self.config.recordings:
                if rec.status == "scheduled":
                    if now >= rec.effective_end:
                        rec.status = "failed"
                        rec.error_message = "Missed scheduled recording window."
                        self.config.save()
                        self._notify_listeners()
                    elif rec.effective_start <= now < rec.effective_end:
                        if rec.id not in self._active_workers:
                            self._launch_worker(rec)

    def _build_output_path(self, rec: ScheduledRecording) -> str:
        rec_dir = Path(self.config.recordings_dir).expanduser()
        rec_dir.mkdir(parents=True, exist_ok=True)
        dt_str = datetime.datetime.fromtimestamp(rec.start_time).strftime("%Y%m%d_%H%M%S")
        ch_part = _sanitize_filename(f"Ch{rec.channel_number}_{rec.channel_name}")
        title_part = _sanitize_filename(rec.title)
        ep_part = f"_{_sanitize_filename(rec.episode_number)}" if rec.episode_number else ""
        ext = "mp4" if (self.config.use_ffmpeg_for_recording and self.config.recording_format == "mp4") else "ts"
        filename = f"{dt_str}_{ch_part}_{title_part}{ep_part}.{ext}"
        return str(rec_dir / filename)

    def _launch_worker(self, rec: ScheduledRecording) -> None:
        if not rec.output_path:
            rec.output_path = self._build_output_path(rec)
        worker = ActiveRecordingWorker(
            job=rec,
            use_ffmpeg=self.config.use_ffmpeg_for_recording,
            rec_format=self.config.recording_format,
            on_update=self._on_worker_update,
        )
        self._active_workers[rec.id] = worker
        worker.start()

    def _on_worker_update(self, rec: ScheduledRecording) -> None:
        self.config.save()
        self._notify_listeners()

    def schedule_recording(
        self,
        title: str,
        channel_number: str,
        channel_name: str,
        stream_url: str,
        start_time: float,
        end_time: float,
        episode_title: str = "",
        episode_number: str = "",
        synopsis: str = "",
        pre_padding_sec: Optional[int] = None,
        post_padding_sec: Optional[int] = None,
    ) -> ScheduledRecording:
        """Create and persist a scheduled recording (starts immediately if currently in window)."""
        with self._lock:
            # Check if an identical recording is already scheduled/recording
            for existing in self.config.recordings:
                if (
                    existing.channel_number == channel_number
                    and abs(existing.start_time - start_time) < 60
                    and existing.status in ("scheduled", "recording")
                ):
                    return existing

            pre = self.config.default_pre_padding_sec if pre_padding_sec is None else pre_padding_sec
            post = self.config.default_post_padding_sec if post_padding_sec is None else post_padding_sec
            now = time.time()
            if start_time <= now:
                pre = 0

            rec = ScheduledRecording(
                id=uuid.uuid4().hex[:10],
                title=title or f"Channel {channel_number} Recording",
                channel_number=channel_number,
                channel_name=channel_name,
                stream_url=stream_url,
                start_time=start_time,
                end_time=end_time,
                pre_padding_sec=pre,
                post_padding_sec=post,
                episode_title=episode_title,
                episode_number=episode_number,
                synopsis=synopsis,
                status="scheduled",
                created_at=now,
            )
            rec.output_path = self._build_output_path(rec)
            self.config.recordings.insert(0, rec)
            self.config.save()

            if rec.effective_start <= now < rec.effective_end:
                self._launch_worker(rec)

        self._notify_listeners()
        return rec

    def start_instant_recording(
        self,
        channel_number: str,
        channel_name: str,
        stream_url: str,
        duration_minutes: int = 60,
        title: str = "",
    ) -> ScheduledRecording:
        """Start recording a channel immediately for `duration_minutes`."""
        now = time.time()
        end_ts = now + (duration_minutes * 60)
        rec_title = title or f"Live {channel_name} ({channel_number})"
        return self.schedule_recording(
            title=rec_title,
            channel_number=channel_number,
            channel_name=channel_name,
            stream_url=stream_url,
            start_time=now,
            end_time=end_ts,
            pre_padding_sec=0,
            post_padding_sec=0,
        )

    def stop_or_cancel_recording(self, rec_id: str) -> bool:
        """Stop an active recording or cancel a scheduled one."""
        with self._lock:
            worker = self._active_workers.pop(rec_id, None)
            target: Optional[ScheduledRecording] = None
            for rec in self.config.recordings:
                if rec.id == rec_id:
                    target = rec
                    break

        if worker:
            worker.stop()
            return True

        if target:
            if target.status == "scheduled":
                target.status = "cancelled"
            elif target.status == "recording":
                target.status = "completed" if target.bytes_written > 0 else "cancelled"
            self.config.save()
            self._notify_listeners()
            return True
        return False

    def delete_recording(self, rec_id: str, delete_file: bool = False) -> bool:
        """Remove a recording entry and optionally delete its media file on disk."""
        self.stop_or_cancel_recording(rec_id)
        with self._lock:
            target: Optional[ScheduledRecording] = None
            for rec in self.config.recordings:
                if rec.id == rec_id:
                    target = rec
                    break
            if not target:
                return False
            if delete_file and target.output_path:
                try:
                    p = Path(target.output_path)
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
            self.config.recordings = [r for r in self.config.recordings if r.id != rec_id]
            self.config.save()
        self._notify_listeners()
        return True

    def is_channel_recording(self, guide_number: str) -> Optional[ScheduledRecording]:
        with self._lock:
            for rec in self.config.recordings:
                if rec.channel_number == guide_number and rec.status == "recording":
                    return rec
            return None

