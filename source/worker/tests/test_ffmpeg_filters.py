from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lecture_poc import video_text_overlay
from lecture_poc.ffmpeg_filters import escape_drawtext_text, escape_filter_value
from lecture_poc.pipeline import build_subtitles_filter

TOKEN_WHITESPACE = " \n\t\r"
TRICKY_VALUES = [
    "/Users/O'Brien/Library/Application Support/TranslateService/jobs/1/en_aligned.srt",
    "C:\\Users\\O'Brien\\AppData\\Local\\TranslateService\\jobs\\1\\en_aligned.srt",
    "/tmp/mix ' : , [ ] ; \\ = % \"/en_aligned.srt",
    "/tmp/テイラー展開（とフーリエ級数展開）/emoji 😀/en_aligned.srt",
    "it's 'quoted'",
    "'",
    "\\",
    "ends with backslash\\",
    " leading and trailing ",
    "",
]
SUBTITLE_DIRECTORY_NAMES = [
    "user's folder",
    "'leading quote",
    "comma,folder",
    "brackets [x]",
    "semi;colon",
    "percent %d %{pts}",
    "equals=sign",
    "テイラー展開（とフーリエ級数展開）",
    "emoji 😀",
    "mix ' , [ ] ; = %",
]
if sys.platform != "win32":
    SUBTITLE_DIRECTORY_NAMES += ["colon:folder", "back\\slash", 'double"quote', "mix ' : , [ ] ; \\ = %"]
DRAWTEXT_TEXTS = ["Don't panic", "ratio: 50% off", "a,b [c]; d", "back\\slash", "%{pts} literal", "it's 'quoted'"]
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def av_get_token(buffer: str, terminators: str) -> tuple[str, str]:
    """Port of FFmpeg's libavutil av_get_token(); returns the token and the unread rest."""
    position = 0
    while position < len(buffer) and buffer[position] in TOKEN_WHITESPACE:
        position += 1
    output: list[str] = []
    protected_length = 0
    while position < len(buffer) and buffer[position] not in terminators:
        char = buffer[position]
        position += 1
        if char == "\\" and position < len(buffer):
            output.append(buffer[position])
            position += 1
            protected_length = len(output)
        elif char == "'":
            while position < len(buffer) and buffer[position] != "'":
                output.append(buffer[position])
                position += 1
            if position < len(buffer):
                position += 1
                protected_length = len(output)
        else:
            output.append(char)
    while len(output) > protected_length and output[-1] in TOKEN_WHITESPACE:
        output.pop()
    return "".join(output), buffer[position:]


def parse_filter_chain(graph: str) -> list[tuple[str, str]]:
    filters = []
    while True:
        name, graph = av_get_token(graph, "=,;[")
        arguments = ""
        if graph.startswith("="):
            arguments, graph = av_get_token(graph[1:], "[],;")
        filters.append((name, arguments))
        if not graph:
            return filters
        if not graph.startswith(","):
            raise AssertionError(f"Unexpected filtergraph remainder: {graph!r}")
        graph = graph[1:]


def parse_named_options(arguments: str) -> dict[str, str]:
    options = {}
    while arguments:
        key, separator, arguments = arguments.partition("=")
        if not separator:
            raise AssertionError(f"Expected key=value option, got {key!r}")
        options[key], arguments = av_get_token(arguments, ":")
        arguments = arguments.removeprefix(":")
    return options


def expand_drawtext(text: str) -> str:
    result, position = [], 0
    while position < len(text):
        if text[position] == "\\" and position + 1 < len(text):
            result.append(text[position + 1])
            position += 2
        elif text[position] == "%":
            raise AssertionError(f"drawtext would expand {text[position:]!r}")
        else:
            result.append(text[position])
            position += 1
    return "".join(result)


def ffmpeg_with_filters(*names: str) -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    listing = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    if not all(re.search(rf"\s{re.escape(name)}\s", listing) for name in names):
        return None
    return ffmpeg


def render_gray_frame(ffmpeg: str, video_filter: str, size: str = "320x180") -> bytes:
    result = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"color=c=black:s={size}:d=1", "-vf", video_filter,
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


class FilterEscapingTests(unittest.TestCase):
    def test_values_round_trip_through_both_ffmpeg_unescaping_levels(self) -> None:
        style = "Alignment=2,MarginV=24,FontScale=0.85"
        for value in TRICKY_VALUES:
            graph = (
                f"pad=iw:ih:0:0:black,subtitles=filename={escape_filter_value(value)}"
                f":force_style={escape_filter_value(style)}"
            )
            with self.subTest(value=value):
                filters = parse_filter_chain(graph)
                self.assertEqual([name for name, _ in filters], ["pad", "subtitles"])
                self.assertEqual(parse_named_options(filters[1][1]), {"filename": value, "force_style": style})

    def test_value_is_not_wrapped_in_quotes_that_cannot_contain_a_quote(self) -> None:
        self.assertEqual(escape_filter_value("/a/O'Brien/x.srt"), "/a/O\\\\\\'Brien/x.srt")
        self.assertEqual(escape_filter_value("C:\\x"), "C\\\\:\\\\\\\\x")

    def test_subtitles_filter_preserves_apostrophe_paths_with_and_without_band(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            subtitles = Path(temporary_dir) / "user's folder" / "en_aligned.srt"
            subtitles.parent.mkdir()
            subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            plain, ratio = build_subtitles_filter(subtitles, False, 0.3)
            self.assertIsNone(ratio)
            (name, arguments), = parse_filter_chain(plain)
            self.assertEqual((name, parse_named_options(arguments)), ("subtitles", {"filename": str(subtitles.resolve())}))
            banded, ratio = build_subtitles_filter(subtitles, True, 0.3)
            self.assertEqual(ratio, 0.3)
            (pad_name, pad_arguments), (name, arguments) = parse_filter_chain(banded)
            self.assertEqual((pad_name, pad_arguments, name), ("pad", "iw:ih+ceil(ih*0.3):0:0:black", "subtitles"))
            options = parse_named_options(arguments)
            self.assertEqual(options["filename"], str(subtitles.resolve()))
            self.assertTrue(options["force_style"].startswith("Alignment=2,MarginV=24,"))

    def test_drawtext_text_is_literal_after_filter_and_expansion_unescaping(self) -> None:
        for text in [*DRAWTEXT_TEXTS, " padded ", "100%", "\\%{"]:
            with self.subTest(text=text):
                (name, arguments), = parse_filter_chain(f"drawtext=text={escape_drawtext_text(text)}:x=4")
                options = parse_named_options(arguments)
                self.assertEqual((name, options["x"]), ("drawtext", "4"))
                self.assertEqual(expand_drawtext(options["text"]), text)

    def test_overlay_escapes_translated_text_and_font_path(self) -> None:
        font = "/Users/O'Brien/fonts [new]/font, bold;.ttf"
        region = {"text": "日本語", "x": 1, "y": 2, "w": 100, "h": 20, "start_sec": 0, "end_sec": 1}
        with patch.object(video_text_overlay, "_resolve_font_file", return_value=font), \
                patch.object(video_text_overlay.subprocess, "run") as run:
            video_text_overlay.overlay_translated_regions(
                Path("in.mp4"), Path(tempfile.gettempdir()) / "out.mp4", [region],
                {"日本語": "Don't: 50% [x]"}, "Unused",
            )
        command = run.call_args.args[0]
        filters = parse_filter_chain(command[command.index("-vf") + 1])
        self.assertEqual([name for name, _ in filters], ["drawbox", "drawtext"])
        options = parse_named_options(filters[1][1])
        self.assertEqual(options["fontfile"], font)
        self.assertEqual(expand_drawtext(options["text"]), "Don't: 50% [x]")
        self.assertEqual(options["enable"], "between(t,0.000,1.000)")

        with patch.object(video_text_overlay, "_resolve_font_file", return_value=None), \
                patch.object(video_text_overlay.subprocess, "run") as run:
            video_text_overlay.overlay_translated_regions(
                Path("in.mp4"), Path(tempfile.gettempdir()) / "out.mp4", [region], {}, "Font: O'Neil",
            )
        command = run.call_args.args[0]
        options = parse_named_options(parse_filter_chain(command[command.index("-vf") + 1])[1][1])
        self.assertEqual(options["font"], "Font: O'Neil")


class RealFFmpegFilterTests(unittest.TestCase):
    def test_subtitles_burn_from_directories_with_filtergraph_characters(self) -> None:
        ffmpeg = ffmpeg_with_filters("subtitles", "pad")
        if ffmpeg is None:
            self.skipTest("ffmpeg with the libass subtitles filter is not on PATH")
        with tempfile.TemporaryDirectory() as temporary_dir:
            for directory_name in SUBTITLE_DIRECTORY_NAMES:
                subtitles = Path(temporary_dir) / directory_name / "en_aligned.srt"
                subtitles.parent.mkdir()
                subtitles.write_text("1\n00:00:00,000 --> 00:00:05,000\nHello テスト\n", encoding="utf-8")
                for in_band in (False, True):
                    with self.subTest(directory=directory_name, in_band=in_band):
                        video_filter, _ = build_subtitles_filter(subtitles, in_band, 0.3)
                        self.assertTrue(any(value > 128 for value in render_gray_frame(ffmpeg, video_filter)))

    def test_drawtext_renders_same_pixels_as_literal_text_file(self) -> None:
        ffmpeg = ffmpeg_with_filters("drawtext")
        font_source = next((Path(path) for path in FONT_CANDIDATES if Path(path).is_file()), None)
        if ffmpeg is None or font_source is None:
            self.skipTest("ffmpeg with drawtext or a known test font is unavailable")
        with tempfile.TemporaryDirectory() as temporary_dir:
            font = Path(temporary_dir) / "user's fonts [x], ;" / "font.ttf"
            font.parent.mkdir()
            shutil.copyfile(font_source, font)
            text_file = Path(temporary_dir) / "text.txt"
            common = f"fontfile={escape_filter_value(str(font))}:fontsize=32:fontcolor=white:x=4:y=4"
            for text in DRAWTEXT_TEXTS:
                with self.subTest(text=text):
                    text_file.write_text(text, encoding="utf-8")
                    expected = render_gray_frame(
                        ffmpeg,
                        f"drawtext=textfile={escape_filter_value(str(text_file))}:expansion=none:{common}",
                        "480x80",
                    )
                    actual = render_gray_frame(ffmpeg, f"drawtext=text={escape_drawtext_text(text)}:{common}", "480x80")
                    self.assertTrue(any(value > 128 for value in expected))
                    self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
