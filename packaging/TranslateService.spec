import os
from pathlib import Path

ROOT = Path(SPECPATH).parent
WORK = ROOT / "packaging" / ".work"
SOURCE = ROOT / "source" / "worker"
VERSION = (ROOT / "packaging" / "VERSION").read_text().strip()
IS_MAC = os.name != "nt"
paths = [str(SOURCE), str(WORK / "generated")]
resources = [
    (str(SOURCE / "lecture_poc" / "prompts"), "lecture_poc/prompts"),
    (str(WORK / "generated" / "version.json"), "."),
    (str(ROOT / "packaging" / "VERSION"), "."),
    (str(ROOT / "packaging" / "BUNDLED"), "."),
    (str(WORK / "notices"), "third-party-notices"),
]
gui_hidden = [
    "bundle_version",
    "keyring.backends.macOS" if IS_MAC else "keyring.backends.Windows",
]
worker_hidden = gui_hidden + ["lecture_poc.__main__", "edge_tts.__main__"]
# XTTS is an optional external-model backend, not part of the default bundle.
excluded = ["TTS", "pytest", "IPython", "cv2", "pytesseract"]

gui = Analysis(
    [str(SOURCE / "standalone_app.py")],
    pathex=paths, binaries=[], datas=resources, hiddenimports=gui_hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=excluded,
    noarchive=False,
)
worker = Analysis(
    [str(SOURCE / "bundled_worker.py")],
    pathex=paths, binaries=[], datas=resources, hiddenimports=worker_hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=excluded,
    noarchive=False,
)
gui_exe = EXE(
    PYZ(gui.pure), gui.scripts, [], exclude_binaries=True,
    name="TranslateService", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False,
    console=False,
    version=str(WORK / "generated" / "windows-version.txt") if not IS_MAC else None,
)
worker_exe = EXE(
    PYZ(worker.pure), worker.scripts, [], exclude_binaries=True,
    name="TranslateWorker", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True,
    version=str(WORK / "generated" / "windows-version.txt") if not IS_MAC else None,
)
# COLLECT merges both dependency sets; no source tree, data directory or venv is copied.
collection = COLLECT(
    gui_exe, worker_exe, gui.binaries, gui.datas, worker.binaries, worker.datas,
    strip=False, upx=False, name="TranslateService",
)
if IS_MAC:
    app = BUNDLE(
        collection, name="TranslateService.app",
        bundle_identifier="com.ondemand-translater.TranslateService",
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
        },
    )
