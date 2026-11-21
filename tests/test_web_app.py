import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

# Tests must not use a developer's configured Supabase project.
os.environ["SUPABASE_DATABASE_URL"] = ""

import web_app
from web_app import (
    CONTROLLERS,
    RequestHandler,
    RunController,
    _connect_database,
    _create_session,
    _execute,
    _load_session,
    _validate_sunstone_mobile,
)


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = web_app.Path(self.directory.name) / "app.sqlite3"
        self.database_patch = patch("web_app.DATABASE_PATH", self.database_path)
        self.database_patch.start()
        self.environment_patch = patch.dict(
            os.environ,
            {
                "APP_SECRET": "test-secret-value-" * 3,
                "ADMIN_USERNAME": "admin",
                "ADMIN_PASSWORD": "safe-admin-password",
            },
        )
        self.environment_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.environment_patch.stop()
        self.database_patch.stop()
        self.directory.cleanup()

    def request(self, path, method="GET", body=None, cookie=None):
        data = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            content_type = response.headers.get("Content-Type", "")
            body_text = response.read().decode("utf-8")
            return response.status, response.headers, (
                json.loads(body_text) if "application/json" in content_type else body_text
            )

    def signup(self, username="student_one"):
        status, headers, body = self.request(
            "/api/auth/signup",
            "POST",
            {
                "username": username,
                "date_of_birth": "2000-01-02",
                "password": "student-password",
            },
        )
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def login_admin(self):
        status, headers, body = self.request(
            "/api/auth/login",
            "POST",
            {"username": "admin", "password": "safe-admin-password"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["role"], "admin")
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_postgres_database_queries_translate_parameter_placeholders(self):
        connection = Mock()
        with patch("web_app.SUPABASE_DATABASE_URL", "postgresql://test"):
            _execute(
                connection,
                "UPDATE users SET password_hash = ? WHERE username = ?",
                ("hash", "user"),
            )
        connection.execute.assert_called_once_with(
            "UPDATE users SET password_hash = %s WHERE username = %s",
            ("hash", "user"),
        )

    def test_sunstone_phone_is_validated_and_only_allowed_for_sunstone_https(self):
        self.assertEqual(
            _validate_sunstone_mobile(
                "https://www.hub.sunstone.in/#/login",
                "+91 98765 43210",
            ),
            "+91 98765 43210",
        )
        with self.assertRaises(ValueError):
            _validate_sunstone_mobile(
                "https://example.test/assignment",
                "+91 98765 43210",
            )
        with self.assertRaises(ValueError):
            _validate_sunstone_mobile(
                "http://hub.sunstone.in/#/login",
                "9876543210",
            )

    def test_legacy_sessions_migrate_to_persistent_per_user_browser_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = web_app.Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    date_of_birth TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    assignment_credits INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL,
                    visitor_id TEXT NOT NULL UNIQUE,
                    browser_context_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE api_keys (
                    id INTEGER PRIMARY KEY,
                    encrypted_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO users (
                    id, username, date_of_birth, password_hash, created_at, assignment_credits
                ) VALUES (1, 'legacy_user', '2000-01-02', 'hash', '2025-01-01', 4);
                INSERT INTO sessions VALUES (
                    'existing-token-hash', 'legacy_user', 'user',
                    'legacy-visitor', 'persistent-browser-context', '2025-01-02'
                );
                """
            )
            connection.close()

            with patch("web_app.DATABASE_PATH", path):
                first_token = _create_session("legacy_user", "user")
                first_session = _load_session(first_token)
                second_session = _load_session(_create_session("legacy_user", "user"))
                with _connect_database() as migrated_connection:
                    migrated_user = _execute(
                        migrated_connection,
                        "SELECT assignment_credits FROM users WHERE username = ?",
                        ("legacy_user",),
                    ).fetchone()
                    migrated_grant = _execute(
                        migrated_connection,
                        """
                        SELECT granted_credits, remaining_credits, expires_at
                        FROM assignment_credit_grants WHERE user_id = 1
                        """,
                    ).fetchone()

            self.assertEqual(
                first_session["browser_context_id"],
                "persistent-browser-context",
            )
            self.assertEqual(first_session["visitor_id"], second_session["visitor_id"])
            self.assertEqual(migrated_user["assignment_credits"], 4)
            self.assertEqual(migrated_grant["granted_credits"], 4)
            self.assertEqual(migrated_grant["remaining_credits"], 4)
            self.assertIsNone(migrated_grant["expires_at"])

    def test_account_creation_login_and_logout_keep_session_private(self):
        cookie = self.signup()
        status, headers, data = self.request("/api/status", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["username"], "student_one")
        self.assertFalse(data["api_key_configured"])
        self.assertNotIn("date_of_birth", json.dumps(data))

        self.assertEqual(self.request("/", cookie=cookie)[0], 200)
        self.assertIn("Quiz Assistant", self.request("/", cookie=cookie)[2])
        self.assertEqual(self.request("/api/auth/logout", "POST", {}, cookie)[0], 200)
        self.assertEqual(self.request("/api/status", cookie=cookie)[0], 401)

        status, headers, result = self.request(
            "/api/auth/login",
            "POST",
            {"username": "student_one", "password": "student-password"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["role"], "user")
        self.assertIn("Max-Age=315360000", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/status", cookie=headers["Set-Cookie"].split(";", 1)[0])[0], 200)

    def test_signup_validates_dob_password_and_reserved_admin_name(self):
        for payload in (
            {"username": "new_user", "date_of_birth": "not-a-date", "password": "long-password"},
            {"username": "new_user", "date_of_birth": "13/50/2000", "password": "long-password"},
            {"username": "new_user", "date_of_birth": "2000-01-01", "password": "short"},
            {"username": "admin", "date_of_birth": "2000-01-01", "password": "long-password"},
        ):
            status, _, body = self.request("/api/auth/signup", "POST", payload)
            self.assertEqual(status, 400)
            self.assertIn("error", body)

    def test_signup_accepts_us_dob_format_and_stores_canonical_iso_date(self):
        status, _, result = self.request(
            "/api/auth/signup",
            "POST",
            {
                "username": "formatted_dob",
                "date_of_birth": "03/05/2000",
                "password": "student-password",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["role"], "user")
        with _connect_database() as connection:
            row = _execute(
                connection,
                "SELECT date_of_birth FROM users WHERE username = ?",
                ("formatted_dob",),
            ).fetchone()
        self.assertEqual(row["date_of_birth"], "2000-03-05")

    def test_admin_key_pool_is_encrypted_masked_and_admin_only(self):
        user_cookie = self.signup()
        admin_cookie = self.login_admin()
        self.assertEqual(self.request("/api/admin/keys", cookie=user_cookie)[0], 403)
        self.assertEqual(self.request("/admin", cookie=admin_cookie)[0], 200)
        status, _, result = self.request(
            "/api/admin/keys",
            "POST",
            {"keys": ["secret-test-key-one", "secret-test-key-two"]},
            admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result, {"ok": True})

        status, _, result = self.request("/api/admin/keys", cookie=admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["keys"]), 2)
        self.assertNotIn("secret-test-key-one", json.dumps(result))
        stored = self.database_path.read_bytes()
        self.assertNotIn(b"secret-test-key-one", stored)
        self.assertNotIn(b"secret-test-key-two", stored)

        status, _, _ = self.request(
            "/api/admin/keys",
            "DELETE",
            {"id": result["keys"][0]["id"]},
            admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(self.request("/api/admin/keys", cookie=admin_cookie)[2]["keys"]), 1)

    def test_admin_can_view_profile_and_reset_password_without_reading_passwords(self):
        user_cookie = self.signup("reset_user")
        admin_cookie = self.login_admin()
        self.assertEqual(self.request("/api/admin/users", cookie=user_cookie)[0], 403)

        status, _, result = self.request("/api/admin/users", cookie=admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(result["users"][0]["username"], "reset_user")
        self.assertEqual(result["users"][0]["date_of_birth"], "2000-01-02")
        self.assertNotIn("password", json.dumps(result).lower())
        self.assertNotIn("student-password", json.dumps(result))

        status, _, result = self.request(
            "/api/admin/users/reset-password",
            "POST",
            {"username": "reset_user", "new_password": "new-password-123"},
            admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.request("/api/status", cookie=user_cookie)[0], 401)

        self.assertEqual(
            self.request(
                "/api/auth/login",
                "POST",
                {"username": "reset_user", "password": "student-password"},
            )[0],
            400,
        )
        self.assertEqual(
            self.request(
                "/api/auth/login",
                "POST",
                {"username": "reset_user", "password": "new-password-123"},
            )[0],
            200,
        )

    def test_admin_can_allocate_non_expiring_assignment_credits(self):
        user_cookie = self.signup("credits_user")
        admin_cookie = self.login_admin()
        with patch("web_app._get_api_keys", return_value=["test-key"]):
            status, _, body = self.request(
                "/api/start",
                "POST",
                {"url": "https://example.test/assignment"},
                user_cookie,
            )
        self.assertEqual(status, 400)
        self.assertIn("No assignments remaining", body["error"])
        self.assertEqual(
            self.request("/api/admin/users", cookie=admin_cookie)[2]["users"][0][
                "assignment_credits"
            ],
            0,
        )
        self.assertEqual(
            self.request(
                "/api/admin/users/credits",
                "POST",
                {"username": "credits_user", "amount": 3},
                user_cookie,
            )[0],
            403,
        )
        for amount in (0, -1, 10001, 1.5, True):
            status, _, body = self.request(
                "/api/admin/users/credits",
                "POST",
                {"username": "credits_user", "amount": amount},
                admin_cookie,
            )
            self.assertEqual(status, 400)
            self.assertIn("error", body)

        status, _, result = self.request(
            "/api/admin/users/credits",
            "POST",
            {"username": "credits_user", "amount": 3},
            admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["assignment_credits"], 3)
        self.assertEqual(
            self.request("/api/admin/users", cookie=admin_cookie)[2]["users"][0][
                "assignment_credits"
            ],
            3,
        )
        with patch("web_app._get_api_keys", return_value=["test-key"]):
            with patch("web_app.CONTROLLERS.start", side_effect=ValueError("start failed")):
                status, _, _ = self.request(
                    "/api/start",
                    "POST",
                    {"url": "https://example.test/assignment"},
                    user_cookie,
                )
        self.assertEqual(status, 400)
        self.assertEqual(
            self.request("/api/status", cookie=user_cookie)[2]["assignment_credits"],
            3,
        )
        status, _, status_data = self.request("/api/status", cookie=user_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(status_data["assignment_credits"], 3)

    def test_assignment_credit_defaults_apply_only_to_future_accounts(self):
        admin_cookie = self.login_admin()
        user_cookie = self.signup("existing_default")
        self.assertEqual(
            self.request(
                "/api/admin/credits/defaults",
                "POST",
                {"credits": 2, "expiry_days": 30},
                admin_cookie,
            )[0],
            200,
        )
        self.assertEqual(
            self.request(
                "/api/admin/credits/defaults",
                "GET",
                cookie=admin_cookie,
            )[2],
            {"credits": 2, "expiry_days": 30},
        )
        self.assertEqual(
            self.request("/api/status", cookie=user_cookie)[2]["assignment_credits"],
            0,
        )

        future_cookie = self.signup("future_default")
        self.assertEqual(
            self.request("/api/status", cookie=future_cookie)[2]["assignment_credits"],
            2,
        )
        self.assertEqual(
            self.request(
                "/api/admin/credits/defaults",
                "POST",
                {"credits": 5, "expiry_days": 0},
                admin_cookie,
            )[0],
            200,
        )
        self.assertEqual(
            self.request("/api/status", cookie=future_cookie)[2]["assignment_credits"],
            2,
        )
        self.assertEqual(
            self.request("/api/status", cookie=self.signup("latest_default"))[2][
                "assignment_credits"
            ],
            5,
        )

    def test_expired_credit_grants_are_ignored_and_unlimited_grants_work(self):
        admin_cookie = self.login_admin()
        user_cookie = self.signup("expiry_user")
        for expiry_days in (-1, 3651, True):
            status, _, body = self.request(
                "/api/admin/users/credits",
                "POST",
                {"username": "expiry_user", "amount": 1, "expiry_days": expiry_days},
                admin_cookie,
            )
            self.assertEqual(status, 400)
            self.assertIn("error", body)

        status, _, _ = self.request(
            "/api/admin/users/credits",
            "POST",
            {"username": "expiry_user", "amount": 2, "expiry_days": 1},
            admin_cookie,
        )
        self.assertEqual(status, 200)
        with _connect_database() as connection:
            _execute(
                connection,
                "UPDATE assignment_credit_grants SET expires_at = ? WHERE user_id = (SELECT id FROM users WHERE username = ?)",
                (int(time.time()) - 1, "expiry_user"),
            )
            connection.commit()

        self.assertEqual(
            self.request("/api/status", cookie=user_cookie)[2]["assignment_credits"],
            0,
        )
        with patch("web_app._get_api_keys", return_value=["test-key"]):
            status, _, body = self.request(
                "/api/start",
                "POST",
                {"url": "https://example.test/assignment"},
                user_cookie,
            )
        self.assertEqual(status, 400)
        self.assertIn("No assignments remaining", body["error"])

        status, _, result = self.request(
            "/api/admin/users/credits",
            "POST",
            {"username": "expiry_user", "amount": 1, "expiry_days": 0},
            admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["assignment_credits"], 1)

    def test_assignment_credit_is_consumed_only_when_run_reaches_review(self):
        admin_cookie = self.login_admin()

        for username, result, expected_credits in (
            ("failed_credit_user", "failed", 1),
            ("completed_credit_user", "awaiting_submission", 0),
        ):
            cookie = self.signup(username)
            allocation_status, _, _ = self.request(
                "/api/admin/users/credits",
                "POST",
                {"username": username, "amount": 1},
                admin_cookie,
            )
            self.assertEqual(allocation_status, 200)
            session = _load_session(cookie.partition("=")[2])

            with patch("web_app._get_api_keys", return_value=["test-key"]):
                with patch("web_app.run_automation", return_value=result):
                    start_status, _, _ = self.request(
                        "/api/start",
                        "POST",
                        {"url": "https://example.test/assignment"},
                        cookie,
                    )
                    self.assertEqual(start_status, 202)
                    controller = CONTROLLERS.get(session["visitor_id"])
                    controller.worker.join(timeout=2)

            status, _, data = self.request("/api/status", cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual(data["assignment_credits"], expected_credits)

    def test_account_pages_and_status_require_login_and_no_key_prompt_is_shown(self):
        status, _, page = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("Create account", page)
        self.assertNotIn("Groq API key", page)
        self.assertEqual(self.request("/api/status")[0], 401)

        cookie = self.signup()
        status, _, page = self.request("/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn('id="logout"', page)
        self.assertNotIn('id="key-form"', page)

    def test_run_controllers_are_isolated_and_workers_receive_admin_key_pool(self):
        with patch("web_app._get_api_keys", return_value=["pool-key-a", "pool-key-b"]):
            first = RunController()
            second = CONTROLLERS.get("isolated-test-user")
            first.report({"status": "waiting", "message": "Private run state."})
            self.assertNotEqual(first.snapshot()["message"], second.snapshot()["message"])

            received = []

            def fake_run(
                url, on_event, stop_event, close_event, auto_start,
                keep_browser_open, browser_context_id, api_keys, mobile_number,
            ):
                received.extend(api_keys)
                on_event({"status": "completed", "message": "Run completed."})
                return "completed"

            with patch("web_app.run_automation", side_effect=fake_run):
                first.start("https://example.test/assignment", False, api_keys=["pool-key-a", "pool-key-b"])
                first.worker.join(timeout=2)
            self.assertEqual(received, ["pool-key-a", "pool-key-b"])
            self.assertEqual(first.snapshot()["state"], "completed")
            self.assertNotIn("pool-key-a", json.dumps(first.snapshot()))

    def test_rejects_non_web_url_before_start(self):
        controller = RunController()
        with patch("web_app._get_api_keys", return_value=["test-key"]):
            with self.assertRaises(ValueError):
                controller.start("not-a-url", False)

    def test_browser_close_action_releases_waiting_worker(self):
        controller = RunController()

        def fake_run(
            url, on_event, stop_event, close_event, auto_start,
            keep_browser_open, browser_context_id, api_keys, mobile_number,
        ):
            on_event({"status": "awaiting_close", "message": "Browser remains open."})
            close_event.wait(timeout=2)
            return "awaiting_submission"

        with patch("web_app._get_api_keys", return_value=["test-key"]):
            with patch("web_app.run_automation", side_effect=fake_run):
                controller.start("https://example.test/assignment", False, api_keys=["test-key"])
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
