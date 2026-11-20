import getpass
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from groq import Groq
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


def load_env_file(path: Path | None = None) -> None:
    env_path = path or Path(__file__).with_name(".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if separator and key.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file()
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_MODEL = os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL).strip() or DEFAULT_GROQ_MODEL
USER_DATA_DIR = str(Path(__file__).with_name("browser_profile"))

START_TRIGGERS = [
    "start quiz",
    "start test",
    "start assignment",
    "begin quiz",
    "begin test",
    "start now",
    "resume quiz",
    "resume test",
    "start",
    "begin",
]

NEXT_TRIGGERS = [
    "save & next",
    "save and next",
    "next question",
    "next",
    "continue",
]

FINAL_STOP_TRIGGERS = {
    "submit",
    "submit quiz",
    "submit test",
    "submit assignment",
    "finish",
    "finish quiz",
    "finish test",
    "end test",
}

SKIP_LABELS = {
    "next",
    "next question",
    "save & next",
    "save and next",
    "continue",
    "submit",
    "submit quiz",
    "submit test",
    "submit assignment",
    "finish",
    "end test",
    "previous",
    "prev",
    "back",
    "clear",
    "clear response",
    "answered",
    "not answered",
    "unanswered",
    "marked",
    "marked for review",
    "mark for review",
    "review",
    "flagged",
    "start",
    "start quiz",
    "start test",
    "start assignment",
    "begin quiz",
    "begin test",
    "☾",
    "☀",
    "moon",
    "sun",
    "toggle theme",
    "dark mode",
    "light mode",
}


def get_api_key(allow_prompt: bool = True) -> str:
    load_env_file()
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        if not allow_prompt:
            raise ValueError("GROQ_API_KEY is missing. Add it to the .env file.")
        key = getpass.getpass("Enter your Groq API key (input hidden): ").strip()
    return key


def get_open_page(context):
    pages = [pg for pg in context.pages if not pg.is_closed()]
    return pages[-1] if pages else None


def get_active_page(context):
    return get_open_page(context) or context.new_page()


@contextmanager
def launch_browser_context():
    public_mode = os.getenv("PUBLIC_MODE", "").strip().lower() in {"1", "true", "yes"}
    headless = os.getenv("BROWSER_HEADLESS", "").strip().lower() in {"1", "true", "yes"}
    profile_dir = USER_DATA_DIR
    temporary_profile = None
    if public_mode:
        temporary_profile = tempfile.TemporaryDirectory(prefix="quiz-browser-")
        profile_dir = temporary_profile.name
        headless = True

    for attempt in range(2):
        manager = sync_playwright()
        playwright = None
        try:
            playwright = manager.start()
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
                timeout=30000,
            )
        except PlaywrightError as exc:
            if playwright is not None:
                try:
                    playwright.stop()
                except PlaywrightError:
                    pass
            if attempt == 0 and "connection closed while reading from the driver" in str(exc).lower():
                time.sleep(0.5)
                continue
            raise RuntimeError(
                "Could not start Chromium. Close any other automation window using "
                "browser_profile, then restart the app. If this keeps happening, run "
                "'python -m playwright install chromium'. "
                f"Playwright detail: {exc}"
            ) from exc

        try:
            yield context
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass
            playwright.stop()
        if temporary_profile is not None:
            temporary_profile.cleanup()
        return


def get_search_scopes(page):
    scopes = [page]
    for frame in page.frames:
        if frame != page.main_frame:
            scopes.append(frame)
    return scopes


def is_navigation_option(text: str) -> bool:
    normalized = " ".join(text.lower().split()).strip(" .:#")
    if normalized in SKIP_LABELS:
        return True
    numeric_label = normalized.replace("question", "", 1).strip(" .:#")
    return numeric_label.isdigit()


def is_loading_text(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return (
        "loading question" in normalized
        or normalized in {"loading", "loading...", "please wait", "please wait..."}
    )


def extract_progress_info(page_text: str) -> tuple[int | None, int | None]:
    match = re.search(
        r"\bquestion\s+(\d+)\s+(?:of|/)\s+(\d+)\b",
        page_text,
        flags=re.IGNORECASE,
    )
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def extract_question_text(scope) -> str:
    selectors = (
        "[data-testid*='question']",
        "[class*='question-container']",
        "[class*='question-content']",
        "[id*='question']",
    )
    candidates = []
    for selector in selectors:
        try:
            matches = scope.locator(selector)
            for index in range(min(matches.count(), 20)):
                element = matches.nth(index)
                if not element.is_visible():
                    continue
                text = " ".join((element.inner_text(timeout=500) or "").split()).strip()
                if 30 <= len(text) <= 5000 and not is_loading_text(text):
                    candidates.append(text)
        except PlaywrightError:
            continue
    if candidates:
        return min(candidates, key=len)
    try:
        return (scope.locator("body").inner_text(timeout=3000) or "").strip()
    except PlaywrightError:
        return ""


def print_page_diagnostics(page):
    print(f"[Debug] Active URL: {page.url}")
    print(f"[Debug] Frames: {[frame.url for frame in page.frames]}")
    candidate_selector = (
        "label, button, [role='button'], [role='radio'], [role='checkbox'], "
        "[role='option'], [aria-checked='true'], [aria-checked='false'], "
        ".option, .choice, .answer, .quiz-option, li"
    )
    for index, scope in enumerate(get_search_scopes(page)):
        try:
            text = (scope.locator("body").inner_text(timeout=2000) or "").strip()
            print(f"[Debug] Scope {index}: text length={len(text)}, preview={text[:500]!r}")
        except Exception as e:
            print(f"[Debug] Scope {index}: could not read body text ({e})")
        try:
            print(
                f"[Debug] Scope {index}: candidate controls="
                f"{scope.locator(candidate_selector).count()}"
            )
        except Exception as e:
            print(f"[Debug] Scope {index}: could not count controls ({e})")


def looks_like_login_page(page) -> bool:
    url = (page.url or "").lower()
    if any(k in url for k in ["login", "signin", "sign-in", "auth", "oauth", "sso"]):
        return True
    for scope in get_search_scopes(page):
        try:
            if scope.locator("input[type='password']").count() > 0:
                return True
        except Exception:
            pass
    return False


def wait_until_loaded(page, max_wait: float = 12.0, stop_event=None) -> bool:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            return False
        for scope in get_search_scopes(page):
            try:
                txt = (scope.locator("body").inner_text(timeout=1500) or "").strip()
                lower = txt.lower()
                if len(txt) > 30 and lower not in {"loading", "loading...", "please wait..."}:
                    return True
            except Exception:
                pass
        time.sleep(0.5)
    return False


def try_click_start_button(page) -> bool:
    for scope in get_search_scopes(page):
        for trigger in START_TRIGGERS:
            try:
                btn = scope.get_by_role("button", name=trigger, exact=False)
                if btn.count() > 0:
                    for i in range(btn.count()):
                        el = btn.nth(i)
                        if el.is_visible() and el.is_enabled():
                            print(f"Clicking start button: '{el.inner_text().strip()}'")
                            el.click(timeout=4000, force=True)
                            return True
            except Exception:
                pass

            try:
                loc = scope.get_by_text(trigger, exact=True)
                if loc.count() > 0 and loc.first.is_visible():
                    print(f"Clicking start text control: '{trigger}'")
                    loc.first.click(timeout=4000, force=True)
                    return True
            except Exception:
                pass
    return False


def collect_page_state(page) -> tuple[str, list[str], list[dict]]:
    texts = []
    options = []
    text_fields = []
    seen_opts = set()

    for scope_index, scope in enumerate(get_search_scopes(page)):
        scope_text = extract_question_text(scope)
        if scope_text:
            texts.append(scope_text)

        try:
            controls = scope.locator(
                "label, button, [role='button'], [role='radio'], [role='checkbox'], "
                "[role='option'], [aria-checked='true'], [aria-checked='false'], "
                ".option, .choice, .answer, .quiz-option, li"
            )
            for i in range(min(controls.count(), 500)):
                el = controls.nth(i)
                if not el.is_visible():
                    continue
                raw = el.inner_text(timeout=500) or el.get_attribute("aria-label") or ""
                cleaned = " ".join(raw.split()).strip()
                if (
                    not cleaned
                    or len(cleaned) > 350
                    or is_navigation_option(cleaned)
                    or cleaned in seen_opts
                ):
                    continue
                seen_opts.add(cleaned)
                options.append(cleaned)
        except Exception as e:
            print(f"Could not inspect answer choices: {e}")

        try:
            inputs = scope.locator("input[type='radio'], input[type='checkbox']")
            for i in range(inputs.count()):
                el = inputs.nth(i)
                if not el.is_visible():
                    continue
                label_text = el.get_attribute("aria-label") or ""
                if not label_text:
                    label_text = el.evaluate(
                        "(el) => el.labels && el.labels.length "
                        "? el.labels[0].innerText : (el.closest('label')?.innerText || '')"
                    ) or ""
                cleaned = " ".join(str(label_text).split()).strip()
                if cleaned and not is_navigation_option(cleaned) and cleaned not in seen_opts:
                    seen_opts.add(cleaned)
                    options.append(cleaned)
        except Exception as e:
            print(f"Could not inspect radio/checkbox labels: {e}")

        try:
            fields = scope.locator(
                "textarea, input[type='text'], input[type='number'], input:not([type]), "
                "[contenteditable='true'], [role='textbox']"
            )
            for field_index in range(fields.count()):
                el = fields.nth(field_index)
                if not el.is_visible() or not el.is_enabled():
                    continue
                label = (
                    el.get_attribute("aria-label")
                    or el.get_attribute("placeholder")
                    or el.get_attribute("name")
                    or ""
                )
                if not label:
                    label = el.evaluate(
                        "(el) => el.labels && el.labels.length "
                        "? el.labels[0].innerText : ''"
                    ) or ""
                label = " ".join(str(label).split()).strip()
                if any(term in label.lower() for term in ("search", "password", "username", "email")):
                    continue
                try:
                    value = el.input_value(timeout=500)
                except Exception:
                    value = el.inner_text(timeout=500) or ""
                text_fields.append({
                    "scope_index": scope_index,
                    "field_index": field_index,
                    "label": label or f"Text field {len(text_fields) + 1}",
                    "value": " ".join(str(value).split()).strip(),
                })
        except Exception as e:
            print(f"Could not inspect text-answer fields: {e}")

    return "\n---\n".join(texts), options, text_fields


def ask_groq(
    client: Groq,
    model_name: str,
    page_text: str,
    options: list[str],
    text_fields: list[dict],
) -> dict:
    prompt = f"""
You are an expert quiz-answering automation assistant.
Read the visible question text and answer using OPTIONS or TEXT_FIELDS.

Rules:
- "exact_option" MUST match one string in OPTIONS character-for-character (or "" if no options apply).
- For text-entry questions, return "text_answers" as an array with one answer per TEXT_FIELDS item, in the same order.
- Do not include an answer for a field that is not part of the question.
- "next_button" is ONLY the button text that advances to the next question (e.g., "Next", "Save & Next", "Continue").

OPTIONS:
{json.dumps(options, indent=2)}

TEXT_FIELDS:
{json.dumps([{"label": field["label"], "current_value": field["value"]} for field in text_fields], indent=2)}

PAGE_TEXT:
{page_text[:4000]}

Respond ONLY with valid JSON:
{{
  "exact_option": "",
  "text_answers": [],
  "next_button": "Next"
}}
"""
    res = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
        timeout=25.0,
    )
    data = json.loads(res.choices[0].message.content or "")
    if not isinstance(data, dict):
        raise ValueError("Model did not return a JSON object.")
    return data


def click_exact_option(page, exact_option: str) -> bool:
    if not exact_option:
        return False

    norm_target = " ".join(exact_option.split()).lower()

    for scope in get_search_scopes(page):
        try:
            inputs = scope.locator("input[type='radio'], input[type='checkbox']")
            for i in range(inputs.count()):
                el = inputs.nth(i)
                if not el.is_visible() or not el.is_enabled():
                    continue
                label_text = el.get_attribute("aria-label") or ""
                if not label_text:
                    label_text = el.evaluate(
                        "(el) => el.labels && el.labels.length "
                        "? el.labels[0].innerText : (el.closest('label')?.innerText || '')"
                    ) or ""
                if " ".join(str(label_text).split()).lower() == norm_target:
                    el.check(timeout=4000, force=True)
                    return True
        except Exception as e:
            print(f"Could not select matching answer control: {e}")

        try:
            loc = scope.get_by_text(exact_option, exact=True)
            for i in range(loc.count()):
                el = loc.nth(i)
                if el.is_visible():
                    el.scroll_into_view_if_needed(timeout=2000)
                    el.click(timeout=4000, force=True)
                    return True
        except Exception:
            pass

        try:
            cands = scope.locator(
                "label, button, [role='button'], [role='radio'], [role='checkbox'], "
                "[role='option'], [aria-checked], [tabindex='0'], [onclick], "
                "div, span, p, li"
            )
            limit = min(cands.count(), 350)
            for i in range(limit):
                el = cands.nth(i)
                if not el.is_visible():
                    continue
                txt = " ".join(
                    (el.inner_text(timeout=500) or el.get_attribute("aria-label") or "")
                    .split()
                ).strip().lower()
                if txt == norm_target:
                    el.scroll_into_view_if_needed(timeout=2000)
                    el.click(timeout=4000, force=True)
                    return True
        except Exception:
            pass

    return False


def verify_option_selected(page, exact_option: str, timeout_s: float = 0.45) -> bool | None:
    target = " ".join(exact_option.split()).lower()
    deadline = time.time() + timeout_s
    observed_unselected = False

    while time.time() < deadline:
        for scope in get_search_scopes(page):
            try:
                inputs = scope.locator("input[type='radio'], input[type='checkbox']")
                for index in range(inputs.count()):
                    control = inputs.nth(index)
                    label = control.get_attribute("aria-label") or ""
                    if not label:
                        label = control.evaluate(
                            "(el) => el.labels && el.labels.length "
                            "? el.labels[0].innerText : (el.closest('label')?.innerText || '')"
                        ) or ""
                    if " ".join(str(label).split()).lower() == target:
                        if control.is_checked():
                            return True
                        observed_unselected = True
            except PlaywrightError:
                continue

            try:
                matches = scope.get_by_text(exact_option, exact=True)
                for index in range(matches.count()):
                    element = matches.nth(index)
                    if not element.is_visible():
                        continue
                    selected = element.evaluate(
                        """el => {
                          let node = el;
                          for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
                            if (node.matches('input[type="radio"], input[type="checkbox"]'))
                              return node.checked;
                            for (const attr of ['aria-checked', 'aria-selected', 'aria-pressed', 'data-selected', 'data-state']) {
                              const value = node.getAttribute(attr);
                              if (value === 'true' || value === 'checked' || value === 'selected') return true;
                              if (value === 'false' || value === 'unchecked') return false;
                            }
                            if (node.classList && [...node.classList].some(name => /(^|[-_])(selected|checked)([-_]|$)/i.test(name)))
                              return true;
                          }
                          return null;
                        }"""
                    )
                    if selected is True:
                        return True
                    if selected is False:
                        observed_unselected = True
            except PlaywrightError:
                continue
        time.sleep(0.1)

    return False if observed_unselected else None


def fill_text_fields(page, text_fields: list[dict], answers: list[str]) -> bool:
    if not text_fields or len(answers) != len(text_fields):
        return False
    if any(not isinstance(answer, str) or not answer.strip() for answer in answers):
        return False

    scopes = get_search_scopes(page)
    selector = (
        "textarea, input[type='text'], input[type='number'], input:not([type]), "
        "[contenteditable='true'], [role='textbox']"
    )
    try:
        for field, answer in zip(text_fields, answers):
            scope = scopes[field["scope_index"]]
            el = scope.locator(selector).nth(field["field_index"])
            if not el.is_visible() or not el.is_enabled():
                return False
            el.fill(answer.strip(), timeout=4000)
            try:
                actual = el.input_value(timeout=500)
            except Exception:
                actual = el.inner_text(timeout=500) or ""
            if " ".join(actual.split()) != " ".join(answer.split()):
                return False
        return True
    except Exception as e:
        print(f"Could not fill text-answer field: {e}")
        return False


def click_next_question(page, ai_next_label: str) -> bool:
    ordered_labels = []
    if ai_next_label and ai_next_label.strip().lower() not in FINAL_STOP_TRIGGERS:
        ordered_labels.append(ai_next_label.strip())

    for fallback in NEXT_TRIGGERS:
        if fallback not in [x.lower() for x in ordered_labels]:
            ordered_labels.append(fallback)

    for scope in get_search_scopes(page):
        for label in ordered_labels:
            try:
                btn = scope.get_by_role("button", name=label, exact=False)
            except PlaywrightError:
                btn = None
            if btn is not None:
                for i in range(btn.count()):
                    el = btn.nth(i)
                    try:
                        if not el.is_visible() or not el.is_enabled():
                            continue
                        el_txt = (el.inner_text(timeout=500) or "").strip().lower()
                        if el_txt in FINAL_STOP_TRIGGERS:
                            return False
                        el.scroll_into_view_if_needed(timeout=2000)
                        el.click(timeout=4000, force=True)
                        return True
                    except PlaywrightError:
                        continue

            try:
                txt_el = scope.get_by_text(label, exact=True)
            except PlaywrightError:
                txt_el = None
            if txt_el is not None:
                for i in range(txt_el.count()):
                    el = txt_el.nth(i)
                    try:
                        if not el.is_visible() or not el.is_enabled():
                            continue
                        el.scroll_into_view_if_needed(timeout=2000)
                        el.click(timeout=4000, force=True)
                        return True
                    except PlaywrightError:
                        continue

    return False


def state_signature(state: tuple[str, list[str], list[dict]]) -> tuple:
    page_text, options, fields = state
    return (
        " ".join(page_text.split()),
        tuple(options),
        tuple((field["label"], field["value"]) for field in fields),
    )


def wait_for_question_state(
    page,
    timeout_s: float = 12.0,
    previous_state: tuple[str, list[str], list[dict]] | None = None,
    stop_event=None,
) -> tuple[str, list[str], list[dict]] | None:
    deadline = time.time() + timeout_s
    previous_signature = state_signature(previous_state) if previous_state else None
    stable_signature = None
    stable_since = 0.0

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            return None
        state = collect_page_state(page)
        page_text, options, fields = state
        signature = state_signature(state)
        ready = (
            bool(page_text.strip())
            and not is_loading_text(page_text)
            and (len(options) >= 2 or bool(fields))
        )
        changed = previous_signature is None or signature != previous_signature

        if ready and changed:
            if signature != stable_signature:
                stable_signature = signature
                stable_since = time.time()
            elif time.time() - stable_since >= 0.6:
                return state
        else:
            stable_signature = None
        time.sleep(0.2)
    return None


def wait_for_question_change(
    page,
    previous_state: tuple[str, list[str], list[dict]],
    timeout_s: float = 20.0,
    stop_event=None,
) -> bool:
    return wait_for_question_state(
        page,
        timeout_s=timeout_s,
        previous_state=previous_state,
        stop_event=stop_event,
    ) is not None


def run_automation(
    target_url: str,
    on_event: Callable[[dict], None] | None = None,
    stop_event: threading.Event | None = None,
    close_event: threading.Event | None = None,
    auto_start: bool = False,
    keep_browser_open: bool = False,
    api_key: str | None = None,
) -> str:
    stop_event = stop_event or threading.Event()

    def report(status: str, message: str, question=None, total=None) -> None:
        event = {
            "status": status,
            "message": message,
            "question": question,
            "total": total,
        }
        if on_event:
            on_event(event)
        else:
            print(message)

    target_url = target_url.strip()
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url
    api_key = (api_key or "").strip() or get_api_key(allow_prompt=False)
    if not target_url.startswith(("http://", "https://")):
        raise ValueError("Enter a valid http:// or https:// URL.")

    client = Groq(api_key=api_key)
    model_name = GROQ_MODEL
    terminal_status = "running"

    try:
        report("starting", "Starting the Playwright browser driver.")
        with launch_browser_context() as context:
            page = get_active_page(context)
            report("starting", f"Opening {target_url}")

            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            except PlaywrightError as exc:
                report("warning", f"Navigation note: {exc}")

            wait_until_loaded(page, stop_event=stop_event)
            page = get_active_page(context)

            if looks_like_login_page(page):
                report("login_required", "Complete sign-in in the browser; waiting here.")
                while not stop_event.is_set() and looks_like_login_page(page):
                    stop_event.wait(0.25)
                    page = get_open_page(context)
                    if page is None:
                        report("stopped", "Browser window was closed during sign-in.")
                        terminal_status = "stopped"
                        break
                if stop_event.is_set():
                    terminal_status = "stopped"
                    return terminal_status
                if terminal_status != "running":
                    return terminal_status
                if target_url not in page.url:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

            if auto_start:
                if try_click_start_button(page):
                    report("starting", "Start button clicked; waiting for the first question.")
                else:
                    report("waiting", "Start the assignment in the browser; waiting for its questions.")
            else:
                report("waiting", "If needed, click Start assignment in the browser; waiting for its questions.")
            consecutive_answer_failures = 0
            consecutive_unready = 0
            question_number = 1

            while question_number <= 250 and not stop_event.is_set():
                page = get_open_page(context)
                if page is None:
                    report("stopped", "Browser window was closed.")
                    terminal_status = "stopped"
                    break

                state = wait_for_question_state(
                    page,
                    timeout_s=8.0,
                    stop_event=stop_event,
                )
                if stop_event.is_set():
                    terminal_status = "stopped"
                    break
                if state is None:
                    consecutive_unready += 1
                    report(
                        "waiting",
                        f"Still waiting for answer controls ({consecutive_unready}/8).",
                        question_number,
                    )
                    if consecutive_unready == 1:
                        print_page_diagnostics(page)
                    if consecutive_unready >= 8:
                        report("needs_attention", "No stable answer controls found; stopped safely.")
                        terminal_status = "needs_attention"
                        break
                    continue

                page_text, options, text_fields = state
                consecutive_unready = 0
                current_question, total_questions = extract_progress_info(page_text)
                question_number = current_question or question_number
                report(
                    "thinking",
                    f"Reading Question {question_number}"
                    + (f" of {total_questions}" if total_questions else ""),
                    question_number,
                    total_questions,
                )

                decision = None
                for request_attempt in range(3):
                    if stop_event.is_set():
                        break
                    try:
                        decision = ask_groq(client, model_name, page_text, options, text_fields)
                        break
                    except Exception as exc:
                        if request_attempt == 2:
                            report("failed", f"Groq request failed after 3 tries: {exc}", question_number)
                            terminal_status = "failed"
                        else:
                            report("retrying", f"Temporary model error; retrying: {exc}", question_number)
                            stop_event.wait(0.4 * (request_attempt + 1))

                if stop_event.is_set():
                    terminal_status = "stopped"
                    break
                if decision is None:
                    break

                raw_option = decision.get("exact_option", "")
                chosen_option = raw_option.strip() if isinstance(raw_option, str) else ""
                if chosen_option not in options:
                    chosen_option = ""
                text_answers = decision.get("text_answers", [])
                next_btn = decision.get("next_button", "")
                next_btn = next_btn.strip() if isinstance(next_btn, str) else ""

                answered = False
                verification = None
                if chosen_option:
                    answered = click_exact_option(page, chosen_option)
                    if answered:
                        verification = verify_option_selected(page, chosen_option)
                        if verification is False:
                            answered = False
                elif text_fields and isinstance(text_answers, list):
                    answered = fill_text_fields(page, text_fields, text_answers)

                if not answered:
                    consecutive_answer_failures += 1
                    report(
                        "retrying",
                        f"Could not confirm an answer ({consecutive_answer_failures}/3); staying on this question.",
                        question_number,
                        total_questions,
                    )
                    if consecutive_answer_failures >= 3:
                        report("needs_attention", "Stopped on this question; no answer was confirmed.")
                        terminal_status = "needs_attention"
                        break
                    stop_event.wait(0.25)
                    continue

                consecutive_answer_failures = 0
                if verification is None and chosen_option:
                    report("answer_set", "Choice clicked; this page exposes no selected-state marker.", question_number)
                else:
                    report("answer_set", "Answer entered and verified.", question_number, total_questions)

                if next_btn.lower() in FINAL_STOP_TRIGGERS:
                    report("awaiting_submission", "Reached the final action. Stopped before submission.", question_number, total_questions)
                    terminal_status = "awaiting_submission"
                    break

                answered_state = collect_page_state(page)
                report("advancing", "Moving to the next question.", question_number, total_questions)
                moved = False
                for attempt in range(3):
                    if stop_event.is_set():
                        break
                    moved = click_next_question(page, next_btn)
                    if moved:
                        break
                    if wait_for_question_change(
                        page,
                        answered_state,
                        timeout_s=1.5,
                        stop_event=stop_event,
                    ):
                        moved = True
                        break
                    stop_event.wait(0.25)

                if stop_event.is_set():
                    terminal_status = "stopped"
                    break
                if not moved:
                    report(
                        "needs_attention",
                        f"Could not activate Next at Question {question_number}; stopped without calling it the end.",
                        question_number,
                        total_questions,
                    )
                    terminal_status = "needs_attention"
                    break

                next_state = wait_for_question_state(
                    page,
                    timeout_s=15.0,
                    previous_state=answered_state,
                    stop_event=stop_event,
                )
                if stop_event.is_set():
                    terminal_status = "stopped"
                    break
                if next_state is None:
                    report("needs_attention", "Next was clicked, but the next question did not become ready.")
                    terminal_status = "needs_attention"
                    break
                question_number += 1

            if stop_event.is_set() and terminal_status == "running":
                terminal_status = "stopped"
                report("stopped", "Stopped by user.")
            elif terminal_status == "running":
                terminal_status = "completed"
                report("completed", "Reached the automation question limit.")
            if keep_browser_open and close_event:
                if terminal_status == "awaiting_submission":
                    message = (
                        "Ready for review. Submit manually in Chrome; the browser "
                        "stays open until you close it."
                    )
                else:
                    message = "Browser remains open for review. Close it when finished."
                report("awaiting_close", message)
                while not close_event.wait(0.25):
                    if stop_event.is_set():
                        break
            return terminal_status
    except Exception as exc:
        terminal_status = "failed"
        report("failed", f"{type(exc).__name__}: {exc}")
        raise


def main():
    target_url = input("Paste the test/quiz URL and press ENTER to start: ").strip()
    api_key = get_api_key()
    close_event = threading.Event()

    def handle_event(event):
        print(event["message"])
        if event["status"] == "awaiting_close":
            input(
                "\nReview the assignment in Chrome and submit it yourself if ready. "
                "Press ENTER here when you want to close the browser..."
            )
            close_event.set()

    run_automation(
        target_url,
        on_event=handle_event,
        stop_event=threading.Event(),
        close_event=close_event,
        auto_start=True,
        keep_browser_open=True,
        api_key=api_key,
    )


if __name__ == "__main__":
    main()