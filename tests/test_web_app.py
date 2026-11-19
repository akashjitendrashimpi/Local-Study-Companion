import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch

from web_app import RequestHandler, RunController, save_api_key


class RunControllerTests(unittest.TestCase):
    def test_rejects_non_web_url(self):
        controller = RunController()
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
            with self.assertRaises(ValueError):
                controller.start("not-a-url", False)

    def test_start_runs_worker_and_reports_completion(self):
        controller = RunController()

        def fake_run(url, on_event, stop_event, close_event, auto_start, keep_browser_open):
            on_event({
                "status": "completed",
                "message": "Test run completed.",
                "question": 2,
                "total": 2,
            })
            return "completed"

        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
            with patch("web_app.run_automation", side_effect=fake_run):
                controller.start("https://example.test/assignment", False)
                controller.worker.join(timeout=2)

        status = controller.snapshot()
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["question"], 2)
        self.assertTrue(status["api_key_configured"])

    def test_local_http_routes_serve_ui_and_status(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base_url, timeout=2) as response:
                page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("Quiz Assistant", page)
                self.assertIn("submit manually in Chrome", page)
                self.assertIn('id="key-form"', page)
                self.assertIn("Made by Akash", page)
            with urlopen(f"{base_url}/api/status", timeout=2) as response:
                status = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertIn("api_key_configured", status)
                self.assertNotIn("GROQ_API_KEY", json.dumps(status))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_api_key_endpoint_saves_locally_without_returning_the_key(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "GROQ_MODEL=test-model\nGROQ_API_KEY=old-test-key\n",
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                request = Request(
                    f"{base_url}/api/config",
                    data=json.dumps({"api_key": "test-key-not-real"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with patch("web_app.ENV_FILE_PATH", env_path), patch.dict(
                    os.environ, {"GROQ_API_KEY": ""}
                ):
                    with urlopen(request, timeout=2) as response:
                        result = json.loads(response.read())
                        self.assertEqual(response.status, 200)
                    saved_env = env_path.read_text(encoding="utf-8")
                    self.assertIn("GROQ_MODEL=test-model", saved_env)
                    self.assertIn("GROQ_API_KEY=test-key-not-real", saved_env)
                    self.assertNotIn("old-test-key", saved_env)
                    self.assertEqual(saved_env.count("GROQ_API_KEY="), 1)
                    self.assertNotIn("test-key-not-real", json.dumps(result))

                    with urlopen(f"{base_url}/api/status", timeout=2) as response:
                        status = json.loads(response.read())
                    self.assertTrue(status["api_key_configured"])
                    self.assertNotIn("test-key-not-real", json.dumps(status))
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_api_key_rejects_newlines_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            with patch("web_app.ENV_FILE_PATH", env_path):
                with self.assertRaises(ValueError):
                    save_api_key("test-key\nGROQ_MODEL=overwritten")
            self.assertFalse(env_path.exists())

    def test_browser_close_action_releases_waiting_worker(self):
        controller = RunController()

        def fake_run(url, on_event, stop_event, close_event, auto_start, keep_browser_open):
            on_event({"status": "awaiting_close", "message": "Browser remains open."})
            close_event.wait(timeout=2)
            return "awaiting_submission"

        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
            with patch("web_app.run_automation", side_effect=fake_run):
                controller.start("https://example.test/assignment", False)
                for _ in range(100):
                    if controller.snapshot()["close_available"]:
                        break
                    time.sleep(0.01)
                self.assertTrue(controller.snapshot()["close_available"])
                controller.close_browser()
                controller.worker.join(timeout=2)

        self.assertFalse(controller.snapshot()["worker_alive"])
        self.assertEqual(controller.snapshot()["state"], "awaiting_submission")


if __name__ == "__main__":
    unittest.main()
