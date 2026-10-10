#!/usr/bin/env python3
"""Tkinter desktop UI for local video translation."""

from __future__ import annotations

import subprocess
import csv
import json
import os
import multiprocessing
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from local_job_manager import LocalJobManager, default_data_dir
from app_runtime import app_version, bundle_self_test, configure_runtime, resource_root
from credential_store import API_VARIABLES, CredentialError, CredentialStore, validate_speech_region


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
PIPER_VOICE_OPTIONS = {
    '米国英語・女性（LJ Speech／ローカル）': "en_US-ljspeech-medium",
    '米国英語・男性（John／ローカル）': "en_US-john-medium",
}
TTS_BACKENDS = {"Piper（ローカル・TTS外部送信なし）": "piper", "Azure AI Speech（公式API・従量課金）": "azure"}
DEFAULT_GLOSSARY_PATH = resource_root() / "lecture_poc" / "prompts" / "engineering_terms.csv"
CUSTOM_GLOSSARY_FILENAME = "engineering_terms_custom.csv"


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


def read_glossary_csv(path: Path) -> list[str]:
    terms: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        for row_index, row in enumerate(csv.reader(csv_file), start=1):
            values = [value.strip() for value in row if value.strip()]
            if not values:
                continue
            if values[0].startswith("#"):
                continue
            if row_index == 1 and values[0].lower() in {"term", "glossary", "専門用語"}:
                continue
            terms.append(values[0])
    return terms


def merge_glossary_terms(existing: list[str], additions: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for term in [*existing, *additions]:
        normalized = term.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            merged.append(normalized)
    return merged


def write_glossary_csv(path: Path, terms: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["term"])
        writer.writerows([[term] for term in terms if term.strip()])


class StandaloneApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"Translate Service {app_version()}")
        self.root.minsize(760, 520)
        self.credentials = CredentialStore()
        self.manager = LocalJobManager(
            default_data_dir(), credential_provider=self.credentials.worker_credentials,
            speech_credential_provider=self.credentials.speech_credentials,
        )
        self.manager.start()
        self.preferences_path = self.manager.data_dir / "settings.json"
        self.custom_glossary_path = self.manager.data_dir / CUSTOM_GLOSSARY_FILENAME
        self.default_glossary_terms = (
            read_glossary_csv(DEFAULT_GLOSSARY_PATH) if DEFAULT_GLOSSARY_PATH.exists() else []
        )
        custom_glossary_terms = (
            read_glossary_csv(self.custom_glossary_path)
            if self.custom_glossary_path.exists()
            else []
        )
        preferences = load_preferences(self.preferences_path)
        self.hide_api_keys_on_startup = tk.BooleanVar(
            value=preferences.get("hide_api_keys_on_startup") == "true"
        )
        self.video_path = tk.StringVar()
        self.japanese_srt_path = tk.StringVar()
        self.english_srt_path = tk.StringVar()
        self.output_dir = tk.StringVar(
            value=preferences.get("output_directory") or str(self.manager.data_dir / "outputs")
        )
        self.translation_backend = tk.StringVar(value="gemini")
        saved_backend = preferences.get("tts_backend", "piper")
        if saved_backend not in ("piper", "azure"):
            saved_backend = "piper"
        self.tts_choice = tk.StringVar(value=next(
            label for label, name in TTS_BACKENDS.items() if name == saved_backend
        ))
        self.azure_speech_region = tk.StringVar(value=preferences.get("azure_speech_region", ""))
        saved_voice = preferences.get("voice")
        voice_options = PIPER_VOICE_OPTIONS if saved_backend == "piper" else VOICE_OPTIONS
        saved_voice_label = next(
            (label for label, voice_name in voice_options.items() if voice_name == saved_voice),
            next(iter(PIPER_VOICE_OPTIONS)) if saved_backend == "piper" else DEFAULT_VOICE_LABEL,
        )
        self.voice_choice = tk.StringVar(value=saved_voice_label)
        self.transcription_preset = tk.StringVar(value="balanced")
        self.status_var = tk.StringVar(value="待機中")
        self.checked_jobs: set[str] = set()
        self._refresh_timer: str | None = None
        self._build_ui()
        initial_terms = merge_glossary_terms(self.default_glossary_terms, custom_glossary_terms)
        self.glossary.insert("1.0", "\n".join(initial_terms))
        self.output_dir.trace_add("write", self._save_preferences)
        self.voice_choice.trace_add("write", self._save_preferences)
        self.tts_choice.trace_add("write", self._change_tts_backend)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._refresh()
        self.root.after_idle(self._show_startup_api_keys)

    def _show_startup_api_keys(self) -> None:
        if not self.hide_api_keys_on_startup.get():
            self.configure_api_keys()

    def _save_api_startup_preference(self) -> None:
        try:
            self._save_preferences()
        except OSError as exc:
            self.hide_api_keys_on_startup.set(not self.hide_api_keys_on_startup.get())
            messagebox.showerror("設定を保存できません", str(exc), parent=self.root)

    def selected_tts_backend(self) -> str:
        label = self.tts_choice.get()
        if label not in TTS_BACKENDS:
            raise ValueError("音声合成方式を選択してください。")
        return TTS_BACKENDS[label]

    def selected_voice(self) -> str:
        options = PIPER_VOICE_OPTIONS if self.selected_tts_backend() == "piper" else VOICE_OPTIONS
        label = self.voice_choice.get()
        if label not in options:
            raise ValueError("選択した音声合成方式に対応する音声を選択してください。")
        return options[label]

    def _change_tts_backend(self, *_args) -> None:
        backend = self.selected_tts_backend()
        options = PIPER_VOICE_OPTIONS if backend == "piper" else VOICE_OPTIONS
        self.voice_selector.configure(values=list(options))
        self.voice_choice.set(next(iter(PIPER_VOICE_OPTIONS)) if backend == "piper" else DEFAULT_VOICE_LABEL)

    def save_azure_speech_region(self, value: str) -> None:
        region = validate_speech_region(value) if value.strip() else ""
        previous = self.azure_speech_region.get()
        self.azure_speech_region.set(region)
        try:
            self._save_preferences()
        except OSError:
            self.azure_speech_region.set(previous)
            raise

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("App.TFrame", background="#f4f6f8")
        style.configure("Panel.TLabelframe", background="#ffffff", bordercolor="#d8dee6")
        style.configure("Panel.TLabelframe.Label", background="#ffffff", foreground="#243447", font=("Helvetica Neue", 14, "bold"))
        style.configure("App.TLabel", background="#f4f6f8", foreground="#243447", font=("Helvetica Neue", 14))
        style.configure("Panel.TLabel", background="#ffffff", foreground="#243447", font=("Helvetica Neue", 14))
        style.configure("Accent.TButton", background="#2a9d8f", foreground="#ffffff", borderwidth=0, padding=(12, 10), font=("Helvetica Neue", 14, "bold"))
        style.map("Accent.TButton", background=[("active", "#21867b"), ("pressed", "#1c7067")])
        style.configure("Quiet.TButton", background="#e8edf2", foreground="#243447", borderwidth=0, padding=(10, 10), font=("Helvetica Neue", 14))
        style.map("Quiet.TButton", background=[("active", "#d8e1e8"), ("pressed", "#cbd6df")])
        style.configure("App.TEntry", fieldbackground="#ffffff", foreground="#243447", padding=8, font=("Helvetica Neue", 14))
        style.configure("App.TCombobox", fieldbackground="#ffffff", foreground="#243447", padding=7, font=("Helvetica Neue", 14))
        style.configure("App.Treeview", background="#ffffff", fieldbackground="#ffffff", foreground="#243447", rowheight=28, borderwidth=0)
        style.configure("App.Treeview.Heading", background="#243447", foreground="#ffffff", padding=(8, 7), font=("Helvetica Neue", 10, "bold"))
        style.map("App.Treeview", background=[("selected", "#bfe3df")], foreground=[("selected", "#243447")])

        self.root.configure(background="#f4f6f8")
        container = ttk.Frame(self.root, padding=18, style="App.TFrame")
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(10, weight=1)

        ttk.Label(container, text="動画ファイル (.mp4)", style="App.TLabel").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(container, textvariable=self.video_path, style="App.TEntry").grid(row=0, column=1, sticky="ew", padx=10, pady=5)
        ttk.Button(container, text="選択...", command=self.choose_video, style="Quiet.TButton").grid(row=0, column=2, pady=5)

        ttk.Label(container, text="動画出力先", style="App.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(container, textvariable=self.output_dir, style="App.TEntry").grid(row=1, column=1, sticky="ew", padx=10, pady=5)
        ttk.Button(container, text="選択...", command=self.choose_output_dir, style="Quiet.TButton").grid(row=1, column=2, pady=5)

        ttk.Label(container, text="日本語字幕 (.srt)", style="App.TLabel").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(container, textvariable=self.japanese_srt_path, style="App.TEntry").grid(row=2, column=1, sticky="ew", padx=10, pady=5)
        ttk.Button(container, text="選択...", command=self.choose_japanese_srt, style="Quiet.TButton").grid(row=2, column=2, pady=5)

        ttk.Label(container, text="英語字幕 (.srt)", style="App.TLabel").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(container, textvariable=self.english_srt_path, style="App.TEntry").grid(row=3, column=1, sticky="ew", padx=10, pady=5)
        ttk.Button(container, text="選択...", command=self.choose_english_srt, style="Quiet.TButton").grid(row=3, column=2, pady=5)

        ttk.Label(container, text="専門用語", style="App.TLabel").grid(row=4, column=0, sticky="nw", pady=5)
        self.glossary = tk.Text(container, height=4, width=50, relief=tk.FLAT, borderwidth=1, highlightthickness=1, highlightbackground="#d8dee6", highlightcolor="#2a9d8f", background="#ffffff", foreground="#243447", insertbackground="#243447", padx=8, pady=8, font=("Helvetica Neue", 14))
        self.glossary.grid(row=4, column=1, sticky="ew", padx=10, pady=5)
        glossary_actions = ttk.Frame(container, style="App.TFrame")
        glossary_actions.grid(row=4, column=2, sticky="nw", pady=4)
        ttk.Button(glossary_actions, text="一覧確認", command=self.show_glossary, style="Quiet.TButton").pack(fill=tk.X)
        ttk.Button(glossary_actions, text="CSV読込", command=self.import_glossary_csv, style="Quiet.TButton").pack(fill=tk.X, pady=(4, 0))
        ttk.Button(glossary_actions, text="CSV書出", command=self.export_glossary_csv, style="Quiet.TButton").pack(fill=tk.X, pady=(4, 0))

        settings = ttk.LabelFrame(container, text="変換設定", padding=10, style="Panel.TLabelframe")
        settings.grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)
        for column in range(6):
            settings.columnconfigure(column, weight=1)
        ttk.Label(settings, text="翻訳", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Combobox(settings, textvariable=self.translation_backend, values=("argos", "deepl", "gemini"), state="readonly", width=12, style="App.TCombobox").grid(row=0, column=1, padx=4)
        ttk.Label(settings, text="音声", style="Panel.TLabel").grid(row=0, column=2, sticky="w")
        self.voice_selector = ttk.Combobox(
            settings,
            textvariable=self.voice_choice,
            values=list(PIPER_VOICE_OPTIONS if self.selected_tts_backend() == "piper" else VOICE_OPTIONS),
            state="readonly",
            width=38,
            style="App.TCombobox",
        )
        self.voice_selector.grid(row=0, column=3, padx=4, sticky="ew")
        ttk.Label(settings, text="文字起こし", style="Panel.TLabel").grid(row=0, column=4, sticky="w")
        ttk.Combobox(settings, textvariable=self.transcription_preset, values=("fast", "balanced", "high-accuracy"), state="readonly", width=14, style="App.TCombobox").grid(row=0, column=5, padx=4)
        ttk.Label(settings, text="音声合成方式", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            settings, textvariable=self.tts_choice, values=list(TTS_BACKENDS), state="readonly",
            width=42, style="App.TCombobox",
        ).grid(row=1, column=1, columnspan=3, sticky="ew", padx=4, pady=(8, 0))
        actions = ttk.Frame(container, style="App.TFrame")
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Button(actions, text="翻訳開始(キューに追加)", command=self.add_job, style="Accent.TButton").pack(side=tk.LEFT)
        ttk.Button(actions, text="チェックしたジョブをキャンセル", command=self.cancel_selected, style="Quiet.TButton").pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="APIキー設定", command=self.configure_api_keys, style="Quiet.TButton").pack(side=tk.LEFT)
        ttk.Label(actions, textvariable=self.status_var, style="App.TLabel").pack(side=tk.RIGHT)
        cleanup_actions = ttk.Frame(container, style="App.TFrame")
        cleanup_actions.grid(row=7, column=0, columnspan=3, sticky="w")
        ttk.Button(
            cleanup_actions, text="全ジョブの中間データを削除",
            command=self.delete_job_intermediates, style="Quiet.TButton",
        ).pack(side=tk.LEFT)
        ttk.Button(
            cleanup_actions, text="チェックしたジョブを削除",
            command=self.delete_checked_jobs, style="Quiet.TButton",
        ).pack(side=tk.LEFT, padx=8)

        ttk.Label(container, text="ジョブ一覧", style="App.TLabel").grid(row=9, column=0, columnspan=3, sticky="w", pady=(14, 5))
        columns = ("check", "job_id", "filename", "status", "progress", "stage", "created", "output")
        self.jobs = ttk.Treeview(container, columns=columns, show="headings", selectmode="browse", style="App.Treeview")
        headings = {"check": "対象", "job_id": "Job ID", "filename": "入力動画", "status": "状態", "progress": "進捗", "stage": "処理段階", "created": "登録日時", "output": "出力"}
        widths = {"check": 55, "job_id": 190, "filename": 190, "status": 80, "progress": 70, "stage": 180, "created": 160, "output": 220}
        for column in columns:
            self.jobs.heading(column, text=headings[column])
            self.jobs.column(column, width=widths[column], anchor="w")
        self.jobs.grid(row=10, column=0, columnspan=3, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self.jobs.yview)
        scrollbar.grid(row=10, column=3, sticky="ns")
        self.jobs.configure(yscrollcommand=scrollbar.set)
        self.jobs.bind("<Button-1>", self.toggle_job_check)
        self.jobs.bind("<space>", self.toggle_selected_check)
        self.jobs.bind("<Double-1>", self._open_double_clicked)
        ttk.Button(container, text="完了ファイルを開く", command=self.open_selected, style="Quiet.TButton").grid(row=11, column=2, sticky="e", pady=10)

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

    def choose_japanese_srt(self) -> None:
        selected = filedialog.askopenfilename(
            title="日本語字幕SRTを選択",
            filetypes=(("SRT files", "*.srt"), ("All files", "*.*")),
        )
        if selected:
            self.japanese_srt_path.set(selected)

    def choose_english_srt(self) -> None:
        selected = filedialog.askopenfilename(
            title="英語字幕SRTを選択",
            filetypes=(("SRT files", "*.srt"), ("All files", "*.*")),
        )
        if selected:
            self.english_srt_path.set(selected)

    def glossary_terms(self) -> list[str]:
        return merge_glossary_terms([], self.glossary.get("1.0", tk.END).splitlines())

    def save_custom_glossary(self) -> None:
        default_terms = set(self.default_glossary_terms)
        custom_terms = [term for term in self.glossary_terms() if term not in default_terms]
        write_glossary_csv(self.custom_glossary_path, custom_terms)

    def show_glossary(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("専門用語一覧")
        window.transient(self.root)
        window.geometry("520x360")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)

        terms_text = tk.Text(window, wrap=tk.WORD)
        terms_text.grid(row=0, column=0, sticky="nsew", padx=(12, 0), pady=12)
        scrollbar = ttk.Scrollbar(window, orient=tk.VERTICAL, command=terms_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 12), pady=12)
        terms_text.configure(yscrollcommand=scrollbar.set, state=tk.NORMAL)
        terms = self.glossary_terms()
        terms_text.insert("1.0", "\n".join(f"{index}. {term}" for index, term in enumerate(terms, start=1)))
        terms_text.configure(state=tk.DISABLED)
        ttk.Button(window, text="閉じる", command=window.destroy).grid(
            row=1, column=0, columnspan=2, sticky="e", padx=12, pady=(0, 12)
        )

    def import_glossary_csv(self) -> None:
        selected = filedialog.askopenfilename(
            title="専門用語CSVを選択",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
        )
        if not selected:
            return
        try:
            terms = read_glossary_csv(Path(selected))
        except (OSError, UnicodeError, csv.Error) as exc:
            messagebox.showerror("CSVを読み込めません", str(exc))
            return
        terms = merge_glossary_terms(self.glossary_terms(), terms)
        self.glossary.delete("1.0", tk.END)
        self.glossary.insert("1.0", "\n".join(terms))
        try:
            self.save_custom_glossary()
        except OSError as exc:
            messagebox.showerror("専門用語を保存できません", str(exc))
            return
        self.status_var.set(f"専門用語を読み込みました: {len(terms)}件")

    def export_glossary_csv(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="専門用語CSVを保存",
            defaultextension=".csv",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
        )
        if not selected:
            return
        try:
            write_glossary_csv(Path(selected), self.glossary_terms())
        except OSError as exc:
            messagebox.showerror("CSVを書き出せません", str(exc))
            return
        self.status_var.set(f"専門用語を書き出しました: {selected}")

    def add_job(self) -> None:
        try:
            self.credentials.worker_credentials(
                self.translation_backend.get(), not bool(self.english_srt_path.get().strip())
            )
            tts_backend = self.selected_tts_backend()
            self.credentials.speech_credentials(tts_backend, self.azure_speech_region.get())
            if tts_backend == "azure" and not messagebox.askyesno(
                "Azure Speechの利用確認",
                "英語字幕のテキストをMicrosoft Azureへ送信して音声を生成します。\n"
                "文字数に応じた料金が発生する場合があります。自動の料金上限はありません。\n"
                "外部送信の許可と契約・料金を確認済みですか？",
                parent=self.root,
            ):
                return
            glossary_terms = self.glossary_terms()
            self.save_custom_glossary()
            options = {
                "translation_backend": self.translation_backend.get(),
                "tts_backend": tts_backend,
                "voice": self.selected_voice(),
                "azure_speech_region": self.azure_speech_region.get(),
                "transcription_preset": self.transcription_preset.get(),
                "burn_subs": False,
            }
            job_id = self.manager.register_job(
                Path(self.video_path.get()),
                "\n".join(glossary_terms),
                options,
                Path(self.output_dir.get()),
                Path(self.japanese_srt_path.get()) if self.japanese_srt_path.get().strip() else None,
                Path(self.english_srt_path.get()) if self.english_srt_path.get().strip() else None,
            )
        except CredentialError as exc:
            messagebox.showerror("APIキーを確認してください", str(exc), parent=self.root)
            self.configure_api_keys()
            return
        except (FileNotFoundError, OSError, ValueError) as exc:
            messagebox.showerror("ジョブを追加できません", str(exc))
            return
        self.status_var.set(f"追加しました: {job_id}")
        self.video_path.set("")
        self.japanese_srt_path.set("")
        self.english_srt_path.set("")
        self._refresh()

    def configure_api_keys(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("APIキー設定")
        window.transient(self.root)
        window.columnconfigure(1, weight=1)
        ttk.Label(
            window,
            text="APIキーはWindowsの資格情報マネージャー／macOSのキーチェーンに保存します。\n"
            "通常の設定ファイル・ジョブ・ログには保存しません。\n"
            "環境変数にキーがある場合は環境変数が優先されます。\n"
            "Google・DeepLへ翻訳テキスト、Azure選択時はMicrosoftへ英語字幕を送信します。\n"
            "Piperの音声生成にはAPIキー不要です。料金・利用条件を確認してください。",
            wraplength=560,
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=12, pady=12)
        entries: dict[str, ttk.Entry] = {}
        statuses: dict[str, tk.StringVar] = {}

        def refresh_status(provider: str) -> None:
            try:
                saved = self.credentials.saved_key(provider)
            except CredentialError as exc:
                statuses[provider].set("保存先にアクセスできません")
                messagebox.showerror("APIキー設定", str(exc), parent=window)
                return
            status = "保存済み（値は表示しません）" if saved else "未保存"
            if os.environ.get(API_VARIABLES[provider], "").strip():
                status += "／環境変数を使用中"
            statuses[provider].set(status)

        def save(provider: str) -> None:
            try:
                self.credentials.save_key(provider, entries[provider].get())
            except (CredentialError, ValueError) as exc:
                messagebox.showerror("APIキーを保存できません", str(exc), parent=window)
                return
            entries[provider].delete(0, tk.END)
            refresh_status(provider)
            messagebox.showinfo(
                "保存完了",
                "OSの安全な保存先へ保存しました。登録する新しいジョブから使えます。\n"
                "エラーになったジョブは再登録してください。",
                parent=window,
            )

        def delete(provider: str) -> None:
            if not messagebox.askyesno("APIキーを削除", f"{provider}の保存済みキーを削除しますか？", parent=window):
                return
            try:
                self.credentials.delete_key(provider)
            except CredentialError as exc:
                messagebox.showerror("APIキーを削除できません", str(exc), parent=window)
                return
            entries[provider].delete(0, tk.END)
            refresh_status(provider)

        for index, provider in enumerate(API_VARIABLES):
            row = 1 + index * 2
            ttk.Label(window, text=provider).grid(row=row, column=0, padx=12, pady=6)
            entry = ttk.Entry(window, show="*", width=42)
            entry.grid(row=row, column=1, sticky="ew", padx=6, pady=6)
            entries[provider] = entry
            ttk.Button(window, text="保存", command=lambda name=provider: save(name)).grid(row=row, column=2, padx=6)
            ttk.Button(window, text="削除", command=lambda name=provider: delete(name)).grid(row=row, column=3, padx=12)
            statuses[provider] = tk.StringVar()
            ttk.Label(window, textvariable=statuses[provider]).grid(row=row + 1, column=1, columnspan=3, sticky="w", padx=6)
            refresh_status(provider)
        next_row = 1 + len(API_VARIABLES) * 2
        ttk.Label(window, text="Azureリージョン").grid(row=next_row, column=0, padx=12, pady=6)
        region_entry = ttk.Entry(window, width=42)
        region_entry.insert(0, self.azure_speech_region.get())
        region_entry.grid(row=next_row, column=1, sticky="ew", padx=6, pady=6)

        def save_region() -> None:
            try:
                self.save_azure_speech_region(region_entry.get())
            except (ValueError, OSError) as exc:
                messagebox.showerror("リージョンを保存できません", str(exc), parent=window)
                return
            messagebox.showinfo("保存完了", "リージョンを保存しました。新しいジョブから使用します。", parent=window)

        ttk.Button(window, text="保存", command=save_region).grid(row=next_row, column=2, padx=6)
        ttk.Label(
            window, text="Speechリソースのリージョン識別子（例: japaneast）。URLではありません。\n"
            "キーと同じリソースの値を入力。AZURE_SPEECH_REGION環境変数がある場合はそちらが優先されます。",
            wraplength=560,
        ).grid(row=next_row + 1, column=0, columnspan=4, sticky="w", padx=12, pady=6)
        ttk.Button(
            window, text="Azureポータルを開く",
            command=lambda: webbrowser.open("https://portal.azure.com/"),
        ).grid(row=next_row + 2, column=0, columnspan=2, sticky="w", padx=12, pady=6)
        ttk.Button(
            window, text="Google AI Studioでキーを取得",
            command=lambda: webbrowser.open("https://aistudio.google.com/apikey?hl=ja"),
        ).grid(row=next_row + 3, column=0, columnspan=2, sticky="w", padx=12, pady=12)
        ttk.Checkbutton(
            window, text="次回から表示しない",
            variable=self.hide_api_keys_on_startup,
            command=self._save_api_startup_preference,
        ).grid(row=next_row + 4, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 12))
        ttk.Button(window, text="閉じる", command=window.destroy).grid(row=next_row + 3, column=3, padx=12, pady=12)

    def selected_job_id(self) -> str | None:
        selection = self.jobs.selection()
        if not selection:
            return None
        return str(selection[0])

    def toggle_job_check(self, event) -> str | None:
        if self.jobs.identify_region(event.x, event.y) != "cell" or self.jobs.identify_column(event.x) != "#1":
            return None
        job_id = self.jobs.identify_row(event.y)
        if job_id:
            self._toggle_check(job_id)
        return "break"

    def toggle_selected_check(self, _event) -> str:
        job_id = self.selected_job_id()
        if job_id:
            self._toggle_check(job_id)
        return "break"

    def _toggle_check(self, job_id: str) -> None:
        if job_id in self.checked_jobs:
            self.checked_jobs.remove(job_id)
        else:
            self.checked_jobs.add(job_id)
        self.jobs.set(job_id, "check", "☑" if job_id in self.checked_jobs else "☐")

    def _open_double_clicked(self, event) -> None:
        if self.jobs.identify_column(event.x) != "#1":
            self.open_selected()

    def delete_checked_jobs(self) -> None:
        if not self.checked_jobs:
            messagebox.showinfo("対象なし", "一覧の左端のチェックボックスでジョブを指定してください。", parent=self.root)
            return
        if not messagebox.askyesno(
            "ジョブを削除",
            f"{len(self.checked_jobs)}件の履歴・ログ・中間データを削除します。元に戻せません。\n"
            "完成動画・出力字幕・元動画・設定・用語・APIキーは残します。\n"
            "処理中・待機中のジョブは先にキャンセルし、停止を待ってください。\n"
            "他のアプリインスタンスも終了してください。削除しますか？",
            parent=self.root,
        ):
            return
        try:
            self.manager.delete_jobs(sorted(self.checked_jobs))
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("ジョブを削除できません", str(exc), parent=self.root)
        else:
            self.checked_jobs.clear()
            messagebox.showinfo("削除完了", "指定したジョブを削除しました。完成動画・出力字幕は保持しています。", parent=self.root)
        self._refresh()

    def delete_job_intermediates(self) -> None:
        if not messagebox.askyesno(
            "全ジョブの中間データを削除",
            "全ジョブのコピー入力動画・中間音声・中間字幕などを削除します。\n"
            "削除した中間データは元に戻せず、途中再開には使えなくなります。\n\n"
            "ジョブ履歴・ログ・完成動画・出力字幕・設定・用語・APIキーは残します。\n"
            "処理中・待機中のジョブがある間は削除できません。\n"
            "他のアプリインスタンスも終了してください。\n\n削除しますか？",
            parent=self.root,
        ):
            return
        try:
            count = self.manager.delete_job_intermediates()
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("中間データを削除できません", str(exc), parent=self.root)
            return
        messagebox.showinfo("削除完了", f"{count}ファイル削除しました。履歴・ログは保持しています。", parent=self.root)

    def cancel_selected(self) -> None:
        if not self.checked_jobs:
            messagebox.showinfo("対象なし", "一覧の左端のチェックボックスでジョブを指定してください。", parent=self.root)
            return
        if not messagebox.askyesno("キャンセル", f"{len(self.checked_jobs)}件にキャンセルを要求しますか？\n履歴とデータは残ります。", parent=self.root):
            return
        cancelled = 0
        try:
            for job_id in sorted(self.checked_jobs):
                if self.manager.cancel(job_id):
                    cancelled += 1
        except OSError as exc:
            messagebox.showerror("キャンセル失敗", str(exc), parent=self.root)
        else:
            messagebox.showinfo(
                "キャンセル結果",
                f"{cancelled}件にキャンセルを要求しました。\n"
                f"{len(self.checked_jobs) - cancelled}件は完了・停止済みなどのため対象外です。\n"
                "処理中のジョブは停止するまで削除できません。",
                parent=self.root,
            )
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
        if self._refresh_timer is not None:
            self.root.after_cancel(self._refresh_timer)
        jobs = self.manager.get_jobs()
        ids = {job["job_id"] for job in jobs}
        self.checked_jobs.intersection_update(ids)
        for item in self.jobs.get_children():
            if item not in ids:
                self.jobs.delete(item)
        status_labels = {"queued": "待機中", "running": "処理中", "done": "完了", "error": "エラー", "cancelled": "キャンセル"}
        for index, job in enumerate(jobs):
            values = (
                    "☑" if job["job_id"] in self.checked_jobs else "☐",
                    job["job_id"],
                    job.get("input_filename") or Path(job["input_video"]).name,
                    status_labels.get(job["status"], job["status"]),
                    f"{job.get('progress_percent', 0)}%",
                    job.get("current_stage_label") or "待機中",
                    job["created_at"],
                    job["output_video"] if job["status"] == "done" else "",
                )
            if self.jobs.exists(job["job_id"]):
                self.jobs.item(job["job_id"], values=values)
                self.jobs.move(job["job_id"], "", index)
            else:
                self.jobs.insert("", index, iid=job["job_id"], values=values)
        self._refresh_timer = self.root.after(1000, self._refresh)

    def close(self) -> None:
        self.save_custom_glossary()
        self._save_preferences()
        self.manager.stop()
        self.root.destroy()

    def _save_preferences(self, *_args) -> None:
        save_preferences(
            self.preferences_path,
            {
                "output_directory": self.output_dir.get(),
                "tts_backend": self.selected_tts_backend(),
                "voice": self.selected_voice(),
                "azure_speech_region": self.azure_speech_region.get(),
                "hide_api_keys_on_startup": "true" if self.hide_api_keys_on_startup.get() else "false",
            },
        )


def main() -> int:
    multiprocessing.freeze_support()
    if sys.argv[1:2] == ["--bundle-self-test"] and len(sys.argv) <= 3:
        return bundle_self_test("gui", Path(sys.argv[2]) if len(sys.argv) == 3 else None)
    root = tk.Tk()
    try:
        configure_runtime()
    except OSError as exc:
        messagebox.showerror("起動できません", str(exc), parent=root)
        root.destroy()
        return 1
    StandaloneApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
