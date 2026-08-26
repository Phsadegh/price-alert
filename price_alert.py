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

    url = f"{BASE_URL}/quote"

    params = {
        "symbol": SYMBOL
    }

    response = requests.get(
        url,
        params=params,
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    print("Biquote response:", data)

    # Try common price fields
    if isinstance(data, dict):

        for key in ["price", "last", "last_price", "close", "bid", "ask"]:

            if key in data:
                return float(data[key])

        # Sometimes data is nested
        for value in data.values():

            if isinstance(value, dict):

                for key in ["price", "last", "last_price", "close", "bid", "ask"]:

                    if key in value:
                        return float(value[key])

    raise ValueError(f"Could not find price in API response: {data}")


# =========================
# MAIN
# =========================

print("Starting USTEC price monitor...")

send_telegram(
    "🟢 USTEC monitor STARTED\n"
    "Checking Biquote every 30 seconds.\n"
    "Telegram update every 1 minute."
)

last_sent = 0

while True:

    try:

        price = get_price()

        now = datetime.now(NY)

        print(
            f"{now.strftime('%Y-%m-%d %H:%M:%S ET')} "
            f"USTEC = {price}"
        )

        # Send Telegram once per minute
        current_time = time.time()

        if current_time - last_sent >= SEND_INTERVAL:

            message = (
                f"📊 USTEC\n\n"
                f"Price: {price}\n"
                f"Time: {now.strftime('%Y-%m-%d %H:%M:%S ET')}"
            )

            send_telegram(message)

            last_sent = current_time

    except Exception as e:

        print(
            f"{datetime.now(NY).strftime('%Y-%m-%d %H:%M:%S ET')} "
            f"ERROR: {e}"
        )

    time.sleep(UPDATE_SECONDS)
