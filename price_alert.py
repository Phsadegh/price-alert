import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

SYMBOL = "USTEC"
BASE_URL = "https://biquote.io/api"

UPDATE_SECONDS = 30
SEND_INTERVAL = 60

NY = ZoneInfo("America/New_York")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def send_telegram(message):
    print("Sending Telegram message...", flush=True)

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=20,
    )

    print(f"Telegram HTTP status: {response.status_code}", flush=True)

    response.raise_for_status()


def get_price():

    url = f"{BASE_URL}/quote"

    params = {
        "symbol": SYMBOL
    }

    print(f"Requesting Biquote: {url}", flush=True)
    print(f"Parameters: {params}", flush=True)

    try:

        response = requests.get(
            url,
            params=params,
            timeout=10
        )

        print(
            f"Biquote HTTP status: {response.status_code}",
            flush=True
        )

        print(
            f"Biquote URL: {response.url}",
            flush=True
        )

        print(
            f"Biquote raw response: {response.text[:2000]}",
            flush=True
        )

        response.raise_for_status()

        data = response.json()

        print(
            f"Biquote JSON: {data}",
            flush=True
        )

        return data

    except Exception as e:

        print(
            f"BIQUOTE ERROR: {type(e).__name__}: {e}",
            flush=True
        )

        return None


print("====================================", flush=True)
print("USTEC PRICE MONITOR STARTING", flush=True)
print("====================================", flush=True)

send_telegram(
    "🟢 USTEC monitor STARTED\n"
    "Testing Biquote connection."
)

last_sent = 0

while True:

    print(
        f"\nChecking price at "
        f"{datetime.now(NY).strftime('%Y-%m-%d %H:%M:%S ET')}",
        flush=True
    )

    data = get_price()

    if data is not None:

        now = datetime.now(NY)

        # For now, send the COMPLETE API response
        # so we can see Biquote's actual structure.

        message = (
            f"📊 USTEC API TEST\n\n"
            f"Time: {now.strftime('%Y-%m-%d %H:%M:%S ET')}\n\n"
            f"{str(data)[:3000]}"
        )

        try:
            send_telegram(message)
            last_sent = time.time()

        except Exception as e:
            print(
                f"Telegram ERROR: {e}",
                flush=True
            )

    time.sleep(UPDATE_SECONDS)
