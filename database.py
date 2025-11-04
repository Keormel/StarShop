import sqlite3
from datetime import datetime
from typing import Optional
from db_helpers import DB_PATH

def ensure_promos_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS promocodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        amount INTEGER NOT NULL,
        uses_left INTEGER,
        active INTEGER DEFAULT 1,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

def create_promo_in_db(code: str, amount: int, uses_left):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO promocodes(code, amount, uses_left, active, created_at) VALUES (?, ?, ?, 1, ?)",
        (code.upper(), amount, uses_left if uses_left is not None else None, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def get_promos_from_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, code, amount, uses_left, active, created_at FROM promocodes ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_promo_by_code(code: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, code, amount, uses_left, active FROM promocodes WHERE code = ?", (code.upper(),))
    row = cursor.fetchone()
    conn.close()
    return row

def get_promo_by_id(pid: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, code, amount, uses_left, active FROM promocodes WHERE id = ?", (pid,))
    row = cursor.fetchone()
    conn.close()
    return row

def delete_promo_from_db(pid: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM promocodes WHERE id = ?", (pid,))
    conn.commit()
    conn.close()

def toggle_promo_active(pid: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT active FROM promocodes WHERE id = ?", (pid,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    new_state = 0 if row[0] == 1 else 1
    cursor.execute("UPDATE promocodes SET active = ? WHERE id = ?", (new_state, pid))
    conn.commit()
    conn.close()
    return new_state

def ensure_payments_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_id INTEGER,
        invoice_id TEXT,
        pay_url TEXT,
        method TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

def create_payment_entry(purchase_id: int, invoice_id: Optional[str], pay_url: Optional[str], method: str = "crypto"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO payments(purchase_id, invoice_id, pay_url, method, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                   (purchase_id, invoice_id, pay_url, method, datetime.utcnow().isoformat()))
    pid = cursor.lastrowid
    conn.commit()
    conn.close()
    return pid

def get_payment_by_id(payment_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, purchase_id, invoice_id, pay_url, method, status FROM payments WHERE id = ?", (payment_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def update_payment_status_by_id(payment_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))
    conn.commit()
    conn.close()

def mark_purchase_paid(purchase_id: int):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("UPDATE purchases SET status = 'paid' WHERE id = ?", (purchase_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass

def ensure_autodeliveries_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS autodeliveries (
        product_id INTEGER PRIMARY KEY,
        enabled INTEGER DEFAULT 0,
        content_text TEXT,
        file_path TEXT,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

def create_autodelivery(product_id: int, enabled: int, content_text: Optional[str], file_path: Optional[str]):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO autodeliveries(product_id, enabled, content_text, file_path, created_at) VALUES (?, ?, ?, ?, ?)",
        (product_id, enabled, content_text, file_path, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def get_autodelivery_for_product(product_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT product_id, enabled, content_text, file_path FROM autodeliveries WHERE product_id = ?", (product_id,))
    row = cursor.fetchone()
    conn.close()
    return row
