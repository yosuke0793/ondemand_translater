#!/usr/bin/env python3
"""Tkinter desktop UI for local video translation."""

from __future__ import annotations

import subprocess
import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from local_job_manager import LocalJobManager, default_data_dir


VOICE_OPTIONS = {
    '落ち着いた男性の声 ("en-US-AndrewNeural")': "en-US-AndrewNeural",
    '自然で聞き取りやすい男性の声 ("en-US-BrianNeural")': "en-US-BrianNeural",
    '情報系向きの男性の声 ("en-US-ChristopherNeural")': "en-US-ChristopherNeural",
    '張りのある男性の声 ("en-US-GuyNeural")': "en-US-GuyNeural",
    '明瞭で安定した女性の声 ("en-US-AriaNeural")': "en-US-AriaNeural",
    '明るく自然な女性の声 ("en-US-EmmaNeural")': "en-US-EmmaNeural",
    'やわらかく親しみやすい女性の声 ("en-US-JennyNeural")': "en-US-JennyNeural",
    '会話調で滑らかな女性の声 ("en-US-AvaNeural")': "en-US-AvaNeural",
    '落ち着いた英国女性の声 ("en-GB-SoniaNeural")': "en-GB-SoniaNeural",
    '明瞭な英国男性の声 ("en-GB-RyanNeural")': "en-GB-RyanNeural",
}
DEFAULT_VOICE_LABEL = next(
    label for label, voice_name in VOICE_OPTIONS.items() if voice_name == "en-US-AriaNeural"
)


def load_preferences(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        preferences = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return preferences if isinstance(preferences, dict) else {}


def save_preferences(path: Path, preferences: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(preferences, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_path.replace(path)


class StandaloneApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Translate Service")
        self.root.minsize(760, 520)
        self.manager = LocalJobManager(default_data_dir())
        self.manager.start()
        self.preferences_path = self.manager.data_dir / "settings.json"
        preferences = load_preferences(self.preferences_path)
        self.video_path = tk.StringVar()
        self.output_dir = tk.StringVar(
            value=preferences.get("output_directory") or str(self.manager.data_dir / "outputs")
        )
        self.translation_backend = tk.StringVar(value="gemini")
        saved_voice = preferences.get("voice")
        saved_voice_label = next(
            (label for label, voice_name in VOICE_OPTIONS.items() if voice_name == saved_voice),
            DEFAULT_VOICE_LABEL,
        )
        self.voice_choice = tk.StringVar(value=saved_voice_label)
        self.transcription_preset = tk.StringVar(value="balanced")
        self.status_var = tk.StringVar(value="待機中")
        self._build_ui()
        self.output_dir.trace_add("write", self._save_preferences)
        self.voice_choice.trace_add("write", self._save_preferences)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._refresh()

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(8, weight=1)

        ttk.Label(container, text="動画ファイル (.mp4)").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(container, textvariable=self.video_path).grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(container, text="選択...", command=self.choose_video).grid(row=0, column=2, pady=4)

        ttk.Label(container, text="動画出力先").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(container, textvariable=self.output_dir).grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(container, text="選択...", command=self.choose_output_dir).grid(row=1, column=2, pady=4)

        ttk.Label(container, text="専門用語").grid(row=2, column=0, sticky="nw", pady=4)
        self.glossary = tk.Text(container, height=4, width=50)
        self.glossary.grid(row=2, column=1, columnspan=2, sticky="ew", padx=8, pady=4)

        settings = ttk.LabelFrame(container, text="変換設定", padding=8)
        settings.grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)
        for column in range(6):
            settings.columnconfigure(column, weight=1)
        ttk.Label(settings, text="翻訳").grid(row=0, column=0, sticky="w")
        ttk.Combobox(settings, textvariable=self.translation_backend, values=("argos", "deepl", "gemini"), state="readonly", width=12).grid(row=0, column=1, padx=4)
        ttk.Label(settings, text="音声").grid(row=0, column=2, sticky="w")
        ttk.Combobox(
            settings,
            textvariable=self.voice_choice,
            values=list(VOICE_OPTIONS),
            state="readonly",
            width=38,
        ).grid(row=0, column=3, padx=4, sticky="ew")
        ttk.Label(settings, text="文字起こし").grid(row=0, column=4, sticky="w")
        ttk.Combobox(settings, textvariable=self.transcription_preset, values=("fast", "balanced", "high-accuracy"), state="readonly", width=14).grid(row=0, column=5, padx=4)
        actions = ttk.Frame(container)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Button(actions, text="翻訳開始(キューに追加)", command=self.add_job).pack(side=tk.LEFT)
        ttk.Button(actions, text="選択ジョブをキャンセル", command=self.cancel_selected).pack(side=tk.LEFT, padx=8)
        ttk.Label(actions, textvariable=self.status_var).pack(side=tk.RIGHT)

        ttk.Label(container, text="ジョブ一覧").grid(row=7, column=0, columnspan=3, sticky="w", pady=(12, 4))
        columns = ("job_id", "filename", "status", "progress", "stage", "created", "output")
        self.jobs = ttk.Treeview(container, columns=columns, show="headings", selectmode="browse")
        headings = {"job_id": "Job ID", "filename": "入力動画", "status": "状態", "progress": "進捗", "stage": "処理段階", "created": "登録日時", "output": "出力"}
        widths = {"job_id": 190, "filename": 190, "status": 80, "progress": 70, "stage": 180, "created": 160, "output": 220}
        for column in columns:
            self.jobs.heading(column, text=headings[column])
            self.jobs.column(column, width=widths[column], anchor="w")
        self.jobs.grid(row=8, column=0, columnspan=3, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self.jobs.yview)
        scrollbar.grid(row=8, column=3, sticky="ns")
        self.jobs.configure(yscrollcommand=scrollbar.set)
        self.jobs.bind("<Double-1>", lambda _event: self.open_selected())
        ttk.Button(container, text="完了ファイルを開く", command=self.open_selected).grid(row=9, column=2, sticky="e", pady=8)

    def choose_video(self) -> None:
        selected = filedialog.askopenfilename(
            title="入力動画を選択",
            filetypes=(("MP4 video", "*.mp4"), ("All files", "*.*")),
        )
        if selected:
            self.video_path.set(selected)

    def choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(title="動画の出力先を選択")
        if selected:
            self.output_dir.set(selected)

    def add_job(self) -> None:
        try:
            options = {
                "translation_backend": self.translation_backend.get(),
                "voice": VOICE_OPTIONS.get(self.voice_choice.get(), "en-US-AriaNeural"),
                "transcription_preset": self.transcription_preset.get(),
                "burn_subs": False,
            }
            job_id = self.manager.register_job(
                Path(self.video_path.get()),
                self.glossary.get("1.0", tk.END).strip(),
                options,
                Path(self.output_dir.get()),
            )
        except (FileNotFoundError, ValueError) as exc:
            messagebox.showerror("ジョブを追加できません", str(exc))
            return
        self.status_var.set(f"追加しました: {job_id}")
        self.video_path.set("")
        self.glossary.delete("1.0", tk.END)
        self._refresh()

    def selected_job_id(self) -> str | None:
        selection = self.jobs.selection()
        if not selection:
            return None
        values = self.jobs.item(selection[0], "values")
        return str(values[0]) if values else None

    def cancel_selected(self) -> None:
        job_id = self.selected_job_id()
        if not job_id:
            return
        if self.manager.cancel(job_id):
            self.status_var.set(f"キャンセルしました: {job_id}")
            self._refresh()

    def open_selected(self) -> None:
        job_id = self.selected_job_id()
        if not job_id:
            return
        job = self.manager.get_job(job_id)
        if not job or job["status"] != "done":
            messagebox.showinfo("未完了", "完了したジョブを選択してください")
            return
        output_path = Path(job["output_video"])
        if not output_path.exists():
            messagebox.showerror("出力なし", str(output_path))
            return
        if os.name == "nt":
            subprocess.run(["explorer.exe", "/select,", str(output_path)], check=False)
        else:
            subprocess.run(["open", "-R", str(output_path)], check=False)

    def _refresh(self) -> None:
        for item in self.jobs.get_children():
            self.jobs.delete(item)
        status_labels = {"queued": "待機中", "running": "処理中", "done": "完了", "error": "エラー", "cancelled": "キャンセル"}
        for job in self.manager.get_jobs():
            self.jobs.insert(
                "",
                tk.END,
                values=(
                    job["job_id"],
                    job.get("input_filename") or Path(job["input_video"]).name,
                    status_labels.get(job["status"], job["status"]),
                    f"{job.get('progress_percent', 0)}%",
                    job.get("current_stage_label") or "待機中",
                    job["created_at"],
                    job["output_video"] if job["status"] == "done" else "",
                ),
            )
        self.root.after(1000, self._refresh)

    def close(self) -> None:
        self._save_preferences()
        self.manager.stop()
        self.root.destroy()

    def _save_preferences(self, *_args) -> None:
        save_preferences(
            self.preferences_path,
            {
                "output_directory": self.output_dir.get(),
                "voice": VOICE_OPTIONS.get(self.voice_choice.get(), "en-US-AriaNeural"),
            },
        )


def main() -> None:
    root = tk.Tk()
    StandaloneApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
