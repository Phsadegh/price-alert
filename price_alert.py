import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# =========================
# CONFIG
# =========================

SYMBOL = "USTEC"
BASE_URL = "https://biquote.io/api"

UPDATE_SECONDS = 30       # price polling interval
SEND_INTERVAL = 60        # periodic price message interval (seconds)

# ---- SRFVG settings (same defaults as the Pine inputs) ----
SWING_LOOKBACK      = 30   # "Swing Lookback (15m bars)"
MONITOR_START_HOUR  = 1    # "Start Monitoring Hour (EST)" -> 01:00
ONLY_FIRST_OF_DAY   = True # True  = exact Pine behaviour (only FIRST SRFVG of the day)
                           # False = alert on EVERY 15m SRFVG
SEND_PERIODIC_PRICE = True # keep the old "price every 1 minute" messages
BARS_TO_KEEP        = 500  # max completed 15m bars in memory (Pine arrayCap)

BAR_MS = 15 * 60 * 1000    # one 15m bar in epoch milliseconds

NY = ZoneInfo("America/New_York")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# =========================
# STATE (in-memory only)
# =========================
# bars       -> completed 15m bars, oldest first: {"t","o","h","l","c"}
#               t = bar OPEN time, epoch ms  (= t15Arr in Pine)
# dev        -> the 15m bar currently being built from ticks
# marked_day -> midnight EST (epoch ms) of the last day whose first
#               SRFVG already alerted  (= markedDay in Pine)

bars = []
dev = None
marked_day = None

# =========================
# TELEGRAM
# =========================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        data={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()

# =========================
# BIQUOTE
# =========================

def get_price():
    url = f"{BASE_URL}/{SYMBOL}"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()
    price = data["mid"]
    return float(price)

# =========================
# TIME HELPERS
# =========================

def est_hm(ms):
    return datetime.fromtimestamp(ms / 1000, NY).strftime("%H:%M ET")

def midnight_est_ms(ts_ms):
    """Midnight (00:00) New York time of the day ts_ms falls in, epoch ms."""
    dt = datetime.fromtimestamp(ts_ms / 1000, NY)
    dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(dt.timestamp() * 1000)

# =========================
# SWING SEARCH  (port of findBullishSwing15 / findBearishSwing15)
# =========================

def find_bullish_swing(i, fvg_bottom, fvg_top, day_start):
    """i = index of candle 3 (just-closed bar). Returns swing index or -1."""
    for x in range(3, SWING_LOOKBACK + 3):        # Pine: for x = 3 to swingLookback + 2
        j = i - x

        if j < 1:                                  # not enough history
            break

        if day_start is not None and bars[j]["t"] < day_start:
            break                                  # before midnight -> older bars even earlier

        swing_high = (
            bars[j]["h"] > bars[j + 1]["h"] and
            bars[j]["h"] > bars[j - 1]["h"]
        )
        inside_fvg = fvg_bottom <= bars[j]["h"] <= fvg_top

        if swing_high and inside_fvg:

            no_higher_between = True
            if i - 3 >= j + 1:                     # Pine: for k = j + 1 to i - 3
                for k in range(j + 1, i - 2):
                    if bars[k]["h"] > bars[j]["h"]:
                        no_higher_between = False
                        break

            if no_higher_between:
                return j

    return -1


def find_bearish_swing(i, fvg_bottom, fvg_top, day_start):
    for x in range(3, SWING_LOOKBACK + 3):
        j = i - x

        if j < 1:
            break

        if day_start is not None and bars[j]["t"] < day_start:
            break

        swing_low = (
            bars[j]["l"] < bars[j + 1]["l"] and
            bars[j]["l"] < bars[j - 1]["l"]
        )
        inside_fvg = fvg_bottom <= bars[j]["l"] <= fvg_top

        if swing_low and inside_fvg:

            no_lower_between = True
            if i - 3 >= j + 1:
                for k in range(j + 1, i - 2):
                    if bars[k]["l"] < bars[j]["l"]:
                        no_lower_between = False
                        break

            if no_lower_between:
                return j

    return -1

# =========================
# SRFVG DETECTION  (port of the Pine detection block)
# =========================

def detect_srfvg():
    """Runs on every COMPLETED 15m bar (closed bar = candle 3)."""
    i = len(bars) - 1
    if i < 2:
        return None

    c1, c2, c3 = bars[i - 2], bars[i - 1], bars[i]

    # presentation time = close of candle 3
    close_time = c3["t"] + BAR_MS
    day_start = midnight_est_ms(close_time)
    monitor_start = day_start + MONITOR_START_HOUR * 60 * 60 * 1000

    if close_time < monitor_start:
        return None
    if ONLY_FIRST_OF_DAY and day_start == marked_day:
        return None

    # ---------- BULLISH ----------
    bull_vi12 = c1["o"] < c1["c"] and c2["o"] > c1["c"] and c2["c"] > c1["c"]
    bull_vi23 = c2["o"] < c2["c"] and c3["o"] > c2["c"] and c3["c"] > c2["c"]

    bull_bottom = c1["c"] if bull_vi12 else c1["h"]
    bull_top    = min(c3["o"], c3["c"]) if bull_vi23 else c3["l"]
    bull_fvg    = bull_top > bull_bottom

    # ---------- BEARISH ----------
    bear_vi12 = c1["o"] > c1["c"] and c2["o"] < c1["c"] and c2["c"] < c1["c"]
    bear_vi23 = c2["o"] > c2["c"] and c3["o"] < c2["c"] and c3["c"] < c2["c"]

    bear_top    = c1["c"] if bear_vi12 else c1["l"]
    bear_bottom = max(c3["o"], c3["c"]) if bear_vi23 else c3["h"]
    bear_fvg    = bear_top > bear_bottom

    # swing-must-be-after-midnight rule only applies to the daily-first mode
    swing_day_limit = day_start if ONLY_FIRST_OF_DAY else None

    if bull_fvg:
        sw = find_bullish_swing(i, bull_bottom, bull_top, swing_day_limit)
        if sw != -1:
            return {
                "direction": "bullish", "susp": bull_vi12 and bull_vi23,
                "top": bull_top, "bottom": bull_bottom,
                "swing_idx": sw, "swing_level": bars[sw]["h"],
                "close_time": close_time, "day_start": day_start,
            }
    elif bear_fvg:
        sw = find_bearish_swing(i, bear_bottom, bear_top, swing_day_limit)
        if sw != -1:
            return {
                "direction": "bearish", "susp": bear_vi12 and bear_vi23,
                "top": bear_top, "bottom": bear_bottom,
                "swing_idx": sw, "swing_level": bars[sw]["l"],
                "close_time": close_time, "day_start": day_start,
            }

    return None

# =========================
# ALERT
# =========================

def send_srfvg_alert(sig):
    bull = sig["direction"] == "bullish"
    emoji = "🟢" if bull else "🔴"
    kind = "SRFVG (Suspension)" if sig["susp"] else "SRFVG"
    first_txt = " — 1st of the day" if ONLY_FIRST_OF_DAY else ""
    swing_label = "Swing high" if bull else "Swing low"
    swing_bar = bars[sig["swing_idx"]]
    close_dt = datetime.fromtimestamp(sig["close_time"] / 1000, NY)

    message = (
        f"{emoji} {sig['direction'].upper()} 15m {kind}{first_txt}\n\n"
        f"Symbol: {SYMBOL}\n"
        f"Zone: {sig['bottom']:.2f} – {sig['top']:.2f}\n"
        f"{swing_label}: {sig['swing_level']:.2f} ({est_hm(swing_bar['t'])})\n"
        f"Bar closed: {close_dt.strftime('%H:%M ET')}\n"
        f"Date: {close_dt.strftime('%Y-%m-%d')}"
    )
    send_telegram(message)

# =========================
# 15m CANDLE BUILDER  (port of the Pine aggregation)
# =========================

def process_tick(price):
    """
    Feed one price tick into the 15m builder. When the first tick of a NEW
    15m bucket arrives, the previous bar is complete -> store it and run
    detection (same moment the Pine script runs its detection).
    """
    global dev, marked_day

    now_ms = int(time.time() * 1000)
    bucket = now_ms - (now_ms % BAR_MS)   # open time of the current 15m bar

    if dev is None:
        dev = {"t": bucket, "o": price, "h": price, "l": price, "c": price}
        return

    if bucket == dev["t"]:
        dev["h"] = max(dev["h"], price)
        dev["l"] = min(dev["l"], price)
        dev["c"] = price
        return

    # ---- a 15m bar just completed ----
    closed = dev
    bars.append(closed)
    if len(bars) > BARS_TO_KEEP:
        del bars[0]

    print(
        f"15m bar closed  O={closed['o']:.2f}  H={closed['h']:.2f}  "
        f"L={closed['l']:.2f}  C={closed['c']:.2f}  ({est_hm(closed['t'])})",
        flush=True,
    )

    # start building the new bar
    dev = {"t": bucket, "o": price, "h": price, "l": price, "c": price}

    # ---- run SRFVG detection on the closed bar ----
    sig = detect_srfvg()
    if sig:
        if ONLY_FIRST_OF_DAY:
            marked_day = sig["day_start"]      # this day is now marked
        send_srfvg_alert(sig)
        print("SRFVG alert sent to Telegram.", flush=True)

# =========================
# MAIN
# =========================

print("====================================", flush=True)
print("USTEC SRFVG MONITOR STARTING", flush=True)
print("====================================", flush=True)

send_telegram(
    "🟢 USTEC SRFVG monitor STARTED\n"
    "Building 15m candles from the Biquote feed.\n"
    f"Alerts: {'FIRST 15m SRFVG of the day' if ONLY_FIRST_OF_DAY else 'every 15m SRFVG'}"
    f" (from {MONITOR_START_HOUR:02d}:00 ET).\n"
    f"Price polled every {UPDATE_SECONDS}s."
)

last_sent = 0

while True:
    try:
        price = get_price()
        now = datetime.now(NY)

        print(f"{now.strftime('%Y-%m-%d %H:%M:%S ET')} USTEC = {price}", flush=True)

        process_tick(price)

        if SEND_PERIODIC_PRICE:
            current_time = time.time()
            if current_time - last_sent >= SEND_INTERVAL:
                message = (
                    f"📊 USTEC\n\n"
                    f"Price: {price:.2f}\n"
                    f"Time: {now.strftime('%Y-%m-%d %H:%M:%S ET')}"
                )
                send_telegram(message)
                print("Telegram price sent.", flush=True)
                last_sent = current_time

    except Exception as e:
        print(
            f"{datetime.now(NY).strftime('%Y-%m-%d %H:%M:%S ET')} "
            f"ERROR: {type(e).__name__}: {e}",
            flush=True,
        )

    time.sleep(UPDATE_SECONDS)
