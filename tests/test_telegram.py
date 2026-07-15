import json
import os
import tempfile
import unittest
import asyncio
from datetime import date
from unittest.mock import patch

import scraper


class OkResponse:
    ok = True
    status_code = 200

    def json(self):
        return {"ok": True, "result": {"message_id": 123}}


class TelegramClientTests(unittest.TestCase):
    def telegram_config(self, thread_id="38"):
        return patch.multiple(
            scraper,
            TELEGRAM_BOT_TOKEN="test-token",
            TELEGRAM_TARGET="-100123",
            TELEGRAM_THREAD_ID=thread_id,
        )

    def test_formats_existing_markdown_as_safe_html(self):
        result = scraper.telegram_html("✅ **Report** <private> `error`")
        self.assertEqual(
            result,
            "✅ <b>Report</b> &lt;private&gt; <code>error</code>",
        )

    def test_sends_message_to_configured_topic(self):
        with self.telegram_config(), patch.object(
            scraper.requests, "post", return_value=OkResponse()
        ) as post:
            result = scraper.send_telegram_message("✅ **Prova**")

        url = post.call_args.args[0]
        data = post.call_args.kwargs["data"]
        self.assertTrue(url.endswith("/bottest-token/sendMessage"))
        self.assertEqual(data["chat_id"], "-100123")
        self.assertEqual(data["message_thread_id"], "38")
        self.assertEqual(data["text"], "✅ <b>Prova</b>")
        self.assertEqual(result["message_id"], 123)

    def test_omits_topic_when_not_configured(self):
        with self.telegram_config(thread_id=""):
            payload = scraper.telegram_base_payload()

        self.assertNotIn("message_thread_id", payload)

    def test_uploads_photo_and_document(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photo_path = os.path.join(temp_dir, "chart.png")
            document_path = os.path.join(temp_dir, "detail.txt")
            with open(photo_path, "wb") as photo:
                photo.write(b"not-a-real-png")
            with open(document_path, "w", encoding="utf-8") as document:
                document.write("hourly detail")

            with self.telegram_config(), patch.object(
                scraper.requests, "post", return_value=OkResponse()
            ) as post:
                scraper.send_telegram_photo(photo_path, "**Chart**")
                self.assertTrue(post.call_args.args[0].endswith("/sendPhoto"))
                self.assertIn("photo", post.call_args.kwargs["files"])

                scraper.send_telegram_document(document_path, "**Detail**")
                self.assertTrue(post.call_args.args[0].endswith("/sendDocument"))
                self.assertIn("document", post.call_args.kwargs["files"])

    def test_pending_media_is_snapshotted_and_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_file = os.path.join(temp_dir, "pending.json")
            media_dir = os.path.join(temp_dir, "media")
            chart_path = os.path.join(temp_dir, "chart.png")
            details_path = os.path.join(temp_dir, "details.txt")
            with open(chart_path, "wb") as chart:
                chart.write(b"chart")
            with open(details_path, "w", encoding="utf-8") as details:
                details.write("details")

            with patch.multiple(
                scraper,
                PENDING_REPORTS_FILE=reports_file,
                PENDING_MEDIA_DIR=media_dir,
            ):
                scraper.queue_pending_report(
                    "10 de julio de 2026",
                    {
                        "message": "report",
                        "chart": chart_path,
                        "details": details_path,
                    },
                )
                with open(reports_file, encoding="utf-8") as reports:
                    payload = json.load(reports)["10 de julio de 2026"]

                self.assertTrue(os.path.isfile(payload["chart"]))
                self.assertTrue(os.path.isfile(payload["details"]))

                scraper.remove_pending_report("10 de julio de 2026")
                self.assertFalse(os.path.exists(payload["chart"]))
                self.assertFalse(os.path.exists(payload["details"]))

    def test_missing_token_fails_before_network_call(self):
        with patch.multiple(
            scraper,
            TELEGRAM_BOT_TOKEN="",
            TELEGRAM_TARGET="-100123",
        ), patch.object(scraper.requests, "post") as post:
            with self.assertRaisesRegex(scraper.TelegramAPIError, "TELEGRAM_BOT_TOKEN"):
                scraper.send_telegram_message("test")

        post.assert_not_called()

    def test_waiting_notification_state_survives_restarts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "waiting.json")
            with patch.object(scraper, "WAITING_NOTIFICATION_FILE", state_file):
                scraper.save_waiting_notification_state(date(2026, 7, 11), "no-data")
                self.assertEqual(
                    scraper.load_waiting_notification_state(),
                    (date(2026, 7, 11), "no-data"),
                )
                scraper.clear_waiting_notification_date()
                self.assertIsNone(scraper.load_waiting_notification_state())

    def test_zero_in_any_interval_is_a_gap(self):
        intervals = [(f"{hour:02d}:00", 1.0) for hour in range(24)]
        intervals[2] = ("02:00", 0.0)
        intervals[17] = ("17:00", 0.0)

        self.assertEqual(scraper.check_zeros(intervals), ["02:00", "17:00"])

    def test_report_with_gap_is_never_sent(self):
        data = {
            "date": "1 de enero de 2099",
            "intervals": [("00:00", 1.0), ("01:00", 0.0)],
        }
        with patch.object(scraper, "send_telegram_report_payload") as send:
            result = asyncio.run(scraper.send_telegram_report(data))

        self.assertFalse(result)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
