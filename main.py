import os
import asyncio
import sqlite3
import traceback  # Добавляем импорт sqlite3
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
from typing import Optional, Any, List

from db_helpers import (
    init_db, add_user, get_categories, add_category, add_product,
    get_products_by_category, get_products, get_product_by_id,
    create_purchase, get_user_profile, get_purchase_history, DB_PATH  # Импортируем DB_PATH
)

# Добавляем импорт клиента оплат (используем реальную библиотеку, если она доступна)
try:
    from AsyncPayments.cryptoBot import AsyncCryptoBot  # type: ignore
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

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")  # Ожидается строка вида "12345678,23456789"
try:
    ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()}
except Exception:
    ADMIN_IDS = set()

# добавляем переменные для крипто-платежей
CRYPTOPAY_TOKEN = os.getenv("CRYPTOPAY_TOKEN", "")
USDT2RUB_RATE = float(os.getenv("USDT2RUB_RATE", "80"))

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
    waiting_for_autodelivery_choice = State()    # new: ask admin yes/no
    waiting_for_autodelivery_content = State()   # new: accept text or file

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

# Изменение обработчика сохранения фото товара (последний определённый в файле) --
# после добавления товара для админа спрашиваем про автовыдачу и сохраняем product_id в state
@dp.message(AddProductState.waiting_for_photo, F.content_type == "photo")
async def process_product_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    photo_dir = "photos"
    photo_path = os.path.join(photo_dir, f"{photo.file_id}.jpg")
    os.makedirs(photo_dir, exist_ok=True)
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, destination=photo_path)

    data = await state.get_data()
    # сохраняем товар в БД
    add_product(data["name"], data["description"], data["price"], data["category_id"], photo_path)

    # попытка найти id добавленного товара (по name, price, category_id и photo_path)
    products = get_products_by_category(data["category_id"])
    product_id = None
    for p in products[::-1]:  # перебираем с конца, чтобы взять последний добавленный
        pid = p[0]
        pname = p[1]
        pprice = p[3] if len(p) > 3 else None
        pphoto = p[4] if len(p) > 4 else None
        if pname == data["name"] and pprice == data["price"] and (pphoto == photo_path or pphoto is None):
            product_id = pid
            break

    await message.reply(f"Товар '{data['name']}' добавлен. ID={product_id if product_id else 'неизвестен'}.")

    # если админ — спрашиваем про автовыдачу, иначе завершаем
    if message.from_user and message.from_user.id in ADMIN_IDS:
        if product_id:
            await state.update_data(product_id=product_id)
            await message.reply("Включить автовыдачу для этого товара? (да/нет)")
            await state.set_state(AddProductState.waiting_for_autodelivery_choice)
            return
        else:
            # не нашли id — всё равно возвращаем в админ-панель
            await send_admin_menu(message.chat.id, message)
            await state.clear()
            return
    else:
        await state.clear()
        await send_main_menu(message.chat.id, message)

# Обработчик выбора включить/выключить автовыдачу
@dp.message(AddProductState.waiting_for_autodelivery_choice)
async def process_autodelivery_choice(message: Message, state: FSMContext):
    ans = message.text.strip().lower()
    data = await state.get_data()
    product_id = data.get("product_id")
    if not product_id:
        await message.reply("Не удалось определить товар. Возвращаю в админ-панель.")
        await state.clear()
        await send_admin_menu(message.chat.id, message)
        return

    if ans in ("да", "yes", "y"):
        await message.reply("Отправьте текстовое содержимое автовыдачи или файл (документ/фото).")
        await state.set_state(AddProductState.waiting_for_autodelivery_content)
        return
    else:
        # записать выключенную автодоставку
        create_autodelivery(product_id, 0, None, None)
        await message.reply("Автовыдача отключена для этого товара.")
        await state.clear()
        await send_admin_menu(message.chat.id, message)

# Обработчик текстовой автодоставки
@dp.message(AddProductState.waiting_for_autodelivery_content, F.content_type == "text")
async def process_autodelivery_text(message: Message, state: FSMContext):
    content = message.text.strip()
    data = await state.get_data()
    product_id = data.get("product_id")
    if not product_id:
        await message.reply("Не найден товар. Отмена.")
        await state.clear()
        await send_admin_menu(message.chat.id, message)
        return
    create_autodelivery(product_id, 1, content, None)
    await message.reply("Автовыдача настроена (текст).")
    await state.clear()
    await send_admin_menu(message.chat.id, message)

# Обработчик файловой автодоставки (photo/document)
@dp.message(AddProductState.waiting_for_autodelivery_content, F.content_type.in_(["document", "photo"]))
async def process_autodelivery_file(message: Message, state: FSMContext):
    data = await state.get_data()
    product_id = data.get("product_id")
    if not product_id:
        await message.reply("Не найден товар. Отмена.")
        await state.clear()
        await send_admin_menu(message.chat.id, message)
        return

    files_dir = "autodeliver_files"
    os.makedirs(files_dir, exist_ok=True)
    file_path = None

    if message.content_type == "photo":
        ph = message.photo[-1]
        file = await bot.get_file(ph.file_id)
        file_path = os.path.join(files_dir, f"{ph.file_id}.jpg")
        await bot.download_file(file.file_path, destination=file_path)
    elif message.content_type == "document":
        doc = message.document
        file = await bot.get_file(doc.file_id)
        # сохраняем с оригинальным именем для удобства
        file_path = os.path.join(files_dir, f"{doc.file_id}_{doc.file_name}")
        await bot.download_file(file.file_path, destination=file_path)

    create_autodelivery(product_id, 1, None, file_path)
    await message.reply("Автовыдача настроена (файл).")
    await state.clear()
    await send_admin_menu(message.chat.id, message)

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

# Заменён обработчик покупки: теперь отправляем новое сообщение с реквизитами (не редактируемое)
@dp.callback_query(F.data.startswith("buy_"))
async def handle_buy_callback(callback: CallbackQuery):
    try:
        product_id = int(callback.data.split("_", 1)[1])
    except ValueError:
        await callback.answer("Неверный ID товара.", show_alert=True)
        return

    product = get_product_by_id(product_id)
    if not product:
        await send_or_edit(callback.message.chat.id, callback, text="Товар не найден.")
        await callback.answer()
        await send_main_menu(callback.message.chat.id, callback)
        return

    _, name, _, price = product

    # создаём запись заказа
    purchase_id = create_purchase(callback.from_user.id, product_id)

    # # если включена автодоставка — выполняем её сразу
    # autodel = get_autodelivery_for_product(product_id)
    # if autodel and autodel[1] == 1:
    #     _, _, content_text, file_path = autodel
    #     try:
    #         if content_text:
    #             await bot.send_message(chat_id=callback.from_user.id, text=f"Автовыдача по заказу {purchase_id} — {name}:\n\n{content_text}")
    #         elif file_path:
    #             ext = os.path.splitext(file_path)[1].lower()
    #             if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
    #                 await bot.send_photo(chat_id=callback.from_user.id, photo=FSInputFile(file_path), caption=f"Автовыдача по заказу {purchase_id} — {name}")
    #             else:
    #                 await bot.send_document(chat_id=callback.from_user.id, document=FSInputFile(file_path), caption=f"Автовыдача по заказу {purchase_id} — {name}")
    #         await callback.message.reply(f"Заказ создан (ID: {purchase_id}). Автовыдача выполнена.")
    #     except Exception:
    #         await callback.message.reply(f"Заказ создан (ID: {purchase_id}). Возникла ошибка при автодоставке — свяжитесь с поддержкой.")
    #     await callback.answer()
    #     await send_main_menu(callback.message.chat.id, callback)
    #     return

    # иначе — создаём инвойс через CryptoPay
    invoice = await create_cryptopay_invoice(amount_rub=price, description=f"Order {purchase_id}: {name}")
    if invoice:
        invoice_id, pay_url = invoice
        # сохраняем запись о платеже и получаем payment_id
        payment_id = create_payment_entry(purchase_id=purchase_id, invoice_id=invoice_id, pay_url=pay_url, method="crypto")

        # формируем текст с реквизитами (новое сообщение)
        text = (
            f"💳 Реквизиты для оплаты заказа #{purchase_id}\n\n"
            f"Товар: {name}\n"
            f"Сумма: {price} ₽ (~{round(float(price)/max(1.0, float(USDT2RUB_RATE)),6)} USDT)\n"
            f"Invoice ID: {invoice_id}\n\n"
            "Нажмите кнопку «Оплатить» чтобы перейти на страницу оплаты. После оплаты нажмите «Проверить оплату»."
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", url=pay_url)],
            [InlineKeyboardButton(text="Проверить оплату", callback_data=f"checkpay_{payment_id}")],
            [InlineKeyboardButton(text="Отменить заказ", callback_data=f"cancel_buy_{purchase_id}")]
        ])

        # отправляем новое сообщение (не edit) чтобы пользователь увидел окно оплаты
        await bot.send_message(chat_id=callback.from_user.id, text=text, reply_markup=keyboard)
    else:
        await bot.send_message(chat_id=callback.from_user.id, text="Не удалось создать платёжную ссылку. Свяжитесь с поддержкой.")
    await callback.answer()

# Callback: проверка статуса платежа (по payment_id)
@dp.callback_query(F.data.startswith("checkpay_"))
async def check_payment_callback(callback: CallbackQuery):
    try:
        _, payment_id_str = callback.data.split("_", 1)
        payment_id = int(payment_id_str)
    except Exception:
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    payment = get_payment_by_id(payment_id)
    if not payment:
        await callback.answer("Платёж не найден.", show_alert=True)
        return
    _, purchase_id, invoice_id, pay_url, method, status = payment

    # проверяем через invoice_id (если есть)
    if invoice_id:
        status_remote = await check_crypto_invoice_status(invoice_id)
    else:
        status_remote = "not"

    if status_remote == "paid":
        # обновляем статус по id записи платежа
        update_payment_status_by_id(payment_id, "paid")
        mark_purchase_paid(purchase_id)

        # получаем данные покупки для доставки: user_id и product_id
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT user_id, product_id FROM purchases WHERE id = ?", (purchase_id,))
            row = cur.fetchone()
            conn.close()
        except Exception:
            row = None

        if row:
            user_id, product_id = row
        else:
            user_id = None
            product_id = None

        # выполняем автодоставку, если есть
        if product_id and user_id:
            autodel = get_autodelivery_for_product(product_id)
            if autodel and autodel[1] == 1:
                _, _, content_text, file_path = autodel
                try:
                    if content_text:
                        await bot.send_message(chat_id=user_id, text=f"Оплата принята. Автовыдача по заказу {purchase_id}:\n\n{content_text}")
                    elif file_path:
                        ext = os.path.splitext(file_path)[1].lower()
                        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                            await bot.send_photo(chat_id=user_id, photo=FSInputFile(file_path), caption=f"Оплата принята. Автовыдача по заказу {purchase_id}")
                        else:
                            await bot.send_document(chat_id=user_id, document=FSInputFile(file_path), caption=f"Оплата принята. Автовыдача по заказу {purchase_id}")
                    await bot.send_message(chat_id=callback.from_user.id, text=f"Оплата подтверждена, заказ {purchase_id} выполнен.")
                except Exception:
                    await bot.send_message(chat_id=callback.from_user.id, text=f"Оплата подтверждена, но произошла ошибка при доставке. Свяжитесь с поддержкой.")
                await callback.answer()
                return

        # если автодоставки нет или не удалось определить продукт — просто подтверждаем оплату
        await bot.send_message(chat_id=callback.from_user.id, text=f"Оплата принята, заказ {purchase_id} отмечен как оплаченный. Администратор обработает заказ.")
        await callback.answer()
    else:
        await bot.send_message(chat_id=callback.from_user.id, text="Платёж не найден / не оплачен. Попробуйте снова позднее.")
        await callback.answer()

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
    # Исправлено: корректный тернарный оператор
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

# --- NEW: таблица payments и CRUD
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
    return pid  # возвращаем id записи платежа

def get_payment_by_id(payment_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, purchase_id, invoice_id, pay_url, method, status FROM payments WHERE id = ?", (payment_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def update_payment_status(invoice_id: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE payments SET status = ? WHERE invoice_id = ?", (status, invoice_id))
    conn.commit()
    conn.close()

# --- NEW helper: обновление статуса платежа по payment_id (используем в callback-проверке)
def update_payment_status_by_id(payment_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))
    conn.commit()
    conn.close()

# --- NEW: helper для пометки покупки как оплаченной
def mark_purchase_paid(purchase_id: int):
    """
    Помечает покупку как оплаченную в таблице purchases (если поле status существует).
    Функция безопасно игнорирует ошибки, если структура БД другая.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("UPDATE purchases SET status = 'paid' WHERE id = ?", (purchase_id,))
        conn.commit()
        conn.close()
    except Exception:
        # игнорируем — таблица/поле может отсутствовать или быть другой структуры
        pass

# --- NEW: Crypto client и функции для создания/проверки инвойсов
crypto_client: Optional[Any] = None

def _get_crypto_client():
    global crypto_client
    print(CRYPTOPAY_TOKEN, CRYPTO_AVAILABLE)
    if crypto_client is None and CRYPTOPAY_TOKEN and CRYPTO_AVAILABLE:
        try:
            is_testnet = os.getenv("CRYPTOPAY_TESTNET", "1") not in ("0", "false", "False")
            crypto_client = AsyncCryptoBot(CRYPTOPAY_TOKEN, is_testnet=is_testnet)
        except Exception:
            print(traceback.format_exc())
            crypto_client = None
    return crypto_client

async def create_cryptopay_invoice(amount_rub: float, description: str = "") -> Optional[tuple]:
    """
    Создаёт инвойс через AsyncCryptoBot и возвращает (invoice_id, pay_url) или None.
    amount_rub — сумма в рублях, конвертируется в USDT по USDT2RUB_RATE.
    """
    client = _get_crypto_client()
    if not client:
        print("CryptoPay client not available.")
        return None
    try:
        rate = float(USDT2RUB_RATE) if USDT2RUB_RATE else 80.0
        amount_usdt = max(0.000001, round(float(amount_rub) / rate, 6))
        invoice = await client.create_invoice(amount=amount_usdt, currency_type="crypto", asset="USDT", description=description)
        # invoice может быть объектом или dict
        invoice_id = getattr(invoice, "invoice_id", None) or (invoice.get("invoice_id") if isinstance(invoice, dict) else None)
        pay_url = getattr(invoice, "pay_url", None) or (invoice.get("pay_url") if isinstance(invoice, dict) else None)
        return (invoice_id, pay_url)
    except Exception:
        print(traceback.format_exc())
        return None

async def check_crypto_invoice_status(invoice_id: str) -> str:
    """
    Проверяет статус инвойса по invoice_id. Возвращает 'paid' или 'not'.
    """
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

# --- NEW: таблица автодоставки и helpers
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

# Запуск бота
async def main():
    init_db()
    ensure_promos_table()
    ensure_autodeliveries_table()
    ensure_payments_table()   # --- NEW: таблица платежей
    logging.info("Bot work../")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
