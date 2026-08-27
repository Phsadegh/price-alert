import os
import sys
import time
import signal
import requests
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

# ============================================================================
# CONFIG
# ============================================================================

SYMBOL = "USTEC"
BASE_URL = "https://biquote.io/api"      # biquote = ONLY data source

POLL_SECONDS      = 30
COMPLETION_MARGIN = 2                     # sec slack before a candle counts closed
WIDE_HOURS        = 48                    # /ohlc request window (also our probe)

# ---- SRFVG settings (same defaults as the Pine inputs) ----
SWING_LOOKBACK     = 30
MONITOR_START_HOUR = 1                    # FVGs eligible from 01:00 New York

# ---- run behaviour ----
RUN_WINDOW_MINUTES    = 345               # stop cleanly before GitHub's 6h cap
SEND_START_NOTICE     = True              # launch message on Telegram
SEND_RUN_REMINDER     = True              # "don't forget" after the alert
SEND_NO_SIGNAL_NOTICE = True              # message if window ends with no SRFVG
STALL_WARN_MINUTES    = 5                 # warn if NO data at all this long
STALL_REPEAT_MINUTES  = 30
PARTIAL_TOLERANCE     = 90                # sec into a bucket => tick candle partial

CHART_FILE = "chart.png"

BAR_MS = 15 * 60 * 1000
NY    = ZoneInfo("America/New_York")
IRAN  = ZoneInfo("Asia/Tehran")
UTC   = timezone.utc

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ============================================================================
# STATE
# ============================================================================

bars = []                # completed 15m candles, merged (/ohlc wins over ticks)
forming = None           # candle currently forming
valid_from = 0           # first index of `bars` with no gap before it
marked_day = None        # ET midnight of the day whose first SRFVG fired
last_srfvg = None
last_processed_t = None
pending_sig, pending_already = None, False

tick_dev  = None         # 15m candle being built from /api price polls
tick_bars = []           # tick-built completed candles

last_data_ok    = 0.0
last_stall_warn = 0.0
shutting_down   = False

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
# BIQUOTE (only data source)
# ============================================================================

def get_price():
    r = requests.get(f"{BASE_URL}/{SYMBOL}", timeout=10)
    r.raise_for_status()
    return float(r.json()["mid"])

def parse_open_time(v):
    try:
        if isinstance(v, (int, float)):
            ms = float(v)
            if ms > 1e14:
                ms /= 1000
            if ms > 1e11:
                return int(ms)
            if ms > 1e8:
                return int(ms * 1000)
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

def _ohlc_request(from_dt, to_dt):
    r = requests.get(
        f"{BASE_URL}/{SYMBOL}/ohlc",
        params={"interval": "15m",
                "from": from_dt.isoformat(),
                "to": to_dt.isoformat(),
                "limit": 1000},
        timeout=10,
    )
    r.raise_for_status()
    return r.json() or {}

def fetch_ohlc():
    """biquote /ohlc for the last WIDE_HOURS hours.
    Returns (completed_bars, forming_bar)."""
    now_ny = datetime.now(NY)
    now_utc = now_ny.astimezone(UTC)
    try:
        data = _ohlc_request(now_utc - timedelta(hours=WIDE_HOURS), now_utc)
    except Exception as e:
        print(f"/ohlc wide request failed ({e}); trying today-midnight window",
              flush=True)
        midnight_ny = datetime.combine(now_ny.date(), dtime(0, 0), tzinfo=NY)
        data = _ohlc_request(midnight_ny.astimezone(UTC), now_utc)

    raw = data.get("bars") or []
    if not raw:
        print(f"DEBUG /ohlc -> EMPTY bars "
              f"(asked last {WIDE_HOURS}h, now {now_ny:%H:%M:%S} ET)", flush=True)

    now_ms = int(time.time() * 1000)
    completed, forming_bar = [], None
    for b in raw:
        t = parse_open_time(b.get("openTime", b.get("time")))
        if t is None or t > now_ms:
            continue
        try:
            o, h, l, c = (float(b["open"]), float(b["high"]),
                          float(b["low"]), float(b["close"]))
        except (KeyError, TypeError, ValueError):
            continue
        bar = {"t": t, "o": o, "h": h, "l": l, "c": c}
        if t + BAR_MS <= now_ms - COMPLETION_MARGIN * 1000:
            completed.append(bar)
        elif forming_bar is None or t > forming_bar["t"]:
            forming_bar = bar
    completed.sort(key=lambda x: x["t"])

    if completed or forming_bar:
        c0 = completed[0]["t"] if completed else forming_bar["t"]
        c1 = completed[-1]["t"] if completed else forming_bar["t"]
        extra = f" + forming {est_hm(forming_bar['t'])}" if forming_bar else ""
        print(f"/ohlc OK: {len(completed)} completed bar(s) "
              f"{est_hm(c0)} → {est_hm(c1)} ET{extra}", flush=True)
    return completed, forming_bar

def tick_update(price, now_ms):
    """Feed one live price into the tick-built 15m candle (EST grid).
    Returns the just-completed candle (or None)."""
    global tick_dev
    bucket = now_ms - (now_ms % BAR_MS)
    if tick_dev is None or bucket != tick_dev["t"]:
        closed = tick_dev
        partial = (now_ms - bucket) > PARTIAL_TOLERANCE * 1000
        tick_dev = {"t": bucket, "o": price, "h": price, "l": price,
                    "c": price, "partial": partial}
        return closed
    tick_dev["h"] = max(tick_dev["h"], price)
    tick_dev["l"] = min(tick_dev["l"], price)
    tick_dev["c"] = price
    return None

# ============================================================================
# TIME HELPERS
# ============================================================================

def est_dt(ms):
    return datetime.fromtimestamp(ms / 1000, NY)

def est_hm(ms):
    return est_dt(ms).strftime("%H:%M")

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
        if bars[j]["t"] < day_start:
            break
        swing_high = (bars[j]["h"] > bars[j + 1]["h"] and
                      bars[j]["h"] > bars[j - 1]["h"])
        inside = fvg_bottom <= bars[j]["h"] <= fvg_top
        if swing_high and inside:
            ok = True
            for k in range(j + 1, i - 2):
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

    close_time = c3["t"] + BAR_MS
    day_start = midnight_est_ms(close_time)
    if close_time < day_start + MONITOR_START_HOUR * 3_600_000:
        return None
    if day_start == marked_day:
        return None

    b_vi12 = c1["o"] < c1["c"] and c2["o"] > c1["c"] and c2["c"] > c1["c"]
    b_vi23 = c2["o"] < c2["c"] and c3["o"] > c2["c"] and c3["c"] > c2["c"]
    b_bot = c1["c"] if b_vi12 else c1["h"]
    b_top = min(c3["o"], c3["c"]) if b_vi23 else c3["l"]
    bull = b_top > b_bot

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
# CHART
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

    day = [b for b in bars if b["t"] >= sig["day_start"]]
    forming_idx = None
    if forming and (not day or forming["t"] > day[-1]["t"]):
        day.append(dict(forming))
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
# ALERTS / REMINDER / STOP
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

def next_run_ny():
    d = (datetime.now(NY) + timedelta(days=1)).replace(
        hour=MONITOR_START_HOUR, minute=0, second=0, microsecond=0)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d

def send_run_reminder():
    run_ny = next_run_ny()
    run_iran = run_ny.astimezone(IRAN)
    tz = "EST" if run_ny.utcoffset() == timedelta(hours=-5) else "EDT"
    text = (f"⏰ Don't forget: the workflow starts {run_ny:%A} at "
            f"{run_iran:%H:%M} Iran time ({run_ny:%H:%M} {tz}).\n"
            f"If it hasn't started by itself, run it manually: "
            f"Actions → Price Alert → Run workflow.")
    for attempt in range(3):
        try:
            send_telegram(text)
            print("Reminder sent.", flush=True)
            return
        except Exception as e:
            print(f"Reminder attempt {attempt + 1} failed: {e}", flush=True)
            time.sleep(5)

def stop_workflow(reason):
    print(reason, flush=True)
    print("Workflow stopping (clean exit).", flush=True)
    sys.exit(0)

def after_alert():
    if SEND_RUN_REMINDER:
        send_run_reminder()
    stop_workflow("SRFVG alerted — done for today.")

def on_srfvg(sig, already=False):
    global marked_day, last_srfvg, pending_sig, pending_already
    marked_day = sig["day_start"]
    last_srfvg = sig
    print(f"SRFVG ({sig['direction']}) {est_hm(sig['fvg_from'])}-"
          f"{est_hm(sig['fvg_to'])} ET detected.", flush=True)
    if deliver_srfvg(sig, already):
        after_alert()
    else:
        pending_sig, pending_already = sig, already
        print("Delivery failed — will retry every poll.", flush=True)

# ============================================================================
# STARTUP SCAN + POLL (hybrid: /ohlc + tick-built candles)
# ============================================================================

def first_scan():
    today = midnight_est_ms(int(time.time() * 1000))
    sig = None
    for i in range(2, len(bars)):
        s = detect_at(i)
        if s and s["day_start"] == today:
            sig = s
            break
    if sig:
        print(f"Startup scan: SRFVG already formed today "
              f"({est_hm(sig['fvg_from'])}-{est_hm(sig['fvg_to'])} ET).", flush=True)
        on_srfvg(sig, already=True)
    elif SEND_START_NOTICE:
        n_today = len([b for b in bars if b["t"] >= today])
        price_txt = ""
        try:
            price_txt = f"\nCurrent price: {get_price():.2f}"
        except Exception:
            pass
        send_telegram(
            f"📡 {SYMBOL} feed connected (biquote)\n"
            f"{n_today} completed candles today, no SRFVG yet.\n"
            f"Watching for the 1st 15m SRFVG "
            f"(eligible from {MONITOR_START_HOUR:02d}:00 ET).{price_txt}"
        )

def poll_once():
    global bars, forming, last_processed_t, last_data_ok

    # ---- source 1: /ohlc (authoritative when it has data) ----
    ohlc_completed, ohlc_forming = fetch_ohlc()

    # ---- source 2: live price -> tick-built candles ----
    price = None
    try:
        price = get_price()
    except Exception as e:
        print(f"price endpoint: {type(e).__name__}: {e}", flush=True)

    if price is not None:
        closed = tick_update(price, int(time.time() * 1000))
        if closed is not None:
            if closed.get("partial"):
                print(f"Partial tick candle {est_hm(closed['t'])} ET discarded.",
                      flush=True)
            else:
                tick_bars.append(closed)

    if ohlc_completed or ohlc_forming or price is not None:
        last_data_ok = time.time()

    # ---- merge (ohlc wins on conflicts) ----
    merged = {b["t"]: dict(b) for b in tick_bars}
    for b in ohlc_completed:
        merged[b["t"]] = dict(b)
    new_bars = [merged[t] for t in sorted(merged)]
    if new_bars:
        bars = new_bars
        recompute_valid_from()
    forming = ohlc_forming if ohlc_forming is not None else tick_dev

    # ---- first data -> startup scan ----
    if last_processed_t is None:
        if bars:
            first_scan()
            last_processed_t = bars[-1]["t"]
    else:
        ohlc_ts = {b["t"] for b in ohlc_completed}
        for b in bars:
            if b["t"] <= last_processed_t:
                continue
            src = "ohlc" if b["t"] in ohlc_ts else "tick"
            print(f"15m closed {est_hm(b['t'])}-{est_hm(b['t'] + BAR_MS)} ET "
                  f"[{src}]  O={b['o']:.2f} H={b['h']:.2f} "
                  f"L={b['l']:.2f} C={b['c']:.2f}", flush=True)
            i = next((k for k, x in enumerate(bars) if x["t"] == b["t"]), None)
            if i is not None and i >= 2:
                sig = detect_at(i)
                if sig:
                    on_srfvg(sig, already=False)
            last_processed_t = b["t"]

    # ---- status line ----
    today = midnight_est_ms(int(time.time() * 1000))
    n_today = len([b for b in bars if b["t"] >= today])
    price_str = f"{price:.2f}" if price is not None else "n/a"
    form_str = est_hm(forming["t"]) if forming else "-"
    print(f"{datetime.now(NY):%H:%M:%S} ET | price: {price_str} | "
          f"candles today: {n_today} | forming: {form_str}", flush=True)

# ============================================================================
# MAIN
# ============================================================================

def _handle_signal(signum, frame):
    global shutting_down
    shutting_down = True

def main():
    # ---- LAUNCH GATE (scheduled runs only; manual runs always pass) ----
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        now_ny = datetime.now(NY)
        if now_ny.weekday() >= 5:
            stop_workflow(f"Scheduled start skipped: weekend ({now_ny:%A}).")
        if now_ny.hour != MONITOR_START_HOUR:
            stop_workflow(
                f"Scheduled start skipped: {now_ny:%H:%M} New York is outside the "
                f"{MONITOR_START_HOUR:02d}:00 launch window (DST twin cron).")

    print("====================================", flush=True)
    print(f"{SYMBOL} SRFVG MONITOR — biquote hybrid feed, EST 15m candles", flush=True)
    print(f"Launch: {datetime.now(NY):%Y-%m-%d %H:%M:%S} ET "
          f"({datetime.now(IRAN):%H:%M} Iran)", flush=True)
    print("====================================", flush=True)

    # ---- launch message (immediately) ----
    if SEND_START_NOTICE:
        try:
            send_telegram(
                f"🟢 {SYMBOL} SRFVG monitor started\n"
                f"{datetime.now(NY):%Y-%m-%d %H:%M:%S} ET "
                f"({datetime.now(IRAN):%H:%M} Iran)\n"
                f"Connecting to the biquote feed..."
            )
        except Exception as e:
            print(f"Start message failed: {e}", flush=True)

    global last_data_ok
    last_data_ok = time.time()
    started_at = time.time()

    while not shutting_down:

        if time.time() - started_at >= RUN_WINDOW_MINUTES * 60:
            if SEND_NO_SIGNAL_NOTICE:
                try:
                    send_telegram(f"⚪ {SYMBOL}: monitoring window ended "
                                  f"(no SRFVG alerted). Workflow stopping.")
                except Exception as e:
                    print(f"No-signal notice failed: {e}", flush=True)
            stop_workflow("Monitoring window finished (5h45m).")

        if pending_sig is not None and deliver_srfvg(pending_sig, pending_already):
            after_alert()

        # ---- stall alarm: BOTH endpoints dead ----
        dead_for = time.time() - last_data_ok
        if (dead_for > STALL_WARN_MINUTES * 60
                and time.time() - last_stall_warn >= STALL_REPEAT_MINUTES * 60):
            try:
                send_telegram(
                    f"⚠️ {SYMBOL}: no data from biquote for "
                    f"{int(dead_for // 60)} min — check the Actions log."
                )
            except Exception as e:
                print(f"Stall warning failed: {e}", flush=True)
            last_stall_warn = time.time()

        try:
            poll_once()
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
