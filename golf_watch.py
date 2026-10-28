#!/usr/bin/env python3
"""
Wynyard Golf Club tee time watcher (EliteLive).

READ ONLY: this script never clicks a tee time and never books anything.
It logs in, sets the number of players, then reads the availability list
for each target date and sends a Telegram message when new slots appear.

Usage:
  python golf_watch.py              normal check (run every 5 mins by cron)
  python golf_watch.py --test       send a test message and print what is free now
  python golf_watch.py --get-chat-id   print your Telegram chat ID
"""
import fcntl
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

BASE = "https://www.e-s-p.com/elitelive/"
HERE = Path(__file__).resolve().parent
# Settings come from config.json if present (own server), otherwise settings.json.
# Secrets can be supplied as environment variables (GitHub Actions secrets).
_cfg_file = HERE / "config.json" if (HERE / "config.json").exists() else HERE / "settings.json"
CONFIG = json.loads(_cfg_file.read_text())
for _key in ("elitelive_username", "elitelive_password", "telegram_bot_token", "telegram_chat_id"):
    if os.environ.get(_key.upper()):
        CONFIG[_key] = os.environ[_key.upper()].strip()
STATE_FILE = HERE / "state.json"
LOCK_FILE = HERE / ".lock"
TZ = ZoneInfo("Europe/London")
TIME_RE = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)\b")
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
FAILS_BEFORE_ALERT = 3


class WatchError(Exception):
    pass


# ---------- helpers ----------

def now():
    return datetime.now(TZ)


def hhmm_to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_quiet_hours(t):
    start, end = (hhmm_to_min(x) for x in CONFIG["quiet_hours"])
    cur = t.hour * 60 + t.minute
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # crosses midnight


def target_dates(today):
    out = []
    for i in range(CONFIG["days_ahead"] + 1):
        d = today + timedelta(days=i)
        if DAY_NAMES[d.weekday()] in CONFIG["windows"]:
            out.append(d)
    return out


def in_window(d, hhmm):
    start, end = CONFIG["windows"][DAY_NAMES[d.weekday()]]
    return hhmm_to_min(start) <= hhmm_to_min(hhmm) <= hhmm_to_min(end)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": {}, "pending": [], "fail_count": 0, "fail_alerted": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------- telegram ----------

def send_telegram(text):
    token = CONFIG["telegram_bot_token"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": CONFIG["telegram_chat_id"], "text": text,
              "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()


def notify(text, state):
    """Send now, or hold until quiet hours end."""
    if in_quiet_hours(now()):
        state["pending"].append(text)
    else:
        send_telegram(text)


def flush_pending(state):
    if state["pending"] and not in_quiet_hours(now()):
        send_telegram("Overnight alerts:\n\n" + "\n\n".join(state["pending"]))
        state["pending"] = []


def get_chat_id():
    token = CONFIG["telegram_bot_token"]
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
    ids = {u["message"]["chat"]["id"] for u in r.get("result", []) if "message" in u}
    if ids:
        print("Your chat ID:", ", ".join(str(i) for i in ids))
    else:
        print("No messages found. Send your bot a message in Telegram first, then run this again.")


# ---------- EliteLive (read only) ----------

def save_debug(page, label):
    try:
        page.screenshot(path=str(HERE / f"debug_{label}.png"), full_page=True)
        (HERE / f"debug_{label}.html").write_text(page.content())
    except Exception:
        pass


def read_availability(dates):
    """Returns {date_iso: [times]} for each date. Never clicks a tee time."""
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})

        # Safety net: if anything ever reaches a booking confirm page, stop dead.
        def guard(frame):
            if frame == page.main_frame and "book_confirm" in frame.url:
                raise WatchError("Safety stop: reached a booking page, aborting")
        page.on("framenavigated", guard)

        try:
            # 1. Log in
            page.goto(f"{BASE}?clubid={CONFIG['club_id']}&", wait_until="networkidle")
            if "login" in page.url:
                page.get_by_placeholder("Username").fill(CONFIG["elitelive_username"])
                page.get_by_placeholder("Password").fill(CONFIG["elitelive_password"])
                btn = page.get_by_role("button", name=re.compile("sign", re.I))
                if btn.count():
                    btn.first.click()
                else:
                    page.get_by_placeholder("Password").press("Enter")
                page.wait_for_load_state("networkidle")
            if "login" in page.url:
                save_debug(page, "login")
                raise WatchError("Login failed, check username and password in config.json")

            # 2. Set number of players (this only filters the tee sheet, it is not a booking)
            if "home.php" not in page.url:
                page.goto(BASE + "home.php", wait_until="networkidle")
            page.get_by_role("button", name=re.compile(r"make.*booking", re.I)).first.evaluate("b => b.click()")
            page.wait_for_url(re.compile("book_participants"), timeout=20000)
            page.wait_for_load_state("networkidle")
            sel = page.locator("select#NumPeople")
            sel.wait_for(timeout=15000)
            if sel.input_value() != str(CONFIG["players"]):
                # changing this reloads the page by itself
                with page.expect_navigation(timeout=20000):
                    sel.select_option(str(CONFIG["players"]))
                page.wait_for_load_state("networkidle")
            page.locator("input[name=Submit]").first.evaluate("b => b.click()")
            try:
                page.wait_for_url(re.compile("book_date"), timeout=20000)
            except Exception:
                pass
            page.wait_for_load_state("networkidle")
            if "book_date" not in page.url:
                save_debug(page, "players")
                raise WatchError(f"Could not get past the players step with {CONFIG['players']} players")

            # 3. Read availability for each date (data request only, no clicks)
            for d in dates:
                res = page.evaluate(
                    """async (d) => {
                        const r = await fetch('ajax/ajax_gettime.php?d=' + d + '&redirect_page=book_date.php&',
                                              {method: 'POST', credentials: 'same-origin'});
                        return {status: r.status, body: await r.text()};
                    }""",
                    d.strftime("%d/%m/%y"),
                )
                if res["status"] != 200 or "login.php" in res["body"]:
                    save_debug(page, "availability")
                    raise WatchError(f"Could not read tee sheet for {d:%a %d %b} (status {res['status']})")
                times = sorted({f"{h}:{m}" for h, m in TIME_RE.findall(res["body"])})
                results[d.isoformat()] = times
        except WatchError:
            raise
        except Exception as e:
            save_debug(page, "error")
            raise WatchError(f"{type(e).__name__} at {page.url}: {str(e).splitlines()[0]}")
        finally:
            browser.close()
    return results


# ---------- main ----------

def format_alert(new_by_date):
    lines = ["⛳ Wynyard tee time free", ""]
    for d, times in new_by_date:
        lines.append(f"{d:%a %d %b}: {', '.join(times)}")
    lines += ["", f"Book: {BASE}?clubid={CONFIG['club_id']}&"]
    return "\n".join(lines)


def run_check(test=False):
    state = load_state()
    today = now().date()
    dates = target_dates(today)

    try:
        avail = read_availability(dates)
    except Exception as e:
        if test:
            raise
        state["fail_count"] += 1
        print(f"{now():%Y-%m-%d %H:%M} FAIL ({state['fail_count']}): {e}")
        if state["fail_count"] >= FAILS_BEFORE_ALERT and not state["fail_alerted"]:
            notify(f"⚠️ Golf watcher problem\n\n{e}\n\nIt will keep retrying.", state)
            state["fail_alerted"] = True
        save_state(state)
        return

    if test:
        send_telegram("✅ Golf watcher test message. Telegram is working.")
        for d in dates:
            match = [t for t in avail[d.isoformat()] if in_window(d, t)]
            print(f"{d:%a %d %b}: {len(avail[d.isoformat()])} free, in your window: {match or 'none'}")
        return

    if state["fail_alerted"]:
        notify("✅ Golf watcher is working again.", state)
    state["fail_count"], state["fail_alerted"] = 0, False

    new_by_date = []
    for d in dates:
        key = d.isoformat()
        current = avail[key]
        previous = set(state["seen"].get(key, []))
        new = [t for t in current if t not in previous and in_window(d, t)]
        if new:
            new_by_date.append((d, new))
        state["seen"][key] = current

    # forget past dates
    state["seen"] = {k: v for k, v in state["seen"].items() if k >= today.isoformat()}

    if new_by_date:
        notify(format_alert(new_by_date), state)
    flush_pending(state)
    save_state(state)
    print(f"{now():%Y-%m-%d %H:%M} OK, {sum(len(t) for _, t in new_by_date)} new slot(s)")


def main():
    if "--get-chat-id" in sys.argv:
        return get_chat_id()
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # previous run still going
    try:
        run_check(test="--test" in sys.argv)
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
