from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Управление категориями", callback_data="manage_categories"),
             InlineKeyboardButton(text="Управление товарами", callback_data="manage_products")],
            [InlineKeyboardButton(text="Добавить товар", callback_data="add_product_menu")],
            [InlineKeyboardButton(text="Удалить каталог", callback_data="delete_catalog")],
            [InlineKeyboardButton(text="Промокоды 🎟️", callback_data="manage_promos")],
            [InlineKeyboardButton(text="Каталог 🛒", callback_data="catalog")]
        ]
    )

def main_menu_keyboard(uid: int = None) -> InlineKeyboardMarkup:
    """
    Главное меню с 2 категориями сверху, профилем посередине, 
    поддержкой и калькулятором, и FAQ внизу.
    """
    from config import ADMIN_IDS
    from db_helpers import get_categories
    
    categories = get_categories()
    inline = []
    
    # Добавляем первые 2 категории в верхний ряд
    if len(categories) >= 2:
        inline.append([
            InlineKeyboardButton(text=f" {categories[0][1]}", callback_data=f"category_{categories[0][0]}"),
            InlineKeyboardButton(text=f" {categories[1][1]}", callback_data=f"category_{categories[1][0]}")
        ])
    elif len(categories) == 1:
        inline.append([
            InlineKeyboardButton(text=f" {categories[0][1]}", callback_data=f"category_{categories[0][0]}")
        ])
    
    # Профиль посередине
    inline.append([
        InlineKeyboardButton(text="👤 Профиль", callback_data="profile")
    ])
    
    # Поддержка и Калькулятор рядом
    inline.append([
        InlineKeyboardButton(text="💬 Поддержка", callback_data="support"),
        InlineKeyboardButton(text="🧮 Калькулятор", callback_data="calculator")
    ])
    
    # Каталог (все категории) и FAQ внизу
    inline.append([
        InlineKeyboardButton(text="📚 Каталог", callback_data="catalog"),
        InlineKeyboardButton(text="❓ FAQ", callback_data="faq")
    ])
    
    # Если админ, добавляем админ панель
    if uid and uid in ADMIN_IDS:
        inline.append([
            InlineKeyboardButton(text="🔐 Админ-панель", callback_data="admin_panel")
        ])
    
    return InlineKeyboardMarkup(inline_keyboard=inline)
