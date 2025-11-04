from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Управление категориями", callback_data="manage_categories"),
             InlineKeyboardButton(text="Управление товарами", callback_data="manage_products")],
            [InlineKeyboardButton(text="Промокоды 🎟️", callback_data="manage_promos")],
            [InlineKeyboardButton(text="Каталог 🛒", callback_data="catalog")]
        ]
    )

def main_menu_keyboard(uid: int = None) -> InlineKeyboardMarkup:
    from config import ADMIN_IDS
    keyboard_rows = [
        [InlineKeyboardButton(text="Каталог 🛒", callback_data="catalog")],
        [InlineKeyboardButton(text="Пополнение 🏦", callback_data="recharge"),
         InlineKeyboardButton(text="Помощь ⁉️", callback_data="help")],
        [InlineKeyboardButton(text="Промокоды 🎟️", callback_data="promo"),
         InlineKeyboardButton(text="Мой профиль 👤", callback_data="profile")]
    ]
    try:
        if uid in ADMIN_IDS:
            keyboard_rows.insert(0, [InlineKeyboardButton(text="Админ-панель ⚙️", callback_data="admin_panel")])
    except Exception:
        pass
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
