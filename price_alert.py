import os
import time
import signal
import requests
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo
from datetime import datetime, time as dtime, timedelta, timezone

# ============================================================================
# CONFIG
# ============================================================================

SYMBOL = "USTEC"
BASE_URL = "https://biquote.io/api"      # biquote = ONLY data source

POLL_SECONDS      = 30    # ask biquote for today's 15m candles every 30 s
COMPLETION_MARGIN = 2     # sec of clock slack before a candle counts as closed

# ---- SRFVG settings (same defaults as the Pine inputs) ----
SWING_LOOKBACK     = 30
MONITOR_START_HOUR = 1    # eligible from 01:00 ET
CHART_FILE = "chart.png"

BAR_MS = 15 * 60 * 1000
NY  = ZoneInfo("America/New_York")
UTC = timezone.utc

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

IRAN = ZoneInfo("Asia/Tehran")   # Iran = UTC+3:30 all year (no DST since 2022)
SEND_RUN_REMINDER = True         # send the "run workflow tomorrow" reminder

# ============================================================================
# STATE
# ============================================================================

bars = []              # TODAY's completed 15m candles from biquote /ohlc
forming = None         # candle currently forming (last, incomplete bar)
valid_from = 0         # first index of `bars` with no gap before it
marked_day = None      # ET midnight (ms) of the day whose first SRFVG fired
last_srfvg = None
last_processed_t = None
shutting_down = False

# ============================================================================
# TELEGRAM
# ============================================================================

def send_telegram(message):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": message}, timeout=20,
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
# BIQUOTE — today's 15m candles (same endpoint & params as your Dash code)
# ============================================================================

def get_price():
    r = requests.get(f"{BASE_URL}/{SYMBOL}", timeout=10)
    r.raise_for_status()
    return float(r.json()["mid"])

def parse_open_time(v):
    """biquote openTime -> epoch ms (accepts epoch s/ms/µs or ISO string, UTC)."""
    try:
        if isinstance(v, (int, float)):
            ms = float(v)
            if ms > 1e14:
                ms /= 1000                       # microseconds
            if ms > 1e11:
                return int(ms)                   # epoch milliseconds
            if ms > 1e8:
                return int(ms * 1000)            # epoch seconds
            return None
        if isinstance(v, str):
            s = v.strip().replace("Z", "+00:00").replace(" ", "T")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return int(dt.timestamp() * 1000)
    except Exception:
        return None
    return None

def midnight_est_ms(ts_ms):
    dt = datetime.fromtimestamp(ts_ms / 1000, NY)
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0)
               .timestamp() * 1000)

def fetch_today_ohlc():
    """(completed_candles, forming_candle) for TODAY, New York time."""
    now_ny = datetime.now(NY)
    midnight_ny = datetime.combine(now_ny.date(), dtime(0, 0), tzinfo=NY)
    r = requests.get(
        f"{BASE_URL}/{SYMBOL}/ohlc",
        params={
            "interval": "15m",
            "from": midnight_ny.astimezone(UTC).isoformat(),
            "to": now_ny.astimezone(UTC).isoformat(),
            "limit": 1000,
        },
        timeout=10,
    )
    r.raise_for_status()
    raw = (r.json() or {}).get("bars") or []

    now_ms = int(time.time() * 1000)
    midnight_ms = midnight_est_ms(now_ms)
    completed, forming_bar = [], None

    for b in raw:
        t = parse_open_time(b.get("openTime", b.get("time")))
        if t is None or t < midnight_ms or t > now_ms:
            continue
        try:
            o, h, l, c = (float(b["open"]), float(b["high"]),
                          float(b["low"]), float(b["close"]))
        except (KeyError, TypeError, ValueError):
            continue
        bar = {"t": t, "o": o, "h": h, "l": l, "c": c}
        if t + BAR_MS <= now_ms - COMPLETION_MARGIN * 1000:
            completed.append(bar)                        # candle is CLOSED
        elif forming_bar is None or t > forming_bar["t"]:
            forming_bar = bar                            # candle still forming
    completed.sort(key=lambda x: x["t"])
    return completed, forming_bar

# ============================================================================
# TIME HELPERS
# ============================================================================

def est_dt(ms):
    return datetime.fromtimestamp(ms / 1000, NY)

def est_hm(ms):
    return est_dt(ms).strftime("%H:%M")

# ============================================================================
# GAP GUARD (patterns/swings may never span a missing candle)
# ============================================================================

def recompute_valid_from():
    global valid_from
    valid_from = 0
    for i in range(1, len(bars)):
        if bars[i]["t"] != bars[i - 1]["t"] + BAR_MS:
            valid_from = i

# ============================================================================
# SWING SEARCH (port of the Pine functions)
# ============================================================================

def find_bullish_swing(i, fvg_bottom, fvg_top, day_start):
    for x in range(3, SWING_LOOKBACK + 3):
        j = i - x
        if j < 1 or j < valid_from:
            break
        if bars[j]["t"] < day_start:                 # swing after ET midnight
            break
        swing_high = (bars[j]["h"] > bars[j + 1]["h"] and
                      bars[j]["h"] > bars[j - 1]["h"])
        inside = fvg_bottom <= bars[j]["h"] <= fvg_top
        if swing_high and inside:
            ok = True
            for k in range(j + 1, i - 2):            # Pine: k = j+1 .. i-3
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
    if i < 2 or i - 2 < valid_from:
        return None
    c1, c2, c3 = bars[i - 2], bars[i - 1], bars[i]

    close_time = c3["t"] + BAR_MS                    # presentation = c3 close
    day_start = midnight_est_ms(close_time)
    if close_time < day_start + MONITOR_START_HOUR * 3_600_000:
        return None                                  # before 01:00 ET
    if day_start == marked_day:
        return None                                  # day already fired

    # bullish
    b_vi12 = c1["o"] < c1["c"] and c2["o"] > c1["c"] and c2["c"] > c1["c"]
    b_vi23 = c2["o"] < c2["c"] and c3["o"] > c2["c"] and c3["c"] > c2["c"]
    b_bot = c1["c"] if b_vi12 else c1["h"]
    b_top = min(c3["o"], c3["c"]) if b_vi23 else c3["l"]
    bull = b_top > b_bot

    # bearish
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
# CHART (today's biquote candles, SRFVG marked) — matplotlib, alert-time only
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

    day = list(bars)                                 # today's completed candles
    forming_idx = None
    if forming and (not day or forming["t"] > day[-1]["t"]):
        day.append(dict(forming))                    # candle forming now (dim)
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

    t1, t3 = sig["fvg_from"], sig["fvg_to"] - BAR_MS
    i1 = next((k for k, b in enumerate(day) if b["t"] == t1), None)
    i3 = next((k for k, b in enumerate(day) if b["t"] == t3), None)
    if i1 is not None and i3 is not None:
        col = ("#00a0a0" if sig["susp"] else up) if sig["direction"] == "bullish" \
              else ("#a000a0" if sig["susp"] else dn)
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
    ax.set_title(f"{SYMBOL} · 15m · {est_dt(sig['day_start']).strftime('%Y-%m-%d')} ET"
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
    try:
        send_telegram(caption)
    except Exception as e:
        print(f"Alert delivery FAILED: {e}", flush=True)
    return False
def send_run_reminder():
    """After the day's SRFVG alert: remind to run the workflow tomorrow at
    01:00 New York time (= 08:30 Iran in summer, 09:30 in winter — computed)."""
    global reminder_day
    if not SEND_RUN_REMINDER:
        return
    today = midnight_est_ms(int(time.time() * 1000))
    if reminder_day == today:                     # max one reminder per ET day
        return
    reminder_day = today

    # tomorrow at MONITOR_START_HOUR (01:00) New York wall-clock time
    run_at_ny = (datetime.now(NY) + timedelta(days=1)).replace(
        hour=MONITOR_START_HOUR, minute=0, second=0, microsecond=0)
    run_at_iran = run_at_ny.astimezone(IRAN)
    tz_label = "EST" if run_at_ny.utcoffset() == timedelta(hours=-5) else "EDT"

    send_telegram(
        f"⏰ Reminder: don't forget to run the workflow tomorrow at "
        f"{run_at_iran:%H:%M} Iran time "
        f"({run_at_ny:%H:%M} {tz_label} New York)."
    )

def on_srfvg(sig, already=False):
    global marked_day, last_srfvg
    marked_day = sig["day_start"]
    last_srfvg = sig
    ok = deliver_srfvg(sig, already)
    print(f"SRFVG ({sig['direction']}) "
          f"{est_hm(sig['fvg_from'])}-{est_hm(sig['fvg_to'])} ET "
          f"{'sent' if ok else 'FAILED'}.", flush=True)
    if ok:
        try:
            send_run_reminder()
        except Exception as e:
            print(f"Reminder failed (non-fatal): {e}", flush=True)

# ============================================================================
# STARTUP SCAN (first successful poll) + LIVE LOOP
# ============================================================================

def first_scan():
    """Runs once, when today's candles first arrive: report the day's state."""
    sig = None
    for i in range(2, len(bars)):                    # chronological scan
        s = detect_at(i)
        if s:
            sig = s
            break
    if sig:
        print(f"Startup scan: SRFVG already formed today "
              f"({est_hm(sig['fvg_from'])}-{est_hm(sig['fvg_to'])} ET).", flush=True)
        on_srfvg(sig, already=True)                  # alert + chart + times
    else:
        price_txt = ""
        try:
            price_txt = f"\nCurrent price: {get_price():.2f}"
        except Exception:
            pass
        send_telegram(
            f"🟢 {SYMBOL} SRFVG monitor STARTED (biquote /ohlc feed)\n"
            f"15m candles are biquote's own, locked to the EST clock.\n"
            f"Today so far: {len(bars)} completed candles.{price_txt}\n"
            f"Watching for the 1st 15m SRFVG of the day "
            f"(eligible from {MONITOR_START_HOUR:02d}:00 ET)."
        )

def poll_once():
    global bars, forming, last_processed_t

    completed, forming_bar = fetch_today_ohlc()
    forming = forming_bar

    if last_processed_t is None:                     # very first successful poll
        if completed:
            bars = completed
            recompute_valid_from()
            first_scan()
            last_processed_t = bars[-1]["t"]
        return

    new = [b for b in completed if b["t"] > last_processed_t]
    if not new:
        return

    bars = completed                                 # refresh today's full list
    recompute_valid_from()
    idx_by_t = {b["t"]: k for k, b in enumerate(bars)}

    for b in new:                                    # chronological order
        print(f"15m closed {est_hm(b['t'])}-{est_hm(b['t'] + BAR_MS)} ET  "
              f"O={b['o']:.2f} H={b['h']:.2f} L={b['l']:.2f} C={b['c']:.2f}",
              flush=True)
        i = idx_by_t.get(b["t"])
        if i is not None and i >= 2:
            sig = detect_at(i)
            if sig:
                on_srfvg(sig, already=False)         # live alert + chart
        last_processed_t = b["t"]

# ============================================================================
# MAIN
# ============================================================================

def _handle_signal(signum, frame):
    global shutting_down
    shutting_down = True

def main():
    print("====================================", flush=True)
    print(f"{SYMBOL} SRFVG MONITOR — biquote /ohlc, EST 15m candles", flush=True)
    print("====================================", flush=True)

    while not shutting_down:
        try:
            poll_once()
            last_close = est_hm(bars[-1]["t"] + BAR_MS) if bars else "-"
            form_txt = est_hm(forming["t"]) if forming else "-"
            print(f"{datetime.now(NY):%H:%M:%S} ET | candles today: {len(bars)} "
                  f"| last close: {last_close} | forming: {form_txt}", flush=True)
        except Exception as e:
            print(f"{datetime.now(NY):%H:%M:%S} ET ERROR: "
                  f"{type(e).__name__}: {e}", flush=True)

        for _ in range(POLL_SECONDS):
            if shutting_down:
                break
            time.sleep(1)

    print("Shut down cleanly.", flush=True)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    main()
