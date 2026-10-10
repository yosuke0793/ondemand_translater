"""Console entry point for the packaged conversion worker."""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

from app_runtime import bundle_self_test, configure_runtime, is_bundled


def main() -> int:
    multiprocessing.freeze_support()
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.reconfigure(encoding="utf-8")
    if sys.argv[1:2] == ["--bundle-self-test"] and len(sys.argv) <= 3:
        return bundle_self_test("worker", Path(sys.argv[2]) if len(sys.argv) == 3 else None)
    configure_runtime()
    if is_bundled() and any(
        argument.split("=", 1)[0].startswith("--video-text-overlay")
        for argument in sys.argv[1:]
    ):
        print("同梱版ではOCRによる動画内文字置換には対応していません。", file=sys.stderr)
        return 1
    from lecture_poc.__main__ import main as pipeline_main

    return pipeline_main()


if __name__ == "__main__":
    raise SystemExit(main())
