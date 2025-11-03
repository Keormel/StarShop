import os
import asyncio
import sqlite3  # Добавляем импорт sqlite3
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputFile, FSInputFile, InputMediaPhoto
import logging
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime
import functools
import aiohttp
from typing import Optional, Any

from db_helpers import (
    init_db, add_user, get_categories, add_category, add_product,
    get_products_by_category, get_products, get_product_by_id,
    create_purchase, get_user_profile, get_purchase_history, DB_PATH  # Импортируем DB_PATH
)

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")  # Ожидается строка вида "12345678,23456789"
CRYPTOPAY_TOKEN = os.getenv("CRYPTOPAY_TOKEN", "")
CRYPTOPAY_API_URL = os.getenv("CRYPTOPAY_API_URL", "")  # полный endpoint для создания инвойса
CRYPTOPAY_DEFAULT_CURRENCY = os.getenv("CRYPTOPAY_CURRENCY", "RUB")
try:
    ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()}
except Exception:
    ADMIN_IDS = set()

def _extract_user_from_args(args, kwargs):
    # Найти Message или CallbackQuery среди аргументов/ключевых аргументов
    for v in list(args) + list(kwargs.values()):
        try:
            # aiogram Message и CallbackQuery имеют from_user
            if hasattr(v, "from_user") and getattr(v, "from_user") is not None:
                return v
        except Exception:
            continue
    return None

def admin_only(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        obj = _extract_user_from_args(args, kwargs)
        user_id = None
        if obj is not None and hasattr(obj, "from_user"):
            user_id = getattr(obj.from_user, "id", None)

        if user_id not in ADMIN_IDS:
            # Ответ для CallbackQuery и Message
            # пытаемся вызвать callback.answer или message.reply
            try:
                if hasattr(obj, "answer") and callable(obj.answer):
                    # CallbackQuery
                    await obj.answer("Доступ запрещён. Только администраторы.", show_alert=True)
                    return
            except Exception:
                pass
            try:
                if hasattr(obj, "reply") and callable(obj.reply):
                    await obj.reply("Доступ запрещён. Только администраторы.")
            except Exception:
                pass
            return
        return await func(*args, **kwargs)
    return wrapper

if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не задана.")

# Настройка бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# unified in-memory map chat_id -> last shown message_id (for menus and products)
last_message = {}

# Состояния для добавления товара
class AddProductState(StatesGroup):
    waiting_for_category = State()
    waiting_for_name = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_photo = State()

# Состояния для управления категориями и товарами
class AdminState(StatesGroup):
    waiting_for_category_name = State()
    waiting_for_product_name = State()
    waiting_for_product_description = State()
    waiting_for_product_price = State()
    waiting_for_product_category = State()

# --- NEW: состояния для промокодов (админ и пользователь)
class PromoAdminState(StatesGroup):
    waiting_for_promo_code = State()
    waiting_for_promo_amount = State()
    waiting_for_promo_uses = State()
    waiting_for_edit_uses = State()
    waiting_for_edit_amount = State()

class UserPromoState(StatesGroup):
    waiting_for_code = State()

# /start — приветствие с inline-кнопками
@dp.message(Command("start"))
async def start_command(message: Message):
    add_user(message.from_user.id)

    # Создаем inline-клавиатуру с кнопками
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Каталог 🛒", callback_data="catalog")],
            [InlineKeyboardButton(text="Пополнение 🏦", callback_data="recharge"),
             InlineKeyboardButton(text="Помощь ⁉️", callback_data="help")],
            [InlineKeyboardButton(text="Промокоды 🎟️", callback_data="promo"),
             InlineKeyboardButton(text="Мой профиль 👤", callback_data="profile")]
        ]
    )

    await message.reply("Добро пожаловать! Выберите действие:", reply_markup=keyboard)

# /add_product — добавить товар
@dp.message(Command("add_product"))
async def add_product_command(message: Message, state: FSMContext):
    await message.reply("Введите название категории для товара:")
    await state.set_state(AddProductState.waiting_for_category)

@dp.message(AddProductState.waiting_for_category)
async def process_category(message: Message, state: FSMContext):
    category_name = message.text.strip()
    add_category(category_name)
    categories = get_categories()
    category_id = next((c[0] for c in categories if c[1] == category_name), None)
    await state.update_data(category_id=category_id)
    await message.reply("Введите название товара:")
    await state.set_state(AddProductState.waiting_for_name)

@dp.message(AddProductState.waiting_for_name)
async def process_product_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.reply("Введите описание товара:")
    await state.set_state(AddProductState.waiting_for_description)

@dp.message(AddProductState.waiting_for_description)
async def process_product_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.reply("Введите цену товара (целое число):")
    await state.set_state(AddProductState.waiting_for_price)

@dp.message(AddProductState.waiting_for_price)
async def process_product_price(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
    except ValueError:
        await message.reply("Цена должна быть числом. Попробуйте снова.")
        return

    await state.update_data(price=price)
    await message.reply("Отправьте фотографию товара:")
    await state.set_state(AddProductState.waiting_for_photo)

@dp.message(AddProductState.waiting_for_photo, F.content_type == "photo")
async def process_product_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]  # Берем последнюю (наибольшего размера) фотографию
    photo_dir = "photos"
    photo_path = os.path.join(photo_dir, f"{photo.file_id}.jpg")

    # Создаем директорию, если она не существует
    os.makedirs(photo_dir, exist_ok=True)

    # Сохраняем фотографию локально через bot.download_file
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, destination=photo_path)

    data = await state.get_data()
    add_product(data["name"], data["description"], data["price"], data["category_id"], photo_path)
    await message.reply(f"Товар '{data['name']}' добавлен.")
    await state.clear()
    # если админ — вернуть в админ-панель, иначе в главное меню
    if message.from_user and message.from_user.id in ADMIN_IDS:
        await send_admin_menu(message.chat.id, message)
    else:
        await send_main_menu(message.chat.id, message)

# Вспомогательная функция: редактировать существующее сообщение в чате или отправить новое и сохранить id
async def send_or_edit(chat_id: int, source_obj, text: str = None, photo_path: str = None,
                       reply_markup: InlineKeyboardMarkup = None, parse_mode: str = None):
    """
    Попытаться отредактировать предыдущее сообщение в чате (last_message[chat_id]).
    Если не получилось — попытаться отредактировать исходное сообщение (source_obj.message_id).
    Если и это не удалось — отправить новое сообщение/фото и сохранять его id.
    source_obj может быть CallbackQuery или Message.
    """
    prev_mid = last_message.get(chat_id)

    # попытка редактирования сохранённого сообщения
    if prev_mid:
        try:
            if photo_path:
                media = InputMediaPhoto(media=FSInputFile(photo_path), caption=text, parse_mode=parse_mode)
                await bot.edit_message_media(media=media, chat_id=chat_id, message_id=prev_mid, reply_markup=reply_markup)
            else:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=prev_mid, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except Exception:
            # если не удалось отредактировать — продолжим к следующей попытке
            pass

    # попытка редактировать исходное сообщение (callback.message или message)
    try:
        src_msg = None
        if isinstance(source_obj, CallbackQuery):
            src_msg = source_obj.message
        elif isinstance(source_obj, Message):
            src_msg = source_obj

        if src_msg:
            if photo_path:
                media = InputMediaPhoto(media=FSInputFile(photo_path), caption=text, parse_mode=parse_mode)
                await bot.edit_message_media(media=media, chat_id=chat_id, message_id=src_msg.message_id, reply_markup=reply_markup)
                last_message[chat_id] = src_msg.message_id
                return
            else:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=src_msg.message_id, reply_markup=reply_markup, parse_mode=parse_mode)
                last_message[chat_id] = src_msg.message_id
                return
    except Exception:
        pass

    # если ничего не получилось — отправляем новое сообщение/фото и сохраняем id
    if photo_path:
        sent = await bot.send_photo(chat_id=chat_id, photo=FSInputFile(photo_path), caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    last_message[chat_id] = sent.message_id

# Callback: показать каталог (с подкаталогами)
@dp.callback_query(F.data == "catalog")
async def catalog_callback(callback: CallbackQuery):
    categories = get_categories()
    if not categories:
        await send_or_edit(callback.message.chat.id, callback, text="Каталог пуст.")
        await callback.answer()
        return

    # Создаем клавиатуру с категориями + кнопка назад
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            *[
                [InlineKeyboardButton(text=category_name, callback_data=f"category_{category_id}")]
                for category_id, category_name in categories
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )
    await send_or_edit(callback.message.chat.id, callback, text="Выберите категорию:", reply_markup=keyboard)
    await callback.answer()

# Callback: показать товары в категории с кнопками "Следующий товар" и "Предыдущий товар"
@dp.callback_query(F.data.startswith("category_"))
async def category_callback(callback: CallbackQuery):
    try:
        category_id = int(callback.data.split("_", 1)[1])
    except ValueError:
        await callback.answer("Неверный ID категории.", show_alert=True)
        return

    products = get_products_by_category(category_id)
    if not products:
        await callback.message.reply("В этой категории пока нет товаров.")
        await callback.answer()
        return

    # Отображаем первый товар
    await show_product(callback, products, 0, category_id)

async def show_product(callback: CallbackQuery, products, index, category_id):
    """
    Отображает товар с указанным индексом из списка товаров.
    Использует send_or_edit — карточки товаров заменяют предыдущие сообщения.
    """
    product_id, name, description, price, photo_path = products[index]
    text = f"🔹 <b>{name}</b>\n" \
           f"💬 {description}\n" \
           f"💰 Цена: {price} ₽"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Предыдущий",
                    callback_data=f"product_{category_id}_{index - 1}" if index > 0 else "disabled"
                ),
                InlineKeyboardButton(
                    text="➡️ Следующий",
                    callback_data=f"product_{category_id}_{index + 1}" if index < len(products) - 1 else "disabled"
                )
            ],
            [
                InlineKeyboardButton(text="🛒 Купить", callback_data=f"buy_{product_id}")
            ],
            [
                InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")
            ]
        ]
    )

    chat_id = callback.message.chat.id
    await send_or_edit(chat_id, callback, text=text, photo_path=photo_path, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("product_"))
async def product_navigation_callback(callback: CallbackQuery):
    try:
        _, category_id, index = callback.data.split("_")
        category_id = int(category_id)
        index = int(index)
    except ValueError:
        await callback.answer("Ошибка навигации.", show_alert=True)
        return

    products = get_products_by_category(category_id)
    if not products or index < 0 or index >= len(products):
        await callback.answer("Товар не найден.", show_alert=True)
        return

    await show_product(callback, products, index, category_id)

# Callback: покупка (создаём запись покупки и даём ссылку на оплату)
@dp.callback_query(F.data.startswith("buy_"))
async def handle_buy_callback(callback: CallbackQuery):
    try:
        product_id = int(callback.data.split("_", 1)[1])
    except ValueError:
        await callback.answer("Неверный ID товара.", show_alert=True)
        return

    product = get_product_by_id(product_id)
    if not product:
        await callback.message.reply("Товар не найден.")
        await callback.answer()
        await send_main_menu(callback.message.chat.id, callback)
        return

    _, name, _, price = product
    purchase_id = create_purchase(callback.from_user.id, product_id)

    # Попытка создать инвойс через CryptoPay (если настроено)
    pay_link = await create_cryptopay_invoice(amount=price, order_id=purchase_id, description=f"Order {purchase_id}: {name}")
    if pay_link:
        await callback.message.reply(f"Заказ создан (ID: {purchase_id}). Для оплаты перейдите по ссылке: {pay_link}")
    else:
        # fallback — инструкцию по пополнению баланса
        await callback.message.reply(
            f"Заказ создан (ID: {purchase_id}) на товар '{name}' на сумму {price} ₽.\n"
            "Для оплаты пополните баланс в разделе «Пополнение» или свяжитесь с поддержкой для согласования оплаты."
        )
    await callback.answer()
    await send_main_menu(callback.message.chat.id, callback)

# Callback: показать профиль пользователя
@dp.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery):
    user = get_user_profile(callback.from_user.id)
    if not user:
        await send_or_edit(callback.message.chat.id, callback, text="Ваш профиль не найден.")
        await callback.answer()
        await send_main_menu(callback.message.chat.id, callback)
        return

    telegram_id, balance = user
    text = f"👤 Ваш профиль:\n\n" \
           f"🔹 Имя: {callback.from_user.full_name}\n" \
           f"🔹 Счет: {balance} ₽"

    # Клавиатура с кнопками + назад
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пополнение счета", callback_data="recharge")],
            [InlineKeyboardButton(text="История покупок", callback_data="purchase_history"),
             InlineKeyboardButton(text="Настройки", callback_data="settings")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )
    await send_or_edit(callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()
    await send_main_menu(callback.message.chat.id, callback)

# Callback: история покупок
@dp.callback_query(F.data == "purchase_history")
async def purchase_history_callback(callback: CallbackQuery):
    purchases = get_purchase_history(callback.from_user.id)
    if not purchases:
        # показываем сообщение с кнопкой назад
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]])
        await send_or_edit(callback.message.chat.id, callback, text="У вас пока нет покупок.", reply_markup=keyboard)
        await callback.answer()
        await send_main_menu(callback.message.chat.id, callback)
        return

    text = "🛒 Ваша история покупок:\n\n"
    for purchase_id, product_name, price, created_at in purchases:
        text += f"🔹 {product_name} — {price} ₽ (ID: {purchase_id}, {created_at})\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]])
    await send_or_edit(callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()
    await send_main_menu(callback.message.chat.id, callback)

# Callback: пополнение счета
@dp.callback_query(F.data == "recharge")
async def recharge_callback(callback: CallbackQuery):
    text = "💳 Для пополнения счета перейдите по следующей ссылке:\n\n" \
           "https://example.com/recharge"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]])
    await send_or_edit(callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()
    await send_main_menu(callback.message.chat.id, callback)

# Callback: настройки
@dp.callback_query(F.data == "settings")
async def settings_callback(callback: CallbackQuery):
    text = "⚙️ Настройки пока недоступны. Следите за обновлениями!"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]])
    await send_or_edit(callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()
    await send_main_menu(callback.message.chat.id, callback)

# /admin — открыть админ-панель
@dp.message(Command("admin"))
@admin_only
async def admin_panel_command(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Управление категориями", callback_data="manage_categories"),
             InlineKeyboardButton(text="Управление товарами", callback_data="manage_products")],
            [InlineKeyboardButton(text="Промокоды 🎟️", callback_data="manage_promos")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )
    await send_or_edit(message.chat.id, message, text="Админ-панель:", reply_markup=keyboard)

# Callback: управление категориями
@dp.callback_query(F.data == "manage_categories")
@admin_only
async def manage_categories_callback(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить категорию", callback_data="add_category")],
            [InlineKeyboardButton(text="Удалить категорию", callback_data="delete_category")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )
    await send_or_edit(callback.message.chat.id, callback, text="Управление категориями:", reply_markup=keyboard)
    await callback.answer()

# Callback: добавить категорию
@dp.callback_query(F.data == "add_category")
@admin_only
async def add_category_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("Введите название новой категории:")
    await state.set_state(AdminState.waiting_for_category_name)
    await callback.answer()

@dp.message(AdminState.waiting_for_category_name)
@admin_only
async def process_add_category(message: Message, state: FSMContext):
    category_name = message.text.strip()
    add_category(category_name)
    await message.reply(f"Категория '{category_name}' добавлена.")
    await state.clear()
    # вернуться в админ-панель
    await send_admin_menu(message.chat.id, message)

# Callback: удалить категорию
@dp.callback_query(F.data == "delete_category")
@admin_only
async def delete_category_callback(callback: CallbackQuery):
    categories = get_categories()
    if not categories:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]])
        await send_or_edit(callback.message.chat.id, callback, text="Нет доступных категорий для удаления.", reply_markup=keyboard)
        await callback.answer()
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            *[
                [InlineKeyboardButton(text=category_name, callback_data=f"delete_category_{category_id}")]
                for category_id, category_name in categories
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )
    await send_or_edit(callback.message.chat.id, callback, text="Выберите категорию для удаления:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_category_"))
@admin_only
async def process_delete_category(callback: CallbackQuery):
    try:
        category_id = int(callback.data.split("_", 1)[1])
    except ValueError:
        await callback.answer("Неверный ID категории.", show_alert=True)
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    conn.commit()
    conn.close()

    await send_or_edit(callback.message.chat.id, callback, text="Категория удалена.")
    await callback.answer()
    # вернуться в админ-панель
    await send_admin_menu(callback.message.chat.id, callback)

# Callback: управление товарами
@dp.callback_query(F.data == "manage_products")
@admin_only
async def manage_products_callback(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить товар", callback_data="add_product")],
            [InlineKeyboardButton(text="Удалить товар", callback_data="delete_product")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )
    await send_or_edit(callback.message.chat.id, callback, text="Управление товарами:", reply_markup=keyboard)
    await callback.answer()

# Callback: добавить товар
@dp.callback_query(F.data == "add_product")
@admin_only
async def add_product_callback(callback: CallbackQuery, state: FSMContext):
    categories = get_categories()
    if not categories:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]])
        await send_or_edit(callback.message.chat.id, callback, text="Сначала добавьте хотя бы одну категорию.", reply_markup=keyboard)
        await callback.answer()
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            *[
                [InlineKeyboardButton(text=category_name, callback_data=f"select_category_{category_id}")]
                for category_id, category_name in categories
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )
    await send_or_edit(callback.message.chat.id, callback, text="Выберите категорию для нового товара:", reply_markup=keyboard)
    await state.set_state(AddProductState.waiting_for_category)
    await callback.answer()

@dp.callback_query(F.data.startswith("select_category_"))
@admin_only
async def select_category_for_product(callback: CallbackQuery, state: FSMContext):
    try:
        category_id = int(callback.data.split("_")[2])
    except ValueError:
        await callback.answer("Неверный ID категории.", show_alert=True)
        return

    await state.update_data(category_id=category_id)
    await callback.message.reply("Введите название товара:")
    await state.set_state(AddProductState.waiting_for_name)
    await callback.answer()

@dp.message(AddProductState.waiting_for_name)
async def process_product_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.reply("Введите описание товара:")
    await state.set_state(AddProductState.waiting_for_description)

@dp.message(AddProductState.waiting_for_description)
async def process_product_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.reply("Введите цену товара (в рублях):")
    await state.set_state(AddProductState.waiting_for_price)

@dp.message(AddProductState.waiting_for_price)
async def process_product_price(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
    except ValueError:
        await message.reply("Цена должна быть числом. Попробуйте снова.")
        return

    await state.update_data(price=price)
    await message.reply("Отправьте фотографию товара:")
    await state.set_state(AddProductState.waiting_for_photo)

@dp.message(AddProductState.waiting_for_photo, F.content_type == "photo")
async def process_product_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]  # Берем последнюю (наибольшего размера) фотографию
    photo_dir = "photos"
    photo_path = os.path.join(photo_dir, f"{photo.file_id}.jpg")

    # Создаем директорию, если она не существует
    os.makedirs(photo_dir, exist_ok=True)

    # Сохраняем фотографию локально через bot.download_file
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, destination=photo_path)

    data = await state.get_data()
    add_product(data["name"], data["description"], data["price"], data["category_id"], photo_path)
    await message.reply(f"Товар '{data['name']}' добавлен.")
    await state.clear()
    # если админ — вернуть в админ-панель, иначе в главное меню
    if message.from_user and message.from_user.id in ADMIN_IDS:
        await send_admin_menu(message.chat.id, message)
    else:
        await send_main_menu(message.chat.id, message)

# Callback: удалить товар
@dp.callback_query(F.data == "delete_product")
@admin_only
async def delete_product_callback(callback: CallbackQuery):
    products = get_products()
    if not products:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]])
        await send_or_edit(callback.message.chat.id, callback, text="Нет доступных товаров для удаления.", reply_markup=keyboard)
        await callback.answer()
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            *[
                [InlineKeyboardButton(text=name, callback_data=f"delete_product_{product_id}")]
                for product_id, name, _, _ in products
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )
    await send_or_edit(callback.message.chat.id, callback, text="Выберите товар для удаления:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_product_"))
@admin_only
async def process_delete_product(callback: CallbackQuery):
    try:
        product_id = int(callback.data.split("_")[2])
    except ValueError:
        await callback.answer("Неверный ID товара.", show_alert=True)
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()

    await callback.message.reply("Товар удален.")
    await callback.answer()
    # вернуться в админ-панель
    await send_admin_menu(callback.message.chat.id, callback)

# --- NEW: управление промокодами (админ)
@dp.callback_query(F.data == "manage_promos")
@admin_only
async def manage_promos_callback(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить промокод", callback_data="add_promo")],
            [InlineKeyboardButton(text="Список/Редактирование", callback_data="list_promos")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )
    await send_or_edit(callback.message.chat.id, callback, text="Управление промокодами:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "add_promo")
@admin_only
async def add_promo_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("Введите код промокода (текст):")
    await state.set_state(PromoAdminState.waiting_for_promo_code)
    await callback.answer()

@dp.message(PromoAdminState.waiting_for_promo_code)
@admin_only
async def process_promo_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    await state.update_data(code=code)
    await message.reply("Введите сумму в рублях, которую добавит промокод (целое число):")
    await state.set_state(PromoAdminState.waiting_for_promo_amount)

@dp.message(PromoAdminState.waiting_for_promo_amount)
@admin_only
async def process_promo_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.reply("Нужно число. Попробуйте ещё раз.")
        return
    await state.update_data(amount=amount)
    await message.reply("Введите количество использований (0 — неограниченно):")
    await state.set_state(PromoAdminState.waiting_for_promo_uses)

@dp.message(PromoAdminState.waiting_for_promo_uses)
@admin_only
async def process_promo_uses(message: Message, state: FSMContext):
    try:
        uses = int(message.text.strip())
    except ValueError:
        await message.reply("Нужно число. Попробуйте ещё раз.")
        return
    data = await state.get_data()
    uses_db = None if uses == 0 else uses
    create_promo_in_db(data["code"], data["amount"], uses_db)
    await message.reply(f"Промокод '{data['code']}' добавлен: +{data['amount']} ₽, uses_left={uses_db if uses_db is not None else '∞'}.")
    await state.clear()
    # вернуться в админ-панель
    await send_admin_menu(message.chat.id, message)

@dp.callback_query(F.data == "list_promos")
@admin_only
async def list_promos_callback(callback: CallbackQuery):
    promos = get_promos_from_db()
    if not promos:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="manage_promos")]])
        await send_or_edit(callback.message.chat.id, callback, text="Промокодов пока нет.", reply_markup=keyboard)
        await callback.answer()
        return

    # Показать краткий список с кнопками для каждого промокода: редактировать/удалить/вкл/выкл
    inline = []
    for pid, code, amount, uses_left, active, created_at in promos:
        label = f"{code} — +{amount}₽ — uses: {uses_left if uses_left is not None else '∞'} — {'ON' if active==1 else 'OFF'}"
        inline.append([InlineKeyboardButton(text=label, callback_data=f"promo_info_{pid}")])
        inline.append([InlineKeyboardButton(text="Вкл/Выкл", callback_data=f"toggle_promo_{pid}"),
                       InlineKeyboardButton(text="Удалить", callback_data=f"delete_promo_{pid}")])
    inline.append([InlineKeyboardButton(text="◀️ Назад", callback_data="manage_promos")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=inline)
    await send_or_edit(callback.message.chat.id, callback, text="Список промокодов:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("promo_info_"))
@admin_only
async def promo_info_callback(callback: CallbackQuery):
    try:
        pid = int(callback.data.split("_")[2])
    except ValueError:
        await callback.answer("Неверный ID.", show_alert=True)
        return
    promo = get_promo_by_id(pid)
    if not promo:
        await callback.answer("Промокод не найден.", show_alert=True)
        return
    pid, code, amount, uses_left, active = promo
    text = f"Код: {code}\nСумма: {amount} ₽\nИспользований осталось: {uses_left if uses_left is not None else '∞'}\nСтатус: {'активен' if active==1 else 'отключён'}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Вкл/Выкл", callback_data=f"toggle_promo_{pid}"), InlineKeyboardButton(text="Удалить", callback_data=f"delete_promo_{pid}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="list_promos")]
    ])
    await send_or_edit(callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_promo_"))
@admin_only
async def delete_promo_callback(callback: CallbackQuery):
    try:
        pid = int(callback.data.split("_")[2])
    except ValueError:
        await callback.answer("Неверный ID.", show_alert=True)
        return
    delete_promo_from_db(pid)
    await callback.answer("Промокод удалён.")
    await send_or_edit(callback.message.chat.id, callback, text="Промокод удалён.")
    # вернуться в админ-панель
    await send_admin_menu(callback.message.chat.id, callback)
    
@dp.callback_query(F.data.startswith("toggle_promo_"))
@admin_only
async def toggle_promo_callback(callback: CallbackQuery):
    try:
        pid = int(callback.data.split("_")[2])
    except ValueError:
        await callback.answer("Неверный ID.", show_alert=True)
        return
    new_state = toggle_promo_active(pid)
    if new_state is None:
        await callback.answer("Промокод не найден.", show_alert=True)
        return
    await callback.answer(f"Новый статус: {'активен' if new_state==1 else 'отключён'}")
    await send_or_edit(callback.message.chat.id, callback, text="Статус промокода изменён.")
    # вернуться в админ-панель
    await send_admin_menu(callback.message.chat.id, callback)

# --- NEW: пользовательское применение промокода
@dp.callback_query(F.data == "promo")
async def user_promo_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("Введите ваш промокод (текст):")
    await state.set_state(UserPromoState.waiting_for_code)
    await callback.answer()

@dp.message(UserPromoState.waiting_for_code)
async def apply_promo_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    promo = get_promo_by_code(code)
    if not promo:
        await message.reply("Промокод не найден или неверен.")
        await state.clear()
        await send_or_edit(message.chat.id, message, text="Добро пожаловать! Выберите действие:", reply_markup=start_menu_keyboard())
        return
    pid, pcode, amount, uses_left, active = promo
    if active != 1:
        await message.reply("Этот промокод отключён.")
        await state.clear()
        await send_or_edit(message.chat.id, message, text="Добро пожаловать! Выберите действие:", reply_markup=start_menu_keyboard())
        return
    if uses_left is not None and uses_left <= 0:
        await message.reply("У этого промокода закончилось количество использований.")
        await state.clear()
        await send_or_edit(message.chat.id, message, text="Добро пожаловать! Выберите действие:", reply_markup=start_menu_keyboard())
        return

    # применяем: добавляем баланс пользователю
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE telegram_id = ?", (amount, message.from_user.id))
    if cursor.rowcount == 0:
        # если пользователя нет в users — создаём запись (вдруг)
        cursor.execute("INSERT OR REPLACE INTO users(telegram_id, balance) VALUES (?, ?)", (message.from_user.id, amount))
    # уменьшаем uses_left если не NULL
    if uses_left is not None:
        new_uses = uses_left - 1
        cursor.execute("UPDATE promocodes SET uses_left = ? WHERE id = ?", (new_uses, pid))
        if new_uses <= 0:
            cursor.execute("UPDATE promocodes SET active = 0 WHERE id = ?", (pid,))
    conn.commit()
    conn.close()

    await message.reply(f"Промокод применён! Вам зачислено {amount} ₽.")
    await state.clear()
    await send_or_edit(message.chat.id, message, text="Добро пожаловать! Выберите действие:", reply_markup=start_menu_keyboard())

# --- NEW: helpers для промокодов (создание таблицы и CRUD)
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

def update_promo_uses(pid: int, uses_left):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE promocodes SET uses_left = ? WHERE id = ?", (uses_left if uses_left is not None else None, pid))
    conn.commit()
    conn.close()

def update_promo_amount(pid: int, amount: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE promocodes SET amount = ? WHERE id = ?", (amount, pid))
    conn.commit()
    conn.close()

# helper: собрать стартовую клавиатуру (используется в нескольких местах)
def start_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Каталог 🛒", callback_data="catalog")],
            [InlineKeyboardButton(text="Пополнение 🏦", callback_data="recharge"),
             InlineKeyboardButton(text="Помощь ⁉️", callback_data="help")],
            [InlineKeyboardButton(text="Промокоды 🎟️", callback_data="promo"),
             InlineKeyboardButton(text="Мой профиль 👤", callback_data="profile")]
        ]
    )

# helper: собрать админ-клавиатуру
def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Управление категориями", callback_data="manage_categories"),
             InlineKeyboardButton(text="Управление товарами", callback_data="manage_products")],
            [InlineKeyboardButton(text="Промокоды 🎟️", callback_data="manage_promos")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )

# helper: отправить/редактировать главное меню
async def send_main_menu(chat_id: int, source_obj):
    await send_or_edit(chat_id, source_obj, text="Добро пожаловать! Выберите действие:", reply_markup=start_menu_keyboard())

# helper: отправить/редактировать админ-панель
async def send_admin_menu(chat_id: int, source_obj):
    await send_or_edit(chat_id, source_obj, text="Админ-панель:", reply_markup=admin_menu_keyboard())

# Обработчик кнопки "Назад" — возвращает в админ-панель для админов или в главное меню для всех остальных
@dp.callback_query(F.data == "back_to_start")
async def back_to_start_callback(callback: CallbackQuery):
    try:
        if callback.from_user and callback.from_user.id in ADMIN_IDS:
            await send_admin_menu(callback.message.chat.id, callback)
        else:
            await send_main_menu(callback.message.chat.id, callback)
    except Exception:
        # в случае ошибки — отправим простое текстовое главное меню
        await send_or_edit(callback.message.chat.id, callback, text="Добро пожаловать! Выберите действие:", reply_markup=start_menu_keyboard())
    await callback.answer()

# Запуск бота
async def main():
    init_db()
    ensure_promos_table()   # --- NEW: создаём таблицу промокодов при старте
    logging.info("Bot work../")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
