import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlparse

from agent import create_browserbase_context, load_env_file, run_automation

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8765"))
PUBLIC_MODE = os.getenv("PUBLIC_MODE", "").strip().lower() in {"1", "true", "yes"}
SESSION_COOKIE = "quiz_session"
ENV_FILE_PATH = Path(__file__).with_name(".env")
PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quiz Assistant</title>
  <style>
    :root { color-scheme: light; font-family: Inter, "Segoe UI", sans-serif; background: #f3f5f9; color: #172033; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 28px 16px; }
    main { width: min(760px, 100%); }
    header { margin: 0 0 22px; }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-mark { display: grid; place-items: center; width: 42px; height: 42px; border-radius: 13px; background: #465cf5; color: white; font-size: .82rem; font-weight: 800; letter-spacing: .04em; box-shadow: 0 5px 14px #465cf533; }
    h1 { margin: 0; font-size: clamp(1.7rem, 4vw, 2.2rem); letter-spacing: -.04em; }
    .byline { margin-left: auto; color: #8a94a5; font-size: .78rem; white-space: nowrap; }
    header p { margin: 12px 0 0; color: #69758b; line-height: 1.55; }
    .card { background: white; border: 1px solid #e3e8f0; border-radius: 18px; padding: 24px; box-shadow: 0 12px 36px #1720330b; margin: 14px 0; }
    label { display: block; font-size: .9rem; font-weight: 650; margin: 0 0 8px; }
    input[type=url], input[type=password] { width: 100%; min-width: 0; padding: 13px 14px; border: 1px solid #cbd4e2; border-radius: 10px; font: inherit; outline: none; }
    input[type=url]:focus, input[type=password]:focus { border-color: #536dfe; box-shadow: 0 0 0 3px #536dfe22; }
    .hint { margin: 8px 0 0; color: #7a8497; font-size: .84rem; line-height: 1.45; }
    .key-row { display: flex; align-items: center; gap: 10px; }
    .key-row button { flex: 0 0 auto; }
    #key-status { min-height: 1.3em; }
    .check { display: flex; gap: 10px; align-items: flex-start; margin: 18px 0; font-weight: 500; line-height: 1.45; }
    .check input { margin-top: 3px; }
    .actions { display: flex; gap: 10px; }
    button { border: 0; border-radius: 10px; padding: 11px 17px; font: inherit; font-weight: 650; cursor: pointer; }
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
    .browser-view { width: 100%; aspect-ratio: 16 / 10; border: 1px solid #dce2ed; border-radius: 10px; background: #111827; }
    h2 { font-size: 1rem; margin: 0 0 12px; }
    #logs { margin: 0; padding: 0; list-style: none; max-height: 230px; overflow: auto; }
    #logs li { padding: 9px 0; border-top: 1px solid #edf0f5; color: #5e6a7e; font-size: .86rem; line-height: 1.4; overflow-wrap: anywhere; }
    #logs time { color: #9aa3b2; margin-right: 8px; font-variant-numeric: tabular-nums; }
    footer { padding: 4px 0 8px; text-align: center; color: #a0a8b6; font-size: .76rem; letter-spacing: .02em; }
    @media (max-width: 520px) { .card { padding: 18px; } .actions button { flex: 1; } .key-row { align-items: stretch; flex-direction: column; } .key-row button { width: 100%; } .byline { font-size: .72rem; } }
  </style>
</head>
<body>
<main>
  <header>
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">AK</span>
      <h1>Quiz Assistant</h1>
      <span class="byline">Made by Akash</span>
    </div>
    <p id="intro">Your local companion for working in a separate browser window. Sign in there if needed; your browser session stays on this computer.</p>
  </header>
  <section class="card">
    <h2>One-time setup (Go to https://groq.com -> API Keys -> Create API Key -> Copy it -> Paste it here)</h2>
    <form id="key-form">
      <label for="api-key">Groq API key</label>
      <div class="key-row">
        <input id="api-key" name="api-key" type="password" placeholder="Paste your Groq API key" autocomplete="off" autocapitalize="off" spellcheck="false" required>
        <button id="save-key" class="secondary" type="submit">Save key</button>
      </div>
      <p class="hint">Saved only on this computer. It isn’t shown in the page or activity log, and you can replace it anytime.</p>
      <p id="key-status" class="hint" role="status" aria-live="polite"></p>
    </form>
  </section>
  <section class="card">
    <form id="start-form">
      <div id="public-key-row" class="public-only">
        <label for="groq-key">Your Groq API key</label>
        <input id="groq-key" name="groq-key" type="password" placeholder="Paste your Groq API key" autocomplete="off" autocapitalize="off" spellcheck="false">
        <p class="hint">Sent over HTTPS for this run only; this website does not save it.</p>
      </div>
      <label for="url">Assignment URL (Note - This will only work for MCQ's only)</label>
      <input id="url" name="url" type="url" placeholder="https://example.com/assignment" required>
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
    <div id="run-notice" class="notice">It stops before final submission. Review and submit manually in Chrome; the browser stays open until you click Close browser. Starting an assignment may use an attempt; confirm you’re allowed to automate it.</div>
  </section>
  <section id="browser-view-card" class="card public-only">
    <div class="status-row">
      <h2>Remote browser</h2>
      <a id="open-browser-view" class="hint" href="#" target="_blank" rel="noopener noreferrer">Open in new tab</a>
    </div>
    <p class="hint">Sign in here if needed. This visitor’s browser session is kept in an isolated hosted context.</p>
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
  const keyForm = document.querySelector("#key-form");
  const keyInput = document.querySelector("#api-key");
  const saveKeyButton = document.querySelector("#save-key");
  const keyStatus = document.querySelector("#key-status");
  const startButton = document.querySelector("#start");
  const stopButton = document.querySelector("#stop");
  const closeButton = document.querySelector("#close-browser");
  const publicKeyRow = document.querySelector("#public-key-row");
  const publicKeyInput = document.querySelector("#groq-key");
  const browserViewCard = document.querySelector("#browser-view-card");
  const browserView = document.querySelector("#browser-view");
  const openBrowserView = document.querySelector("#open-browser-view");
  const badge = document.querySelector("#badge");
  const stateLabel = document.querySelector("#state");
  const message = document.querySelector("#message");
  const progress = document.querySelector("#progress");
  const logs = document.querySelector("#logs");
  let lastLogCount = -1;
  let keyStatusEdited = false;
  let publicMode = false;
  const labels = {
    idle: "Idle", starting: "Starting", navigating: "Opening browser",
    login_required: "Sign in required", waiting: "Waiting",
    thinking: "Working", answer_set: "Answer set", advancing: "Advancing",
    retrying: "Retrying", warning: "Notice", awaiting_submission: "Ready for review",
    completed: "Finished", failed: "Error", needs_attention: "Needs attention",
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

  keyForm.addEventListener("submit", async event => {
    event.preventDefault();
    saveKeyButton.disabled = true;
    keyStatusEdited = true;
    keyStatus.textContent = "Saving your key on this computer…";
    try {
      await send("/api/config", {api_key: keyInput.value});
      keyInput.value = "";
      keyStatus.textContent = "Saved. You can replace the key anytime.";
      await refresh();
    } catch (error) {
      keyStatus.textContent = error.message;
    } finally {
      saveKeyButton.disabled = false;
    }
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    startButton.disabled = true;
    try {
      await send("/api/start", {
        url: document.querySelector("#url").value.trim(),
        auto_start: document.querySelector("#auto-start").checked,
        api_key: publicMode ? publicKeyInput.value : undefined
      });
      if (publicMode) publicKeyInput.value = "";
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
      const state = data.state || "idle";
      const active = activeStates.has(state);
      stateLabel.textContent = labels[state] || state;
      badge.classList.toggle("active", active);
      message.textContent = data.message || "Ready when you are.";
      progress.textContent = data.question
        ? `Question ${data.question}${data.total ? ` of ${data.total}` : ""}`
        : "";
      startButton.disabled = active || data.worker_alive
        || (data.public_mode
          ? !publicKeyInput.value.trim()
          : !data.api_key_configured);
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
        message.textContent = "Save your Groq key above to enable Start.";
      }
      if (data.public_mode) {
        keyForm.closest(".card").style.display = "none";
        publicKeyRow.style.display = "block";
        publicKeyInput.required = true;
        browserViewCard.style.display = data.live_view_url ? "block" : "none";
        if (data.live_view_url && browserView.src !== data.live_view_url) {
          browserView.src = data.live_view_url;
          openBrowserView.href = data.live_view_url;
        } else if (!data.live_view_url && browserView.src) {
          browserView.src = "about:blank";
          openBrowserView.href = "#";
        }
        document.querySelector("#intro").textContent =
          "Use the hosted browser below. Sign-in is kept in an isolated Browserbase cloud context linked to this browser.";
        document.querySelector("#browser-hint").textContent =
          "Your sign-in is stored in this visitor’s Browserbase context, not on this device or shared with other visitors.";
        document.querySelector("#run-notice").textContent =
          "It stops before final submission. Review and submit manually in the browser view. Starting an assignment may use an attempt; confirm you’re allowed to automate it.";
      }
      if (!keyStatusEdited && !data.public_mode) {
        keyStatus.textContent = data.api_key_configured
          ? "A key is saved on this computer. Paste another one above to replace it."
          : "Add it once here; you won’t need to re-enter it each time.";
      }
    } catch (error) {
      message.textContent = error.message || (publicMode
        ? "Could not load your hosted browser session. Refresh and try again."
        : "Could not reach the local app. Check that web_app.py is still running.");
    }
  }

  refresh();
  setInterval(refresh, 800);
</script>
</body>
</html>"""


def save_api_key(api_key: str) -> None:
    key = api_key.strip()
    if not key:
        raise ValueError("Paste your Groq API key first.")
    if any(character in key for character in ("\r", "\n", "\0")):
        raise ValueError("The API key must be a single line.")

    lines = ENV_FILE_PATH.read_text(encoding="utf-8").splitlines() if ENV_FILE_PATH.exists() else []
    updated_lines = []
    key_written = False
    for line in lines:
        normalized = line.strip()
        if normalized.startswith("GROQ_API_KEY=") or normalized.startswith("export GROQ_API_KEY="):
            if not key_written:
                updated_lines.append(f"GROQ_API_KEY={key}")
                key_written = True
            continue
        updated_lines.append(line)
    if not key_written:
        if updated_lines and updated_lines[-1]:
            updated_lines.append("")
        updated_lines.append(f"GROQ_API_KEY={key}")

    ENV_FILE_PATH.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    os.environ["GROQ_API_KEY"] = key


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

    def start(self, url, auto_start, api_key=None, browser_context_id=None):
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Enter a full http:// or https:// assignment URL.")
        if PUBLIC_MODE:
            if not isinstance(api_key, str) or not api_key.strip():
                raise ValueError("Paste your Groq API key to start.")
            if not browser_context_id:
                raise ValueError("The visitor browser context is missing. Refresh the page.")
        else:
            load_env_file()
            if not os.environ.get("GROQ_API_KEY", "").strip():
                raise ValueError("GROQ_API_KEY is missing from .env.")
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
                args=(url, auto_start, self.stop_event, api_key, browser_context_id),
                name="quiz-runner",
                daemon=True,
            )
            self.worker.start()

    def _run(self, url, auto_start, stop_event, api_key, browser_context_id):
        try:
            result = run_automation(
                url,
                on_event=self.report,
                stop_event=stop_event,
                close_event=self.close_event,
                auto_start=auto_start,
                keep_browser_open=True,
                api_key=api_key,
                browser_context_id=browser_context_id,
            )
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
            return {
                **self.state,
                "logs": list(self.logs),
                "api_key_configured": (
                    True if PUBLIC_MODE
                    else bool(os.environ.get("GROQ_API_KEY", "").strip())
                ),
                "worker_alive": worker_alive,
                "close_available": worker_alive and self.state["state"] == "awaiting_close",
                "public_mode": PUBLIC_MODE,
            }


def encode_session_cookie(visitor_id, browser_context_id=None):
    secret = os.getenv("APP_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("Set APP_SECRET to a random value of at least 32 characters.")
    payload = json.dumps(
        {"visitor_id": visitor_id, "browser_context_id": browser_context_id},
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def decode_session_cookie(token):
    secret = os.getenv("APP_SECRET", "")
    if len(secret) < 32 or not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = encoded.encode("ascii")
        payload += b"=" * (-len(payload) % 4)
        identity = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    visitor_id = identity.get("visitor_id") if isinstance(identity, dict) else None
    browser_context_id = identity.get("browser_context_id") if isinstance(identity, dict) else None
    if not isinstance(visitor_id, str) or not visitor_id:
        return None
    if browser_context_id is not None and not isinstance(browser_context_id, str):
        return None
    return {"visitor_id": visitor_id, "browser_context_id": browser_context_id}


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

    def start(self, visitor_id, url, auto_start, api_key, browser_context_id):
        with self.lock:
            if self.active_visitor_id:
                active_controller = self.controllers.get(self.active_visitor_id)
                if active_controller and active_controller.snapshot()["worker_alive"]:
                    if self.active_visitor_id != visitor_id:
                        raise ValueError("Another visitor is using the browser right now. Please try again later.")
                else:
                    self.active_visitor_id = None

            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Enter a full http:// or https:// assignment URL.")
            if not isinstance(api_key, str) or not api_key.strip():
                raise ValueError("Paste your Groq API key to start.")
            if not browser_context_id:
                browser_context_id = create_browserbase_context()

            controller = self.controllers.get(visitor_id)
            if controller is None:
                controller = RunController()
                self.controllers[visitor_id] = controller
            controller.start(
                url,
                auto_start,
                api_key=api_key,
                browser_context_id=browser_context_id,
            )
            self.active_visitor_id = visitor_id
            return controller, browser_context_id


CONTROLLER = RunController()
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
                f"{SESSION_COOKIE}={session_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000{secure}",
            )
        self.end_headers()
        self.wfile.write(payload)

    def _get_identity(self, create=False):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            pass
        morsel = cookie.get(SESSION_COOKIE)
        token = morsel.value if morsel else ""
        identity = decode_session_cookie(token)
        if identity:
            return identity, None
        if not create:
            return None, None
        visitor_id = secrets.token_urlsafe(24)
        identity = {"visitor_id": visitor_id, "browser_context_id": None}
        return identity, encode_session_cookie(visitor_id)

    def do_GET(self):
        if self.path == "/api/status":
            if PUBLIC_MODE:
                try:
                    identity, new_cookie = self._get_identity(create=True)
                    status = CONTROLLERS.snapshot(identity["visitor_id"])
                except RuntimeError as exc:
                    self._send_json(503, {"error": str(exc)})
                    return
                self._send_json(
                    200,
                    status,
                    session_cookie=new_cookie,
                )
            else:
                self._send_json(200, CONTROLLER.snapshot())
            return
        if self.path == "/":
            payload = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        self._send_json(404, {"error": "Not found."})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 8192:
                raise ValueError("Request is too large.")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Invalid request body.")
            if self.path == "/api/start":
                if PUBLIC_MODE:
                    api_key = payload.get("api_key")
                    if not isinstance(api_key, str) or not api_key.strip():
                        raise ValueError("Paste your Groq API key to start.")
                    identity, _ = self._get_identity(create=True)
                    controller, browser_context_id = CONTROLLERS.start(
                        identity["visitor_id"],
                        str(payload.get("url", "")).strip(),
                        bool(payload.get("auto_start", False)),
                        api_key.strip(),
                        identity["browser_context_id"],
                    )
                    session_cookie = encode_session_cookie(
                        identity["visitor_id"],
                        browser_context_id,
                    )
                    self._send_json(
                        202,
                        {"ok": True, "state": controller.snapshot()["state"]},
                        session_cookie=session_cookie,
                    )
                else:
                    CONTROLLER.start(
                        str(payload.get("url", "")).strip(),
                        bool(payload.get("auto_start", False)),
                    )
                    self._send_json(202, {"ok": True})
            elif self.path == "/api/config":
                if PUBLIC_MODE:
                    self._send_json(
                        403,
                        {"error": "API key configuration is disabled in public mode."},
                    )
                    return
                api_key = payload.get("api_key")
                if not isinstance(api_key, str):
                    raise ValueError("Enter a valid Groq API key.")
                try:
                    save_api_key(api_key)
                except OSError as exc:
                    self._send_json(
                        500,
                        {"error": f"Could not save the key locally: {exc}"},
                    )
                    return
                self._send_json(200, {"ok": True})
            elif self.path == "/api/stop":
                controller = self._get_request_controller()
                controller.stop()
                self._send_json(202, {"ok": True})
            elif self.path == "/api/close":
                controller = self._get_request_controller()
                controller.close_browser()
                self._send_json(202, {"ok": True})
            else:
                self._send_json(404, {"error": "Not found."})
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._send_json(502, {"error": str(exc)})

    def _get_request_controller(self):
        if not PUBLIC_MODE:
            return CONTROLLER
        identity, _ = self._get_identity()
        if identity is None:
            raise ValueError("Session expired. Reload the page and try again.")
        return CONTROLLERS.get(identity["visitor_id"])

    def log_message(self, format_string, *args):
        return


def main():
    if PUBLIC_MODE:
        if len(os.getenv("APP_SECRET", "")) < 32:
            raise RuntimeError("Set APP_SECRET to a random value of at least 32 characters.")
        if not os.getenv("BROWSERBASE_API_KEY", "").strip():
            raise RuntimeError("Set BROWSERBASE_API_KEY in the hosted service environment.")
    load_env_file()
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


if __name__ == "__main__":
    main()
