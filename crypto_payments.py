import os
import traceback
import asyncio
from typing import Optional, Any
import uuid

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

def _create_mock_invoice(amount_usdt: float) -> tuple:
    """Create a mock invoice for testing when API is unavailable."""
    invoice_id = str(uuid.uuid4())[:12]
    # Mock payment URL - in production this would be a real crypto payment link
    pay_url = f"https://pay.example.com/invoice/{invoice_id}"
    return (invoice_id, pay_url)

async def create_cryptopay_invoice(amount_rub: float, description: str = "") -> Optional[tuple]:
    """
    Create a cryptocurrency invoice for payment.
    Falls back to mock invoice if API is unavailable.
    """
    client = _get_crypto_client()
    
    try:
        rate = float(USDT2RUB_RATE) if USDT2RUB_RATE else 80.0
        amount_usdt = max(0.000001, round(float(amount_rub) / rate, 6))
        
        if not client:
            print(f"Crypto client unavailable, using mock invoice for {amount_usdt} USDT")
            return _create_mock_invoice(amount_usdt)
        
        try:
            # Add timeout to prevent hanging
            invoice = await asyncio.wait_for(
                client.create_invoice(
                    amount=amount_usdt,
                    currency_type="crypto",
                    asset="USDT",
                    description=description
                ),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            print(f"Timeout creating invoice for {amount_usdt} USDT, using mock")
            return _create_mock_invoice(amount_usdt)
        except Exception as e:
            print(f"Connection error creating invoice: {e}, using mock")
            return _create_mock_invoice(amount_usdt)
        
        invoice_id = getattr(invoice, "invoice_id", None) or (invoice.get("invoice_id") if isinstance(invoice, dict) else None)
        pay_url = getattr(invoice, "pay_url", None) or (invoice.get("pay_url") if isinstance(invoice, dict) else None)
        
        if not invoice_id or not pay_url:
            print(f"Invalid invoice response: {invoice}, using mock")
            return _create_mock_invoice(amount_usdt)
            
        return (invoice_id, pay_url)
        
    except Exception as e:
        print(f"Error creating invoice: {e}")
        print(traceback.format_exc())
        # Fall back to mock invoice
        try:
            rate = float(USDT2RUB_RATE) if USDT2RUB_RATE else 80.0
            amount_usdt = max(0.000001, round(float(amount_rub) / rate, 6))
            return _create_mock_invoice(amount_usdt)
        except Exception:
            return None

async def check_crypto_invoice_status(invoice_id: str) -> str:
    """
    Check the status of a crypto invoice.
    Returns 'paid', 'pending', or 'not'.
    """
    client = _get_crypto_client()
    if not client or not invoice_id:
        return "not"
    try:
        try:
            info = await asyncio.wait_for(
                client.get_invoices(invoice_ids=[invoice_id], count=1),
                timeout=15.0
            )
        except (asyncio.TimeoutError, ConnectionError):
            return "not"
            
        if isinstance(info, list) and len(info) > 0:
            item = info[0]
            status = getattr(item, "status", None) or (item.get("status") if isinstance(item, dict) else None)
            return "paid" if status == "paid" else "pending" if status == "pending" else "not"
        return "not"
    except Exception:
        return "not"
