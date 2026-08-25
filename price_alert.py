import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

CHECK_INTERVAL = 15 * 60       # 15 minutes
RUN_TIME = 6 * 60 * 60         # 6 hours


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


def get_price():
    # Temporary test price
    # We will replace this with the real market API next.
    return 100.00


start_time = time.time()

send_telegram("🟢 Price monitor STARTED")

while time.time() - start_time < RUN_TIME:

    now = datetime.now(ZoneInfo("America/New_York"))
    price = get_price()

    message = (
        f"📊 Price Check\n\n"
        f"Price: {price}\n"
        f"Time: {now.strftime('%Y-%m-%d %H:%M:%S ET')}"
    )

    try:
        send_telegram(message)
        print(message)

    except Exception as e:
        print(f"Telegram error: {e}")

    # Wait 15 minutes
    time.sleep(CHECK_INTERVAL)

send_telegram("🔴 Price monitor STOPPED")
print("Finished.")
