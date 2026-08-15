"""
Playwright script: open the CGI Frankfurt and Berlin embassy visa
appointment sites (same underlying platform, different jurisdictions),
fill the booking form (state, appointment type, service), read the next
available appointment date from the datepicker, and Telegram/email-alert
if a date is open on or before CUTOFF_DATE.

Uses Playwright (not Selenium) because it bundles its own tested
Chromium build — no chromedriver-vs-browser version matching, no snap
package interference, much more reliable on headless Linux servers.
First-time setup after `uv sync` needs the browser binary once:
    uv run playwright install chromium --with-deps

Runs forever by default, checking every 5 minutes (POLL_SECONDS env var
to change). Meant to run as a long-lived process on a server:

    uv run main.py

Single check and exit (e.g. for cron/systemd timer instead):

    RUN_ONCE=1 uv run main.py

Show the browser window instead of headless (debugging):

    SHOW_BROWSER=1 uv run main.py

If a site fails to load / times out / behaves unexpectedly, that site is
NOT retried within the same cycle by default (RETRY_ATTEMPTS=1) — it's
logged and skipped, and the next POLL_SECONDS tick (default 5 min) picks
it back up naturally. Set RETRY_ATTEMPTS higher to retry in-cycle first,
waiting RETRY_DELAY_SECONDS (default 60s) between tries.

Sends a Telegram heartbeat every HEARTBEAT_SECONDS (default 3600s / 1hr)
so you know it's still alive and checking.
"""

import json
import os
import smtplib
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

CUTOFF_DATE = date(2026, 10, 21)

# Don't hit the sites during this local window — likely the platform's own
# maintenance window, so it's especially flaky/likely to be down then.
CET_ZONE = ZoneInfo("Europe/Berlin")  # auto CET/CEST per DST
BLACKOUT_START_HOUR = 1  # 1AM CEST
BLACKOUT_END_HOUR = 4  # 4AM CEST

GMAIL_USERNAME = os.environ.get("GMAIL_USERNAME")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")


def _parse_chat_ids(raw: str | None) -> list[str]:
    """TELEGRAM_*_CHAT_ID / _USERS env vars may be a single id or a
    JSON-array-looking string (e.g. '["123", "456"]'). Normalize to a
    list of strings either way."""
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            return [str(x) for x in json.loads(raw)]
        except (json.JSONDecodeError, TypeError):
            print(f"Could not parse chat id list {raw!r} — ignoring.")
            return []
    return [raw]


@dataclass
class Site:
    """One appointment site to watch. Both known sites run the same
    underlying booking platform, so only these values differ."""
    name: str
    url: str
    state_value: str  # <select id="dropdown"> option value to pick
    telegram_bot: str | None
    telegram_chat_ids: list[str] = field(default_factory=list)


SITES = [
    Site(
        name="CGI Frankfurt",
        url="https://appointment.cgifrankfurt.gov.in/",
        state_value="North Rhine-Westphalia",  # matches the site's spelling exactly
        telegram_bot=os.environ.get("TELEGRAM_BOT"),
        telegram_chat_ids=_parse_chat_ids(os.environ.get("TELEGRAM_CHAT_ID")),
    ),
    Site(
        name="Embassy Berlin",
        url="https://appointment.indianembassyberlin.gov.in/",
        # Site's own <option> has a mislabeled value attr (value="Hesse"
        # for the option whose visible text is "Berlin" — copy-paste bug
        # on their end). We select by visible label, not value, so this
        # picks "Berlin" regardless of what their value attr says.
        state_value="Berlin",
        telegram_bot=os.environ.get("TELEGRAM_BERLIN_BOT"),
        telegram_chat_ids=_parse_chat_ids(os.environ.get("TELEGRAM_BERLIN_BOT_USERS")),
    ),
]


def send_email(subject: str, body: str) -> None:
    if not GMAIL_USERNAME or not GMAIL_APP_PASSWORD or not RECEIVER_EMAIL:
        print("GMAIL_USERNAME / GMAIL_APP_PASSWORD / RECEIVER_EMAIL not set in .env — skipping email.")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USERNAME
    msg["To"] = RECEIVER_EMAIL
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(GMAIL_USERNAME, GMAIL_APP_PASSWORD)
            smtp.send_message(msg)
    except Exception as e:
        print(f"Email send failed: {e}")


def send_telegram_message(site: Site, text: str) -> None:
    if not site.telegram_bot or not site.telegram_chat_ids:
        print(f"[{site.name}] Telegram bot/chat id(s) not set in .env — skipping alert.")
        return
    for chat_id in site.telegram_chat_ids:
        resp = requests.post(
            f"https://api.telegram.org/bot{site.telegram_bot}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        if not resp.ok:
            print(f"[{site.name}] Telegram send to {chat_id} failed: {resp.status_code} {resp.text}")


PAGE_TIMEOUT_MS = 20_000


def click_checkbox_and_proceed(page):
    checkbox = page.locator("input[type='checkbox']").first
    checkbox.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)
    if not checkbox.is_checked():
        checkbox.click()  # real click fires the page's change listener

    proceed_btn = page.locator(
        "button:has-text('PROCEED'), button:has-text('Proceed'), "
        "input[type='submit'][value*='PROCEED' i]"
    ).first
    proceed_btn.wait_for(state="attached", timeout=PAGE_TIMEOUT_MS)
    # Button may start disabled until validation JS re-enables it.
    page.wait_for_function(
        "(el) => el.disabled === false || el.getAttribute('disabled') === null",
        arg=proceed_btn.element_handle(),
        timeout=PAGE_TIMEOUT_MS,
    )
    proceed_btn.click()


def fill_booking_form(page, site: Site):
    """Pages 1-3: agreement, state, appointment type/category/service."""
    page.goto(site.url, timeout=PAGE_TIMEOUT_MS)

    # --- Page 1: agree checkbox + proceed ---
    click_checkbox_and_proceed(page)
    page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)

    # --- Page 2: select state, checkbox, proceed ---
    state_select = page.locator("#dropdown")
    state_select.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)
    # Select by visible label, not value — Berlin's own <option> has a
    # mismatched value attribute (see Site definition above).
    state_select.select_option(label=site.state_value)
    # dropdown has no 'selected' HTML attr, so .select_option()'s implicit
    # change event may not be enough for the page's own listener; fire it
    # explicitly to be safe.
    state_select.dispatch_event("change")
    click_checkbox_and_proceed(page)
    page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)

    # --- Page 3: appointment type, service category, service ---

    # 1. Radio "Individual" (name=apt_group, value=1) — present on some
    #    sites (e.g. CGI Frankfurt), commented out / absent on others
    #    (e.g. Embassy Berlin, which skips straight to category). Only
    #    interact with it if it's actually rendered.
    individual_radio = page.locator("input[name='apt_group'][value='1']")
    try:
        individual_radio.wait_for(state="visible", timeout=3_000)
        individual_radio.click()
    except PlaywrightTimeoutError:
        pass  # this site doesn't have the step — nothing to select

    # 2. Service Category -> OCI Services (value=1)
    category_select = page.locator("#category")
    category_select.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)
    category_select.select_option(value="1")
    category_select.dispatch_event("change")
    category_select.dispatch_event("input")

    # 3. Wait for the AJAX-populated "Select Service" dropdown to appear
    #    (the #service element itself doesn't exist until the AJAX
    #    response injects it), then wait for it to have real options,
    #    then choose Fresh OCI.
    service_select = page.locator("#service")
    service_select.wait_for(state="attached", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_function(
        "(el) => el.options.length > 1",
        arg=service_select.element_handle(),
        timeout=PAGE_TIMEOUT_MS,
    )

    options = service_select.locator("option").all_text_contents()
    matched = next((o for o in options if "fresh oci" in o.strip().lower()), None)
    if not matched:
        raise RuntimeError(f"'Fresh OCI' not found in service options: {options}")
    service_select.select_option(label=matched)
    service_select.dispatch_event("change")


def find_next_available_date(page) -> date | None:
    """Open the appointment datepicker and return the first available
    (non-struck) date, or None if nothing is open in the browsable range.

    jQuery UI datepicker marks unavailable days as disabled <span> cells
    (class 'booked-dates' or 'weekends'); available days render as a
    clickable <a> inside the <td>. Site allows browsing ~90 days ahead.
    """
    date_input = page.locator("#appmnt_date")
    date_input.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)
    date_input.click()

    picker = page.locator("#ui-datepicker-div")
    picker.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)

    for _ in range(4):  # current month + a few months ahead, bounded safety
        calendar = picker.locator(".ui-datepicker-calendar")
        calendar.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)

        available_links = calendar.locator("td:not(.ui-datepicker-unselectable) a")
        if available_links.count() > 0:
            first_available = available_links.first
            day_text = first_available.inner_text().strip()
            # changeMonth/changeYear render these as <select> dropdowns;
            # read the selected option's text directly.
            month_text = picker.locator("select.ui-datepicker-month").evaluate(
                "el => el.options[el.selectedIndex].text"
            )
            year_text = picker.locator("select.ui-datepicker-year").evaluate(
                "el => el.options[el.selectedIndex].text"
            )
            fmt = "%d %B %Y" if len(month_text) > 3 else "%d %b %Y"
            parsed = datetime.strptime(f"{day_text} {month_text} {year_text}", fmt).date()
            first_available.click()
            return parsed

        # No available date this month -> go to next month
        next_btn = picker.locator(".ui-datepicker-next")
        next_btn_classes = next_btn.get_attribute("class") or ""
        if "ui-state-disabled" in next_btn_classes:
            break  # can't page further forward
        next_btn.click()
        picker.locator(".ui-datepicker-calendar").wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)

    return None


def check_once(site: Site) -> date | None:
    """Run the full flow once for one site. Returns the next available date, if any."""
    headless = not os.environ.get("SHOW_BROWSER")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            # Linux server hardening: no sandbox (needed running as root /
            # in most containers) and use /tmp instead of /dev/shm (often
            # tiny on VPS/containers and causes crashes mid-run otherwise).
            args=["--no-sandbox", "--disable-dev-shm-usage"] if headless else [],
        )
        try:
            context = browser.new_context(viewport={"width": 1920, "height": 1080})
            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            fill_booking_form(page, site)
            return find_next_available_date(page)
        finally:
            browser.close()


RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", "1"))
RETRY_DELAY_SECONDS = int(os.environ.get("RETRY_DELAY_SECONDS", "60"))


def check_with_retry(site: Site) -> date | None:
    """Run check_once(site), retrying with a delay if the site fails to
    load or behaves unexpectedly (timeouts, missing elements, connection
    errors, etc). Raises only after all attempts are exhausted."""
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return check_once(site)
        except Exception as e:
            last_error = e
            stamp = datetime.now().isoformat(timespec="seconds")
            print(f"[{stamp}] [{site.name}] Attempt {attempt}/{RETRY_ATTEMPTS} failed: {e}")
            if attempt < RETRY_ATTEMPTS:
                print(f"[{stamp}] [{site.name}] Retrying in {RETRY_DELAY_SECONDS}s...")
                time.sleep(RETRY_DELAY_SECONDS)
    raise last_error


def run_check_for_site(site: Site) -> None:
    """Single check-and-alert cycle for one site. Never raises — logs and
    swallows errors so a bad run doesn't kill the surrounding loop or the
    other sites. Retries a few times internally in case the site is
    temporarily down/flaky."""
    try:
        next_date = check_with_retry(site)
    except Exception as e:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] [{site.name}] "
              f"Check failed after {RETRY_ATTEMPTS} attempts: {e}")
        return

    stamp = datetime.now().isoformat(timespec="seconds")

    if next_date is None:
        print(f"[{stamp}] [{site.name}] No available appointment date found in the browsable range.")
        return

    print(f"[{stamp}] [{site.name}] Next available appointment date: {next_date.strftime('%d %B %Y')}")

    if next_date <= CUTOFF_DATE:
        msg = (
            f"{site.name} OCI appointment available: "
            f"{next_date.strftime('%d %B %Y')} (on/before {CUTOFF_DATE.strftime('%d %B %Y')})\n"
            f"Book now: {site.url}"
        )
        send_telegram_message(site, msg)
        print(f"[{stamp}] [{site.name}] Telegram alert sent.")
        send_email(
            subject=f"{site.name} OCI appointment available — {next_date.strftime('%d %B %Y')}",
            body=msg,
        )
        print(f"[{stamp}] [{site.name}] Email alert sent.")
    else:
        print(f"[{stamp}] [{site.name}] Earliest slot is after cutoff "
              f"({CUTOFF_DATE.strftime('%d %B %Y')}) — no alert sent.")


def run_check() -> None:
    """Runs the check-and-alert cycle for every configured site."""
    for site in SITES:
        run_check_for_site(site)


def seconds_until_blackout_ends() -> float:
    """0 if we're outside the blackout window right now; otherwise how
    many seconds until BLACKOUT_END_HOUR CEST today/tomorrow."""
    now = datetime.now(CET_ZONE)
    if not (BLACKOUT_START_HOUR <= now.hour < BLACKOUT_END_HOUR):
        return 0

    end = now.replace(hour=BLACKOUT_END_HOUR, minute=0, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return (end - now).total_seconds()


HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", str(60 * 60)))  # 1 hour


def maybe_send_heartbeat(last_heartbeat: float) -> float:
    """Send a Telegram 'still alive' ping (per site) if HEARTBEAT_SECONDS
    have elapsed since the last one. Returns the (possibly updated)
    last-sent timestamp (time.monotonic())."""
    now = time.monotonic()
    if now - last_heartbeat < HEARTBEAT_SECONDS:
        return last_heartbeat

    stamp = datetime.now(CET_ZONE).isoformat(timespec="seconds")
    for site in SITES:
        send_telegram_message(site, f"{site.name} appointment watcher is alive and checking. ({stamp})")
    print(f"[{stamp}] Heartbeat sent.")
    return now


def main() -> None:
    """Runs forever, checking every POLL_SECONDS (default 300s / 5min).
    Set RUN_ONCE=1 to run a single check and exit (useful for testing
    or if you want to drive the interval with an external scheduler
    instead).

    Skips checks during the {BLACKOUT_START_HOUR}AM-{BLACKOUT_END_HOUR}AM
    CEST blackout window (site is especially likely to be down/flaky
    then) — sleeps until the window ends instead of polling through it.

    Sends a Telegram heartbeat every HEARTBEAT_SECONDS (default 1 hour)
    so you know the watcher is still running, independent of whether an
    appointment was found.
    """
    poll_seconds = int(os.environ.get("POLL_SECONDS", "300"))

    if os.environ.get("RUN_ONCE"):
        wait = seconds_until_blackout_ends()
        if wait > 0:
            print(f"In blackout window ({BLACKOUT_START_HOUR}-{BLACKOUT_END_HOUR} CEST) — skipping this run.")
            return
        run_check()
        return

    print(f"Starting watcher loop: checking every {poll_seconds}s, "
          f"heartbeat every {HEARTBEAT_SECONDS}s. Ctrl+C to stop.")
    last_heartbeat = 0.0  # forces an immediate heartbeat on first loop
    while True:
        last_heartbeat = maybe_send_heartbeat(last_heartbeat)

        wait = seconds_until_blackout_ends()
        if wait > 0:
            stamp = datetime.now(CET_ZONE).isoformat(timespec="seconds")
            print(f"[{stamp}] In blackout window ({BLACKOUT_START_HOUR}-{BLACKOUT_END_HOUR} CEST) — "
                  f"sleeping {int(wait)}s until it ends.")
            time.sleep(wait)
            continue
        run_check()
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
