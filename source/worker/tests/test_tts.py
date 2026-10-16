from __future__ import annotations

import io
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import unittest
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from lecture_poc import pipeline, tts
from lecture_poc.__main__ import build_parser, ensure_python_dependencies
from lecture_poc.srt_utils import extract_srt_entries


def wav_bytes(duration: float = 0.1, frequency: float = 440.0) -> bytes:
    output = io.BytesIO()
    samples = [
        int(3000 * math.sin(2 * math.pi * frequency * i / 24000))
        for i in range(int(24000 * duration))
    ]
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return output.getvalue()


class ScratchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(__file__).parent / f".tts-test-{uuid.uuid4().hex}"
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)


class SettingsTests(unittest.TestCase):
    def test_deepl_api_and_cli_are_removed(self) -> None:
        parser = build_parser()
        self.assertNotIn("--deepl-api-key", parser.format_help())
        with self.assertRaises(SystemExit):
            parser.parse_args(["--input", "i", "--outdir", "o", "--translation-backend", "deepl"])
        self.assertFalse(hasattr(pipeline, "translate_with_deepl"))
        self.assertFalse(hasattr(pipeline.PipelineConfig(Path("i"), Path("o")), "deepl_api_key"))
        with self.assertRaises(pipeline.PipelineStepError):
            pipeline.run_pipeline(pipeline.PipelineConfig(
                Path("i"), Path("o"), translation_backend="deepl",
            ))

    def test_gemini_limit_falls_back_directly_to_argos(self) -> None:
        source = "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n"
        translated = source.replace("こんにちは", "Hello")
        with patch.object(pipeline, "get_gemini_api_key", return_value="test-key"), \
                patch.object(pipeline, "resolve_gemini_model_candidates", return_value=["test-model"]), \
                patch.object(pipeline, "translate_with_gemini", side_effect=RuntimeError("quota exceeded")), \
                patch.object(pipeline, "translate_with_argos", return_value=translated) as argos:
            self.assertEqual(
                pipeline.translate_plain_lines(["こんにちは"], "gemini", None, "test-model"),
                {"こんにちは": "Hello"},
            )
            argos.assert_called_once()
            argos.reset_mock()
            with patch.object(Path, "read_text", return_value=source), \
                    patch.object(Path, "write_text") as output:
                pipeline.translate_jp_srt(Path("jp"), Path("en"), True, "gemini", None, "test-model")
                output.assert_called_once_with(translated, encoding="utf-8")
                argos.assert_called_once_with(source)

    def test_gemini_nonlimit_failure_does_not_fall_back(self) -> None:
        with patch.object(pipeline, "get_gemini_api_key", return_value="test-key"), \
                patch.object(pipeline, "resolve_gemini_model_candidates", return_value=["test-model"]), \
                patch.object(pipeline, "translate_with_gemini", side_effect=RuntimeError("invalid key")), \
                patch.object(pipeline, "translate_with_argos") as argos:
            with self.assertRaisesRegex(RuntimeError, "invalid key"):
                pipeline.translate_plain_lines(["こんにちは"], "gemini", None, "test-model")
            argos.assert_not_called()

    def test_backend_defaults(self) -> None:
        config = pipeline.PipelineConfig(Path("input"), Path("output"))
        self.assertEqual(config.tts_backend, "piper")
        self.assertEqual(tts.resolve_voice("piper", config.voice), "en_US-ljspeech-medium")
        self.assertEqual(tts.resolve_voice("azure"), "en-US-AriaNeural")
        args = build_parser().parse_args(["--input", "i", "--outdir", "o"])
        self.assertEqual(args.tts_backend, "piper")
        self.assertEqual(args.tts_volume, "+100%")
        azure = build_parser().parse_args(
            ["--input", "i", "--outdir", "o", "--tts-backend", "azure"]
        )
        self.assertEqual(tts.resolve_voice(azure.tts_backend, azure.voice), "en-US-AriaNeural")

    def test_removed_backends_and_cross_backend_voices_are_rejected(self) -> None:
        for backend in ("edge-tts", "xtts-v2", "", "unknown"):
            with self.subTest(backend=backend), self.assertRaises(tts.TTSError):
                tts.resolve_voice(backend)
        for backend, voice in (
            ("piper", "en-US-AriaNeural"), ("azure", "en_US-ljspeech-medium"),
            ("azure", 'en-US-"><break/>Neural'),
        ):
            with self.subTest(backend=backend), self.assertRaises(tts.TTSError):
                tts.resolve_voice(backend, voice)

    def test_male_and_female_voice_selection_for_each_backend(self) -> None:
        parser = build_parser()
        voice_help = next(action.help for action in parser._actions if action.dest == "voice")
        self.assertIn("en_US-john-medium", voice_help)
        self.assertIn("en-US-AndrewNeural", voice_help)
        for backend, voices in (
            ("piper", ("en_US-ljspeech-medium", "en_US-john-medium")),
            ("azure", ("en-US-AriaNeural", "en-US-AndrewNeural")),
        ):
            for voice in voices:
                with self.subTest(backend=backend, voice=voice):
                    args = build_parser().parse_args(
                        ["--input", "i", "--outdir", "o", "--tts-backend", backend, "--voice", voice],
                    )
                    self.assertEqual(tts.resolve_voice(args.tts_backend, args.voice), voice)

    def test_region_and_volume_validation(self) -> None:
        self.assertEqual(tts.validate_azure_region(" EastUS "), "eastus")
        for value in ("", "a", "eastus.example", "eastus/path", "eastus:443", "a\nb", "a" * 51):
            with self.subTest(value=value), self.assertRaises(tts.TTSError):
                tts.validate_azure_region(value)
        for value, gain in (("+0%", 1), ("+100%", 2), ("+200%", 3), ("-100%", 0)):
            self.assertEqual(tts.volume_gain(value), gain)
        for value in ("1", "100%", "-101%", "NaN", "+2%;evil"):
            with self.subTest(value=value), self.assertRaises(tts.TTSError):
                tts.volume_gain(value)

    def test_selected_backend_dependencies_only(self) -> None:
        with patch("lecture_poc.__main__.importlib.util.find_spec", return_value=object()) as find:
            ensure_python_dependencies("argos", False, "piper")
            self.assertEqual(
                {call.args[0] for call in find.call_args_list},
                {"faster_whisper", "piper", "argostranslate"},
            )
            find.reset_mock()
            ensure_python_dependencies("argos", False, "azure")
            self.assertEqual(
                {call.args[0] for call in find.call_args_list},
                {"faster_whisper", "requests", "argostranslate"},
            )

    def test_invalid_settings_rejected_before_cached_output(self) -> None:
        for backend, voice in (("xtts-v2", None), ("edge-tts", None), ("piper", "wrong")):
            with self.subTest(backend=backend), patch.object(
                pipeline, "should_regenerate_en_mp3", return_value=False,
            ) as cache, self.assertRaises(pipeline.PipelineStepError):
                pipeline.generate_en_mp3(
                    Path("en.srt"), Path("jp.srt"), Path("input"), Path("en.mp3"),
                    voice, "+100%", False, 1, "no-overlap", backend, "tts-aligned", None,
                )
            cache.assert_not_called()

    def test_azure_missing_credentials_fail_before_pipeline_work(self) -> None:
        config = pipeline.PipelineConfig(Path("input"), Path("output"), tts_backend="azure")
        with patch.dict(os.environ, {"AZURE_SPEECH_KEY": "", "AZURE_SPEECH_REGION": ""}), patch.object(
            pipeline, "ensure_ffmpeg",
        ) as ffmpeg, self.assertRaises(pipeline.PipelineStepError):
            pipeline.run_pipeline(config)
        ffmpeg.assert_not_called()


class AzureTests(ScratchTest):
    def setUp(self) -> None:
        super().setUp()
        self.environment = patch.dict(
            os.environ, {"AZURE_SPEECH_KEY": "sentinel-secret", "AZURE_SPEECH_REGION": "eastus"},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.request_error = type("RequestException", (Exception,), {})
        self.timeout = type("Timeout", (self.request_error,), {})
        self.response = Mock(status_code=200, content=wav_bytes())
        self.post = Mock(return_value=self.response)
        fake_requests = SimpleNamespace(
            post=self.post, Timeout=self.timeout, RequestException=self.request_error,
        )
        self.modules = patch.dict(sys.modules, {"requests": fake_requests})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_official_rest_escaped_ssml_and_wav(self) -> None:
        target = self.directory / "speech.wav"
        text = '<voice name="evil"> & quotes "\' Japanese 日本語'
        tts.AzureBackend().synthesize(text, target)
        self.post.assert_called_once()
        args, kwargs = self.post.call_args
        self.assertEqual(args[0], "https://eastus.tts.speech.microsoft.com/cognitiveservices/v1")
        self.assertEqual(kwargs["headers"]["Ocp-Apim-Subscription-Key"], "sentinel-secret")
        self.assertEqual(kwargs["timeout"], (10, 60))
        self.assertFalse(kwargs["allow_redirects"])
        root = ElementTree.fromstring(kwargs["data"])
        voices = list(root)
        self.assertEqual(len(voices), 1)
        self.assertEqual(voices[0].attrib["name"], "en-US-AriaNeural")
        self.assertEqual(voices[0].text, text)
        self.assertNotIn(b"sentinel-secret", kwargs["data"])
        with wave.open(str(target), "rb") as wav_file:
            self.assertEqual(wav_file.getnframes(), 2400)
        self.response.close.assert_called_once()

    def test_http_failures_are_sanitized_and_never_retried(self) -> None:
        for status in (301, 400, 401, 403, 429, 500, 503):
            self.post.reset_mock()
            self.response.status_code = status
            self.response.content = b"sentinel-secret private subtitle"
            target = self.directory / f"speech-{status}.wav"
            with self.subTest(status=status), self.assertRaises(tts.TTSError) as caught:
                tts.AzureBackend().synthesize("private subtitle", target)
            self.assertIn(f"HTTP {status}", str(caught.exception))
            self.assertNotIn("sentinel-secret", str(caught.exception))
            self.assertNotIn("private subtitle", str(caught.exception))
            self.assertFalse(target.exists())
            self.post.assert_called_once()

    def test_selected_male_and_female_voice_sent_to_official_api(self) -> None:
        for voice in ("en-US-AriaNeural", "en-US-AndrewNeural"):
            with self.subTest(voice=voice):
                self.post.reset_mock()
                target = self.directory / f"{voice}.wav"
                tts.AzureBackend(voice).synthesize("Welcome.", target)
                self.post.assert_called_once()
                root = ElementTree.fromstring(self.post.call_args.kwargs["data"])
                self.assertEqual(list(root)[0].attrib["name"], voice)
                self.assertTrue(target.is_file())

    def test_network_failures_are_sanitized_and_never_retried(self) -> None:
        for error in (self.timeout, self.request_error):
            self.post.reset_mock()
            self.post.side_effect = error("sentinel-secret private subtitle")
            with self.subTest(error=error), self.assertRaises(tts.TTSError) as caught:
                tts.AzureBackend().synthesize("private subtitle", self.directory / "speech.wav")
            self.assertNotIn("sentinel-secret", str(caught.exception))
            self.assertNotIn("private subtitle", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
            self.post.assert_called_once()

    def test_invalid_audio_and_credentials_fail_explicitly(self) -> None:
        self.response.content = b"not WAV"
        with self.assertRaises(tts.TTSError):
            tts.AzureBackend().synthesize("hello", self.directory / "speech.wav")
        self.assertFalse((self.directory / "speech.wav").exists())
        self.post.reset_mock()
        for environment in (
            {"AZURE_SPEECH_KEY": ""},
            {"AZURE_SPEECH_KEY": "sentinel\nsecret"},
            {"AZURE_SPEECH_REGION": "eastus.evil.test"},
        ):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                with self.assertRaises(tts.TTSError):
                    tts.AzureBackend()
        self.post.assert_not_called()


class PiperTests(ScratchTest):
    def test_selected_voice_resolves_and_loads_its_own_model(self) -> None:
        chunk = SimpleNamespace(
            sample_channels=1, sample_width=2, sample_rate=24000,
            audio_int16_bytes=b"\x01\x00" * 2400,
        )
        for voice in tts.PIPER_VOICES:
            engine = Mock()
            engine.synthesize.return_value = [chunk]
            load = Mock(return_value=engine)
            fake_piper = SimpleNamespace(PiperVoice=SimpleNamespace(load=load))
            model, config = Path(f"{voice}.onnx"), Path(f"{voice}.onnx.json")
            with self.subTest(voice=voice), patch.dict(sys.modules, {"piper": fake_piper}), patch(
                "app_runtime.piper_model_paths", return_value=(model, config),
            ) as paths:
                tts.PiperBackend(voice).synthesize("Welcome.", self.directory / f"{voice}.wav")
                paths.assert_called_once_with(voice)
                load.assert_called_once_with(str(model), config_path=str(config), use_cuda=False)

    def test_missing_runtime_fails_without_network_fallback(self) -> None:
        with patch.dict(sys.modules, {"piper": None}), patch(
            "app_runtime.piper_model_paths",
        ) as paths, self.assertRaises(tts.TTSError):
            tts.PiperBackend().synthesize("hello", self.directory / "speech.wav")
        paths.assert_not_called()

    def test_multiple_sentence_chunks_and_thread_local_sessions(self) -> None:
        barrier = threading.Barrier(2)
        engines = []
        chunk = SimpleNamespace(
            sample_channels=1, sample_width=2, sample_rate=24000,
            audio_int16_bytes=b"\x01\x00" * 2400,
        )

        def load(*args, **kwargs):
            engine = Mock()
            engine.synthesize.return_value = [chunk, chunk]
            engines.append(engine)
            return engine

        load_mock = Mock(side_effect=load)
        fake_piper = SimpleNamespace(PiperVoice=SimpleNamespace(load=load_mock))
        backend = tts.PiperBackend()

        def worker(index):
            barrier.wait()
            for invocation in range(2):
                backend.synthesize("Sentence one. Sentence two.", self.directory / f"{index}-{invocation}.wav")

        with patch.dict(sys.modules, {"piper": fake_piper}), patch(
            "app_runtime.piper_model_paths", return_value=(Path("voice.onnx"), Path("voice.onnx.json")),
        ) as paths, ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(worker, (1, 2)))
        self.assertEqual(load_mock.call_count, 2)
        paths.assert_called_once_with("en_US-ljspeech-medium")
        load_mock.assert_called_with("voice.onnx", config_path="voice.onnx.json", use_cuda=False)
        self.assertEqual([engine.synthesize.call_count for engine in engines], [2, 2])
        for path in self.directory.glob("*.wav"):
            with wave.open(str(path), "rb") as wav_file:
                self.assertEqual(wav_file.getnframes(), 4800)

    def test_model_failure_has_no_fallback(self) -> None:
        fake_piper = SimpleNamespace(PiperVoice=SimpleNamespace(load=Mock()))
        with patch.dict(sys.modules, {"piper": fake_piper}), patch(
            "app_runtime.piper_model_paths", side_effect=RuntimeError("asset SHA mismatch"),
        ), self.assertRaises(tts.TTSError) as caught:
            tts.PiperBackend().synthesize("hello", self.directory / "speech.wav")
        self.assertIn("asset SHA mismatch", str(caught.exception))
        self.assertIn("RuntimeError", str(caught.exception))
        fake_piper.PiperVoice.load.assert_not_called()
        self.assertFalse((self.directory / "speech.wav").exists())

    def test_expected_onnx_errors_are_reported_by_type_without_private_text(self) -> None:
        inference_error = type("InvalidGraph", (Exception,), {})
        engine = Mock()
        engine.synthesize.side_effect = inference_error("private subtitle sentinel-secret")
        fake_piper = SimpleNamespace(PiperVoice=SimpleNamespace(load=Mock(return_value=engine)))
        with patch.dict(sys.modules, {"piper": fake_piper}), patch(
            "app_runtime.piper_model_paths", return_value=(Path("voice.onnx"), Path("voice.onnx.json")),
        ), patch.object(tts, "_onnx_runtime_errors", return_value=(inference_error,)):
            with self.assertRaises(tts.TTSError) as caught:
                tts.PiperBackend().synthesize("private subtitle", self.directory / "speech.wav")
        self.assertIn("InvalidGraph", str(caught.exception))
        self.assertIn("inference failed", str(caught.exception))
        self.assertNotIn("sentinel-secret", str(caught.exception))
        self.assertNotIn("private subtitle", str(caught.exception))

    def test_programming_errors_are_not_hidden(self) -> None:
        engine = Mock()
        engine.synthesize.side_effect = AttributeError("unexpected implementation defect")
        fake_piper = SimpleNamespace(PiperVoice=SimpleNamespace(load=Mock(return_value=engine)))
        with patch.dict(sys.modules, {"piper": fake_piper}), patch(
            "app_runtime.piper_model_paths", return_value=(Path("voice.onnx"), Path("voice.onnx.json")),
        ), self.assertRaises(AttributeError):
            tts.PiperBackend().synthesize("hello", self.directory / "speech.wav")

    def test_audio_write_error_is_explicit_and_sanitized(self) -> None:
        chunk = SimpleNamespace(
            sample_channels=1, sample_width=2, sample_rate=24000,
            audio_int16_bytes=b"\x01\x00" * 2400,
        )
        engine = Mock()
        engine.synthesize.return_value = [chunk]
        fake_piper = SimpleNamespace(PiperVoice=SimpleNamespace(load=Mock(return_value=engine)))
        with patch.dict(sys.modules, {"piper": fake_piper}), patch(
            "app_runtime.piper_model_paths", return_value=(Path("voice.onnx"), Path("voice.onnx.json")),
        ), patch.object(Path, "write_bytes", side_effect=PermissionError(13, "sentinel-secret")):
            with self.assertRaises(tts.TTSError) as caught:
                tts.PiperBackend().synthesize("hello", self.directory / "speech.wav")
        self.assertIn("audio write failed (PermissionError, errno=13)", str(caught.exception))
        self.assertNotIn("sentinel-secret", str(caught.exception))


SOURCE_SRT = (
    "1\n00:00:00,000 --> 00:00:00,200\nAlpha\n\n"
    "2\n00:00:00,300 --> 00:00:00,500\nBeta\n\n"
    "3\n00:00:00,600 --> 00:00:00,800\nAlpha\n"
)


class TimingTests(unittest.TestCase):
    def test_no_overlap_alignment_has_exact_audio_starts(self) -> None:
        entries = extract_srt_entries(SOURCE_SRT)
        durations = {"Alpha": 1.0, "Beta": 0.75}
        starts, adjusted, shift = pipeline.build_tts_start_times(entries, durations, "no-overlap")
        self.assertEqual(starts, [0, 1000, 1750])
        self.assertEqual(adjusted, 2)
        self.assertAlmostEqual(shift, 1.15)
        text, first, last = pipeline.build_aligned_srt_text(SOURCE_SRT, entries, starts, durations)
        aligned = extract_srt_entries(text)
        self.assertEqual([entry.start_seconds for entry in aligned], [0, 1, 1.75])
        self.assertEqual([entry.end_seconds for entry in aligned], [1, 1.75, 2.75])
        self.assertEqual([entry.text for entry in aligned], ["Alpha", "Beta", "Alpha"])
        self.assertEqual(first, 0)
        self.assertAlmostEqual(last, 1.15)

    def test_fractional_milliseconds_never_overlap_or_accumulate_subtitle_drift(self) -> None:
        entries = [SimpleNamespace(text="A", start_seconds=0.0)] * 20
        source = "\n\n".join(f"{i+1}\n00:00:00,000 --> 00:00:00,100\nA" for i in range(20))
        starts, _, _ = pipeline.build_tts_start_times(entries, {"A": 0.3334}, "no-overlap")
        self.assertTrue(all(b / 1000 - a / 1000 >= 0.3334 for a, b in zip(starts, starts[1:])))
        aligned, _, _ = pipeline.build_aligned_srt_text(source, entries, starts, {"A": 0.3334})
        self.assertEqual(
            [entry.start_seconds for entry in extract_srt_entries(aligned)],
            [start / 1000 for start in starts],
        )

    def test_strict_mode_preserves_original_starts(self) -> None:
        starts, adjusted, shift = pipeline.build_tts_start_times(
            extract_srt_entries(SOURCE_SRT), {"Alpha": 1.0, "Beta": 0.75}, "strict",
        )
        self.assertEqual((starts, adjusted, shift), ([0, 300, 600], 0, 0))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools required")
class AudioIntegrationTests(ScratchTest):
    def setUp(self) -> None:
        super().setUp()
        original = pipeline.run_command

        def quiet_command(command, step):
            original([command[0], "-v", "error", *command[1:]], step)

        quiet = patch.object(pipeline, "run_command", side_effect=quiet_command)
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_shared_mixer_alignment_volume_workers_and_cache(self) -> None:
        source = self.directory / "en.srt"
        source.write_text(SOURCE_SRT, encoding="utf-8")
        reference = self.directory / "jp.srt"
        reference.write_text(SOURCE_SRT, encoding="utf-8")
        video = self.directory / "input.wav"
        video.write_bytes(wav_bytes(4.0, 0))
        aligned = self.directory / "aligned.srt"
        backend = Mock()

        def synthesize(text, target):
            target.write_bytes(wav_bytes(1.0 if text == "Alpha" else 0.75))

        backend.synthesize.side_effect = synthesize
        decoded = []
        for volume in ("+0%", "+100%"):
            output = self.directory / f"audio-{volume}.mp3"
            with patch.object(pipeline, "create_backend", return_value=backend):
                pipeline.generate_en_mp3(
                    source, reference, video, output, None, volume, False, 2,
                    "no-overlap", "piper", "tts-aligned", aligned,
                )
            self.assertGreaterEqual(pipeline.get_media_duration_seconds(output), 4.0)
            audio = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(output), "-f", "s16le",
                 "-ar", "24000", "-ac", "1", "pipe:1"],
                check=True, capture_output=True,
            ).stdout
            samples = struct.unpack(f"<{len(audio)//2}h", audio)
            decoded.append(samples)
            self.assertFalse((self.directory / ".tts_segments").exists())
        self.assertEqual(backend.synthesize.call_count, 4)  # Two unique texts per render.
        subtitles = extract_srt_entries(aligned.read_text(encoding="utf-8"))
        self.assertEqual([entry.start_seconds for entry in subtitles], [0.0, 1.0, 1.75])
        self.assertEqual([entry.end_seconds for entry in subtitles], [1.0, 1.75, 2.75])
        rms = lambda values: math.sqrt(sum(v*v for v in values) / len(values))
        self.assertAlmostEqual(rms(decoded[1][2400:12000]) / rms(decoded[0][2400:12000]), 2, delta=0.05)
        self.assertLess(rms(decoded[1][72000:84000]), 5)  # Silence after last segment.
        with patch.object(pipeline, "create_backend") as factory:
            pipeline.generate_en_mp3(
                source, reference, video, self.directory / "audio-+100%.mp3", None,
                "+100%", False, 2, "no-overlap", "piper", "tts-aligned", aligned,
            )
        factory.assert_not_called()
        backend.reset_mock()
        with patch.object(pipeline, "create_backend", return_value=backend) as factory:
            pipeline.generate_en_mp3(
                source, reference, video, self.directory / "audio-+100%.mp3", "en_US-john-medium",
                "+100%", False, 2, "no-overlap", "piper", "tts-aligned", aligned,
            )
        factory.assert_called_once_with("piper", "en_US-john-medium")
        self.assertEqual(backend.synthesize.call_count, 2)
        metadata = (self.directory / "audio-+100%.mp3.meta.json").read_text()
        self.assertIn('"voice": "en_US-john-medium"', metadata)

    def test_mixer_batches_many_subtitle_inputs(self) -> None:
        entries = [
            f"{index}\n00:00:00,000 --> 00:00:00,100\nLine {index}"
            for index in range(1, 66)
        ]
        source = self.directory / "many-lines.srt"
        source.write_text("\n\n".join(entries), encoding="utf-8")
        video = self.directory / "input.wav"
        video.write_bytes(wav_bytes(1.0, 0))
        output = self.directory / "many-lines.mp3"
        backend = Mock()
        backend.synthesize.side_effect = lambda text, target: target.write_bytes(wav_bytes(0.05))
        commands = []
        runner = pipeline.run_command

        def record_command(command, step):
            commands.append(command)
            runner(command, step)

        with patch.object(pipeline, "create_backend", return_value=backend), patch.object(
            pipeline, "run_command", side_effect=record_command,
        ):
            pipeline.generate_en_mp3(
                source, source, video, output, None, "+100%", False, 1,
                "strict", "piper", "tts-aligned", None,
            )

        self.assertEqual(backend.synthesize.call_count, 65)
        self.assertEqual(len(commands), 4)
        self.assertTrue(all(command.count("-i") <= pipeline.TTS_MIX_BATCH_SIZE + 1 for command in commands))
        self.assertGreaterEqual(pipeline.get_media_duration_seconds(output), 1.0)
        self.assertFalse((self.directory / ".tts_segments").exists())

    def test_synthesis_failure_cleans_segments_and_does_not_write_metadata(self) -> None:
        source = self.directory / "en.srt"
        source.write_text(SOURCE_SRT, encoding="utf-8")
        video = self.directory / "input.wav"
        video.write_bytes(wav_bytes(4.0, 0))
        backend = Mock()
        backend.synthesize.side_effect = tts.TTSError("Local model unavailable")
        output = self.directory / "audio.mp3"
        with patch.object(pipeline, "create_backend", return_value=backend) as factory:
            with self.assertRaises(pipeline.PipelineStepError):
                pipeline.generate_en_mp3(
                    source, source, video, output, None, "+100%", False, 2,
                    "no-overlap", "piper", "tts-aligned", self.directory / "aligned.srt",
                )
        factory.assert_called_once()
        self.assertFalse((self.directory / ".tts_segments").exists())
        self.assertFalse((self.directory / "audio.mp3.meta.json").exists())

    def test_empty_subtitles_produce_silence_and_aligned_file_without_synthesis(self) -> None:
        source = self.directory / "empty.srt"
        source.write_text("", encoding="utf-8")
        video = self.directory / "input.wav"
        video.write_bytes(wav_bytes(1.0, 0))
        output = self.directory / "silence.mp3"
        aligned = self.directory / "aligned.srt"
        synthesis = Mock()
        pipeline.generate_tts_mp3_from_segments(
            source, video, output, 2, "no-overlap", "tts-aligned", aligned, synthesis, "+100%",
        )
        synthesis.assert_not_called()
        self.assertEqual(aligned.read_text(encoding="utf-8"), "")
        self.assertGreaterEqual(pipeline.get_media_duration_seconds(output), 1.0)


if __name__ == "__main__":
    unittest.main()
