import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
try:
    ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()}
except Exception:
    ADMIN_IDS = set()

CRYPTOPAY_TOKEN = os.getenv("CRYPTOPAY_TOKEN", "")
USDT2RUB_RATE = float(os.getenv("USDT2RUB_RATE", "80"))

if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не задана.")
