import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import webbrowser
from collections import deque
from contextlib import contextmanager
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout
from agent import create_browserbase_context, load_env_file, run_automation

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8765"))
PUBLIC_MODE = os.getenv("PUBLIC_MODE", "").strip().lower() in {"1", "true", "yes"}
SESSION_COOKIE = "quiz_session"
APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("APP_DATA_DIR", str(APP_ROOT / "data")))
if not DATA_DIR.is_absolute():
    DATA_DIR = APP_ROOT / DATA_DIR
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_DIR / "study_companion.sqlite3")))
if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = APP_ROOT / DATABASE_PATH
SUPABASE_DATABASE_URL = os.getenv("SUPABASE_DATABASE_URL", "").strip()
DB_LOCK = threading.Lock()
INITIALIZED_DATABASES = set()
DATABASE_POOL = None


def _validate_sunstone_mobile(url, mobile_number):
    if mobile_number is None:
        return ""
    if not isinstance(mobile_number, str):
        raise ValueError("Enter a valid phone number.")
    mobile_number = mobile_number.strip()
    if not mobile_number:
        return ""
    phone_digits = re.sub(r"\D", "", mobile_number)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        not re.fullmatch(r"\+?[0-9][0-9 ()-]{6,19}", mobile_number)
        or not 8 <= len(phone_digits) <= 15
    ):
        raise ValueError("Enter a valid phone number, including its country code if needed.")
    if parsed.scheme != "https" or host not in {
        "hub.sunstone.in",
        "www.hub.sunstone.in",
    }:
        raise ValueError("The phone number can only be used with the Sunstone HTTPS site.")
    return mobile_number


def _get_database_pool():
    global DATABASE_POOL
    with DB_LOCK:
        if DATABASE_POOL is None:
            DATABASE_POOL = ConnectionPool(
                conninfo=SUPABASE_DATABASE_URL,
                min_size=1,
                max_size=10,
                timeout=10,
                kwargs={
                    "row_factory": dict_row,
                    "connect_timeout": 10,
                    "prepare_threshold": None,
                },
                open=False,
            )
            DATABASE_POOL.open(wait=True, timeout=10)
    return DATABASE_POOL


def _initialize_database(connection, is_postgres):
    database_identity = (
        f"postgres:{SUPABASE_DATABASE_URL}"
        if is_postgres
        else f"sqlite:{DATABASE_PATH.resolve()}"
    )
    with DB_LOCK:
        if database_identity not in INITIALIZED_DATABASES:
            if is_postgres:
                statements = (
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGSERIAL PRIMARY KEY,
                        username TEXT NOT NULL UNIQUE,
                        date_of_birth TEXT NOT NULL,
                        password_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        visitor_id TEXT,
                        browser_context_id TEXT,
                        assignment_credits INTEGER NOT NULL DEFAULT 0
                    )
                    """,
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        token_hash TEXT PRIMARY KEY,
                        username TEXT NOT NULL,
                        role TEXT NOT NULL,
                        visitor_id TEXT NOT NULL,
                        browser_context_id TEXT,
                        created_at TEXT NOT NULL
                    )
                    """,
                    """
                    CREATE TABLE IF NOT EXISTS api_keys (
                        id BIGSERIAL PRIMARY KEY,
                        encrypted_key TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """,
                    """
                    CREATE TABLE IF NOT EXISTS app_settings (
                        setting_key TEXT PRIMARY KEY,
                        setting_value TEXT NOT NULL
                    )
                    """,
                    """
                    CREATE TABLE IF NOT EXISTS assignment_credit_grants (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        granted_credits INTEGER NOT NULL,
                        remaining_credits INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at BIGINT
                    )
                    """,
                )
                for statement in statements:
                    connection.execute(statement)
                connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS visitor_id TEXT")
                connection.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS browser_context_id TEXT"
                )
                connection.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS assignment_credits INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_visitor_id_key"
                )
            else:
                connection.executescript(
                    """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    date_of_birth TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    visitor_id TEXT,
                    browser_context_id TEXT,
                    assignment_credits INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL,
                    visitor_id TEXT NOT NULL,
                    browser_context_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY,
                    encrypted_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assignment_credit_grants (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    granted_credits INTEGER NOT NULL,
                    remaining_credits INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at INTEGER
                );
                """
                )
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(users)")
                }
                if "visitor_id" not in columns:
                    connection.execute("ALTER TABLE users ADD COLUMN visitor_id TEXT")
                if "browser_context_id" not in columns:
                    connection.execute(
                        "ALTER TABLE users ADD COLUMN browser_context_id TEXT"
                    )
                if "assignment_credits" not in columns:
                    connection.execute(
                        "ALTER TABLE users ADD COLUMN assignment_credits INTEGER NOT NULL DEFAULT 0"
                    )

                unique_visitor_index = False
                for index in connection.execute("PRAGMA index_list(sessions)"):
                    if not index["unique"]:
                        continue
                    index_columns = [
                        row["name"]
                        for row in connection.execute(
                            f"PRAGMA index_info({index['name']})"
                        )
                    ]
                    if index_columns == ["visitor_id"]:
                        unique_visitor_index = True
                        break
                if unique_visitor_index:
                    connection.execute("DROP TABLE IF EXISTS sessions_rebuild")
                    connection.execute(
                        """
                        CREATE TABLE sessions_rebuild (
                            token_hash TEXT PRIMARY KEY,
                            username TEXT NOT NULL,
                            role TEXT NOT NULL,
                            visitor_id TEXT NOT NULL,
                            browser_context_id TEXT,
                            created_at TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO sessions_rebuild
                        SELECT token_hash, username, role, visitor_id,
                               browser_context_id, created_at
                        FROM sessions
                        """
                    )
                    connection.execute("DROP TABLE sessions")
                    connection.execute(
                        "ALTER TABLE sessions_rebuild RENAME TO sessions"
                    )

            _execute(
                connection,
                "CREATE INDEX IF NOT EXISTS assignment_credit_grants_user_expiry_idx ON assignment_credit_grants (user_id, expires_at, id)",
            )
            for setting_key in (
                "default_assignment_credits",
                "default_assignment_expiry_days",
            ):
                _execute(
                    connection,
                    """
                    INSERT INTO app_settings (setting_key, setting_value)
                    VALUES (?, '0') ON CONFLICT(setting_key) DO NOTHING
                    """,
                    (setting_key,),
                )
            legacy_credit_users = _execute(
                connection,
                """
                SELECT u.id, u.assignment_credits FROM users u
                WHERE u.assignment_credits > 0
                  AND NOT EXISTS (
                      SELECT 1 FROM assignment_credit_grants g
                      WHERE g.user_id = u.id
                  )
                """,
            ).fetchall()
            for user in legacy_credit_users:
                _execute(
                    connection,
                    """
                    INSERT INTO assignment_credit_grants (
                        user_id, granted_credits, remaining_credits, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, NULL)
                    """,
                    (
                        user["id"],
                        user["assignment_credits"],
                        user["assignment_credits"],
                        datetime.now().isoformat(),
                    ),
                )

            existing_users = connection.execute(
                "SELECT username, visitor_id, browser_context_id FROM users"
            ).fetchall()
            for user in existing_users:
                visitor_id = user["visitor_id"] or secrets.token_urlsafe(24)
                browser_context_id = user["browser_context_id"]
                if not browser_context_id:
                    previous_session = _execute(
                        connection,
                        """
                        SELECT browser_context_id FROM sessions
                        WHERE LOWER(username) = LOWER(?) AND browser_context_id IS NOT NULL
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (user["username"],),
                    ).fetchone()
                    if previous_session:
                        browser_context_id = previous_session["browser_context_id"]
                _execute(
                    connection,
                    """
                    UPDATE users SET visitor_id = ?, browser_context_id = ?
                    WHERE LOWER(username) = LOWER(?)
                    """,
                    (visitor_id, browser_context_id, user["username"]),
                )

            if is_postgres:
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS users_visitor_id_unique ON users (visitor_id)"
                )
                connection.execute("ALTER TABLE app_settings ENABLE ROW LEVEL SECURITY")
                connection.execute(
                    "ALTER TABLE assignment_credit_grants ENABLE ROW LEVEL SECURITY"
                )
                connection.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
                connection.execute("ALTER TABLE sessions ENABLE ROW LEVEL SECURITY")
                connection.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY")
                connection.execute(
                    "REVOKE ALL ON TABLE users, sessions, api_keys, app_settings, assignment_credit_grants FROM anon, authenticated"
                )
                connection.execute(
                    "REVOKE ALL ON SEQUENCE users_id_seq, api_keys_id_seq, assignment_credit_grants_id_seq FROM anon, authenticated"
                )
            else:
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS users_visitor_id_unique ON users (visitor_id)"
                )
                connection.commit()
            connection.commit()
            INITIALIZED_DATABASES.add(database_identity)


@contextmanager
def _connect_database():
    if SUPABASE_DATABASE_URL:
        with _get_database_pool().connection() as connection:
            _initialize_database(connection, is_postgres=True)
            yield connection
        return

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        _initialize_database(connection, is_postgres=False)
        yield connection
    finally:
        connection.close()


def _execute(connection, statement, parameters=()):
    if SUPABASE_DATABASE_URL:
        statement = statement.replace("?", "%s")
    return connection.execute(statement, parameters)


def _get_app_setting(connection, setting_key):
    row = _execute(
        connection,
        "SELECT setting_value FROM app_settings WHERE setting_key = ?",
        (setting_key,),
    ).fetchone()
    return row["setting_value"] if row else "0"


def _set_app_setting(connection, setting_key, setting_value):
    _execute(
        connection,
        """
        INSERT INTO app_settings (setting_key, setting_value)
        VALUES (?, ?)
        ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
        """,
        (setting_key, str(setting_value)),
    )


def _get_default_assignment_settings(connection):
    return {
        "credits": int(_get_app_setting(connection, "default_assignment_credits")),
        "expiry_days": int(
            _get_app_setting(connection, "default_assignment_expiry_days")
        ),
    }


def _available_assignment_credits(connection, user_id):
    now = int(datetime.now().timestamp())
    row = _execute(
        connection,
        """
        SELECT COALESCE(SUM(remaining_credits), 0) AS credits
        FROM assignment_credit_grants
        WHERE user_id = ? AND remaining_credits > 0
          AND (expires_at IS NULL OR expires_at > ?)
        """,
        (user_id, now),
    ).fetchone()
    credits = int(row["credits"] or 0)
    _execute(
        connection,
        "UPDATE users SET assignment_credits = ? WHERE id = ?",
        (credits, user_id),
    )
    return credits


def _insert_assignment_credit_grant(connection, user_id, amount, expiry_days):
    now = int(datetime.now().timestamp())
    expires_at = now + expiry_days * 86400 if expiry_days else None
    _execute(
        connection,
        """
        INSERT INTO assignment_credit_grants (
            user_id, granted_credits, remaining_credits, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, amount, amount, datetime.now().isoformat(), expires_at),
    )
    return _available_assignment_credits(connection, user_id)


def _add_assignment_credits(username, amount, expiry_days=None):
    if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= 10000:
        raise ValueError("Enter a whole number of assignments from 1 to 10,000.")
    if expiry_days is None:
        with _connect_database() as connection:
            expiry_days = _get_default_assignment_settings(connection)["expiry_days"]
    if (
        isinstance(expiry_days, bool)
        or not isinstance(expiry_days, int)
        or not 0 <= expiry_days <= 3650
    ):
        raise ValueError("Enter an expiry from 0 to 3,650 days; use 0 for no expiry.")
    with _connect_database() as connection:
        user = _execute(
            connection,
            "SELECT id FROM users WHERE LOWER(username) = LOWER(?)",
            (username,),
        ).fetchone()
        if not user:
            raise ValueError("User account not found.")
        balance = _insert_assignment_credit_grant(
            connection, user["id"], amount, expiry_days
        )
        connection.commit()
        return balance


def _reserve_assignment_credit(username):
    with _connect_database() as connection:
        user = _execute(
            connection,
            "SELECT id FROM users WHERE LOWER(username) = LOWER(?)",
            (username,),
        ).fetchone()
        if not user:
            raise ValueError("User account not found.")
        now = int(datetime.now().timestamp())
        statement = """
            SELECT id FROM assignment_credit_grants
            WHERE user_id = ? AND remaining_credits > 0
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END,
                     expires_at, id
            LIMIT 1
        """
        if SUPABASE_DATABASE_URL:
            statement += " FOR UPDATE"
        grant = _execute(connection, statement, (user["id"], now)).fetchone()
        if not grant:
            _available_assignment_credits(connection, user["id"])
            connection.commit()
            raise ValueError("No assignments remaining. Contact the administrator for more credits.")
        cursor = _execute(
            connection,
            """
            UPDATE assignment_credit_grants
            SET remaining_credits = remaining_credits - 1
            WHERE id = ? AND user_id = ? AND remaining_credits > 0
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (grant["id"], user["id"], now),
        )
        if not cursor.rowcount:
            raise ValueError("No assignments remaining. Contact the administrator for more credits.")
        _available_assignment_credits(connection, user["id"])
        connection.commit()
        return grant["id"]


def _refund_assignment_credit(username, grant_id):
    with _connect_database() as connection:
        user = _execute(
            connection,
            "SELECT id FROM users WHERE LOWER(username) = LOWER(?)",
            (username,),
        ).fetchone()
        if not user:
            raise RuntimeError("The assignment credit could not be restored; contact the administrator.")
        cursor = _execute(
            connection,
            """
            UPDATE assignment_credit_grants
            SET remaining_credits = remaining_credits + 1
            WHERE id = ? AND user_id = ?
            """,
            (grant_id, user["id"]),
        )
        if not cursor.rowcount:
            raise RuntimeError("The assignment credit could not be restored; contact the administrator.")
        _available_assignment_credits(connection, user["id"])
        connection.commit()


def _password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000)
    return f"{base64.urlsafe_b64encode(salt).decode()}:{base64.urlsafe_b64encode(digest).decode()}"


def _verify_password(password, encoded):
    try:
        salt_text, digest_text = encoded.split(":", 1)
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(digest_text)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000)
    return hmac.compare_digest(actual, expected)


def _key_cipher():
    secret = os.getenv("APP_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("Set APP_SECRET to a random value of at least 32 characters.")
    derived_key = hashlib.sha256(b"local-study-companion-api-keys:" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived_key))


def _get_api_keys():
    with _connect_database() as connection:
        rows = _execute(
            connection,
            "SELECT encrypted_key FROM api_keys ORDER BY id"
        ).fetchall()
    cipher = _key_cipher()
    try:
        return [cipher.decrypt(row["encrypted_key"].encode()).decode() for row in rows]
    except InvalidToken as exc:
        raise RuntimeError("Stored API keys cannot be decrypted; check APP_SECRET.") from exc


def _save_api_keys(keys):
    normalized = []
    for key in keys:
        if not isinstance(key, str):
            raise ValueError("Each API key must be text.")
        key = key.strip()
        if not key:
            continue
        if any(character in key for character in ("\r", "\n", "\0")):
            raise ValueError("Each API key must be a single line.")
        if key not in normalized:
            normalized.append(key)
    if not normalized:
        raise ValueError("Enter at least one API key.")

    cipher = _key_cipher()
    with _connect_database() as connection:
        _execute(connection, "DELETE FROM api_keys")
        for key in normalized:
            _execute(
                connection,
                "INSERT INTO api_keys (encrypted_key, created_at) VALUES (?, ?)",
                (cipher.encrypt(key.encode()).decode(), datetime.now().isoformat()),
            )
        connection.commit()


def _create_session(username, role):
    token = secrets.token_urlsafe(48)
    with _connect_database() as connection:
        browser_context_id = None
        if role == "user":
            user = _execute(
                connection,
                "SELECT visitor_id, browser_context_id FROM users WHERE LOWER(username) = LOWER(?)",
                (username,),
            ).fetchone()
            if not user:
                raise RuntimeError("The user account could not be loaded.")
            visitor_id = user["visitor_id"]
            browser_context_id = user["browser_context_id"]
        else:
            visitor_id = secrets.token_urlsafe(24)
        _execute(
            connection,
            """
            INSERT INTO sessions (
                token_hash, username, role, visitor_id, browser_context_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                hashlib.sha256(token.encode()).hexdigest(),
                username,
                role,
                visitor_id,
                browser_context_id,
                datetime.now().isoformat(),
            ),
        )
        connection.commit()
    return token


def _load_session(token):
    if not token:
        return None
    with _connect_database() as connection:
        row = _execute(
            connection,
            "SELECT * FROM sessions WHERE token_hash = ?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
    return dict(row) if row else None


def _session_from_request(handler):
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
    except CookieError:
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return _load_session(morsel.value if morsel else "")


AUTH_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f5f7fc"><title>Sign in · Study Companion</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;background:#f5f7fc;color:#172033;font:16px/1.5 Inter,"Segoe UI",sans-serif}
main{width:min(440px,100%)}.brand{font-weight:800;font-size:1.55rem;letter-spacing:-.04em;margin:0 0 5px}.intro{color:#69758b;margin:0 0 24px}
.card{background:#fff;border:1px solid #e3e8f0;border-radius:20px;padding:24px;box-shadow:0 14px 44px #1720330c}
nav{display:flex;gap:8px;margin-bottom:20px}.tab{flex:1;background:#edf0f5;color:#46536a}.tab.selected{background:#465cf5;color:white}
label{display:block;font-weight:650;font-size:.9rem;margin:14px 0 6px}input{width:100%;padding:13px 14px;border:1px solid #cbd4e2;border-radius:11px;font:inherit;min-height:48px}
input:focus{outline:3px solid #536dfe22;border-color:#536dfe}button{border:0;border-radius:11px;padding:12px 16px;font:inherit;font-weight:700;cursor:pointer;min-height:46px}.primary{width:100%;margin-top:20px;background:#465cf5;color:#fff}.primary:disabled{opacity:.55}
.hint,#feedback{font-size:.87rem;color:#768197}#feedback{min-height:1.4em;margin:12px 0 0}.hidden{display:none!important}
@media(max-width:480px){body{padding:14px} .card{padding:20px;border-radius:16px} }
</style></head><body><main><h1 class="brand">Study Companion</h1><p class="intro">Sign in to continue, or create your account.</p>
<section class="card"><nav><button id="login-tab" class="tab selected" type="button">Sign in</button><button id="signup-tab" class="tab" type="button">Create account</button></nav>
<form id="auth-form"><label for="username">Username</label><input id="username" name="username" autocomplete="username" minlength="3" maxlength="32" required>
<div id="dob-wrap" class="hidden"><label for="dob">Date of birth</label><input id="dob" name="dob" type="text" inputmode="numeric" autocomplete="bday" maxlength="10" placeholder="MM/DD/YYYY" aria-describedby="dob-hint"><p id="dob-hint" class="hint">Enter digits like 352000 or 03052000; dates display as MM/DD/YYYY.</p></div>
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" minlength="10" required>
<button id="submit" class="primary" type="submit">Sign in</button><p id="feedback" role="status" aria-live="polite"></p></form>
<p class="hint">Your session stays signed in on this device until you log out.</p></section></main>
<script>
const form=document.querySelector("#auth-form"),feedback=document.querySelector("#feedback"),dobWrap=document.querySelector("#dob-wrap"),dobInput=document.querySelector("#dob"),password=document.querySelector("#password"),submit=document.querySelector("#submit");
let mode="login";
function formatDob(value,allowCompact=false){if(value.includes("/")){const parts=value.split("/");if(parts.length===3&&/^\d{1,2}$/.test(parts[0])&&/^\d{1,2}$/.test(parts[1])&&/^\d{4}$/.test(parts[2]))return`${parts[0].padStart(2,"0")}/${parts[1].padStart(2,"0")}/${parts[2]}`;return value.slice(0,10)}const digits=value.replace(/\D/g,"").slice(0,8);if(digits.length===8)return`${digits.slice(0,2)}/${digits.slice(2,4)}/${digits.slice(4)}`;if(!allowCompact||![6,7].includes(digits.length))return digits;const year=digits.slice(-4),dateDigits=digits.slice(0,-4);let month,day;if(digits.length===6){month=dateDigits[0];day=dateDigits[1]}else{const twoDigitMonth=Number(dateDigits.slice(0,2)),oneDigitDay=Number(dateDigits[2]);if(twoDigitMonth>=1&&twoDigitMonth<=12&&oneDigitDay>=1&&oneDigitDay<=31){month=dateDigits.slice(0,2);day=dateDigits[2]}else{const oneDigitMonth=Number(dateDigits[0]),twoDigitDay=Number(dateDigits.slice(1));if(oneDigitMonth<1||oneDigitMonth>12||twoDigitDay<1||twoDigitDay>31)return digits;month=dateDigits[0];day=dateDigits.slice(1)}}return`${month.padStart(2,"0")}/${day.padStart(2,"0")}/${year}`}
function birthDateToIso(value){const match=/^(\d{2})\/(\d{2})\/(\d{4})$/.exec(value);if(!match)throw new Error("Enter your date of birth as MM/DD/YYYY.");return`${match[3]}-${match[1]}-${match[2]}`}
dobInput.addEventListener("input",()=>{const digits=dobInput.value.replace(/\D/g,"").slice(0,8),year=Number(digits.slice(-4));let formatted=formatDob(dobInput.value);if(!dobInput.value.includes("/")&&[6,7].includes(digits.length)&&year>=1900&&year<=new Date().getFullYear())formatted=formatDob(dobInput.value,true);if(formatted!==dobInput.value)dobInput.value=formatted});
dobInput.addEventListener("blur",()=>{dobInput.value=formatDob(dobInput.value,true)});
function choose(next){mode=next;document.querySelector("#login-tab").classList.toggle("selected",mode==="login");document.querySelector("#signup-tab").classList.toggle("selected",mode==="signup");dobWrap.classList.toggle("hidden",mode!=="signup");dobInput.required=mode==="signup";password.autocomplete=mode==="signup"?"new-password":"current-password";submit.textContent=mode==="signup"?"Create account":"Sign in";feedback.textContent=""}
document.querySelector("#login-tab").addEventListener("click",()=>choose("login"));document.querySelector("#signup-tab").addEventListener("click",()=>choose("signup"));
form.addEventListener("submit",async event=>{event.preventDefault();const body={username:form.username.value.trim(),password:password.value};if(mode==="signup"){dobInput.value=formatDob(dobInput.value,true);try{body.date_of_birth=birthDateToIso(dobInput.value)}catch(error){feedback.textContent=error.message;return}}submit.disabled=true;feedback.textContent="Please wait…";try{const response=await fetch(`/api/auth/${mode}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw new Error(result.error||"Could not sign in.");location.href=result.role==="admin"?"/admin":"/"}catch(error){feedback.textContent=error.message}finally{submit.disabled=false}});
</script></body></html>"""


ADMIN_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f5f7fc"><title>Admin · Study Companion</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f5f7fc;color:#172033;font:16px/1.5 Inter,"Segoe UI",sans-serif;padding:24px}
main{width:min(900px,100%);margin:auto}header{display:flex;align-items:center;justify-content:space-between;gap:14px;margin:0 0 24px}
h1{font-size:clamp(1.5rem,5vw,2rem);margin:0;letter-spacing:-.04em}.subtitle,.hint{color:#768197;font-size:.9rem}
.header-actions,.tabs,.user-meta,.reset-form{display:flex;align-items:center;gap:10px}.header-actions{flex-wrap:wrap}
.card{background:white;border:1px solid #e3e8f0;border-radius:18px;padding:24px;margin:14px 0;box-shadow:0 10px 32px #17203308}
.tabs{padding:5px;background:#e9edf5;border-radius:12px;width:max-content;max-width:100%;margin-bottom:18px}
button,a.button{min-height:44px;display:inline-flex;justify-content:center;align-items:center;border:0;border-radius:10px;padding:10px 16px;font:inherit;font-weight:700;cursor:pointer;text-decoration:none}
.tab{background:transparent;color:#536078}.tab[aria-selected="true"]{background:#fff;color:#3549dd;box-shadow:0 2px 6px #17203312}
.primary{background:#465cf5;color:white}.secondary{background:#edf0f5;color:#344158}.danger{color:#9e3434;background:#fff0ef}
label{display:block;font-weight:650;margin-bottom:8px}input,textarea{width:100%;min-height:46px;padding:11px 13px;border:1px solid #cbd4e2;border-radius:10px;font:inherit}
textarea{min-height:110px;resize:vertical}.search-row{display:flex;align-items:center;gap:12px;margin:18px 0}.search-row input{max-width:420px}
.count{white-space:nowrap;color:#68758a;font-size:.9rem}.user-list,#keys{display:grid;gap:12px;margin:16px 0 0;padding:0;list-style:none}
.user-card,#keys li{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;padding:16px;border:1px solid #e8ecf3;border-radius:13px}
.user-name{font-weight:750;font-size:1.03rem}.user-meta{flex-wrap:wrap;margin-top:5px;color:#758096;font-size:.87rem}
.reset-form{align-items:flex-end;min-width:min(360px,100%)}.reset-form label{flex:1;min-width:160px;margin:0}
.credit-form{display:flex;align-items:flex-end;gap:8px;margin-top:10px;max-width:480px}.credit-form label{flex:1;min-width:100px;margin:0}.credit-form input{margin-top:5px}.credit-form button{white-space:nowrap}
.default-credit-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;max-width:560px;margin-top:14px}.default-credit-form label{margin:0}.default-credit-form input{margin-top:5px}.default-credit-form button,.default-credit-form .feedback{grid-column:1/-1}
.hint{margin:8px 0 0;line-height:1.45}.feedback,#feedback{min-height:1.4em;color:#46536a;margin:12px 0 0}
.panel[hidden]{display:none!important}button:disabled{opacity:.55;cursor:not-allowed}
@media(max-width:620px){body{padding:14px}header{align-items:flex-start;flex-direction:column}.header-actions{width:100%;justify-content:space-between}.card{padding:17px;border-radius:15px}.tabs{width:100%}.tab{flex:1}.search-row{align-items:stretch;flex-direction:column}.search-row input{max-width:none}.user-card,#keys li{flex-direction:column;gap:14px}.reset-form{width:100%;align-items:stretch;flex-direction:column}.reset-form label{width:100%}.reset-form button{width:100%}.credit-form{width:100%;max-width:none;align-items:stretch;flex-wrap:wrap}.credit-form label{min-width:0}.default-credit-form{grid-template-columns:1fr}}
</style></head><body><main><header><div><h1>Admin dashboard</h1><div class="subtitle">Manage member profiles and shared model access.</div></div><div class="header-actions"><a class="button secondary" href="/">Back to app</a><button id="logout" class="secondary" type="button">Log out</button></div></header>
<nav class="tabs" aria-label="Admin sections"><button class="tab" data-panel="users-panel" aria-selected="true" type="button">Users</button><button class="tab" data-panel="keys-panel" aria-selected="false" type="button">API keys</button></nav>
<section id="users-panel" class="panel card"><div><h2>Accounts</h2><p class="hint">Manage new-account defaults and individual assignment credits. Failed runs return their reserved credit.</p><p id="user-feedback" class="feedback" role="status" aria-live="polite"></p>
<form id="default-credit-form" class="default-credit-form"><label for="default-credit-amount">Default assignments for new accounts<input id="default-credit-amount" type="number" min="0" max="10000" step="1" required></label><label for="default-credit-expiry">Default expiry in days<input id="default-credit-expiry" type="number" min="0" max="3650" step="1" required></label><p class="hint" style="grid-column:1/-1">Changing defaults affects only accounts created afterward. Set assignments or expiry to 0 for none or no expiry, respectively.</p><button class="primary" type="submit">Save new-account defaults</button><p id="defaults-feedback" class="feedback" role="status" aria-live="polite"></p></form></div>
<div class="search-row"><input id="user-search" type="search" placeholder="Search usernames" aria-label="Search users"><span id="user-count" class="count">Loading users…</span></div><div id="users" class="user-list"></div></section>
<section id="keys-panel" class="panel card" hidden><form id="key-form"><label for="api-keys">Add API keys</label><textarea id="api-keys" placeholder="Paste one Groq API key per line" autocomplete="off" spellcheck="false" required></textarea><p class="hint">Keys are encrypted at rest and never shown again.</p><button class="primary" id="save" type="submit">Add keys</button><p id="feedback" role="status" aria-live="polite"></p></form>
<h2 style="margin-top:24px">Configured keys</h2><p class="hint">A failed key is skipped during the current run.</p><ul id="keys"></ul></section></main>
<script>
const feedback=document.querySelector("#feedback"),userFeedback=document.querySelector("#user-feedback"),defaultsFeedback=document.querySelector("#defaults-feedback"),usersList=document.querySelector("#users"),userSearch=document.querySelector("#user-search"),userCount=document.querySelector("#user-count");
async function request(path,options={}){const response=await fetch(path,{...options,headers:{"Content-Type":"application/json",...(options.headers||{})}});const data=await response.json();if(!response.ok)throw new Error(data.error||"Request failed.");return data}
let users=[];
let creditDefaults={credits:0,expiry_days:0};
function formatBirthDate(value){const match=/^(\d{4})-(\d{2})-(\d{2})$/.exec(value);return match?`${match[2]}/${match[3]}/${match[1]}`:value}
function renderUsers(){
  const query=userSearch.value.trim().toLowerCase(),visible=users.filter(user=>user.username.toLowerCase().includes(query));
  usersList.replaceChildren();
  userCount.textContent=`${visible.length} of ${users.length} accounts`;
  if(!visible.length){const empty=document.createElement("p");empty.className="hint";empty.textContent=users.length?"No usernames match your search.":"No accounts yet.";usersList.append(empty);return}
  for(const user of visible){
    const card=document.createElement("article"),profile=document.createElement("div"),name=document.createElement("div"),meta=document.createElement("div"),credits=document.createElement("div"),creditForm=document.createElement("form"),creditLabel=document.createElement("label"),creditInput=document.createElement("input"),expiryLabel=document.createElement("label"),expiryInput=document.createElement("input"),creditButton=document.createElement("button"),resetForm=document.createElement("form"),resetLabel=document.createElement("label"),resetInput=document.createElement("input"),resetButton=document.createElement("button");
    card.className="user-card";profile.className="profile";name.className="user-name";name.textContent=user.username;
    meta.className="user-meta";meta.textContent=`Date of birth: ${formatBirthDate(user.date_of_birth)} · Joined: ${new Date(user.created_at).toLocaleDateString()}`;
    credits.className="user-meta";credits.textContent=`Assignments available: ${user.assignment_credits}`;
    creditForm.className="credit-form";creditLabel.textContent="Add assignments";creditInput.type="number";creditInput.name="amount";creditInput.min="1";creditInput.max="10000";creditInput.step="1";creditInput.required=true;creditInput.value=creditDefaults.credits||"";creditInput.setAttribute("aria-label",`Assignments to add for ${user.username}`);creditLabel.append(creditInput);
    expiryLabel.textContent="Expiry (days; 0 = never)";expiryInput.type="number";expiryInput.name="expiry_days";expiryInput.min="0";expiryInput.max="3650";expiryInput.step="1";expiryInput.required=true;expiryInput.value=creditDefaults.expiry_days;expiryInput.setAttribute("aria-label",`Credit expiry in days for ${user.username}`);expiryLabel.append(expiryInput);
    creditButton.type="submit";creditButton.className="secondary";creditButton.textContent="Allocate";creditForm.append(creditLabel,expiryLabel,creditButton);
    creditForm.addEventListener("submit",async event=>{event.preventDefault();creditButton.disabled=true;try{const result=await request("/api/admin/users/credits",{method:"POST",body:JSON.stringify({username:user.username,amount:Number(creditInput.value),expiry_days:Number(expiryInput.value)})});userFeedback.textContent=`Added credits for ${user.username}; ${result.assignment_credits} assignments are now available.`;await refreshUsers()}catch(error){userFeedback.textContent=error.message}finally{creditButton.disabled=false}});
    resetForm.className="reset-form";resetLabel.textContent="Set new password";resetInput.type="password";resetInput.name="new_password";resetInput.autocomplete="new-password";resetInput.minLength=10;resetInput.required=true;resetLabel.append(resetInput);
    resetButton.type="submit";resetButton.className="secondary";resetButton.textContent="Reset password";resetForm.append(resetLabel,resetButton);
    resetForm.addEventListener("submit",async event=>{event.preventDefault();resetButton.disabled=true;try{await request("/api/admin/users/reset-password",{method:"POST",body:JSON.stringify({username:user.username,new_password:resetInput.value})});resetInput.value="";userFeedback.textContent=`Password reset for ${user.username}; their existing sessions were revoked.`}catch(error){userFeedback.textContent=error.message}finally{resetButton.disabled=false}});
    profile.append(name,meta,credits,creditForm);card.append(profile,resetForm);usersList.append(card);
  }
}
async function refreshUsers(){try{const data=await request("/api/admin/users");users=data.users;renderUsers()}catch(error){userCount.textContent=error.message}}
async function refreshDefaults(){try{creditDefaults=await request("/api/admin/credits/defaults");document.querySelector("#default-credit-amount").value=creditDefaults.credits;document.querySelector("#default-credit-expiry").value=creditDefaults.expiry_days;renderUsers()}catch(error){defaultsFeedback.textContent=error.message}}
async function refreshKeys(){const list=document.querySelector("#keys");try{const data=await request("/api/admin/keys");list.replaceChildren();if(!data.keys.length){const item=document.createElement("li");item.textContent="No keys configured yet.";list.append(item);return}for(const key of data.keys){const item=document.createElement("li"),label=document.createElement("span"),remove=document.createElement("button");label.textContent=`Key ${key.id} · ${key.masked}`;remove.textContent="Remove";remove.className="secondary";remove.addEventListener("click",async()=>{try{await request("/api/admin/keys",{method:"DELETE",body:JSON.stringify({id:key.id})});await refreshKeys()}catch(error){feedback.textContent=error.message}});item.append(label,remove);list.append(item)}}catch(error){feedback.textContent=error.message}}
document.querySelectorAll(".tab").forEach(tab=>tab.addEventListener("click",()=>{document.querySelectorAll(".tab").forEach(item=>item.setAttribute("aria-selected",String(item===tab)));document.querySelectorAll(".panel").forEach(panel=>panel.hidden=panel.id!==tab.dataset.panel);if(tab.dataset.panel==="keys-panel")refreshKeys()}));
userSearch.addEventListener("input",renderUsers);
document.querySelector("#default-credit-form").addEventListener("submit",async event=>{event.preventDefault();const button=event.currentTarget.querySelector("button");button.disabled=true;try{creditDefaults=await request("/api/admin/credits/defaults",{method:"POST",body:JSON.stringify({credits:Number(document.querySelector("#default-credit-amount").value),expiry_days:Number(document.querySelector("#default-credit-expiry").value)})});defaultsFeedback.textContent="New-account defaults saved; existing accounts were not changed.";renderUsers()}catch(error){defaultsFeedback.textContent=error.message}finally{button.disabled=false}});
document.querySelector("#key-form").addEventListener("submit",async event=>{event.preventDefault();const button=document.querySelector("#save");button.disabled=true;try{await request("/api/admin/keys",{method:"POST",body:JSON.stringify({keys:document.querySelector("#api-keys").value.split(/\r?\n/)})});document.querySelector("#api-keys").value="";feedback.textContent="Keys added.";await refreshKeys()}catch(error){feedback.textContent=error.message}finally{button.disabled=false}});
document.querySelector("#logout").addEventListener("click",async()=>{await request("/api/auth/logout",{method:"POST",body:"{}"});location.href="/"});refreshDefaults();refreshUsers();
</script></body></html>"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quiz Assistant</title>
  <style>
    :root { color-scheme: light; font-family: Inter, "Segoe UI", sans-serif; background: #f3f5f9; color: #172033; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: start center; padding: 24px 16px; }
    main { width: min(820px, 100%); }
    header { margin: 0 0 22px; }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-mark { display: grid; place-items: center; width: 42px; height: 42px; border-radius: 13px; background: #465cf5; color: white; font-size: .82rem; font-weight: 800; letter-spacing: .04em; box-shadow: 0 5px 14px #465cf533; }
    h1 { margin: 0; font-size: clamp(1.7rem, 4vw, 2.2rem); letter-spacing: -.04em; }
    .byline { margin-left: auto; color: #8a94a5; font-size: .78rem; white-space: nowrap; }
    header p { margin: 12px 0 0; color: #69758b; line-height: 1.55; }
    .card { background: white; border: 1px solid #e3e8f0; border-radius: 18px; padding: 24px; box-shadow: 0 12px 36px #1720330b; margin: 14px 0; }
    label { display: block; font-size: .9rem; font-weight: 650; margin: 0 0 8px; }
    input[type=url], input[type=tel] { width: 100%; min-width: 0; min-height: 48px; padding: 13px 14px; border: 1px solid #cbd4e2; border-radius: 10px; font: inherit; outline: none; }
    input[type=url]:focus, input[type=tel]:focus { border-color: #536dfe; box-shadow: 0 0 0 3px #536dfe22; }
    .hint { margin: 8px 0 0; color: #7a8497; font-size: .84rem; line-height: 1.45; }
    .key-row { display: flex; align-items: center; gap: 10px; }
    .key-row button { flex: 0 0 auto; }
    #key-status { min-height: 1.3em; }
    .check { display: flex; gap: 10px; align-items: flex-start; margin: 18px 0; font-weight: 500; line-height: 1.45; }
    .check input { margin-top: 3px; }
    .actions { display: flex; gap: 10px; }
    button { min-height: 44px; border: 0; border-radius: 10px; padding: 11px 17px; font: inherit; font-weight: 650; cursor: pointer; }
    button:disabled { opacity: .48; cursor: not-allowed; }
    .primary { color: white; background: #465cf5; }
    .primary:hover:not(:disabled) { background: #3549dd; }
    .secondary { color: #344158; background: #edf0f5; }
    .status-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .badge { display: inline-flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 99px; background: #edf0f5; color: #4a5870; font-size: .82rem; font-weight: 700; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #8c96a8; }
    .badge.active { background: #eef1ff; color: #364bd8; }
    .badge.active .dot { background: #536dfe; box-shadow: 0 0 0 4px #536dfe20; }
    #message { margin: 16px 0 0; line-height: 1.5; color: #4e5b72; overflow-wrap: anywhere; }
    #progress { margin-top: 8px; font-size: .9rem; color: #768197; }
    .notice { padding: 12px 14px; background: #fff8e8; border: 1px solid #f3e4bc; border-radius: 10px; color: #6a5421; font-size: .86rem; line-height: 1.5; margin-top: 18px; }
    .public-only { display: none; }
    .account-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; }
    .account-actions a { color: #5365e7; font-size: .86rem; font-weight: 650; text-decoration: none; white-space: nowrap; }
    .browser-view { width: 100%; aspect-ratio: 16 / 10; border: 1px solid #dce2ed; border-radius: 10px; background: #111827; }
    h2 { font-size: 1rem; margin: 0 0 12px; }
    #logs { margin: 0; padding: 0; list-style: none; max-height: 230px; overflow: auto; }
    #logs li { padding: 9px 0; border-top: 1px solid #edf0f5; color: #5e6a7e; font-size: .86rem; line-height: 1.4; overflow-wrap: anywhere; }
    #logs time { color: #9aa3b2; margin-right: 8px; font-variant-numeric: tabular-nums; }
    footer { padding: 4px 0 8px; text-align: center; color: #a0a8b6; font-size: .76rem; letter-spacing: .02em; }
    @media (max-width: 520px) {
      body { padding: 16px 12px; }
      .card { padding: 17px; border-radius: 15px; }
      .brand { gap: 9px; flex-wrap: wrap; }
      .brand-mark { width: 38px; height: 38px; }
      h1 { font-size: 1.55rem; }
      .byline { display: none; }
      .account-actions { width: 100%; justify-content: flex-end; margin-left: 0; }
      .actions { flex-wrap: wrap; }
      .actions button { flex: 1 1 120px; min-height: 46px; }
      .status-row { align-items: flex-start; flex-wrap: wrap; }
      .browser-view { height: min(70vh, 620px); aspect-ratio: auto; }
      #logs { max-height: 180px; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">AK</span>
      <h1>Quiz Assistant</h1>
      <span class="byline">Made by Akash</span>
      <nav class="account-actions"><span id="account-name"></span><a id="admin-link" href="/admin" hidden>Admin</a><button id="logout" class="secondary" type="button">Log out</button></nav>
    </div>
    <p id="intro">Your study companion. Sign in to the assignment in the browser when prompted.</p>
  </header>
  <p id="assignment-credit-count" class="hint" hidden></p>
  <section class="card">
    <form id="start-form">
      <label for="url">Assignment URL</label>
      <input id="url" name="url" type="url" placeholder="https://example.com/assignment" required>
      <div id="sunstone-phone-row" hidden>
        <label for="sunstone-phone">Sunstone mobile number (optional)</label>
        <input id="sunstone-phone" name="sunstone-phone" type="tel" inputmode="tel" autocomplete="tel" placeholder="Include country code if needed">
        <p class="hint">Only sent to Sunstone to fill its phone field. The app does not save your number or handle OTPs. Sunstone's browser session can stay signed in for later visits.</p>
      </div>
      <p id="browser-hint" class="hint">The automation uses the local <code>browser_profile</code> and never uploads your browser session.</p>
      <label class="check">
        <input id="auto-start" type="checkbox">
        <span>Click the assignment’s Start button automatically. Leave unchecked to start it yourself in the browser.</span>
      </label>
      <div class="actions">
        <button id="start" class="primary" type="submit" disabled>Start</button>
        <button id="stop" class="secondary" type="button" disabled>Stop</button>
      </div>
    </form>
    <div id="run-notice" class="notice">It stops before final submission. Review and submit manually in Chrome; the browser stays open until you click Close browser. For phone + OTP sign-in, type the code yourself in the browser; this app never asks for it.</div>
  </section>
  <section id="browser-view-card" class="card public-only">
    <div class="status-row">
      <h2>Remote browser</h2>
      <a id="open-browser-view" class="hint" href="#" target="_blank" rel="noopener noreferrer">Open in new tab</a>
    </div>
    <p class="hint">Sign in here if needed. You can type your phone number and OTP directly into this isolated browser; the code is not sent to this app.</p>
    <iframe id="browser-view" class="browser-view" title="Remote browser live view" sandbox="allow-same-origin allow-scripts" allow="clipboard-read; clipboard-write"></iframe>
  </section>
  <section class="card" aria-live="polite">
    <div class="status-row">
      <h2>Run status</h2>
      <span id="badge" class="badge"><span class="dot"></span><span id="state">Idle</span></span>
    </div>
    <p id="message">Ready when you are.</p>
    <div id="progress"></div>
    <div class="actions" style="margin-top:16px">
      <button id="close-browser" class="secondary" type="button" disabled>Close browser</button>
    </div>
  </section>
  <section class="card">
    <h2>Activity</h2>
    <ul id="logs"><li>Activity will appear here.</li></ul>
  </section>
  <footer>AK · by Akash</footer>
</main>
<script>
  const form = document.querySelector("#start-form");
  const urlInput = document.querySelector("#url");
  const sunstonePhoneRow = document.querySelector("#sunstone-phone-row");
  const sunstonePhoneInput = document.querySelector("#sunstone-phone");
  const startButton = document.querySelector("#start");
  const stopButton = document.querySelector("#stop");
  const closeButton = document.querySelector("#close-browser");
  const browserViewCard = document.querySelector("#browser-view-card");
  const browserView = document.querySelector("#browser-view");
  const openBrowserView = document.querySelector("#open-browser-view");
  const badge = document.querySelector("#badge");
  const stateLabel = document.querySelector("#state");
  const message = document.querySelector("#message");
  const progress = document.querySelector("#progress");
  const assignmentCreditCount = document.querySelector("#assignment-credit-count");
  const logs = document.querySelector("#logs");
  let lastLogCount = -1;
  let publicMode = false;
  let lastState = null;
  const labels = {
    idle: "Idle", starting: "Starting", navigating: "Opening browser",
    login_required: "Sign in required", waiting: "Waiting",
    thinking: "Working", answer_set: "Answer set", advancing: "Advancing",
    retrying: "Retrying", warning: "Notice", awaiting_submission: "Ready for review",
    completed: "Finished", failed: "Error", needs_attention: "Needs attention",
    api_key_rejected: "Groq key rejected",
    awaiting_close: "Browser open", closing_browser: "Closing browser",
    stopping: "Stopping", stopped: "Stopped"
  };
  const activeStates = new Set(["starting", "navigating", "login_required", "waiting", "thinking", "answer_set", "advancing", "retrying", "warning", "closing_browser", "stopping"]);

  async function send(path, data) {
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data || {})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Request failed.");
    return result;
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    startButton.disabled = true;
    try {
      await send("/api/start", {
        url: urlInput.value.trim(),
        auto_start: document.querySelector("#auto-start").checked,
        mobile_number: sunstonePhoneRow.hidden ? "" : sunstonePhoneInput.value.trim()
      });
      await refresh();
    } catch (error) {
      message.textContent = error.message;
      startButton.disabled = false;
    }
  });

  stopButton.addEventListener("click", async () => {
    stopButton.disabled = true;
    try {
      await send("/api/stop");
      await refresh();
    } catch (error) {
      message.textContent = error.message;
    }
  });

  closeButton.addEventListener("click", async () => {
    closeButton.disabled = true;
    try {
      await send("/api/close");
      await refresh();
    } catch (error) {
      message.textContent = error.message;
    }
  });

  async function refresh() {
    try {
      const response = await fetch("/api/status", {cache: "no-store"});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not load session status.");
      publicMode = Boolean(data.public_mode);
      document.querySelector("#account-name").textContent = data.username || "";
      document.querySelector("#admin-link").hidden = data.role !== "admin";
      const hasAssignmentCredits = data.role !== "user" || Number(data.assignment_credits) > 0;
      assignmentCreditCount.hidden = data.role !== "user";
      assignmentCreditCount.textContent = data.role === "user"
        ? `Assignments available: ${data.assignment_credits}`
        : "";
      const state = data.state || "idle";
      lastState = state;
      const active = activeStates.has(state);
      stateLabel.textContent = labels[state] || state;
      badge.classList.toggle("active", active);
      message.textContent = data.message || "Ready when you are.";
      progress.textContent = data.question
        ? `Question ${data.question}${data.total ? ` of ${data.total}` : ""}`
        : "";
      startButton.disabled = active || data.worker_alive || !data.api_key_configured || !hasAssignmentCredits;
      stopButton.disabled = !active;
      closeButton.disabled = !data.close_available;
      if (data.logs.length !== lastLogCount) {
        lastLogCount = data.logs.length;
        logs.replaceChildren();
        for (const entry of data.logs.slice(-50)) {
          const item = document.createElement("li");
          const time = document.createElement("time");
          time.textContent = entry.time;
          item.append(time, document.createTextNode(entry.message));
          logs.append(item);
        }
      }
      if (!data.api_key_configured) {
        message.textContent = "The administrator needs to configure an API key before runs can start.";
      } else if (data.role === "user" && !hasAssignmentCredits) {
        message.textContent = "No assignments remaining. Contact the administrator for more credits.";
      }
      if (data.public_mode) {
        browserViewCard.style.display = data.live_view_url ? "block" : "none";
        if (data.live_view_url && browserView.src !== data.live_view_url) {
          browserView.src = data.live_view_url;
          openBrowserView.href = data.live_view_url;
        } else if (!data.live_view_url && browserView.src) {
          browserView.src = "about:blank";
          openBrowserView.href = "#";
        }
        document.querySelector("#browser-hint").textContent =
          "Your sign-in is stored in an isolated hosted browser context linked to your account.";
        document.querySelector("#run-notice").textContent =
          "It stops before final submission. Review and submit manually in the browser view. Enter any phone number and OTP yourself; the app never asks for your code.";
      }
    } catch (error) {
      message.textContent = error.message || (publicMode
        ? "Could not load your hosted browser session. Refresh and try again."
        : "Could not reach the local app. Check that web_app.py is still running.");
      if (error.message && error.message.includes("Sign in")) location.href = "/login";
    }
  }

  document.querySelector("#logout").addEventListener("click", async () => {
    try { await send("/api/auth/logout", {}); location.href = "/login"; }
    catch (error) { message.textContent = error.message; }
  });

  function updateSunstonePhoneField() {
    let isSunstone = false;
    try {
      const host = new URL(urlInput.value).hostname.toLowerCase().replace(/\.$/, "");
      isSunstone = host === "hub.sunstone.in" || host === "www.hub.sunstone.in";
    } catch (error) {
      isSunstone = false;
    }
    sunstonePhoneRow.hidden = !isSunstone;
    if (!isSunstone) sunstonePhoneInput.value = "";
  }
  urlInput.addEventListener("input", updateSunstonePhoneField);
  updateSunstonePhoneField();

  refresh();
  setInterval(refresh, 1800);
</script>
</body>
</html>"""


class RunController:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = None
        self.close_event = None
        self.worker = None
        self.logs = deque(maxlen=200)
        self.state = {
            "state": "idle",
            "message": "Ready when you are.",
            "question": None,
            "total": None,
            "live_view_url": None,
        }

    def report(self, event):
        message = event.get("message", "")
        with self.lock:
            self.state["state"] = event.get("status", "running")
            self.state["message"] = message
            if event.get("question") is not None:
                self.state["question"] = event["question"]
            if event.get("total") is not None:
                self.state["total"] = event["total"]
            if "live_view_url" in event:
                self.state["live_view_url"] = event["live_view_url"]
            self.logs.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": message,
            })

    def start(
        self,
        url,
        auto_start,
        api_keys=None,
        browser_context_id=None,
        mobile_number=None,
        credit_username=None,
        credit_grant_id=None,
    ):
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Enter a full http:// or https:// assignment URL.")
        mobile_number = _validate_sunstone_mobile(url, mobile_number)
        api_keys = api_keys if api_keys is not None else _get_api_keys()
        if not api_keys:
            raise ValueError("The administrator has not configured any API keys yet.")
        mobile_number = _validate_sunstone_mobile(url, mobile_number)
        if PUBLIC_MODE and not browser_context_id:
            raise ValueError("The visitor browser context is missing. Refresh the page.")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("A run is already active.")
            self.stop_event = threading.Event()
            self.close_event = threading.Event()
            self.state = {
                "state": "starting",
                "message": "Starting the browser runner.",
                "question": None,
                "total": None,
                "live_view_url": None,
            }
            self.logs.clear()
            self.logs.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": self.state["message"],
            })
            self.worker = threading.Thread(
                target=self._run,
                args=(
                    url,
                    auto_start,
                    self.stop_event,
                    api_keys,
                    browser_context_id,
                    mobile_number,
                    credit_username,
                    credit_grant_id,
                ),
                name="quiz-runner",
                daemon=True,
            )
            self.worker.start()

    def _run(
        self,
        url,
        auto_start,
        stop_event,
        api_keys,
        browser_context_id,
        mobile_number,
        credit_username,
        credit_grant_id,
    ):
        credit_consumed = False
        try:
            result = run_automation(
                url,
                on_event=self.report,
                stop_event=stop_event,
                close_event=self.close_event,
                auto_start=auto_start,
                keep_browser_open=True,
                browser_context_id=browser_context_id,
                api_keys=api_keys,
                mobile_number=mobile_number,
            )
            credit_consumed = result == "awaiting_submission"
            if result == "api_key_rejected":
                self.report({
                    "status": result,
                    "message": "All configured Groq API keys were rejected. Ask the administrator to replace them.",
                    "live_view_url": "",
                })
                return
            self.report({
                "status": result,
                "message": f"Browser closed. Run ended: {result.replace('_', ' ')}.",
                "live_view_url": "",
            })
        except Exception as exc:
            snapshot = self.snapshot()
            if snapshot["state"] != "failed":
                self.report({
                    "status": "failed",
                    "message": str(exc),
                    "live_view_url": "",
                })
        finally:
            if credit_username and not credit_consumed:
                try:
                    _refund_assignment_credit(credit_username, credit_grant_id)
                except Exception as exc:
                    self.report({
                        "status": "failed",
                        "message": f"Could not restore the assignment credit ({type(exc).__name__}); contact the administrator.",
                        "live_view_url": "",
                    })

    def stop(self):
        with self.lock:
            if not self.worker or not self.worker.is_alive() or not self.stop_event:
                raise ValueError("There is no active run to stop.")
            self.stop_event.set()
            self.state.update({
                "state": "stopping",
                "message": "Stopping safely; waiting for the current browser/API action.",
            })
            self.logs.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": self.state["message"],
            })

    def close_browser(self):
        with self.lock:
            if not self.worker or not self.worker.is_alive() or not self.close_event:
                raise ValueError("There is no browser window waiting to close.")
            if self.state["state"] != "awaiting_close":
                raise ValueError("The browser can be closed after the run stops.")
            self.close_event.set()
            self.state.update({
                "state": "closing_browser",
                "message": "Closing the automation browser.",
            })
            self.logs.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": self.state["message"],
            })

    def snapshot(self):
        with self.lock:
            worker_alive = bool(self.worker and self.worker.is_alive())
            keys_configured = bool(_get_api_keys())
            return {
                **self.state,
                "logs": list(self.logs),
                "api_key_configured": keys_configured,
                "worker_alive": worker_alive,
                "close_available": worker_alive and self.state["state"] == "awaiting_close",
                "public_mode": PUBLIC_MODE,
            }


class ControllerRegistry:
    def __init__(self):
        self.lock = threading.Lock()
        self.controllers = {}
        self.active_visitor_id = None

    def get(self, visitor_id):
        with self.lock:
            if visitor_id not in self.controllers:
                self.controllers[visitor_id] = RunController()
            return self.controllers[visitor_id]

    def snapshot(self, visitor_id):
        with self.lock:
            controller = self.controllers.get(visitor_id)
        return controller.snapshot() if controller else RunController().snapshot()

    def start(
        self,
        visitor_id,
        url,
        auto_start,
        api_keys,
        browser_context_id,
        mobile_number,
        credit_username=None,
        credit_grant_id=None,
    ):
        with self.lock:
            if not PUBLIC_MODE and self.active_visitor_id:
                active_controller = self.controllers.get(self.active_visitor_id)
                if active_controller and active_controller.snapshot()["worker_alive"]:
                    if self.active_visitor_id != visitor_id:
                        raise ValueError("Another visitor is using the browser right now. Please try again later.")
                else:
                    self.active_visitor_id = None

            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Enter a full http:// or https:// assignment URL.")
            if not api_keys:
                raise ValueError("The administrator has not configured any API keys yet.")
            if PUBLIC_MODE and not browser_context_id:
                browser_context_id = create_browserbase_context()

            controller = self.controllers.get(visitor_id)
            if controller is None:
                controller = RunController()
                self.controllers[visitor_id] = controller
            controller.start(
                url,
                auto_start,
                api_keys=api_keys,
                browser_context_id=browser_context_id,
                mobile_number=mobile_number,
                credit_username=credit_username,
                credit_grant_id=credit_grant_id,
            )
            if not PUBLIC_MODE:
                self.active_visitor_id = visitor_id
            return controller, browser_context_id


CONTROLLERS = ControllerRegistry()


class RequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, status, data, session_cookie=None):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if session_cookie:
            secure = "; Secure" if PUBLIC_MODE else ""
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={session_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age=315360000{secure}",
            )
        self.end_headers()
        self.wfile.write(payload)

    def _send_page(self, status, page):
        payload = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_cookie_expired(self):
        secure = "; Secure" if PUBLIC_MODE else ""
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}",
        )

    def _require_admin(self, session):
        if not session or session["role"] != "admin":
            self._send_json(403 if session else 401, {"error": "Admin sign-in required."})
            return False
        return True

    def _get_payload(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 16384:
            raise ValueError("Request is too large.")
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("Invalid request body.")
        return payload

    def do_GET(self):
        try:
            self._handle_get()
        except RuntimeError as exc:
            self._send_json(502, {"error": str(exc)})
        except PoolTimeout:
            self._send_json(503, {"error": "Database is busy; please retry shortly."})
        except psycopg.Error as exc:
            self._send_json(500, {"error": f"Database operation failed: {exc}."})
        except sqlite3.Error as exc:
            self._send_json(500, {"error": f"Database operation failed: {exc}"})

    def _handle_get(self):
        path = urlparse(self.path).path
        session = _session_from_request(self)
        if path == "/api/status":
            if not session:
                self._send_json(401, {"error": "Sign in required."})
                return
            status = CONTROLLERS.snapshot(session["visitor_id"])
            if session["role"] == "user":
                with _connect_database() as connection:
                    row = _execute(
                        connection,
                        """
                        SELECT COALESCE((
                            SELECT SUM(g.remaining_credits)
                            FROM assignment_credit_grants g
                            WHERE g.user_id = u.id AND g.remaining_credits > 0
                              AND (g.expires_at IS NULL OR g.expires_at > ?)
                        ), 0) AS assignment_credits
                        FROM users u WHERE LOWER(u.username) = LOWER(?)
                        """,
                        (int(datetime.now().timestamp()), session["username"]),
                    ).fetchone()
                status["assignment_credits"] = row["assignment_credits"] if row else 0
            status.update({
                "username": session["username"],
                "role": session["role"],
            })
            self._send_json(200, status)
            return
        if path == "/api/admin/users":
            if not self._require_admin(session):
                return
            with _connect_database() as connection:
                rows = _execute(
                    connection,
                    """
                    SELECT u.username, u.date_of_birth, u.created_at,
                           COALESCE((
                               SELECT SUM(g.remaining_credits)
                               FROM assignment_credit_grants g
                               WHERE g.user_id = u.id AND g.remaining_credits > 0
                                 AND (g.expires_at IS NULL OR g.expires_at > ?)
                           ), 0) AS assignment_credits
                    FROM users u ORDER BY u.created_at DESC
                    """,
                    (int(datetime.now().timestamp()),),
                ).fetchall()
            self._send_json(200, {"users": [dict(row) for row in rows]})
            return
        if path == "/api/admin/credits/defaults":
            if not self._require_admin(session):
                return
            with _connect_database() as connection:
                settings = _get_default_assignment_settings(connection)
            self._send_json(200, settings)
            return
        if path == "/api/admin/keys":
            if not self._require_admin(session):
                return
            with _connect_database() as connection:
                rows = _execute(
                    connection,
                    "SELECT id, encrypted_key FROM api_keys ORDER BY id"
                ).fetchall()
            cipher = _key_cipher()
            keys = []
            for row in rows:
                try:
                    value = cipher.decrypt(row["encrypted_key"].encode()).decode()
                except InvalidToken as exc:
                    raise RuntimeError("Stored API keys cannot be decrypted; check APP_SECRET.") from exc
                keys.append({"id": row["id"], "masked": f"••••{value[-4:]}"})
            self._send_json(200, {"keys": keys})
            return
        if path in {"/", "/login"}:
            self._send_page(200, PAGE if session else AUTH_PAGE)
            return
        if path == "/admin":
            if not session:
                self._send_page(200, AUTH_PAGE)
            elif session["role"] != "admin":
                self._send_page(403, "<!doctype html><meta name='viewport' content='width=device-width'><h1>Admin sign-in required</h1><a href='/'>Return to app</a>")
            else:
                self._send_page(200, ADMIN_PAGE)
            return
        self._send_json(404, {"error": "Not found."})

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            payload = self._get_payload()
            if path == "/api/auth/logout":
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get("Cookie", ""))
                except CookieError:
                    pass
                morsel = cookie.get(SESSION_COOKIE)
                if morsel:
                    with _connect_database() as connection:
                        _execute(
                            connection,
                            "DELETE FROM sessions WHERE token_hash = ?",
                            (hashlib.sha256(morsel.value.encode()).hexdigest(),),
                        )
                        connection.commit()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self._send_cookie_expired()
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return
            if path in {"/api/auth/login", "/api/auth/signup"}:
                username = payload.get("username")
                password = payload.get("password")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError("Enter a username and password.")
                username = username.strip()
                admin_username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
                if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username):
                    raise ValueError("Username must be 3–32 letters, numbers, or underscores.")
                role = "user"
                if (
                    path.endswith("/login")
                    and hmac.compare_digest(username.casefold(), admin_username.casefold())
                    and os.getenv("ADMIN_PASSWORD")
                    and hmac.compare_digest(password, os.getenv("ADMIN_PASSWORD", ""))
                ):
                    role = "admin"
                elif path.endswith("/signup"):
                    if username.casefold() == admin_username.casefold():
                        raise ValueError("That username is reserved.")
                    username = username.casefold()
                    if len(password) < 10:
                        raise ValueError("Password must be at least 10 characters.")
                    birth_date = payload.get("date_of_birth")
                    try:
                        if isinstance(birth_date, str) and re.fullmatch(
                            r"\d{2}/\d{2}/\d{4}", birth_date
                        ):
                            parsed_birth_date = datetime.strptime(
                                birth_date, "%m/%d/%Y"
                            ).date()
                        else:
                            parsed_birth_date = date.fromisoformat(birth_date)
                    except (TypeError, ValueError):
                        raise ValueError("Enter a valid date of birth.")
                    if parsed_birth_date >= date.today():
                        raise ValueError("Date of birth must be in the past.")
                    birth_date = parsed_birth_date.isoformat()
                    with _connect_database() as connection:
                        _execute(
                            connection,
                            """
                            INSERT INTO users (
                                username, date_of_birth, password_hash, created_at,
                                visitor_id, browser_context_id
                            ) VALUES (?, ?, ?, ?, ?, NULL)
                            """,
                            (
                                username,
                                birth_date,
                                _password_hash(password),
                                datetime.now().isoformat(),
                                secrets.token_urlsafe(24),
                            ),
                        )
                        user = _execute(
                            connection,
                            "SELECT id FROM users WHERE LOWER(username) = LOWER(?)",
                            (username,),
                        ).fetchone()
                        defaults = _get_default_assignment_settings(connection)
                        if defaults["credits"]:
                            _insert_assignment_credit_grant(
                                connection,
                                user["id"],
                                defaults["credits"],
                                defaults["expiry_days"],
                            )
                        connection.commit()
                else:
                    with _connect_database() as connection:
                        user = _execute(
                            connection,
                            "SELECT username, password_hash FROM users WHERE LOWER(username) = LOWER(?)",
                            (username,),
                        ).fetchone()
                    if not user or not _verify_password(password, user["password_hash"]):
                        raise ValueError("Username or password is incorrect.")
                    username = user["username"]
                session_cookie = _create_session(username, role)
                self._send_json(200, {"ok": True, "role": role}, session_cookie=session_cookie)
                return

            session = _session_from_request(self)
            if not session:
                self._send_json(401, {"error": "Sign in required."})
                return
            if path == "/api/admin/keys":
                if not self._require_admin(session):
                    return
                if self.command == "POST":
                    keys = payload.get("keys")
                    if not isinstance(keys, list):
                        raise ValueError("Provide API keys, one per line.")
                    _save_api_keys(_get_api_keys() + keys)
                    self._send_json(200, {"ok": True})
                elif self.command == "DELETE":
                    try:
                        key_id = int(payload.get("id"))
                    except (TypeError, ValueError):
                        raise ValueError("Select a valid API key.")
                    with _connect_database() as connection:
                        cursor = _execute(connection, "DELETE FROM api_keys WHERE id = ?", (key_id,))
                        connection.commit()
                    if not cursor.rowcount:
                        self._send_json(404, {"error": "API key not found."})
                    else:
                        self._send_json(200, {"ok": True})
                else:
                    self._send_json(405, {"error": "Method not allowed."})
                return
            if path == "/api/admin/users/reset-password":
                if not self._require_admin(session):
                    return
                username = payload.get("username")
                password = payload.get("new_password")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError("Select an account and enter a new password.")
                username = username.strip()
                if len(password) < 10:
                    raise ValueError("New password must be at least 10 characters.")
                with _connect_database() as connection:
                    cursor = _execute(
                        connection,
                        "UPDATE users SET password_hash = ? WHERE LOWER(username) = LOWER(?)",
                        (_password_hash(password), username),
                    )
                    if cursor.rowcount:
                        _execute(
                            connection,
                            "DELETE FROM sessions WHERE LOWER(username) = LOWER(?) AND role = 'user'",
                            (username,),
                        )
                        connection.commit()
                if not cursor.rowcount:
                    self._send_json(404, {"error": "User account not found."})
                else:
                    self._send_json(200, {"ok": True})
                return
            if path == "/api/admin/users/credits":
                if not self._require_admin(session):
                    return
                username = payload.get("username")
                amount = payload.get("amount")
                expiry_days = payload.get("expiry_days")
                if not isinstance(username, str) or not username.strip():
                    raise ValueError("Select a valid user account.")
                balance = _add_assignment_credits(
                    username.strip(), amount, expiry_days
                )
                self._send_json(200, {"assignment_credits": balance})
                return
            if path == "/api/admin/credits/defaults":
                if not self._require_admin(session):
                    return
                amount = payload.get("credits")
                expiry_days = payload.get("expiry_days")
                if (
                    isinstance(amount, bool)
                    or not isinstance(amount, int)
                    or not 0 <= amount <= 10000
                ):
                    raise ValueError("Default assignments must be a whole number from 0 to 10,000.")
                if (
                    isinstance(expiry_days, bool)
                    or not isinstance(expiry_days, int)
                    or not 0 <= expiry_days <= 3650
                ):
                    raise ValueError("Expiry must be a whole number from 0 to 3,650 days; use 0 for no expiry.")
                with _connect_database() as connection:
                    _set_app_setting(connection, "default_assignment_credits", amount)
                    _set_app_setting(
                        connection,
                        "default_assignment_expiry_days",
                        expiry_days,
                    )
                    connection.commit()
                self._send_json(
                    200,
                    {"credits": amount, "expiry_days": expiry_days},
                )
                return
            if path == "/api/start":
                api_keys = _get_api_keys()
                mobile_number = payload.get("mobile_number")
                if mobile_number is not None and not isinstance(mobile_number, str):
                    raise ValueError("Enter a valid phone number.")
                credit_username = session["username"] if session["role"] == "user" else None
                credit_grant_id = None
                if credit_username:
                    credit_grant_id = _reserve_assignment_credit(credit_username)
                try:
                    controller, browser_context_id = CONTROLLERS.start(
                        session["visitor_id"],
                        str(payload.get("url", "")).strip(),
                        bool(payload.get("auto_start", False)),
                        api_keys,
                        session["browser_context_id"],
                        mobile_number,
                        credit_username=credit_username,
                        credit_grant_id=credit_grant_id,
                    )
                except Exception:
                    if credit_username:
                        _refund_assignment_credit(credit_username, credit_grant_id)
                    raise
                if browser_context_id != session["browser_context_id"]:
                    with _connect_database() as connection:
                        if session["role"] == "user":
                            _execute(
                                connection,
                                """
                                UPDATE sessions SET browser_context_id = ?
                                WHERE LOWER(username) = LOWER(?) AND role = 'user'
                                """,
                                (browser_context_id, session["username"]),
                            )
                            _execute(
                                connection,
                                "UPDATE users SET browser_context_id = ? WHERE LOWER(username) = LOWER(?)",
                                (browser_context_id, session["username"]),
                            )
                        else:
                            _execute(
                                connection,
                                "UPDATE sessions SET browser_context_id = ? WHERE token_hash = ?",
                                (
                                    browser_context_id,
                                    hashlib.sha256(self._session_token().encode()).hexdigest(),
                                ),
                            )
                        connection.commit()
                self._send_json(202, {"ok": True, "state": controller.snapshot()["state"]})
            elif path == "/api/stop":
                controller = self._get_request_controller(session)
                controller.stop()
                self._send_json(202, {"ok": True})
            elif path == "/api/close":
                controller = self._get_request_controller(session)
                controller.close_browser()
                self._send_json(202, {"ok": True})
            else:
                self._send_json(404, {"error": "Not found."})
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._send_json(502, {"error": str(exc)})
        except PoolTimeout:
            self._send_json(503, {"error": "Database is busy; please retry shortly."})
        except psycopg.errors.UniqueViolation:
            self._send_json(409, {"error": "That username already exists."})
        except sqlite3.IntegrityError:
            self._send_json(409, {"error": "That username already exists."})
        except psycopg.Error as exc:
            self._send_json(500, {"error": f"Database operation failed: {exc}."})
        except sqlite3.Error as exc:
            self._send_json(500, {"error": f"Database operation failed: {exc}"})

    def do_DELETE(self):
        self.do_POST()

    def _session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    def _get_request_controller(self, session):
        return CONTROLLERS.get(session["visitor_id"])

    def log_message(self, format_string, *args):
        return


def main():
    load_env_file()
    if len(os.getenv("APP_SECRET", "")) < 32:
        raise RuntimeError("Set APP_SECRET to a random value of at least 32 characters.")
    if not os.getenv("ADMIN_PASSWORD", ""):
        raise RuntimeError("Set ADMIN_PASSWORD to enable the administrator account.")
    if PUBLIC_MODE:
        if not os.getenv("BROWSERBASE_API_KEY", "").strip():
            raise RuntimeError("Set BROWSERBASE_API_KEY in the hosted service environment.")
    with _connect_database():
        pass
    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    server.daemon_threads = True
    address = f"http://{HOST}:{PORT}"
    print(f"Local control page: {address}")
    print("Press Ctrl+C to stop the server.")
    webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local control page.")
    finally:
        server.server_close()
        if DATABASE_POOL is not None:
            DATABASE_POOL.close()


if __name__ == "__main__":
    main()
