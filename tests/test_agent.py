import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from playwright.sync_api import Error as PlaywrightError

from agent import (
    extract_progress_info,
    is_loading_text,
    is_navigation_option,
    launch_browser_context,
    load_env_file,
    click_next_question,
    main,
    wait_for_question_state,
)


class FakeElement:
    def __init__(self, visible, enabled=True):
        self.visible = visible
        self.enabled = enabled
        self.clicked = False

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def scroll_into_view_if_needed(self, **kwargs):
        return None

    def click(self, **kwargs):
        self.clicked = True


class FakeLocator:
    def __init__(self, elements):
        self.elements = elements

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]


class FakePage:
    main_frame = object()
    frames = []

    def __init__(self, text_elements):
        self.text_elements = text_elements

    def get_by_role(self, *args, **kwargs):
        return FakeLocator([])

    def get_by_text(self, *args, **kwargs):
        return FakeLocator(self.text_elements)


class AgentHelperTests(unittest.TestCase):
    def test_filters_navigation_and_review_controls(self):
        self.assertTrue(is_navigation_option("27"))
        self.assertTrue(is_navigation_option("Question 27"))
        self.assertTrue(is_navigation_option("Mark for review"))
        self.assertFalse(is_navigation_option("D 180"))

    def test_extracts_question_progress(self):
        self.assertEqual(extract_progress_info("Question 7 of 30"), (7, 30))
        self.assertEqual(extract_progress_info("No progress"), (None, None))

    def test_env_file_load_preserves_existing_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                'GROQ_API_KEY="from-file"\nGROQ_MODEL=model-from-file\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"GROQ_API_KEY": "already-set"}, clear=True):
                load_env_file(env_path)
                self.assertEqual(os.environ["GROQ_API_KEY"], "already-set")
                self.assertEqual(os.environ["GROQ_MODEL"], "model-from-file")

    def test_question_state_waits_until_loading_ends_and_state_stabilizes(self):
        loading = ("Loading question...", ["A", "B"], [])
        ready = ("Question 1 of 2\nChoose an answer.", ["A", "B"], [])
        with patch("agent.collect_page_state", side_effect=[loading, ready, ready, ready, ready]):
            state = wait_for_question_state(object(), timeout_s=2)
        self.assertEqual(state, ready)
        self.assertTrue(is_loading_text(loading[0]))

    def test_next_click_skips_hidden_duplicate_text_match(self):
        hidden = FakeElement(visible=False)
        visible = FakeElement(visible=True)
        self.assertTrue(click_next_question(FakePage([hidden, visible]), "Next"))
        self.assertFalse(hidden.clicked)
        self.assertTrue(visible.clicked)

    @patch("agent.sync_playwright")
    def test_browser_context_closes_context_and_driver(self, sync_playwright):
        manager = Mock()
        playwright = Mock()
        context = Mock()
        manager.start.return_value = playwright
        playwright.chromium.launch_persistent_context.return_value = context
        sync_playwright.return_value = manager

        with launch_browser_context() as launched_context:
            self.assertIs(launched_context, context)

        context.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()

    @patch("agent.browserbase_request")
    @patch("agent.sync_playwright")
    def test_public_browser_context_connects_and_reports_live_view(
        self, sync_playwright, browserbase_request
    ):
        manager = Mock()
        playwright = Mock()
        browser = Mock()
        context = Mock()
        browser.contexts = [context]
        manager.start.return_value = playwright
        playwright.chromium.connect_over_cdp.return_value = browser
        sync_playwright.return_value = manager
        browserbase_request.side_effect = [
            {"id": "session-id", "connectUrl": "wss://connect.browserbase.com"},
            {"debuggerFullscreenUrl": "https://debug.browserbase.com/live"},
        ]
        events = []

        with patch.dict(os.environ, {"PUBLIC_MODE": "true"}):
            with launch_browser_context(
                on_event=events.append,
                browser_context_id="context-id",
            ) as launched_context:
                self.assertIs(launched_context, context)

        session_payload = browserbase_request.call_args_list[0].kwargs["payload"]
        self.assertEqual(
            session_payload["browserSettings"]["context"],
            {"id": "context-id", "persist": True},
        )
        self.assertFalse(session_payload["browserSettings"]["recordSession"])
        self.assertEqual(events[0]["live_view_url"], "https://debug.browserbase.com/live")
        browser.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()

    @patch("agent.time.sleep")
    @patch("agent.sync_playwright")
    def test_browser_context_retries_driver_disconnect_once(
        self, sync_playwright, sleep
    ):
        first_manager = Mock()
        first_playwright = Mock()
        first_manager.start.return_value = first_playwright
        first_playwright.chromium.launch_persistent_context.side_effect = (
            PlaywrightError("Connection closed while reading from the driver")
        )

        second_manager = Mock()
        second_playwright = Mock()
        second_context = Mock()
        second_manager.start.return_value = second_playwright
        second_playwright.chromium.launch_persistent_context.return_value = (
            second_context
        )
        sync_playwright.side_effect = [first_manager, second_manager]

        with launch_browser_context() as launched_context:
            self.assertIs(launched_context, second_context)

        self.assertEqual(sync_playwright.call_count, 2)
        first_playwright.stop.assert_called_once_with()
        second_context.close.assert_called_once_with()
        second_playwright.stop.assert_called_once_with()
        sleep.assert_called_once_with(0.5)

    @patch("agent.get_api_key", return_value="test-key")
    @patch("builtins.input", side_effect=["https://example.test/assignment", ""])
    @patch("agent.run_automation")
    def test_cli_keeps_browser_open_until_enter(
        self, run_automation, input_mock, get_api_key
    ):
        def fake_run(
            target_url,
            on_event,
            stop_event,
            close_event,
            auto_start,
            keep_browser_open,
            api_key,
        ):
            self.assertTrue(keep_browser_open)
            self.assertTrue(auto_start)
            on_event({"status": "awaiting_close", "message": "Ready for review."})
            self.assertTrue(close_event.is_set())
            return "awaiting_submission"

        run_automation.side_effect = fake_run
        main()

        self.assertEqual(input_mock.call_count, 2)
        run_automation.assert_called_once()


if __name__ == "__main__":
    unittest.main()
