"""Console entry point for the packaged conversion worker."""

from __future__ import annotations

import multiprocessing
import runpy
import sys
from pathlib import Path

from app_runtime import bundle_self_test, configure_runtime


def main() -> int:
    multiprocessing.freeze_support()
    if sys.argv[1:2] == ["--bundle-self-test"] and len(sys.argv) <= 3:
        return bundle_self_test("worker", Path(sys.argv[2]) if len(sys.argv) == 3 else None)
    configure_runtime()
    if sys.argv[1:2] == ["--edge-tts"]:
        del sys.argv[1]
        runpy.run_module("edge_tts", run_name="__main__")
        return 0
    from lecture_poc.__main__ import main as pipeline_main

    return pipeline_main()


if __name__ == "__main__":
    raise SystemExit(main())
