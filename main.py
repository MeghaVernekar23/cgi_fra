"""
Selenium script: open CGI Frankfurt visa appointment site, fill the
booking form (state, appointment type, service), read the next available
appointment date from the datepicker, and Telegram-alert if a date is
open on or before CUTOFF_DATE.

Runs forever by default, checking every 5 minutes (POLL_SECONDS env var
to change). Meant to run as a long-lived process on a server:

    uv run main.py

Single check and exit (e.g. for cron/systemd timer instead):

    RUN_ONCE=1 uv run main.py

Show the browser window instead of headless (debugging):

    SHOW_BROWSER=1 uv run main.py

If the site fails to load / times out / behaves unexpectedly, each check
is retried up to RETRY_ATTEMPTS times (default 3), waiting
RETRY_DELAY_SECONDS between tries (default 60s), before giving up for
that cycle and waiting for the next POLL_SECONDS tick.

Sends a Telegram heartbeat every HEARTBEAT_SECONDS (default 3600s / 1hr)
so you know it's still alive and checking.
"""

import os
import smtplib
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

load_dotenv()

URL = "https://appointment.cgifrankfurt.gov.in/"
STATE = "North Rhine-Westphalia"  # matches the site's spelling exactly
CUTOFF_DATE = date(2026, 10, 21)

# Don't hit the site during this local window — CGI Frankfurt's own
# maintenance window, so it's especially flaky/likely to be down then.
CET_ZONE = ZoneInfo("Europe/Berlin")  # auto CET/CEST per DST
BLACKOUT_START_HOUR = 1  # 1AM CEST
BLACKOUT_END_HOUR = 4  # 4AM CEST

TELEGRAM_BOT = os.environ.get("TELEGRAM_BOT")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GMAIL_USERNAME = os.environ.get("GMAIL_USERNAME")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")


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


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT / TELEGRAM_CHAT_ID not set in .env — skipping alert.")
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=15,
    )
    if not resp.ok:
        print(f"Telegram send failed: {resp.status_code} {resp.text}")


def click_checkbox_and_proceed(driver, wait):
    checkbox = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='checkbox']"))
    )
    if not checkbox.is_selected():
        checkbox.click()  # real click fires the page's change listener

    proceed_btn = wait.until(
        EC.presence_of_element_located(
            (By.XPATH, "//button[contains(translate(., 'PROCEED', 'proceed'), 'proceed')] "
                       "| //input[@type='submit' and contains(translate(@value,'PROCEED','proceed'),'proceed')]")
        )
    )
    # Button may start disabled until validation JS re-enables it.
    wait.until(lambda d: proceed_btn.get_attribute("disabled") is None)
    proceed_btn.click()


def fill_booking_form(driver, wait):
    """Pages 1-3: agreement, state, appointment type/category/service."""
    driver.get(URL)

    # --- Page 1: agree checkbox + proceed ---
    click_checkbox_and_proceed(driver, wait)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")

    # --- Page 2: select state, checkbox, proceed ---
    state_select_el = wait.until(EC.presence_of_element_located((By.ID, "dropdown")))
    Select(state_select_el).select_by_value(STATE)
    # dropdown has no 'selected' HTML attr, so <select>.value won't
    # auto-fire; trigger the page's change listener explicitly.
    driver.execute_script(
        "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
        state_select_el,
    )
    click_checkbox_and_proceed(driver, wait)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")

    # --- Page 3: appointment type, service category, service ---

    # 1. Radio "Individual" (name=apt_group, value=1)
    individual_radio = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='apt_group'][value='1']"))
    )
    individual_radio.click()

    # 2. Service Category -> OCI Services (value=1)
    category_select_el = wait.until(EC.presence_of_element_located((By.ID, "category")))
    Select(category_select_el).select_by_value("1")
    driver.execute_script(
        "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));"
        "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
        category_select_el,
    )

    # 3. Wait for the AJAX-populated "Select Service" dropdown to appear
    #    (the #service element itself doesn't exist until the AJAX
    #    response injects it), then wait for it to have real options,
    #    then choose Fresh OCI.
    def service_dropdown_ready(d):
        els = d.find_elements(By.ID, "service")
        if not els:
            return False
        opts = els[0].find_elements(By.TAG_NAME, "option")
        return els[0] if len(opts) > 1 else False

    service_select_el = wait.until(service_dropdown_ready)
    service_select = Select(service_select_el)
    matched = False
    for option in service_select.options:
        if "fresh oci" in option.text.strip().lower():
            service_select.select_by_visible_text(option.text)
            matched = True
            break
    if not matched:
        available = [o.text for o in service_select.options]
        raise RuntimeError(f"'Fresh OCI' not found in service options: {available}")

    driver.execute_script(
        "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
        service_select_el,
    )


def find_next_available_date(driver, wait) -> date | None:
    """Open the appointment datepicker and return the first available
    (non-struck) date, or None if nothing is open in the browsable range.

    jQuery UI datepicker marks unavailable days as disabled <span> cells
    (class 'booked-dates' or 'weekends'); available days render as a
    clickable <a> inside the <td>. Site allows browsing ~90 days ahead.
    """
    date_input = wait.until(EC.element_to_be_clickable((By.ID, "appmnt_date")))
    date_input.click()

    wait.until(EC.visibility_of_element_located((By.ID, "ui-datepicker-div")))

    for _ in range(4):  # current month + a few months ahead, bounded safety
        wait.until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "#ui-datepicker-div .ui-datepicker-calendar"))
        )
        links = driver.find_elements(
            By.CSS_SELECTOR,
            "#ui-datepicker-div .ui-datepicker-calendar td:not(.ui-datepicker-unselectable) a",
        )
        if links:
            first_available = links[0]
            day_text = first_available.text.strip()
            month_name = Select(
                driver.find_element(By.CSS_SELECTOR, "#ui-datepicker-div select.ui-datepicker-month")
            ).first_selected_option.text
            year_name = Select(
                driver.find_element(By.CSS_SELECTOR, "#ui-datepicker-div select.ui-datepicker-year")
            ).first_selected_option.text
            fmt = "%d %B %Y" if len(month_name) > 3 else "%d %b %Y"
            parsed = datetime.strptime(f"{day_text} {month_name} {year_name}", fmt).date()
            return parsed

        # No available date this month -> go to next month
        next_btn = driver.find_element(By.CSS_SELECTOR, "#ui-datepicker-div .ui-datepicker-next")
        if "ui-state-disabled" in next_btn.get_attribute("class"):
            break  # can't page further forward
        stale_marker = driver.find_element(By.CSS_SELECTOR, "#ui-datepicker-div .ui-datepicker-calendar")
        next_btn.click()
        wait.until(EC.staleness_of(stale_marker))

    return None


def check_once() -> date | None:
    """Run the full flow once. Returns the next available date, if any."""
    options = Options()
    if not os.environ.get("SHOW_BROWSER"):
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 20)

    try:
        fill_booking_form(driver, wait)
        return find_next_available_date(driver, wait)
    finally:
        driver.quit()


RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", "3"))
RETRY_DELAY_SECONDS = int(os.environ.get("RETRY_DELAY_SECONDS", "60"))


def check_with_retry() -> date | None:
    """Run check_once(), retrying with a delay if the site fails to load
    or behaves unexpectedly (timeouts, missing elements, connection
    errors, etc). Raises only after all attempts are exhausted."""
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return check_once()
        except Exception as e:
            last_error = e
            stamp = datetime.now().isoformat(timespec="seconds")
            print(f"[{stamp}] Attempt {attempt}/{RETRY_ATTEMPTS} failed: {e}")
            if attempt < RETRY_ATTEMPTS:
                print(f"[{stamp}] Retrying in {RETRY_DELAY_SECONDS}s...")
                time.sleep(RETRY_DELAY_SECONDS)
    raise last_error


def run_check() -> None:
    """Single check-and-alert cycle. Never raises — logs and swallows
    errors so a bad run doesn't kill the surrounding loop. Retries a
    few times internally in case the site is temporarily down/flaky."""
    try:
        next_date = check_with_retry()
    except Exception as e:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Check failed after {RETRY_ATTEMPTS} attempts: {e}")
        return

    stamp = datetime.now().isoformat(timespec="seconds")

    if next_date is None:
        print(f"[{stamp}] No available appointment date found in the browsable range.")
        return

    print(f"[{stamp}] Next available appointment date: {next_date.strftime('%d %B %Y')}")

    if next_date <= CUTOFF_DATE:
        msg = (
            f"CGI Frankfurt OCI appointment available: "
            f"{next_date.strftime('%d %B %Y')} (on/before {CUTOFF_DATE.strftime('%d %B %Y')})\n"
            f"Book now: {URL}"
        )
        send_telegram_message(msg)
        print(f"[{stamp}] Telegram alert sent.")
        send_email(
            subject=f"CGI Frankfurt OCI appointment available — {next_date.strftime('%d %B %Y')}",
            body=msg,
        )
        print(f"[{stamp}] Email alert sent.")
    else:
        print(f"[{stamp}] Earliest slot is after cutoff ({CUTOFF_DATE.strftime('%d %B %Y')}) — no alert sent.")


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
    """Send a Telegram 'still alive' ping if HEARTBEAT_SECONDS have
    elapsed since the last one. Returns the (possibly updated) last-sent
    timestamp (time.monotonic())."""
    now = time.monotonic()
    if now - last_heartbeat < HEARTBEAT_SECONDS:
        return last_heartbeat

    stamp = datetime.now(CET_ZONE).isoformat(timespec="seconds")
    send_telegram_message(f"CGI Frankfurt appointment watcher is alive and checking. ({stamp})")
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
