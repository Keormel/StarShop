import sqlite3
import os
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()
DB_PATH = os.getenv("DB_PATH", "shop.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER UNIQUE,
            stars INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            price INTEGER NOT NULL,
            category_id INTEGER REFERENCES categories(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            product_id INTEGER,
            payment_status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    """)
    conn.commit()

    # Проверяем наличие колонки category_id в таблице products
    cursor.execute("PRAGMA table_info(products)")
    columns = [column[1] for column in cursor.fetchall()]
    if "category_id" not in columns:
        cursor.execute("ALTER TABLE products ADD COLUMN category_id INTEGER REFERENCES categories(id)")
        conn.commit()

    # Проверяем наличие колонки photo_path в таблице products
    if "photo_path" not in columns:
        cursor.execute("ALTER TABLE products ADD COLUMN photo_path TEXT")
        conn.commit()

    conn.close()

def add_user(telegram_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,))
    conn.commit()
    conn.close()

def get_products():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, description, price FROM products")
    products = cursor.fetchall()
    conn.close()
    return products

def get_product_by_id(product_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, description, price FROM products WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    conn.close()
    return product

def create_purchase(telegram_id, product_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,))
    conn.commit()
    cursor.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    user_row = cursor.fetchone()
    if not user_row:
        conn.close()
        raise RuntimeError("Не удалось получить user id для telegram_id")
    user_id = user_row[0]
    cursor.execute("INSERT INTO purchases (user_id, product_id) VALUES (?, ?)", (user_id, product_id))
    purchase_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return purchase_id

def add_category(name):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()

def get_categories():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM categories")
    categories = cursor.fetchall()
    conn.close()
    return categories

def add_product(name, description, price, category_id, photo_path):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO products (name, description, price, category_id, photo_path) VALUES (?, ?, ?, ?, ?)",
                   (name, description, price, category_id, photo_path))
    conn.commit()
    conn.close()

def get_products_by_category(category_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, description, price, photo_path FROM products WHERE category_id = ?", (category_id,))
    products = cursor.fetchall()
    conn.close()
    return products

def get_user_profile(telegram_id):
    """
    Получить профиль пользователя: имя и счет.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, stars FROM users WHERE telegram_id = ?", (telegram_id,))
    user = cursor.fetchone()
    conn.close()
    return user  # (telegram_id, stars) или None

def get_purchase_history(telegram_id):
    """
    Получить историю покупок пользователя.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id, pr.name, pr.price, p.created_at
        FROM purchases p
        JOIN users u ON p.user_id = u.id
        JOIN products pr ON p.product_id = pr.id
        WHERE u.telegram_id = ?
        ORDER BY p.created_at DESC
    """, (telegram_id,))
    purchases = cursor.fetchall()
    conn.close()
    return purchases  # [(purchase_id, product_name, price, created_at), ...]
