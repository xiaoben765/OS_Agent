#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from types import SimpleNamespace

from src.providers.openai import OpenAIProvider
from src.ui.console import ConsoleUI


class StreamDiagnosticsTest(unittest.TestCase):
    def test_openai_provider_summarizes_stream_metrics(self):
        provider = OpenAIProvider(
            SimpleNamespace(
                api_key="test-key",
                base_url="https://example.com/v1",
                model="test-model",
                timeout=45,
            )
        )

        summary = provider._summarize_stream_metrics(
            messages=[
                {"role": "system", "content": "abc"},
                {"role": "user", "content": "hello"},
            ],
            request_started=10.0,
            response_started=10.5,
            first_line_at=10.8,
            first_content_at=11.1,
            completed_at=16.1,
            chunk_count=4,
            content_chars=20,
            chunk_gap_samples_ms=[200, 400, 100],
        )

        self.assertEqual(summary["request_message_count"], 2)
        self.assertEqual(summary["request_chars"], 8)
        self.assertEqual(summary["connect_ms"], 500)
        self.assertEqual(summary["first_event_ms"], 800)
        self.assertEqual(summary["first_content_ms"], 1100)
        self.assertEqual(summary["stream_duration_ms"], 6100)
        self.assertEqual(summary["chunk_count"], 4)
        self.assertEqual(summary["content_chars"], 20)
        self.assertEqual(summary["avg_chunk_gap_ms"], 233)
        self.assertEqual(summary["max_chunk_gap_ms"], 400)

    def test_console_ui_summarizes_display_metrics(self):
        summary = ConsoleUI._summarize_stream_display_metrics(
            started_at=2.0,
            first_chunk_at=2.25,
            completed_at=5.5,
            consumed_chunks=6,
            rendered_updates=3,
            content_chars=120,
            render_elapsed_ms=88,
        )

        self.assertEqual(summary["first_chunk_ms"], 250)
        self.assertEqual(summary["display_duration_ms"], 3500)
        self.assertEqual(summary["consumed_chunks"], 6)
        self.assertEqual(summary["rendered_updates"], 3)
        self.assertEqual(summary["content_chars"], 120)
        self.assertEqual(summary["render_elapsed_ms"], 88)


if __name__ == "__main__":
    unittest.main()
