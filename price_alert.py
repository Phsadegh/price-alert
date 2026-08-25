import os
import requests
from datetime import datetime, timezone

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


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


# Temporary test price
price = 100.00

now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

message = (
    f"🟢 Price Alert Test\n\n"
    f"Price: {price}\n"
    f"Time: {now}"
)

send_telegram(message)

print(message)
