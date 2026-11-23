import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import call, patch
from urllib.parse import quote

import requests

from lecture_poc import pipeline
from lecture_poc.security import redact_secrets


FIXTURE_KEY = "fixture credential/+?"


def response(status=200, *, payload=None, retry_after=None):
    result = requests.Response()
    result.status_code = status
    result.url = "https://example.test/generate"
    result._content = json.dumps(payload).encode()
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


def translations(*items):
    text = json.dumps({"translations": [{"id": index, "text": text} for index, text in items]})
    return response(payload={"candidates": [{"content": {"parts": [{"text": text}]}}]})


class GeminiTests(unittest.TestCase):
    def setUp(self):
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        self.enter_context = contexts.enter_context
        self.post = self.enter_context(patch("requests.post"))
        self.sleep = self.enter_context(patch.object(pipeline, "sleep"))
        self.output = self.enter_context(redirect_stdout(io.StringIO()))

    def input_batches(self):
        return [
            request.kwargs["json"]["contents"][0]["parts"][0]["text"].split("\nInput:\n")[1]
            for request in self.post.call_args_list
        ]

    def test_transient_http_errors_retry_with_backoff_and_header_auth(self):
        self.post.side_effect = [
            response(503), response(429, retry_after="8"),
            translations((2, "Second\nstill second"), (1, "First")),
        ]
        result = pipeline.translate_lines_with_gemini(["first", "second"], FIXTURE_KEY, "model")
        self.assertEqual(result, {"first": "First", "second": "Second still second"})
        self.assertEqual(self.sleep.call_args_list, [call(1.5), call(8)])
        self.assertEqual(self.input_batches(), ["1\tfirst\n2\tsecond"] * 3)
        request = self.post.call_args
        self.assertNotIn("params", request.kwargs)
        self.assertEqual(request.kwargs["headers"], {"x-goog-api-key": FIXTURE_KEY})
        self.assertFalse(request.kwargs["allow_redirects"])

    def test_invalid_batch_splits_immediately_and_keeps_good_half_on_model_switch(self):
        self.enter_context(patch.object(pipeline, "resolve_gemini_model_candidates", return_value=["a", "b"]))
        self.post.side_effect = [
            translations((1, "incomplete")),
            translations((1, "First")),
            response(503, retry_after="120"),
            translations((1, "Second")),
        ]
        result = pipeline.translate_plain_lines(["first", "second", "first"], "gemini", FIXTURE_KEY, "a")
        self.assertEqual(result, {"first": "First", "second": "Second"})
        self.assertEqual(self.input_batches(), ["1\tfirst\n2\tsecond", "1\tfirst", "1\tsecond", "1\tsecond"])
        self.assertIn("/b:generateContent", self.post.call_args.args[0])
        self.sleep.assert_not_called()

    def test_duplicate_invalid_ids_and_empty_text_never_pass(self):
        for invalid in (
            translations((1, "First"), (1, "Wrong")),
            translations((True, "Wrong"), (2, "Second")),
            translations((1, "First"), (2, " ")),
        ):
            with self.subTest():
                with self.assertRaises(pipeline.GeminiAlignmentError):
                    pipeline.parse_gemini_translations(invalid.json(), ["first", "second"])

    def test_permanent_outage_leaves_no_english_subtitles_and_masks_http_error(self):
        self.enter_context(patch.object(pipeline, "resolve_gemini_model_candidates", return_value=["a", "b"]))
        argos = self.enter_context(patch.object(pipeline, "translate_with_argos"))
        failed = response(503, payload={"message": f"UNAVAILABLE {FIXTURE_KEY}"})
        encoded_key = quote(FIXTURE_KEY, safe="")
        failed.url += f"?key={encoded_key}&access_token=other-fixture&alt=json"
        self.post.return_value = failed
        with tempfile.TemporaryDirectory() as directory:
            jp_srt = Path(directory) / "jp.srt"
            en_srt = Path(directory) / "en.srt"
            jp_srt.write_text(pipeline._build_srt_from_plain_lines(["first"]), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, r"status_code=503") as raised:
                pipeline.translate_jp_srt(jp_srt, en_srt, False, "gemini", FIXTURE_KEY, "a")
            self.assertFalse(en_srt.exists())
            self.assertTrue(jp_srt.exists())
        argos.assert_not_called()
        self.assertEqual(self.post.call_count, 6)
        self.assertIsNone(raised.exception.__context__)
        for text in (str(raised.exception), self.output.getvalue()):
            self.assertIn("UNAVAILABLE", text)
            self.assertIn("&alt=json", text)
            self.assertNotIn(FIXTURE_KEY, text)
            self.assertNotIn(encoded_key, text)
            self.assertNotIn("other-fixture", text)

    def test_auth_failure_does_not_retry_or_switch_model(self):
        self.enter_context(patch.object(pipeline, "resolve_gemini_model_candidates", return_value=["a", "b"]))
        for status in (400, 401, 403):
            with self.subTest(status=status):
                self.post.reset_mock()
                self.post.return_value = response(status, payload={"message": "quota exceeded"})
                with patch.object(pipeline, "translate_with_argos") as argos:
                    with self.assertRaisesRegex(pipeline.GeminiRequestError, rf"status_code={status}"):
                        pipeline.translate_plain_lines(["first"], "gemini", FIXTURE_KEY, "a")
                    argos.assert_not_called()
                self.post.assert_called_once()
                self.sleep.assert_not_called()

    def test_quota_only_failure_still_falls_back_to_argos(self):
        source = pipeline._build_srt_from_plain_lines(["first"])
        translated = pipeline._build_srt_from_plain_lines(["First"])
        self.post.return_value = response(429, retry_after="120")
        with patch.object(pipeline, "resolve_gemini_model_candidates", return_value=["a", "b"]), \
                patch.object(pipeline, "translate_with_argos", return_value=translated) as argos:
            self.assertEqual(pipeline.translate_with_gemini_candidates(source, FIXTURE_KEY, "a"), translated)
        argos.assert_called_once_with(source)
        self.assertEqual(self.post.call_count, 2)

    def test_model_discovery_keeps_auth_out_of_query(self):
        self.enter_context(patch.object(pipeline, "_GEMINI_AVAILABLE_MODELS_CACHE", None))
        self.enter_context(patch.dict("os.environ", {"GEMINI_DISCOVER_MODELS": "true"}))
        page = response(payload={"models": [
            {"name": "models/gemini-fixture", "supportedGenerationMethods": ["generateContent"]},
        ]})
        with patch("requests.get", return_value=page) as get:
            self.assertEqual(pipeline.fetch_available_gemini_models(FIXTURE_KEY), {"gemini-fixture"})
        self.assertEqual(get.call_args.kwargs["params"], {"pageSize": 100})
        self.assertEqual(get.call_args.kwargs["headers"], {"x-goog-api-key": FIXTURE_KEY})
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_legacy_http_error_and_header_redaction(self):
        failed = response(503)
        failed.url += "?%6bey=query-fixture&alt=json"
        with self.assertRaises(requests.HTTPError) as raised:
            failed.raise_for_status()
        rendered = pipeline.build_exception_summary(raised.exception)
        self.assertNotIn("query-fixture", rendered)
        self.assertIn("?%6bey=[REDACTED]&alt=json", rendered)
        self.assertEqual(redact_secrets("{'x-goog-api-key': 'header fixture'}"),
                         "{'x-goog-api-key': [REDACTED]}")
