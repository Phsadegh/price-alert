import os
import time
import json
import signal
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ============================================================================
# CONFIG
# ============================================================================

SYMBOL = "USTEC"
BASE_URL = "https://biquote.io/api"      # ONLY data source

UPDATE_SECONDS = 30        # live price polling interval
LATE_TOLERANCE = 90        # sec into a 15m bucket after which a candle first
                           # seen mid-way counts as partial (discarded)
GAP_TOLERANCE  = 60        # sec of downtime allowed to resume a forming candle
POLL_SLACK     = 35        # sec slack when judging if a saved candle was
                           # observed up to its close

# ---- SRFVG settings (same defaults as the Pine inputs) ----
SWING_LOOKBACK     = 30
MONITOR_START_HOUR = 1     # start monitoring at 01:00 ET
BARS_TO_KEEP       = 500

REPEAT_ALREADY_FORMED = True   # re-send "already formed today" at every restart
                               # (False = only once per day)
SAVE_DEV_EVERY        = 300    # sec; periodic state commit while a candle forms

STATE_FILE = "state.json"
CHART_FILE = "chart.png"

BAR_MS = 15 * 60 * 1000
NY = ZoneInfo("America/New_York")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

ON_GITHUB = "GITHUB_REPOSITORY" in os.environ and "GITHUB_TOKEN" in os.environ

# ============================================================================
# STATE
# ============================================================================

bars = []                 # completed 15m candles {"t","o","h","l","c"}, oldest first
                          # t = bar OPEN time, epoch ms, on the EST :00/:15/:30/:45 grid
dev = None                # candle currently being built from biquote polls
marked_day = None         # EST midnight (ms) of last day whose first SRFVG fired
last_srfvg = None         # details of that SRFVG
valid_from = 0            # first index of `bars` with no data gap before it
already_notified_day = None
shutting_down = False
last_dev_save = 0.0

# ============================================================================
# TELEGRAM
# ============================================================================

def send_telegram(message):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    r.raise_for_status()

def send_telegram_photo(image_path, caption):
    with open(image_path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": CHAT_ID, "caption": caption},
            files={"photo": ("chart.png", f, "image/png")},
            timeout=60,
        )
    r.raise_for_status()

# ============================================================================
# BIQUOTE (the only feed)
# ============================================================================

def get_price():
    r = requests.get(f"{BASE_URL}/{SYMBOL}", timeout=10)
    r.raise_for_status()
    return float(r.json()["mid"])

# ============================================================================
# TIME HELPERS (EST)
# ============================================================================

def est_dt(ms):
    return datetime.fromtimestamp(ms / 1000, NY)

def est_hm(ms):
    return est_dt(ms).strftime("%H:%M")

def midnight_est_ms(ts_ms):
    dt = datetime.fromtimestamp(ts_ms / 1000, NY)
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0)
               .timestamp() * 1000)

# ============================================================================
# CANDLE STORAGE
# ============================================================================

def recompute_valid_from():
    global valid_from
    valid_from = 0
    for i in range(1, len(bars)):
        if bars[i]["t"] != bars[i - 1]["t"] + BAR_MS:
            valid_from = i              # gap -> older history unusable for patterns

def append_bar(bar):
    global valid_from
    if bars and bars[-1]["t"] >= bar["t"]:
        return                          # duplicate / out of order
    if bars and bar["t"] != bars[-1]["t"] + BAR_MS:
        valid_from = len(bars)          # gap detected
    bars.append(bar)
    while len(bars) > BARS_TO_KEEP:
        bars.pop(0)
        valid_from = max(0, valid_from - 1)

# ============================================================================
# SWING SEARCH (port of the Pine functions)
# ============================================================================

def find_bullish_swing(i, fvg_bottom, fvg_top, day_start):
    for x in range(3, SWING_LOOKBACK + 3):
        j = i - x
        if j < 1 or j < valid_from:
            break
        if bars[j]["t"] < day_start:            # swing must be after midnight EST
            break
        swing_high = (bars[j]["h"] > bars[j + 1]["h"] and
                      bars[j]["h"] > bars[j - 1]["h"])
        inside = fvg_bottom <= bars[j]["h"] <= fvg_top
        if swing_high and inside:
            ok = True
            for k in range(j + 1, i - 2):       # Pine: for k = j+1 to i-3
                if bars[k]["h"] > bars[j]["h"]:
                    ok = False
                    break
            if ok:
                return j
    return -1

def find_bearish_swing(i, fvg_bottom, fvg_top, day_start):
    for x in range(3, SWING_LOOKBACK + 3):
        j = i - x
        if j < 1 or j < valid_from:
            break
        if bars[j]["t"] < day_start:
            break
        swing_low = (bars[j]["l"] < bars[j + 1]["l"] and
                     bars[j]["l"] < bars[j - 1]["l"])
        inside = fvg_bottom <= bars[j]["l"] <= fvg_top
        if swing_low and inside:
            ok = True
            for k in range(j + 1, i - 2):
                if bars[k]["l"] < bars[j]["l"]:
                    ok = False
                    break
            if ok:
                return j
    return -1

# ============================================================================
# SRFVG DETECTION (port of the Pine detection block)
# ============================================================================

def detect_at(i):
    """SRFVG check with candle 3 = bars[i]. Returns signal dict or None."""
    if i < 2 or i - 2 < valid_from:
        return None
    c1, c2, c3 = bars[i - 2], bars[i - 1], bars[i]

    close_time = c3["t"] + BAR_MS               # presentation = close of candle 3
    day_start = midnight_est_ms(close_time)
    if close_time < day_start + MONITOR_START_HOUR * 3_600_000:
        return None                             # presented before 01:00 ET
    if day_start == marked_day:
        return None                             # this day already fired

    # ---- bullish ----
    b_vi12 = c1["o"] < c1["c"] and c2["o"] > c1["c"] and c2["c"] > c1["c"]
    b_vi23 = c2["o"] < c2["c"] and c3["o"] > c2["c"] and c3["c"] > c2["c"]
    b_bot = c1["c"] if b_vi12 else c1["h"]
    b_top = min(c3["o"], c3["c"]) if b_vi23 else c3["l"]
    bull = b_top > b_bot

    # ---- bearish ----
    s_vi12 = c1["o"] > c1["c"] and c2["o"] < c1["c"] and c2["c"] < c1["c"]
    s_vi23 = c2["o"] > c2["c"] and c3["o"] < c2["c"] and c3["c"] < c2["c"]
    s_top = c1["c"] if s_vi12 else c1["l"]
    s_bot = max(c3["o"], c3["c"]) if s_vi23 else c3["h"]
    bear = s_top > s_bot

    if bull:
        sw = find_bullish_swing(i, b_bot, b_top, day_start)
        if sw != -1:
            return {"direction": "bullish", "susp": b_vi12 and b_vi23,
                    "top": b_top, "bottom": b_bot,
                    "swing_level": bars[sw]["h"], "swing_t": bars[sw]["t"],
                    "fvg_from": c1["t"], "fvg_to": close_time,
                    "day_start": day_start}
    elif bear:
        sw = find_bearish_swing(i, s_bot, s_top, day_start)
        if sw != -1:
            return {"direction": "bearish", "susp": s_vi12 and s_vi23,
                    "top": s_top, "bottom": s_bot,
                    "swing_level": bars[sw]["l"], "swing_t": bars[sw]["t"],
                    "fvg_from": c1["t"], "fvg_to": close_time,
                    "day_start": day_start}
    return None

# ============================================================================
# CHART (today's 15m candles from biquote data, SRFVG marked)
# ============================================================================

def render_chart(sig):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception as e:
        print(f"Chart skipped: {e}", flush=True)
        return None

    today = sig["day_start"]
    day = [b for b in bars if b["t"] >= today]
    forming_idx = None
    if dev and dev["t"] >= today and (not day or dev["t"] > day[-1]["t"]):
        day.append(dict(dev))                   # show the candle forming now
        forming_idx = len(day) - 1
    if not day:
        return None

    up, dn = "#26a69a", "#ef5350"
    fig, ax = plt.subplots(figsize=(12, 6), dpi=110)

    for idx, b in enumerate(day):
        col = up if b["c"] >= b["o"] else dn
        alpha = 0.5 if idx == forming_idx else 1.0
        ax.plot([idx, idx], [b["l"], b["h"]], color=col, lw=1, alpha=alpha, zorder=2)
        lo, hi = min(b["o"], b["c"]), max(b["o"], b["c"])
        ax.add_patch(Rectangle((idx - 0.35, lo), 0.7, max(hi - lo, 1e-9),
                               facecolor=col, edgecolor=col, alpha=alpha, zorder=3))

    # ---- mark the SRFVG ----
    t1, t3 = sig["fvg_from"], sig["fvg_to"] - BAR_MS
    i1 = next((k for k, b in enumerate(day) if b["t"] == t1), None)
    i3 = next((k for k, b in enumerate(day) if b["t"] == t3), None)
    if i1 is not None and i3 is not None:
        if sig["direction"] == "bullish":
            col = "#00a0a0" if sig["susp"] else up
        else:
            col = "#a000a0" if sig["susp"] else dn
        ax.add_patch(Rectangle((i1 - 0.45, sig["bottom"]), (i3 - i1) + 0.9,
                               sig["top"] - sig["bottom"],
                               facecolor=col, edgecolor=col,
                               alpha=0.22, lw=1.5, zorder=4))
        ax.text(i3 + 0.7, (sig["top"] + sig["bottom"]) / 2, "SRFVG",
                color=col, fontsize=11, fontweight="bold", va="center", zorder=5)
    k = next((k for k, b in enumerate(day) if b["t"] == sig["swing_t"]), None)
    if k is not None:
        ax.scatter([k], [sig["swing_level"]],
                   marker=("v" if sig["direction"] == "bullish" else "^"),
                   s=70, color="#ff9800", zorder=6)

    n = len(day)
    step = max(1, n // 10)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([est_hm(day[k]["t"]) for k in ticks])
    ax.set_xlim(-1, n + 1)
    ax.grid(alpha=0.25)
    ax.set_title(f"{SYMBOL} · 15m · {est_dt(today).strftime('%Y-%m-%d')} ET"
                 " — 1st SRFVG of the day", fontsize=13)
    fig.tight_layout()
    fig.savefig(CHART_FILE)
    plt.close(fig)
    return CHART_FILE

# ============================================================================
# ALERTS
# ============================================================================

def srfvg_caption(sig, already):
    bull = sig["direction"] == "bullish"
    head = (f"{'🟢' if bull else '🔴'} {SYMBOL} — 1st 15m SRFVG of the day"
            + (" — ALREADY FORMED TODAY" if already else ""))
    kind = " (Suspension)" if sig["susp"] else ""
    return (f"{head}{kind}\n"
            f"FVG candles: {est_hm(sig['fvg_from'])} → {est_hm(sig['fvg_to'])} ET\n"
            f"(3-candle pattern closed at {est_hm(sig['fvg_to'])} ET)\n"
            f"Zone: {sig['bottom']:.2f} – {sig['top']:.2f}\n"
            f"Swing {'high' if bull else 'low'}: {sig['swing_level']:.2f} "
            f"@ {est_hm(sig['swing_t'])} ET")

def deliver_srfvg(sig, already=False):
    caption = srfvg_caption(sig, already)
    for attempt in range(3):
        try:
            path = render_chart(sig)
            if path:
                send_telegram_photo(path, caption)
            else:
                send_telegram(caption)
            return True
        except Exception as e:
            print(f"Alert delivery failed (attempt {attempt + 1}): {e}", flush=True)
            time.sleep(5)
    try:                                          # last resort: text only
        send_telegram(caption)
    except Exception as e:
        print(f"Alert delivery FAILED: {e}", flush=True)
    return False

def on_srfvg(sig, already=False):
    global marked_day, last_srfvg
    marked_day = sig["day_start"]
    last_srfvg = sig
    ok = deliver_srfvg(sig, already)
    print(f"SRFVG ({sig['direction']}) "
          f"{est_hm(sig['fvg_from'])}-{est_hm(sig['fvg_to'])} ET "
          f"{'sent' if ok else 'FAILED'}.", flush=True)

# ============================================================================
# STATE PERSISTENCE (state.json committed to the repo — the bot's memory)
# ============================================================================

def write_state_file():
    with open(STATE_FILE, "w") as f:
        json.dump({"saved_at": int(time.time() * 1000),
                   "bars": bars, "dev": dev,
                   "marked_day": marked_day, "last_srfvg": last_srfvg,
                   "already_notified_day": already_notified_day}, f)

def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)

def merge_with_remote_state():
    """Union-merge the freshly fetched state.json into memory (local wins)."""
    global dev, marked_day, last_srfvg
    try:
        with open(STATE_FILE) as f:
            remote = json.load(f)
    except Exception:
        return
    rb = remote.get("bars", [])
    if rb:
        by_t = {b["t"]: dict(b) for b in bars}
        for b in rb:
            by_t.setdefault(b["t"], dict(b))
        bars[:] = [by_t[t] for t in sorted(by_t)][-BARS_TO_KEEP:]
        recompute_valid_from()
    rd = remote.get("dev")
    if rd and (dev is None or rd["t"] > dev["t"]):
        dev = rd
    rday = remote.get("marked_day")
    if rday is not None and (marked_day is None or rday > marked_day):
        marked_day = rday
        last_srfvg = remote.get("last_srfvg")

def commit_and_push(msg):
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    ident = ["-c", "user.name=srfvg-bot",
             "-c", "user.email=srfvg-bot@users.noreply.github.com"]
    if git(*ident, "commit", "-m", msg).returncode != 0:
        return True                                  # nothing to commit
    if git("push", "origin", f"HEAD:{branch}").returncode == 0:
        return True
    git("fetch", "origin", branch)                   # remote moved -> merge
    git("reset", "--hard", "FETCH_HEAD")
    merge_with_remote_state()
    write_state_file()
    git("add", STATE_FILE)
    git(*ident, "commit", "-m", f"{msg} (merged)")
    return git("push", "--force", "origin", f"HEAD:{branch}").returncode == 0

def save_state(reason="update"):
    global last_dev_save
    write_state_file()
    last_dev_save = time.time()
    if not ON_GITHUB:
        return
    try:
        git("add", STATE_FILE)
        if not commit_and_push(f"state {reason} [skip ci]"):
            print("State push failed (non-fatal).", flush=True)
    except Exception as e:
        print(f"State save failed (non-fatal): {e}", flush=True)

def load_state():
    global bars, marked_day, last_srfvg, already_notified_day
    if not os.path.exists(STATE_FILE):
        return None, 0
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception as e:
        print(f"Could not read {STATE_FILE}: {e}", flush=True)
        return None, 0
    bars = [dict(b) for b in s.get("bars", [])][-BARS_TO_KEEP:]
    recompute_valid_from()
    marked_day = s.get("marked_day")
    last_srfvg = s.get("last_srfvg")
    already_notified_day = s.get("already_notified_day")
    print(f"State loaded: {len(bars)} candles, marked_day={marked_day}", flush=True)
    return s.get("dev"), s.get("saved_at", 0)

# ============================================================================
# LIVE 15m CANDLE BUILDER (locked to the EST wall clock)
# ============================================================================

def process_tick(price):
    global dev
    now_ms = int(time.time() * 1000)
    bucket = now_ms - (now_ms % BAR_MS)             # EST :00/:15/:30/:45 grid

    if dev is None:
        partial = (now_ms - bucket) > LATE_TOLERANCE * 1000
        dev = {"t": bucket, "o": price, "h": price, "l": price,
               "c": price, "partial": partial}
        if partial:
            print(f"Joined candle {est_hm(bucket)} ET mid-way -> will discard", flush=True)
        return

    if bucket == dev["t"]:
        dev["h"] = max(dev["h"], price)
        dev["l"] = min(dev["l"], price)
        dev["c"] = price
        return

    # ---- a 15m candle just COMPLETED ----
    closed = dev
    if closed.get("partial"):
        print(f"Partial candle {est_hm(closed['t'])} ET discarded.", flush=True)
    else:
        append_bar(closed)
        print(f"15m closed {est_hm(closed['t'])}-{est_hm(closed['t'] + BAR_MS)} ET  "
              f"O={closed['o']:.2f} H={closed['h']:.2f} "
              f"L={closed['l']:.2f} C={closed['c']:.2f}", flush=True)
        sig = detect_at(len(bars) - 1)
        if sig:
            on_srfvg(sig, already=False)

    late = (now_ms - bucket) > LATE_TOLERANCE * 1000
    dev = {"t": bucket, "o": price, "h": price, "l": price,
           "c": price, "partial": late}
    save_state("candle-close")

# ============================================================================
# STARTUP
# ============================================================================

def initialize():
    global dev, already_notified_day
    raw_dev, saved_at = load_state()
    now_ms = int(time.time() * 1000)
    bucket = now_ms - (now_ms % BAR_MS)
    today = midnight_est_ms(now_ms)

    # ---- 1) candle that was forming when the previous run stopped ----
    if raw_dev and raw_dev["t"] + BAR_MS <= now_ms:
        # it closed during downtime
        if (not raw_dev.get("partial")
                and saved_at >= raw_dev["t"] + BAR_MS - POLL_SLACK * 1000):
            append_bar(raw_dev)
            print(f"Recovered closed candle {est_hm(raw_dev['t'])} ET from state.",
                  flush=True)
        else:
            print(f"Candle {est_hm(raw_dev['t'])} ET closed during downtime "
                  f"-> discarded.", flush=True)
    elif raw_dev and raw_dev["t"] == bucket:
        if (now_ms - saved_at) <= GAP_TOLERANCE * 1000 and not raw_dev.get("partial"):
            dev = raw_dev
            print(f"Resuming forming candle {est_hm(bucket)} ET.", flush=True)
        else:
            print(f"Forming candle {est_hm(bucket)} ET missed too much data "
                  f"-> will discard.", flush=True)
    # else: stale or none -> dev stays None; first poll starts a fresh candle

    # ---- 2) did today's first SRFVG already form? ----
    if marked_day == today and last_srfvg:
        if REPEAT_ALREADY_FORMED or already_notified_day != today:
            on_srfvg(last_srfvg, already=True)
            already_notified_day = today
    elif marked_day == today:
        send_telegram(f"ℹ️ {SYMBOL} monitor restarted.\n"
                      f"Today's first 15m SRFVG already fired (details unavailable).")
        already_notified_day = today
    else:
        # retro-scan candles recorded today (crash-recovery: catches an SRFVG
        # whose candle closed right before an abrupt kill)
        sig = None
        for i in range(2, len(bars)):
            s = detect_at(i)
            if s and s["day_start"] == today:
                sig = s
                break
        if sig:
            print(f"Startup check: SRFVG already formed today "
                  f"({est_hm(sig['fvg_from'])}-{est_hm(sig['fvg_to'])} ET).", flush=True)
            on_srfvg(sig, already=True)
            already_notified_day = today
        else:
            n_today = len([b for b in bars if b["t"] >= today])
            hist = (f"Today so far: {n_today} candles recorded."
                    if n_today else
                    "No candles recorded yet today (biquote gives live prices "
                    "only — nothing before startup is known).")
            send_telegram(
                f"🟢 {SYMBOL} SRFVG monitor STARTED (biquote feed only)\n"
                f"15m candles locked to the EST clock (:00/:15/:30/:45).\n"
                f"{hist}\n"
                f"Watching for the 1st 15m SRFVG of the day "
                f"(eligible from {MONITOR_START_HOUR:02d}:00 ET)."
            )
    save_state("startup")

# ============================================================================
# MAIN
# ============================================================================

def _handle_signal(signum, frame):
    global shutting_down
    shutting_down = True

def main():
    print("====================================", flush=True)
    print(f"{SYMBOL} SRFVG MONITOR — biquote feed, EST 15m candles", flush=True)
    print("====================================", flush=True)
    try:
        initialize()
    except Exception as e:
        print(f"Startup error (continuing): {type(e).__name__}: {e}", flush=True)

    while not shutting_down:
        try:
            price = get_price()
            print(f"{datetime.now(NY).strftime('%Y-%m-%d %H:%M:%S ET')} "
                  f"{SYMBOL} = {price}", flush=True)
            process_tick(price)

            if dev is not None and time.time() - last_dev_save >= SAVE_DEV_EVERY:
                save_state("dev")
        except Exception as e:
            print(f"{datetime.now(NY).strftime('%Y-%m-%d %H:%M:%S ET')} "
                  f"ERROR: {type(e).__name__}: {e}", flush=True)

        for _ in range(UPDATE_SECONDS):            # short sleeps -> fast shutdown
            if shutting_down:
                break
            time.sleep(1)

    save_state("shutdown")
    print("Shut down cleanly.", flush=True)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    main()
