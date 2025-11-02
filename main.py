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

from db_helpers import (
    init_db, add_user, get_categories, add_category, add_product,
    get_products_by_category, get_products, get_product_by_id,
    create_purchase, get_user_profile, get_purchase_history, DB_PATH  # Импортируем DB_PATH
)

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYSTALPAY_SECRET = os.getenv("CRYSTALPAY_SECRET")
CRYSTALPAY_MERCHANT_ID = os.getenv("CRYSTALPAY_MERCHANT_ID")
CRYSTALPAY_API_URL = "https://api.crystalpay.io/v1/"

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

# Вспомогательная функция: редактировать существующее сообщение в чате или отправить новое и сохранить id
async def send_or_edit(chat_id: int, source_obj, text: str = None, photo_path: str = None,
                       reply_markup: InlineKeyboardMarkup = None, parse_mode: str = None):
    """
    Попытаться отредактировать предыдущее сообщение в чате (last_message[chat_id]).
    Если не получилось — попытаться отредактировать исходное сообщение (source_obj.message_id).
    Если и это не удалось — отправить новое сообщение/фото и сохранить его id.
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
        return

    _, name, _, price = product
    purchase_id = create_purchase(callback.from_user.id, product_id)

    # Ссылка на оплату через CrystalPay
    payment_link = f"{CRYSTALPAY_API_URL}invoice?merchant_id={CRYSTALPAY_MERCHANT_ID}&amount={price}&order_id={purchase_id}&secret={CRYSTALPAY_SECRET}"
    await callback.message.reply(f"Для оплаты товара '{name}' на сумму {price} ₽ перейдите по ссылке: {payment_link}")
    await callback.answer()

# Callback: показать профиль пользователя
@dp.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery):
    user = get_user_profile(callback.from_user.id)
    if not user:
        await send_or_edit(callback.message.chat.id, callback, text="Ваш профиль не найден.")
        await callback.answer()
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

# Callback: история покупок
@dp.callback_query(F.data == "purchase_history")
async def purchase_history_callback(callback: CallbackQuery):
    purchases = get_purchase_history(callback.from_user.id)
    if not purchases:
        # показываем сообщение с кнопкой назад
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]])
        await send_or_edit(callback.message.chat.id, callback, text="У вас пока нет покупок.", reply_markup=keyboard)
        await callback.answer()
        return

    text = "🛒 Ваша история покупок:\n\n"
    for purchase_id, product_name, price, created_at in purchases:
        text += f"🔹 {product_name} — {price} ₽ (ID: {purchase_id}, {created_at})\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]])
    await send_or_edit(callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()

# Callback: пополнение счета
@dp.callback_query(F.data == "recharge")
async def recharge_callback(callback: CallbackQuery):
    text = "💳 Для пополнения счета перейдите по следующей ссылке:\n\n" \
           "https://example.com/recharge"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]])
    await send_or_edit(callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()

# Callback: настройки
@dp.callback_query(F.data == "settings")
async def settings_callback(callback: CallbackQuery):
    text = "⚙️ Настройки пока недоступны. Следите за обновлениями!"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]])
    await send_or_edit(callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()

# /admin — открыть админ-панель
@dp.message(Command("admin"))
async def admin_panel_command(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Управление категориями", callback_data="manage_categories")],
            [InlineKeyboardButton(text="Управление товарами", callback_data="manage_products")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )
    await send_or_edit(message.chat.id, message, text="Админ-панель:", reply_markup=keyboard)

# Callback: управление категориями
@dp.callback_query(F.data == "manage_categories")
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
async def add_category_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("Введите название новой категории:")
    await state.set_state(AdminState.waiting_for_category_name)
    await callback.answer()

@dp.message(AdminState.waiting_for_category_name)
async def process_add_category(message: Message, state: FSMContext):
    category_name = message.text.strip()
    add_category(category_name)
    await message.reply(f"Категория '{category_name}' добавлена.")
    await state.clear()

# Callback: удалить категорию
@dp.callback_query(F.data == "delete_category")
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
async def process_delete_category(callback: CallbackQuery):
    try:
        category_id = int(callback.data.split("_")[2])
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

# Callback: управление товарами
@dp.callback_query(F.data == "manage_products")
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

# Callback: удалить товар
@dp.callback_query(F.data == "delete_product")
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

# Callback: управление товарами
@dp.callback_query(F.data == "manage_products")
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

# Callback: удалить товар
@dp.callback_query(F.data == "delete_product")
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

# Callback: показать категории (обработчик для кнопки "Назад")
@dp.callback_query(F.data == "back_to_start")
async def back_to_start_callback(callback: CallbackQuery):
    # Показываем стартовое меню (аналог /start) — заменяем текущее сообщение
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Каталог 🛒", callback_data="catalog")],
            [InlineKeyboardButton(text="Пополнение ??", callback_data="recharge"),
             InlineKeyboardButton(text="Помощь ??", callback_data="help")],
            [InlineKeyboardButton(text="Промокоды ??", callback_data="promo"),
             InlineKeyboardButton(text="Мой профиль 👤", callback_data="profile")]
        ]
    )
    await send_or_edit(callback.message.chat.id, callback, text="Добро пожаловать! Выберите действие:", reply_markup=keyboard)
    await callback.answer()

# Запуск бота
async def main():
    init_db()
    logging.info("Bot work../")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
