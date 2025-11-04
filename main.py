import os
import asyncio
import sqlite3
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext

from config import BOT_TOKEN, ADMIN_IDS, USDT2RUB_RATE
from decorators import admin_only
from keyboards import admin_menu_keyboard, main_menu_keyboard
from utils import send_or_edit
from states import AddProductState, PromoAdminState, UserPromoState
from database import (
    ensure_promos_table, create_promo_in_db, get_promos_from_db, get_promo_by_id,
    delete_promo_from_db, toggle_promo_active, get_promo_by_code,
    ensure_payments_table, create_payment_entry, get_payment_by_id, update_payment_status_by_id, mark_purchase_paid,
    ensure_autodeliveries_table, create_autodelivery, get_autodelivery_for_product
)
from crypto_payments import create_cryptopay_invoice, check_crypto_invoice_status
from db_helpers import (
    init_db, add_user, get_categories, add_category, add_product,
    get_products_by_category, get_product_by_id, create_purchase, DB_PATH
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

logging.basicConfig(level=logging.INFO)

@dp.message(Command("start"))
async def start_command(message: Message):
    add_user(message.from_user.id)
    uid = message.from_user.id if message.from_user else None
    keyboard = main_menu_keyboard(uid)
    await send_or_edit(bot, message.chat.id, message, text="Добро пожаловать! Выберите действие:", reply_markup=keyboard)

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
    photo = message.photo[-1]
    photo_dir = "photos"
    photo_path = os.path.join(photo_dir, f"{photo.file_id}.jpg")
    os.makedirs(photo_dir, exist_ok=True)
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, destination=photo_path)

    data = await state.get_data()
    add_product(data["name"], data["description"], data["price"], data["category_id"], photo_path)

    products = get_products_by_category(data["category_id"])
    product_id = None
    for p in products[::-1]:
        pid = p[0]
        pname = p[1]
        pprice = p[3] if len(p) > 3 else None
        pphoto = p[4] if len(p) > 4 else None
        if pname == data["name"] and pprice == data["price"] and (pphoto == photo_path or pphoto is None):
            product_id = pid
            break

    await message.reply(f"Товар '{data['name']}' добавлен. ID={product_id if product_id else 'неизвестен'}.")

    if message.from_user and message.from_user.id in ADMIN_IDS:
        if product_id:
            await state.update_data(product_id=product_id)
            await message.reply("Включить автовыдачу для этого товара? (да/нет)")
            await state.set_state(AddProductState.waiting_for_autodelivery_choice)
            return
        else:
            await send_admin_menu(message.chat.id, message)
            await state.clear()
            return
    else:
        await state.clear()
        await send_main_menu(message.chat.id, message)

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
        create_autodelivery(product_id, 0, None, None)
        await message.reply("Автовыдача отключена для этого товара.")
        await state.clear()
        await send_admin_menu(message.chat.id, message)

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
        file_path = os.path.join(files_dir, f"{doc.file_id}_{doc.file_name}")
        await bot.download_file(file.file_path, destination=file_path)

    create_autodelivery(product_id, 1, None, file_path)
    await message.reply("Автовыдача настроена (файл).")
    await state.clear()
    await send_admin_menu(message.chat.id, message)

@dp.callback_query(F.data == "catalog")
async def catalog_callback(callback: CallbackQuery):
    categories = get_categories()
    if not categories:
        await send_or_edit(bot, callback.message.chat.id, callback, text="Каталог пуст.")
        await callback.answer()
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            *[
                [InlineKeyboardButton(text=category_name, callback_data=f"category_{category_id}")]
                for category_id, category_name in categories
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )
    await send_or_edit(bot, callback.message.chat.id, callback, text="Выберите категорию:", reply_markup=keyboard)
    await callback.answer()

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

    await show_product(callback, products, 0, category_id)

async def show_product(callback: CallbackQuery, products, index, category_id):
    product_id, name, description, price, photo_path = products[index]
    text = f"🔹 <b>{name}</b>\n💬 {description}\n💰 Цена: {price} ₽"

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
                InlineKeyboardButton(text="◀️ Назад", callback_data="start_command")
            ]
        ]
    )

    chat_id = callback.message.chat.id
    await send_or_edit(bot, chat_id, callback, text=text, photo_path=photo_path, reply_markup=keyboard, parse_mode="HTML")
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

@dp.callback_query(F.data.startswith("buy_"))
async def handle_buy_callback(callback: CallbackQuery):
    try:
        product_id = int(callback.data.split("_", 1)[1])
    except ValueError:
        await callback.answer("Неверный ID товара.", show_alert=True)
        return

    product = get_product_by_id(product_id)
    if not product:
        await send_or_edit(bot, callback.message.chat.id, callback, text="Товар не найден.")
        await callback.answer()
        await send_main_menu(callback.message.chat.id, callback)
        return

    _, name, _, price = product
    purchase_id = create_purchase(callback.from_user.id, product_id)

    invoice = await create_cryptopay_invoice(amount_rub=price, description=f"Order {purchase_id}: {name}")
    if invoice:
        invoice_id, pay_url = invoice
        payment_id = create_payment_entry(purchase_id=purchase_id, invoice_id=invoice_id, pay_url=pay_url, method="crypto")

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

        await bot.send_message(chat_id=callback.from_user.id, text=text, reply_markup=keyboard)
    else:
        await bot.send_message(chat_id=callback.from_user.id, text="Не удалось создать платёжную ссылку. Свяжитесь с поддержкой.")
    await callback.answer()

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

    if invoice_id:
        status_remote = await check_crypto_invoice_status(invoice_id)
    else:
        status_remote = "not"

    if status_remote == "paid":
        update_payment_status_by_id(payment_id, "paid")
        mark_purchase_paid(purchase_id)

        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT user_id, product_id FROM purchases WHERE id = ?", (purchase_id,))
            row = cur.fetchone()
            conn.close()
        except Exception:
            row = None

        owner_id = None
        product_id = None
        if row:
            owner_id, product_id = row

        delivered = False
        if product_id and owner_id:
            autodel = get_autodelivery_for_product(product_id)
            if autodel and autodel[1] == 1:
                try:
                    _, _, content_text, file_path = autodel
                    if content_text:
                        await bot.send_message(chat_id=owner_id, text=f"Оплата принята. Автовыдача по заказу {purchase_id}:\n\n{content_text}")
                        delivered = True
                    elif file_path:
                        ext = os.path.splitext(file_path)[1].lower()
                        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                            await bot.send_photo(chat_id=owner_id, photo=FSInputFile(file_path), caption=f"Оплата принята. Автовыдача по заказу {purchase_id}")
                        else:
                            await bot.send_document(chat_id=owner_id, document=FSInputFile(file_path), caption=f"Оплата принята. Автовыдача по заказу {purchase_id}")
                        delivered = True
                except Exception:
                    try:
                        await bot.send_message(chat_id=callback.from_user.id, text=f"Оплата принята, но ошибка при автодовке владельцу заказа {purchase_id}. Свяжитесь с поддержкой.")
                    except Exception:
                        pass

        try:
            if delivered:
                if callback.from_user and callback.from_user.id != owner_id:
                    await bot.send_message(chat_id=callback.from_user.id, text=f"Оплата подтверждена. Автовыдача доставлена пользователю (ID: {owner_id}) по заказу #{purchase_id}.")
            else:
                await bot.send_message(chat_id=callback.from_user.id, text=f"Оплата принята, заказ #{purchase_id} отмечен как оплаченный. Администратор обработает заказ.")
        except Exception:
            pass

        await callback.answer()
    else:
        await bot.send_message(chat_id=callback.from_user.id, text="Платёж не найден / не оплачен. Попробуйте снова позднее.")
        await callback.answer()

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
    await send_or_edit(bot, callback.message.chat.id, callback, text="Управление промокодами:", reply_markup=keyboard)
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
    await send_admin_menu(message.chat.id, message)

@dp.callback_query(F.data == "list_promos")
@admin_only
async def list_promos_callback(callback: CallbackQuery):
    promos = get_promos_from_db()
    if not promos:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="manage_promos")]])
        await send_or_edit(bot, callback.message.chat.id, callback, text="Промокодов пока нет.", reply_markup=keyboard)
        await callback.answer()
        return

    inline = []
    for pid, code, amount, uses_left, active, created_at in promos:
        label = f"{code} — +{amount}₽ — uses: {uses_left if uses_left is not None else '∞'} — {'ON' if active==1 else 'OFF'}"
        inline.append([InlineKeyboardButton(text=label, callback_data=f"promo_info_{pid}")])
        inline.append([InlineKeyboardButton(text="Вкл/Выкл", callback_data=f"toggle_promo_{pid}"),
                       InlineKeyboardButton(text="Удалить", callback_data=f"delete_promo_{pid}")])
    inline.append([InlineKeyboardButton(text="◀️ Назад", callback_data="manage_promos")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=inline)
    await send_or_edit(bot, callback.message.chat.id, callback, text="Список промокодов:", reply_markup=keyboard)
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
    await send_or_edit(bot, callback.message.chat.id, callback, text=text, reply_markup=keyboard)
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
    await send_or_edit(bot, callback.message.chat.id, callback, text="Промокод удалён.")
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
    await send_or_edit(bot, callback.message.chat.id, callback, text="Статус промокода изменён.")
    await send_admin_menu(callback.message.chat.id, callback)

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
        await send_main_menu(message.chat.id, message)
        return
    pid, pcode, amount, uses_left, active = promo
    if active != 1:
        await message.reply("Этот промокод отключён.")
        await state.clear()
        await send_main_menu(message.chat.id, message)
        return
    if uses_left is not None and uses_left <= 0:
        await message.reply("У этого промокода закончилось количество использований.")
        await state.clear()
        await send_main_menu(message.chat.id, message)
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE telegram_id = ?", (amount, message.from_user.id))
    if cursor.rowcount == 0:
        cursor.execute("INSERT OR REPLACE INTO users(telegram_id, balance) VALUES (?, ?)", (message.from_user.id, amount))
    if uses_left is not None:
        new_uses = uses_left - 1
        cursor.execute("UPDATE promocodes SET uses_left = ? WHERE id = ?", (new_uses, pid))
        if new_uses <= 0:
            cursor.execute("UPDATE promocodes SET active = 0 WHERE id = ?", (pid,))
    conn.commit()
    conn.close()

    await message.reply(f"Промокод применён! Вам зачислено {amount} ₽.")
    await state.clear()
    await send_main_menu(message.chat.id, message)

@dp.message(Command("admin"))
async def admin_command(message: Message):
    if message.from_user and message.from_user.id in ADMIN_IDS:
        await send_admin_menu(message.chat.id, message)
    else:
        await message.reply("Доступ запрещён. Эта команда доступна только администраторам.")

@dp.callback_query(F.data == "admin_panel")
@admin_only
async def admin_panel_callback(callback: CallbackQuery):
    await send_admin_menu(callback.message.chat.id, callback)
    await callback.answer()

@dp.callback_query(F.data == "back_to_main")
async def back_to_main_callback(callback: CallbackQuery):
    try:
        await send_main_menu(callback.message.chat.id, callback)
        await callback.answer()
    except Exception:
        try:
            await callback.message.reply("Возвращаю в главное меню.")
        except Exception:
            pass
        await callback.answer()

@dp.callback_query(F.data == "back_to_start")
async def back_to_start_callback(callback: CallbackQuery):
    try:
        await send_main_menu(callback.message.chat.id, callback)
    except Exception:
        try:
            await send_or_edit(bot, callback.message.chat.id, callback, text="Добро пожаловать! Выберите действие:")
        except Exception:
            pass
    await callback.answer()

@dp.callback_query(F.data.startswith("cancel_buy_"))
async def cancel_buy_callback(callback: CallbackQuery):
    try:
        purchase_id = int(callback.data.split("_", 1)[1])
    except Exception:
        await send_main_menu(callback.message.chat.id, callback)
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT user_id, product_id FROM purchases WHERE id = ?", (purchase_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            await callback.answer("Покупка не найдена.", show_alert=True)
            await send_main_menu(callback.message.chat.id, callback)
            return
        owner_id, product_id = row

        requester = getattr(callback.from_user, "id", None)
        if requester not in ADMIN_IDS and requester != owner_id:
            conn.close()
            await callback.answer("Отмена доступна только владельцу заказа или администратору.", show_alert=True)
            return

        cur.execute("DELETE FROM payments WHERE purchase_id = ?", (purchase_id,))
        cur.execute("DELETE FROM purchases WHERE id = ?", (purchase_id,))
        conn.commit()
        conn.close()
    except Exception:
        await callback.answer("Ошибка при отмене заказа. Свяжитесь с поддержкой.", show_alert=True)
        return

    if product_id:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT category_id FROM products WHERE id = ?", (product_id,))
            c_row = cur.fetchone()
            conn.close()
            if c_row:
                category_id = c_row[0]
                products = get_products_by_category(category_id)
                if products:
                    prod = products[0]
                    pid, name, description, price, photo_path = prod
                    text = f"🔹 <b>{name}</b>\n💬 {description}\n💰 Цена: {price} ₽"
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(text="⬅️ Предыдущий", callback_data="disabled"),
                            InlineKeyboardButton(text="➡️ Следующий", callback_data=f"product_{category_id}_1" if len(products) > 1 else "disabled")
                        ],
                        [InlineKeyboardButton(text="🛒 Купить", callback_data=f"buy_{pid}")],
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
                    ])
                    await bot.send_message(chat_id=callback.message.chat.id, text=text, reply_markup=keyboard, parse_mode="HTML")
                    await callback.answer("Покупка отменена. Возврат к товарам категории.")
                    return
        except Exception:
            pass

    await send_or_edit(bot, callback.message.chat.id, callback, text=f"Покупка {purchase_id} отменена.")
    await callback.answer()
    await send_main_menu(callback.message.chat.id, callback)

@dp.callback_query(F.data == "start_command")
async def start_command_callback(callback: CallbackQuery):
    try:
        if callback.from_user and callback.from_user.id:
            add_user(callback.from_user.id)
    except Exception:
        pass
    await send_main_menu(callback.message.chat.id, callback)
    await callback.answer()

async def send_main_menu(chat_id: int, source_obj):
    uid = None
    try:
        if isinstance(source_obj, CallbackQuery) and source_obj.from_user:
            uid = source_obj.from_user.id
        elif isinstance(source_obj, Message) and source_obj.from_user:
            uid = source_obj.from_user.id
    except Exception:
        uid = None

    keyboard = main_menu_keyboard(uid)
    await send_or_edit(bot, chat_id, source_obj, text="Добро пожаловать! Выберите действие:", reply_markup=keyboard)

async def send_admin_menu(chat_id: int, source_obj):
    await send_or_edit(bot, chat_id, source_obj, text="Админ-панель:", reply_markup=admin_menu_keyboard())

async def main():
    init_db()
    ensure_promos_table()
    ensure_autodeliveries_table()
    ensure_payments_table()
    logging.info("Bot work...")
    try:
        await dp.start_polling(bot)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logging.info("Polling cancelled / interrupted.")
    except Exception:
        logging.exception("Unexpected error while polling:")
    finally:
        try:
            if hasattr(dp, "shutdown"):
                await dp.shutdown()
        except Exception:
            logging.exception("Error during dispatcher shutdown:")

        try:
            storage = getattr(dp, "storage", None)
            if storage is not None:
                if hasattr(storage, "close"):
                    await storage.close()
                if hasattr(storage, "wait_closed"):
                    await storage.wait_closed()
        except Exception:
            logging.exception("Error while closing storage:")

        try:
            sess = getattr(bot, "session", None)
            if sess is not None and hasattr(sess, "close"):
                await sess.close()
        except Exception:
            logging.exception("Error while closing bot session:")

if __name__ == "__main__":
    asyncio.run(main())
