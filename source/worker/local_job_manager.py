#!/usr/bin/env python3
"""Local FIFO job manager for the standalone translation application."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


TERMINAL_STATES = {"done", "error", "cancelled"}

PIPELINE_STAGES = {
    "extract_audio": (8, "音声を抽出中"),
    "transcribe_audio": (25, "日本語音声を文字起こし中"),
    "clean_jp_srt": (8, "日本語字幕を整形中"),
    "translate_jp_srt": (18, "英語へ翻訳中"),
    "write_en_txt": (3, "英語字幕を書き出し中"),
    "generate_en_mp3": (25, "英語音声を生成中"),
    "overlay_video_text": (5, "動画内テキストを置換中"),
    "mux_final_video": (5, "動画と英語音声を結合中"),
    "burn_subtitles": (3, "字幕を焼き込み中"),
    "embed_subtitles": (3, "字幕を埋め込み中"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_path.replace(path)


class LocalJobManager:
    """Own local job storage and execute one translation process at a time."""

    def __init__(
        self,
        data_dir: Path,
        worker_root: Path | None = None,
        command_builder: Callable[[dict, Path], list[str]] | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.worker_root = (worker_root or Path(__file__).resolve().parent).resolve()
        self.command_builder = command_builder or self._default_command
        self.poll_interval = max(0.1, poll_interval)
        self.queue_dir = self.data_dir / "queue"
        self.processing_dir = self.data_dir / "processing"
        self.done_dir = self.data_dir / "done"
        self.error_dir = self.data_dir / "error"
        self.jobs_dir = self.data_dir / "jobs"
        self.logs_dir = self.data_dir / "logs"
        self.db_path = self.data_dir / "jobs.db"
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._process_job_id: str | None = None
        self._lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        for directory in (
            self.queue_dir,
            self.processing_dir,
            self.done_dir,
            self.error_dir,
            self.jobs_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    progress_percent INTEGER NOT NULL DEFAULT 0,
                    current_stage TEXT,
                    current_stage_label TEXT,
                    input_filename TEXT NOT NULL DEFAULT 'input.mp4',
                    input_video TEXT NOT NULL,
                    output_video TEXT NOT NULL,
                    output_directory TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error_message TEXT
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "progress_percent" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN progress_percent INTEGER NOT NULL DEFAULT 0")
            if "current_stage" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN current_stage TEXT")
            if "current_stage_label" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN current_stage_label TEXT")
            if "input_filename" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN input_filename TEXT NOT NULL DEFAULT 'input.mp4'"
                )
            if "output_directory" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN output_directory TEXT")
            connection.commit()
        self._recover_processing_jobs()

    def _recover_processing_jobs(self) -> None:
        for job_file in sorted(self.processing_dir.glob("*.json")):
            job_id = job_file.stem
            with sqlite3.connect(self.db_path) as connection:
                row = connection.execute(
                    "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row and row[0] == "running":
                    connection.execute(
                        "UPDATE jobs SET status = 'queued', started_at = NULL WHERE job_id = ?",
                        (job_id,),
                    )
                    connection.commit()
            target = self.queue_dir / job_file.name
            if not target.exists():
                job_file.replace(target)

    def _default_command(self, job: dict, job_dir: Path) -> list[str]:
        options = job.get("options", {})
        python_executable = os.environ.get("TRANSLATE_WORKER_PYTHON", sys.executable)
        command = [
            python_executable,
            "-m",
            "lecture_poc",
            "--input",
            str(job_dir / "input.mp4"),
            "--outdir",
            str(job_dir),
            "--jp-clean-mode",
            str(options.get("jp_clean_mode", "rule")),
            "--transcription-preset",
            str(options.get("transcription_preset", "balanced")),
            "--voice",
            str(options.get("voice", "en-US-AriaNeural")),
            "--tts-volume",
            str(options.get("tts_volume", "+100%")),
            "--tts-timing-mode",
            str(options.get("tts_timing_mode", "no-overlap")),
            "--subs-sync-mode",
            str(options.get("subs_sync_mode", "tts-aligned")),
            "--translation-backend",
            str(options.get("translation_backend", "gemini")),
        ]
        glossary_text = str(job.get("glossary_text") or "").strip()
        if glossary_text:
            prompt_path = self._write_job_prompt(job_dir, glossary_text)
            command.extend(["--whisper-prompt-file", str(prompt_path)])
        if options.get("burn_subs", False):
            command.append("--burn-subs")
        else:
            command.extend(["--embed-subs", "--embed-subs-container", "mp4"])
        if options.get("force", True):
            command.append("--force")
        return command

    def _write_job_prompt(self, job_dir: Path, glossary_text: str) -> Path:
        default_prompt = self.worker_root / "lecture_poc" / "prompts" / "whisper_initial_prompt.txt"
        content = default_prompt.read_text(encoding="utf-8") if default_prompt.exists() else ""
        if "【ユーザー定義欄（蓄積）】" not in content:
            content = content.rstrip() + "\n\n【ユーザー定義欄（蓄積）】\n"
        content += glossary_text + "\n"
        prompt_path = job_dir / "whisper_prompt.txt"
        prompt_path.write_text(content, encoding="utf-8")
        return prompt_path

    def register_job(
        self,
        input_video: Path,
        glossary_text: str = "",
        options: dict | None = None,
        output_directory: Path | None = None,
    ) -> str:
        source = Path(input_video).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"入力動画が見つかりません: {source}")
        if source.suffix.lower() != ".mp4":
            raise ValueError("mp4ファイルのみ対応しています")

        job_id = str(uuid.uuid4())
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(source, job_dir / "input.mp4")
        destination_dir = Path(output_directory).expanduser().resolve() if output_directory else job_dir
        destination_dir.mkdir(parents=True, exist_ok=True)
        output_path = destination_dir / f"{source.stem}_translated.mp4"
        payload = {
            "job_id": job_id,
            "created_at": now_iso(),
            "status": "queued",
            "progress_percent": 0,
            "current_stage": None,
            "current_stage_label": "待機中",
            "input_filename": source.name,
            "input_video": str(job_dir / "input.mp4"),
            "output_video": str(output_path),
            "output_directory": str(destination_dir),
            "glossary_text": glossary_text,
            "options": options or {},
        }
        write_json(self.queue_dir / f"{job_id}.json", payload)
        write_json(job_dir / "status.json", payload)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO jobs (job_id, status, progress_percent, current_stage, current_stage_label, input_filename, input_video, output_video, output_directory, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, "queued", 0, None, "待機中", payload["input_filename"], payload["input_video"], payload["output_video"], payload["output_directory"], payload["created_at"]),
            )
            connection.commit()
        self._wake_event.set()
        return job_id

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="local-translation-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        process = self._process
        if process and process.poll() is None:
            process.terminate()
        thread = self._thread
        if thread:
            thread.join(timeout)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            if self._process_job_id == job_id and self._process and self._process.poll() is None:
                self._process.terminate()
                self._update_status(job_id, "cancelled", error_message="ユーザーによりキャンセルされました")
                return True
            with sqlite3.connect(self.db_path) as connection:
                row = connection.execute(
                    "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if not row or row[0] != "queued":
                    return False
                connection.execute(
                    "UPDATE jobs SET status = 'cancelled', completed_at = ? WHERE job_id = ?",
                    (now_iso(), job_id),
                )
                connection.commit()
            self._move_job_file(job_id, self.error_dir)
            self._update_status(job_id, "cancelled", error_message="ユーザーによりキャンセルされました")
            return True

    def get_jobs(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str) -> dict | None:
        for job in self.get_jobs():
            if job["job_id"] == job_id:
                return job
        return None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            job_file = self._next_job_file()
            if job_file is None:
                self._wake_event.wait(self.poll_interval)
                self._wake_event.clear()
                continue
            self._run_job(job_file)

    def _next_job_file(self) -> Path | None:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT job_id FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        job_file = self.queue_dir / f"{row[0]}.json"
        return job_file if job_file.exists() else None

    def _run_job(self, queue_file: Path) -> None:
        job_id = queue_file.stem
        processing_file = self.processing_dir / queue_file.name
        try:
            queue_file.replace(processing_file)
            job = json.loads(processing_file.read_text(encoding="utf-8"))
            job_dir = self.jobs_dir / job_id
            self._update_status(job_id, "running", started_at=now_iso())
            command = self.command_builder(job, job_dir)
            log_path = self.logs_dir / f"{job_id}.log"
            with log_path.open("w", encoding="utf-8") as log_file:
                log_file.write("command: " + " ".join(command) + "\n")
                log_file.flush()
                with self._lock:
                    self._process_job_id = job_id
                    self._process = subprocess.Popen(
                        command,
                        cwd=self.worker_root,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        text=True,
                        start_new_session=True,
                    )
                return_code = self._wait_for_process(job_id, self._process, job_dir)
            with self._lock:
                self._process = None
                self._process_job_id = None
            if return_code != 0:
                raise RuntimeError(f"変換処理が失敗しました (exit code {return_code})")
            local_output = self._ensure_output(job_dir)
            output_path = Path(job["output_video"]).expanduser().resolve()
            if output_path != local_output.resolve():
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_output, output_path)
            self._move_job_file(job_id, self.done_dir)
            self._update_status(
                job_id,
                "done",
                completed_at=now_iso(),
                progress_percent=100,
                current_stage=None,
                current_stage_label="完了",
            )
        except Exception as exc:
            with self._lock:
                self._process = None
                self._process_job_id = None
            if self.get_job(job_id) and self.get_job(job_id)["status"] == "cancelled":
                self._move_job_file(job_id, self.error_dir)
                continue_processing = False
            else:
                self._move_job_file(job_id, self.error_dir)
                self._update_status(job_id, "error", completed_at=now_iso(), error_message=str(exc))
                continue_processing = True
            if not continue_processing:
                return

    def _wait_for_process(
        self, job_id: str, process: subprocess.Popen[str], job_dir: Path
    ) -> int:
        pipeline_log = job_dir / "pipeline.log"
        while process.poll() is None:
            self._update_progress_from_log(job_id, pipeline_log)
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        self._update_progress_from_log(job_id, pipeline_log)
        return process.returncode or 0

    def _update_progress_from_log(self, job_id: str, pipeline_log: Path) -> None:
        if not pipeline_log.exists():
            return
        try:
            lines = pipeline_log.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        started_stages = [
            line.split("START ", 1)[1].strip()
            for line in lines
            if "START " in line
        ]
        if not started_stages:
            return
        current_stage = started_stages[-1]
        if current_stage not in PIPELINE_STAGES:
            return
        # Keep a fixed denominator so the percentage never jumps backward when
        # an optional stage (such as video text overlay) is absent.
        stage_items = list(PIPELINE_STAGES)
        total_weight = sum(PIPELINE_STAGES[stage][0] for stage in stage_items)
        completed_weight = sum(
            PIPELINE_STAGES[stage][0]
            for stage in stage_items
            if any(f"DONE  {stage} " in line for line in lines)
        )
        progress = min(99, int(completed_weight * 100 / total_weight))
        self._update_status(
            job_id,
            "running",
            progress_percent=progress,
            current_stage=current_stage,
            current_stage_label=PIPELINE_STAGES[current_stage][1],
        )

    def _ensure_output(self, job_dir: Path) -> Path:
        output_path = job_dir / "output.mp4"
        if output_path.exists() and output_path.stat().st_size > 0:
            return output_path
        for candidate_name in ("final_soft_sub.mp4", "final_sub.mp4", "final.mp4"):
            candidate = job_dir / candidate_name
            if candidate.exists() and candidate.stat().st_size > 0:
                candidate.replace(output_path)
                return output_path
        pipeline_log = job_dir / "pipeline.log"
        detail = "output.mp4 が生成されませんでした"
        if pipeline_log.exists():
            detail += f"。ログ: {pipeline_log}"
        raise RuntimeError(detail)

    def _update_status(
        self,
        job_id: str,
        status: str,
        started_at: str | None = None,
        completed_at: str | None = None,
        error_message: str | None = None,
        progress_percent: int | None = None,
        current_stage: str | None = None,
        current_stage_label: str | None = None,
    ) -> None:
        with sqlite3.connect(self.db_path) as connection:
            current = connection.execute(
                "SELECT started_at, completed_at, error_message FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if not current:
                return
            connection.execute(
                "UPDATE jobs SET status = ?, progress_percent = COALESCE(?, progress_percent), current_stage = COALESCE(?, current_stage), current_stage_label = COALESCE(?, current_stage_label), started_at = COALESCE(?, started_at), completed_at = COALESCE(?, completed_at), error_message = ? WHERE job_id = ?",
                (status, progress_percent, current_stage, current_stage_label, started_at, completed_at, error_message, job_id),
            )
            connection.commit()
        job_dir = self.jobs_dir / job_id
        status_payload = self.get_job(job_id) or {"job_id": job_id}
        status_payload["status"] = status
        status_payload["updated_at"] = now_iso()
        if progress_percent is not None:
            status_payload["progress_percent"] = progress_percent
        if current_stage is not None:
            status_payload["current_stage"] = current_stage
        if current_stage_label is not None:
            status_payload["current_stage_label"] = current_stage_label
        if error_message is not None:
            status_payload["error_message"] = error_message
        write_json(job_dir / "status.json", status_payload)

    def _move_job_file(self, job_id: str, target_dir: Path) -> None:
        for source_dir in (self.queue_dir, self.processing_dir):
            source = source_dir / f"{job_id}.json"
            if source.exists():
                source.replace(target_dir / source.name)
                return


def default_data_dir() -> Path:
    configured = os.environ.get("TRANSLATE_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TranslateService"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "TranslateService"
    return Path.home() / ".local" / "share" / "translate-service"


__all__ = ["LocalJobManager", "default_data_dir", "TERMINAL_STATES"]
