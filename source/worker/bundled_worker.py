"""Console entry point for the packaged conversion worker."""

from __future__ import annotations

import multiprocessing
import json
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
    if sys.argv[1:2] == ["--pyav-decode-self-test"] and len(sys.argv) == 4:
        import av

        with av.open(sys.argv[2]) as container:
            stream = container.streams.video[0]
            frame = next(container.decode(stream), None)
            if av.__version__ != "15.1.0" or frame is None or (frame.width, frame.height) != (160, 120):
                raise RuntimeError("Frozen PyAV managed-library decode failed")
            Path(sys.argv[3]).write_text(json.dumps({
                "pyav_version": av.__version__,
                "decoded": {"codec": stream.codec_context.name, "width": frame.width, "height": frame.height},
            }), encoding="utf-8")
        return 0
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
