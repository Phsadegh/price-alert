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

UPDATE_SECONDS = 30
SEND_INTERVAL = 60

NY = ZoneInfo("America/New_York")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


# =========================
# TELEGRAM
# =========================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=20,
    )

    response.raise_for_status()


# =========================
# BIQUOTE
# =========================

def get_price():

    url = f"{BASE_URL}/{SYMBOL}"

    response = requests.get(
        url,
        timeout=10
    )

    response.raise_for_status()

    data = response.json()

    print("Biquote:", data, flush=True)

    # Biquote uses "mid" for CFD/index prices
    price = data["mid"]

    return float(price)


# =========================
# MAIN
# =========================

print("====================================", flush=True)
print("USTEC PRICE MONITOR STARTING", flush=True)
print("====================================", flush=True)

send_telegram(
    "🟢 USTEC monitor STARTED\n"
    "Biquote price feed connected.\n"
    "Checking every 30 seconds.\n"
    "Telegram update every 1 minute."
)

last_sent = 0

while True:

    try:

        price = get_price()

        now = datetime.now(NY)

        print(
            f"{now.strftime('%Y-%m-%d %H:%M:%S ET')} "
            f"USTEC = {price}",
            flush=True
        )

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
            flush=True
        )

    time.sleep(UPDATE_SECONDS)
