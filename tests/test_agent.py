import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import httpx
from groq import APIStatusError, AuthenticationError
from playwright.sync_api import Error as PlaywrightError

from agent import (
    extract_progress_info,
    is_loading_text,
    is_navigation_option,
    launch_browser_context,
    load_env_file,
    ask_groq,
    click_next_question,
    is_sunstone_login_page,
    main,
    run_automation,
    submit_sunstone_mobile,
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

    def test_sunstone_phone_autofill_is_domain_scoped_and_leaves_otp_on_site(self):
        page = Mock()
        page.url = "https://www.hub.sunstone.in/#/login"
        page.frames = []
        page.main_frame = object()
        field = Mock()
        field.is_visible.return_value = True
        field.is_enabled.return_value = True
        field.get_attribute.side_effect = lambda name: {
            "aria-label": "Mobile number",
            "placeholder": "Enter mobile number",
            "type": "text",
        }.get(name)
        field_locator = Mock()
        field_locator.count.return_value = 1
        field_locator.nth.return_value = field
        button = Mock()
        button.is_visible.return_value = True
        button.is_enabled.return_value = True
        button.inner_text.return_value = "Send OTP"
        button.get_attribute.return_value = None
        button_locator = Mock()
        button_locator.count.return_value = 1
        button_locator.nth.return_value = button
        page.locator.side_effect = lambda selector: (
            field_locator if selector == "input" else button_locator
        )

        self.assertTrue(is_sunstone_login_page(page))
        self.assertTrue(submit_sunstone_mobile(page, "+91 98765 43210"))
        field.fill.assert_called_once_with("+919876543210", timeout=3000)
        button.click.assert_called_once_with(timeout=3000)

        page.url = "https://example.test/login"
        self.assertFalse(submit_sunstone_mobile(page, "9876543210"))

    def test_extracts_question_progress(self):
        self.assertEqual(extract_progress_info("Question 7 of 30"), (7, 30))
        self.assertEqual(extract_progress_info("No progress"), (None, None))

    def test_default_groq_model_uses_strict_schema_and_parses_answers(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"exact_option":"A","text_answers":[],"next_button":"Next"}'
                    )
                )
            ]
        )

        result = ask_groq(client, "openai/gpt-oss-20b", "Question?", ["A"], [])

        response_format = client.chat.completions.create.call_args.kwargs["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertEqual(result["exact_option"], "A")

    def test_custom_model_parses_fenced_json_without_json_mode(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            'Answer:\n```json\n'
                            '{"exact_option":"","text_answers":["42"],"next_button":""}\n```'
                        )
                    )
                )
            ]
        )

        result = ask_groq(client, "custom/model", "Question?", [], [{"label": "Answer", "value": ""}])

        self.assertEqual(result["text_answers"], ["42"])
        self.assertNotIn(
            "response_format",
            client.chat.completions.create.call_args.kwargs,
        )

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

    def test_rejected_groq_key_stops_without_retrying_or_waiting_for_close(self):
        response = httpx.Response(
            401,
            request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
        )
        auth_error = AuthenticationError("Invalid API key", response=response, body=None)
        page = Mock()
        page.url = "https://example.test/assignment"
        events = []

        @contextmanager
        def fake_browser_context(**kwargs):
            yield object()

        with patch("agent.Groq"), patch(
            "agent.launch_browser_context", side_effect=fake_browser_context
        ), patch("agent.get_active_page", return_value=page), patch(
            "agent.get_open_page", return_value=page
        ), patch(
            "agent.wait_until_loaded", return_value=True
        ), patch("agent.looks_like_login_page", return_value=False), patch(
            "agent.wait_for_question_state",
            return_value=("Question 1\nChoose one.", ["A"], []),
        ), patch("agent.ask_groq", side_effect=auth_error) as ask_groq:
            result = run_automation(
                "https://example.test/assignment",
                on_event=events.append,
                close_event=threading.Event(),
                keep_browser_open=True,
                api_key="rejected-key",
            )

        self.assertEqual(result, "api_key_rejected")
        self.assertEqual(ask_groq.call_count, 1)
        self.assertIn("api_key_rejected", [event["status"] for event in events])
        self.assertNotIn("awaiting_close", [event["status"] for event in events])

    def test_api_failure_switches_key_and_retries_current_question(self):
        response = httpx.Response(
            503,
            request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
        )
        service_error = APIStatusError("Service unavailable", response=response, body=None)
        page = Mock()
        page.url = "https://example.test/assignment"
        events = []
        close_event = threading.Event()
        close_event.set()
        first_client = Mock()
        second_client = Mock()

        @contextmanager
        def fake_browser_context(**kwargs):
            yield object()

        with patch("agent.Groq", side_effect=[first_client, second_client]), patch(
            "agent.launch_browser_context", side_effect=fake_browser_context
        ), patch("agent.get_active_page", return_value=page), patch(
            "agent.get_open_page", return_value=page
        ), patch("agent.wait_until_loaded", return_value=True), patch(
            "agent.looks_like_login_page", return_value=False
        ), patch(
            "agent.wait_for_question_state",
            return_value=("Question 1\nChoose one.", ["A"], []),
        ), patch("agent.ask_groq", side_effect=[
            service_error,
            {"exact_option": "A", "text_answers": [], "next_button": "Submit"},
        ]) as ask_groq, patch("agent.click_exact_option", return_value=True), patch(
            "agent.verify_option_selected", return_value=True
        ):
            result = run_automation(
                "https://example.test/assignment",
                on_event=events.append,
                close_event=close_event,
                keep_browser_open=True,
                api_keys=["first-key", "second-key"],
            )

        self.assertEqual(result, "awaiting_submission")
        self.assertEqual(ask_groq.call_count, 2)
        self.assertIs(ask_groq.call_args_list[0].args[0], first_client)
        self.assertIs(ask_groq.call_args_list[1].args[0], second_client)
        self.assertTrue(any("switching to another configured key" in event["message"] for event in events))

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
