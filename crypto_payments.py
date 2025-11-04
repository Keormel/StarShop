import os
import traceback
from typing import Optional, Any

from config import CRYPTOPAY_TOKEN, USDT2RUB_RATE

try:
    from AsyncPayments.cryptoBot import AsyncCryptoBot
    CRYPTO_AVAILABLE = True
except Exception:
    print(traceback.format_exc())
    CRYPTO_AVAILABLE = False
    class AsyncCryptoBot:
        def __init__(self, token: str, is_testnet: bool = True):
            self.token = token
            self.is_testnet = is_testnet
        async def create_invoice(self, *args, **kwargs):
            return {"invoice_id": None, "pay_url": None}
        async def get_invoices(self, *args, **kwargs):
            return []

crypto_client: Optional[Any] = None

def _get_crypto_client():
    global crypto_client
    if crypto_client is None and CRYPTOPAY_TOKEN and CRYPTO_AVAILABLE:
        try:
            is_testnet = os.getenv("CRYPTOPAY_TESTNET", "1") not in ("0", "false", "False")
            crypto_client = AsyncCryptoBot(CRYPTOPAY_TOKEN, is_testnet=is_testnet)
        except Exception:
            print(traceback.format_exc())
            crypto_client = None
    return crypto_client

async def create_cryptopay_invoice(amount_rub: float, description: str = "") -> Optional[tuple]:
    client = _get_crypto_client()
    if not client:
        return None
    try:
        rate = float(USDT2RUB_RATE) if USDT2RUB_RATE else 80.0
        amount_usdt = max(0.000001, round(float(amount_rub) / rate, 6))
        invoice = await client.create_invoice(amount=amount_usdt, currency_type="crypto", asset="USDT", description=description)
        invoice_id = getattr(invoice, "invoice_id", None) or (invoice.get("invoice_id") if isinstance(invoice, dict) else None)
        pay_url = getattr(invoice, "pay_url", None) or (invoice.get("pay_url") if isinstance(invoice, dict) else None)
        return (invoice_id, pay_url)
    except Exception:
        print(traceback.format_exc())
        return None

async def check_crypto_invoice_status(invoice_id: str) -> str:
    client = _get_crypto_client()
    if not client or not invoice_id:
        return "not"
    try:
        info = await client.get_invoices(invoice_ids=[invoice_id], count=1)
        if isinstance(info, list) and len(info) > 0:
            item = info[0]
            status = getattr(item, "status", None) or (item.get("status") if isinstance(item, dict) else None)
            return "paid" if status == "paid" else "not"
        return "not"
    except Exception:
        return "not"
