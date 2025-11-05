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
from states import AddProductState, PromoAdminState, UserPromoState, PurchaseState, DeleteState
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

logging.basicConfig(level=logging.INFO)

# Initialize database BEFORE creating bot and dispatcher
init_db()
ensure_promos_table()
ensure_autodeliveries_table()
ensure_payments_table()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def start_command(message: Message):
    add_user(message.from_user.id)
    uid = message.from_user.id if message.from_user else None
    keyboard = main_menu_keyboard(uid)
    await send_or_edit(bot, message.chat.id, message, text="Добро пожаловать! Выберите действие:", reply_markup=keyboard)

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

    # Показываем все товары как кнопки
    inline = []
    for product_id, name, description, price, photo_path in products:
        label = f" {name} — {price}₽"
        inline.append([InlineKeyboardButton(text=label, callback_data=f"buy_{product_id}")])
    inline.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=inline)
    await send_or_edit(bot, callback.message.chat.id, callback, text="Товары в категории:", reply_markup=keyboard)
    await callback.answer()

# Удалите или оставьте функцию show_product закомментированной, так как она больше не используется:
# async def show_product(callback: CallbackQuery, products, index, category_id):
#     ...

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
async def handle_buy_callback(callback: CallbackQuery, state: FSMContext):
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

    product_id, name, description, price = product
    await state.update_data(product_id=product_id, product_name=name, original_price=price)
    
    # Показываем информацию о товаре с фото
    text = f" <b>{name}</b>\n💬 {description}\n💰 Цена: {price} ₽"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ввести промокод", callback_data="apply_promo_in_purchase")],
        [InlineKeyboardButton(text="Оплатить без промокода", callback_data="skip_promo_purchase")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
    ])
    await send_or_edit(bot, callback.message.chat.id, callback, text=text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "apply_promo_in_purchase")
async def apply_promo_in_purchase(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("Введите ваш промокод:")
    await state.set_state(PurchaseState.waiting_for_promo)
    await callback.answer()

async def create_payment_with_data(callback: CallbackQuery, product_id: int, product_name: str, final_price: int, state: FSMContext):
    """
    Создаёт платёж с финальной ценой (после применения промокода).
    """
    purchase_id = create_purchase(callback.from_user.id, product_id)

    invoice = await create_cryptopay_invoice(amount_rub=final_price, description=f"Order {purchase_id}: {product_name}")
    if invoice:
        invoice_id, pay_url = invoice
        payment_id = create_payment_entry(purchase_id=purchase_id, invoice_id=invoice_id, pay_url=pay_url, method="crypto")

        text = (
            f"💳 Реквизиты для оплаты заказа #{purchase_id}\n\n"
            f"Товар: {product_name}\n"
            f"Сумма: {final_price} ₽ (~{round(float(final_price)/max(1.0, float(USDT2RUB_RATE)),6)} USDT)\n"
            f"Invoice ID: {invoice_id}\n\n"
            "Нажмите кнопку «Оплатить» чтобы перейти на страницу оплаты."
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", url=pay_url)],
            [InlineKeyboardButton(text="Проверить оплату", callback_data=f"checkpay_{payment_id}")],
            [InlineKeyboardButton(text="Отменить заказ", callback_data=f"cancel_buy_{purchase_id}")]
        ])

        await bot.send_message(chat_id=callback.from_user.id, text=text, reply_markup=keyboard)
    else:
        await bot.send_message(chat_id=callback.from_user.id, text="Не удалось создать платёжную ссылку. Свяжитесь с поддержкой.")
    
    await state.clear()

@dp.message(PurchaseState.waiting_for_promo)
async def process_promo_in_purchase(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    promo = get_promo_by_code(code)
    
    data = await state.get_data()
    product_id = data.get("product_id")
    product_name = data.get("product_name")
    original_price = data.get("original_price")
    
    if not promo:
        await message.reply("❌ Промокод не найден или неверен.")
        await state.clear()
        await send_main_menu(message.chat.id, message)
        return
    
    pid, pcode, amount, uses_left, active = promo
    if active != 1:
        await message.reply("❌ Этот промокод отключён.")
        await state.clear()
        await send_main_menu(message.chat.id, message)
        return
    
    if uses_left is not None and uses_left <= 0:
        await message.reply("❌ У этого промокода закончилось количество использований.")
        await state.clear()
        await send_main_menu(message.chat.id, message)
        return
    
    # Вычисляем новую цену
    final_price = max(1, original_price - amount)
    await state.update_data(promo_id=pid, promo_amount=amount, final_price=final_price, promo_code=code)
    
    # Деакрементируем uses_left
    if uses_left is not None:
        new_uses = uses_left - 1
        from database import update_promo_uses_db
        update_promo_uses_db(pid, new_uses)
        if new_uses <= 0:
            from database import deactivate_promo_db
            deactivate_promo_db(pid)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_purchase_with_promo")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_purchase")]
    ])
    text = f"✅ Промокод применён!\n\n {product_name}\n💰 Исходная цена: {original_price} ₽\n🎟️ Скидка: -{amount} ₽\n💵 Итого: {final_price} ₽"
    await message.reply(text=text, reply_markup=keyboard)

@dp.callback_query(F.data == "skip_promo_purchase")
async def skip_promo_purchase(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    product_id = data.get("product_id")
    product_name = data.get("product_name")
    original_price = data.get("original_price")
    
    if not product_id or not product_name or original_price is None:
        await send_main_menu(callback.message.chat.id, callback)
        await callback.answer()
        return
    
    await create_payment_with_data(callback, product_id, product_name, original_price, state)
    await callback.answer()

@dp.callback_query(F.data == "confirm_purchase_with_promo")
async def confirm_purchase_with_promo(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    product_id = data.get("product_id")
    product_name = data.get("product_name")
    final_price = data.get("final_price")
    
    if not product_id or not product_name or final_price is None:
        await send_main_menu(callback.message.chat.id, callback)
        await state.clear()
        await callback.answer()
        return
    
    await create_payment_with_data(callback, product_id, product_name, final_price, state)
    await callback.answer()

@dp.callback_query(F.data == "cancel_purchase")
async def cancel_purchase(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await send_main_menu(callback.message.chat.id, callback)
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

@dp.message(Command("delete_category"))
async def delete_category_command(message: Message, state: FSMContext):
    if message.from_user and message.from_user.id not in ADMIN_IDS:
        await message.reply("Доступ запрещён. Команда доступна только администраторам.")
        return
    
    await message.reply("Введите название категории для удаления:")
    await state.set_state(DeleteState.waiting_for_category_name)

@dp.message(DeleteState.waiting_for_category_name)
async def process_delete_category_name(message: Message, state: FSMContext):
    category_name = message.text.strip()
    
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = OFF")
        cur = conn.cursor()
        
        # Получаем ID категории по названию
        cur.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
        cat_row = cur.fetchone()
        
        if not cat_row:
            await message.reply(f"❌ Категория '{category_name}' не найдена.")
            await state.clear()
            return
        
        cat_id = cat_row[0]
        
        # Получаем все товары в этой категории
        cur.execute("SELECT id FROM products WHERE category_id = ?", (cat_id,))
        products = cur.fetchall()
        
        # Удаляем платежи и покупки связанные с товарами
        for (prod_id,) in products:
            cur.execute("DELETE FROM autodeliveries WHERE product_id = ?", (prod_id,))
            cur.execute("DELETE FROM payments WHERE purchase_id IN (SELECT id FROM purchases WHERE product_id = ?)", (prod_id,))
            cur.execute("DELETE FROM purchases WHERE product_id = ?", (prod_id,))
        
        # Удаляем товары
        cur.execute("DELETE FROM products WHERE category_id = ?", (cat_id,))
        
        # Удаляем категорию
        cur.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        
        conn.commit()
        conn.close()
        
        await message.reply(f"✅ Категория '{category_name}' удалена вместе с {len(products)} товарами.")
        await state.clear()
    except Exception as e:
        logging.error(f"Error deleting category by name: {e}")
        await message.reply(f"❌ Ошибка при удалении категории: {str(e)}")
        await state.clear()

@dp.message(Command("delete_product"))
async def delete_product_command(message: Message, state: FSMContext):
    if message.from_user and message.from_user.id not in ADMIN_IDS:
        await message.reply("Доступ запрещён. Команда доступна только администраторам.")
        return
    
    await message.reply("Введите название товара для удаления:")
    await state.set_state(DeleteState.waiting_for_product_name)

@dp.message(DeleteState.waiting_for_product_name)
async def process_delete_product_name(message: Message, state: FSMContext):
    product_name = message.text.strip()
    
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = OFF")
        cur = conn.cursor()
        
        # Получаем ID товара по названию
        cur.execute("SELECT id FROM products WHERE name = ?", (product_name,))
        prod_row = cur.fetchone()
        
        if not prod_row:
            await message.reply(f"❌ Товар '{product_name}' не найден.")
            await state.clear()
            return
        
        prod_id = prod_row[0]
        
        # Удаляем автодоставку
        cur.execute("DELETE FROM autodeliveries WHERE product_id = ?", (prod_id,))
        
        # Удаляем связанные платежи и покупки
        cur.execute("DELETE FROM payments WHERE purchase_id IN (SELECT id FROM purchases WHERE product_id = ?)", (prod_id,))
        cur.execute("DELETE FROM purchases WHERE product_id = ?", (prod_id,))
        
        # Удаляем сам товар
        cur.execute("DELETE FROM products WHERE id = ?", (prod_id,))
        
        conn.commit()
        conn.close()
        
        await message.reply(f"✅ Товар '{product_name}' удалён.")
        await state.clear()
    except Exception as e:
        logging.error(f"Error deleting product by name: {e}")
        await message.reply(f"❌ Ошибка при удалении товара: {str(e)}")
        await state.clear()

@dp.callback_query(F.data == "admin_panel")
@admin_only
async def admin_panel_callback(callback: CallbackQuery):
    await send_admin_menu(callback.message.chat.id, callback)
    await callback.answer()

@dp.callback_query(F.data == "manage_categories")
@admin_only
async def manage_categories_callback(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить категорию", callback_data="add_category")],
            [InlineKeyboardButton(text="Список категорий", callback_data="list_categories")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
        ]
    )
    await send_or_edit(bot, callback.message.chat.id, callback, text="Управление категориями:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "add_category")
@admin_only
async def add_category_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("Введите название новой категории:")
    await state.set_state(AddProductState.waiting_for_category)
    await callback.answer()

@dp.callback_query(F.data == "list_categories")
@admin_only
async def list_categories_callback(callback: CallbackQuery):
    categories = get_categories()
    if not categories:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="manage_categories")]])
        await send_or_edit(bot, callback.message.chat.id, callback, text="Категорий не найдено.", reply_markup=keyboard)
        await callback.answer()
        return
    
    inline = []
    for cat_id, cat_name in categories:
        inline.append([InlineKeyboardButton(text=f"📁 {cat_name}", callback_data=f"category_info_{cat_id}")])
    inline.append([InlineKeyboardButton(text="◀️ Назад", callback_data="manage_categories")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=inline)
    await send_or_edit(bot, callback.message.chat.id, callback, text="Список категорий:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("category_info_"))
@admin_only
async def category_info_callback(callback: CallbackQuery):
    try:
        cat_id = int(callback.data.split("_")[2])
    except ValueError:
        await callback.answer("Ошибка.", show_alert=True)
        return
    
    categories = get_categories()
    cat_name = next((c[1] for c in categories if c[0] == cat_id), None)
    if not cat_name:
        await callback.answer("Категория не найдена.", show_alert=True)
        return
    
    products = get_products_by_category(cat_id)
    text = f" Категория: {cat_name}\n Товаров: {len(products)}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Удалить категорию", callback_data=f"delete_category_{cat_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="list_categories")]
    ])
    await send_or_edit(bot, callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_category_"))
@admin_only
async def delete_category_callback(callback: CallbackQuery):
    try:
        cat_id = int(callback.data.split("_")[2])
    except ValueError:
        await callback.answer("Ошибка.", show_alert=True)
        return
    
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = OFF")
        cur = conn.cursor()
        
        # Получаем все товары в этой категории
        cur.execute("SELECT id FROM products WHERE category_id = ?", (cat_id,))
        products = cur.fetchall()
        
        # Удаляем платежи и покупки связанные с товарами в категории
        for (prod_id,) in products:
            cur.execute("DELETE FROM autodeliveries WHERE product_id = ?", (prod_id,))
            cur.execute("DELETE FROM payments WHERE purchase_id IN (SELECT id FROM purchases WHERE product_id = ?)", (prod_id,))
            cur.execute("DELETE FROM purchases WHERE product_id = ?", (prod_id,))
        
        # Удаляем товары
        cur.execute("DELETE FROM products WHERE category_id = ?", (cat_id,))
        
        # Удаляем категорию
        cur.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        
        conn.commit()
        conn.close()
        
        await callback.answer("Категория удалена.")
        await send_or_edit(bot, callback.message.chat.id, callback, text="Категория удалена.")
        await asyncio.sleep(1)
        await list_categories_callback(callback)
    except Exception as e:
        logging.error(f"Error deleting category: {e}")
        await callback.answer(f"Ошибка при удалении категории: {str(e)}", show_alert=True)

@dp.callback_query(F.data == "manage_products")
@admin_only
async def manage_products_callback(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить товар", callback_data="add_product_menu")],
            [InlineKeyboardButton(text="Список товаров", callback_data="list_products")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
        ]
    )
    await send_or_edit(bot, callback.message.chat.id, callback, text="Управление товарами:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "add_product_menu")
@admin_only
async def add_product_menu_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.reply("Введите название категории для товара:")
    await state.set_state(AddProductState.waiting_for_category)
    await callback.answer()

@dp.message(AddProductState.waiting_for_category)
@admin_only
async def process_category(message: Message, state: FSMContext):
    category_name = message.text.strip()
    add_category(category_name)
    categories = get_categories()
    category_id = next((c[0] for c in categories if c[1] == category_name), None)
    await state.update_data(category_id=category_id)
    await message.reply("Введите название товара:")
    await state.set_state(AddProductState.waiting_for_name)

@dp.message(AddProductState.waiting_for_name)
@admin_only
async def process_product_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.reply("Введите описание товара:")
    await state.set_state(AddProductState.waiting_for_description)

@dp.message(AddProductState.waiting_for_description)
@admin_only
async def process_product_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.reply("Введите цену товара (целое число):")
    await state.set_state(AddProductState.waiting_for_price)

@dp.message(AddProductState.waiting_for_price)
@admin_only
async def process_product_price(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
    except ValueError:
        await message.reply("Цена должна быть числом. Попробуйте снова.")
        return
    
    data = await state.get_data()
    add_product(data["name"], data["description"], price, data["category_id"], None)
    
    await message.reply(f"✅ Товар '{data['name']}' добавлен.")
    await state.clear()
    await send_admin_menu(message.chat.id, message)

@dp.callback_query(F.data == "list_products")
@admin_only
async def list_products_callback(callback: CallbackQuery):
    categories = get_categories()
    if not categories:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="manage_products")]])
        await send_or_edit(bot, callback.message.chat.id, callback, text="Категорий не найдено.", reply_markup=keyboard)
        await callback.answer()
        return

    inline = []
    for cat_id, cat_name in categories:
        products = get_products_by_category(cat_id)
        inline.append([InlineKeyboardButton(text=f" {cat_name} ({len(products)})", callback_data=f"cat_products_{cat_id}")])
    inline.append([InlineKeyboardButton(text="◀️ Назад", callback_data="manage_products")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=inline)
    await send_or_edit(bot, callback.message.chat.id, callback, text="Выберите категорию:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("cat_products_"))
@admin_only
async def cat_products_callback(callback: CallbackQuery):
    try:
        cat_id = int(callback.data.split("_")[2])
    except ValueError:
        await callback.answer("Ошибка.", show_alert=True)
        return
    
    products = get_products_by_category(cat_id)
    if not products:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="list_products")]])
        await send_or_edit(bot, callback.message.chat.id, callback, text="Товаров не найдено.", reply_markup=keyboard)
        await callback.answer()
        return
    
    inline = []
    for prod in products:
        prod_id, name, description, price, photo_path = prod
        label = f" {name} — {price}₽"
        inline.append([InlineKeyboardButton(text=label, callback_data=f"product_detail_{prod_id}")])
    inline.append([InlineKeyboardButton(text="◀️ Назад", callback_data="list_products")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=inline)
    await send_or_edit(bot, callback.message.chat.id, callback, text="Товары в категории:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("product_detail_"))
@admin_only
async def product_detail_callback(callback: CallbackQuery):
    try:
        prod_id = int(callback.data.split("_")[2])
    except ValueError:
        await callback.answer("Ошибка.", show_alert=True)
        return
    
    product = get_product_by_id(prod_id)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    
    pid, name, description, price = product
    text = f" {name}\n\n{description}\n\n💰 Цена: {price}₽"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Удалить товар", callback_data=f"delete_product_{prod_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="list_products")]
    ])
    await send_or_edit(bot, callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_product_"))
@admin_only
async def delete_product_callback(callback: CallbackQuery):
    try:
        prod_id = int(callback.data.split("_")[2])
    except ValueError:
        await callback.answer("Ошибка.", show_alert=True)
        return
    
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = OFF")
        cur = conn.cursor()
        
        # Удаляем автодоставку если она существует
        cur.execute("DELETE FROM autodeliveries WHERE product_id = ?", (prod_id,))
        
        # Удаляем связанные платежи и покупки
        cur.execute("DELETE FROM payments WHERE purchase_id IN (SELECT id FROM purchases WHERE product_id = ?)", (prod_id,))
        cur.execute("DELETE FROM purchases WHERE product_id = ?", (prod_id,))
        
        # Удаляем сам товар
        cur.execute("DELETE FROM products WHERE id = ?", (prod_id,))
        
        conn.commit()
        conn.close()
        
        await callback.answer("Товар удалён.")
        await send_or_edit(bot, callback.message.chat.id, callback, text="Товар удалён.")
        await asyncio.sleep(1)
        await list_products_callback(callback)
    except Exception as e:
        logging.error(f"Error deleting product: {e}")
        await callback.answer(f"Ошибка при удалении товара: {str(e)}", show_alert=True)

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

@dp.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery):
    uid = callback.from_user.id if callback.from_user else None
    text = f"👤 Ваш профиль\n\nID: {uid}\n\nЗдесь будет информация о вашем профиле."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
    ])
    await send_or_edit(bot, callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "support")
async def support_callback(callback: CallbackQuery):
    text = "💬 Служба поддержки\n\nhttps://t.me/grumpaaa\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
    ])
    await send_or_edit(bot, callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "calculator")
async def calculator_callback(callback: CallbackQuery):
    text = "🧮 Калькулятор\n\nЭта функция в разработке."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
    ])
    await send_or_edit(bot, callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "faq")
async def faq_callback(callback: CallbackQuery):
    text = "❓ Часто задаваемые вопросы\n\n1. Как оплатить? — Выберите товар и следуйте инструкциям.\n2. Как получить товар? — Автоматическая доставка после оплаты."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
    ])
    await send_or_edit(bot, callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "delete_catalog")
@admin_only
async def delete_catalog_callback(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить всё", callback_data="confirm_delete_catalog")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]
    ])
    text = "⚠️ ВНИМАНИЕ!\n\nВы собираетесь удалить весь каталог со всеми категориями и товарами.\n\nЭто действие необратимо!"
    await send_or_edit(bot, callback.message.chat.id, callback, text=text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "confirm_delete_catalog")
@admin_only
async def confirm_delete_catalog_callback(callback: CallbackQuery):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = OFF")
        cur = conn.cursor()
        
        # Удаляем все автодоставки
        cur.execute("DELETE FROM autodeliveries")
        
        # Удаляем все платежи и покупки
        cur.execute("DELETE FROM payments")
        cur.execute("DELETE FROM purchases")
        
        # Удаляем все товары
        cur.execute("DELETE FROM products")
        
        # Удаляем все категории
        cur.execute("DELETE FROM categories")
        
        conn.commit()
        conn.close()
        
        await callback.answer("Каталог полностью удалён.")
        await send_or_edit(bot, callback.message.chat.id, callback, text="✅ Каталог успешно удалён.")
        await asyncio.sleep(1)
        await send_admin_menu(callback.message.chat.id, callback)
    except Exception as e:
        logging.error(f"Error deleting catalog: {e}")
        await callback.answer(f"Ошибка при удалении каталога: {str(e)}", show_alert=True)

@dp.callback_query(F.data.startswith("checkpay_"))
async def checkpay_callback(callback: CallbackQuery):
    try:
        payment_id = int(callback.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка при проверке платежа.", show_alert=True)
        return
    
    try:
        payment = get_payment_by_id(payment_id)
        if not payment:
            await callback.answer("Платёж не найден.", show_alert=True)
            return
        
        # Распаковываем правильное количество значений
        pid, purchase_id, invoice_id, pay_url, method, status = payment
        
        if status == "paid":
            await callback.answer("✅ Платёж успешно проведён!", show_alert=True)
            await send_or_edit(bot, callback.message.chat.id, callback, text="✅ Ваш платёж успешно принят. Спасибо за покупку! Ожидайте сообщение от поддержки.")
            
            # Отправляем информацию об заказе админам
            await notify_admins_about_purchase(purchase_id, callback.from_user)
            
        elif status == "pending":
            # Проверяем статус в Cryptopay
            invoice_status = await check_crypto_invoice_status(invoice_id)
            if invoice_status == "paid":
                update_payment_status_by_id(payment_id, "paid")
                await callback.answer("✅ Платёж успешно проведён!", show_alert=True)
                await send_or_edit(bot, callback.message.chat.id, callback, text="✅ Ваш платёж успешно принят. Спасибо за покупку!")
                
                # Отправляем информацию об заказе админам
                await notify_admins_about_purchase(purchase_id, callback.from_user)
            else:
                await callback.answer("⏳ Платёж ещё не поступил. Попробуйте позже.", show_alert=True)
        else:
            await callback.answer("❌ Платёж отклонен или отменен.", show_alert=True)
    except Exception as e:
        logging.error(f"Error checking payment: {e}")
        await callback.answer(f"Ошибка при проверке платежа: {str(e)}", show_alert=True)

async def notify_admins_about_purchase(purchase_id: int, user):
    """
    Отправляет информацию об заказе всем администраторам.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        # Получаем информацию о покупке
        cur.execute("SELECT user_id, product_id FROM purchases WHERE id = ?", (purchase_id,))
        purchase_row = cur.fetchone()
        
        if not purchase_row:
            conn.close()
            return
        
        user_id, product_id = purchase_row
        
        # Получаем информацию о товаре
        product = get_product_by_id(product_id)
        if not product:
            conn.close()
            return
        
        _, product_name, _, price = product
        
        conn.close()
        
        # Формируем сообщение для админов
        user_first_name = user.first_name or "Unknown"
        user_username = f"@{user.username}" if user.username else "Нет юзернейма"
        user_telegram_id = user.id
        
        admin_message = (
            f"📦 <b>Новый заказ #{purchase_id}</b>\n\n"
            f"<b>Товар:</b> {product_name}\n"
            f"<b>Цена:</b> {price} ₽\n\n"
            f"<b>Информация о покупателе:</b>\n"
            f"<b>Имя:</b> {user_first_name}\n"
            f"<b>Юзернейм:</b> {user_username}\n"
            f"<b>Telegram ID:</b> <code>{user_telegram_id}</code>"
        )
        
        # Отправляем сообщение всем администраторам
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=admin_message, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Error sending admin notification to {admin_id}: {e}")
    except Exception as e:
        logging.error(f"Error in notify_admins_about_purchase: {e}")

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

async def process_pending_deliveries():
    """
    Фоновая задача: периодически проверяет оплаченные заказы и отправляет автовыдачу.
    """
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            
            # Получаем оплаченные, но не доставленные заказы
            cur.execute("""
                SELECT p.id, p.user_id, p.product_id 
                FROM purchases p
                JOIN payments pm ON p.id = pm.purchase_id
                WHERE pm.status = 'paid' AND p.status IS NULL
                LIMIT 10
            """)
            orders = cur.fetchall()
            conn.close()
            
            for order_id, user_id, product_id in orders:
                try:
                    # Получаем информацию о пользователе
                    conn = sqlite3.connect(DB_PATH)
                    cur = conn.cursor()
                    cur.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,))
                    user_row = cur.fetchone()
                    conn.close()
                    
                    if not user_row:
                        continue
                    
                    telegram_id = user_row[0]
                    
                    # Получаем информацию об автодоставке
                    autodel = get_autodelivery_for_product(product_id)
                    if autodel and autodel[1] == 1:
                        _, _, content_text, file_path = autodel
                        try:
                            if content_text:
                                await bot.send_message(
                                    chat_id=telegram_id,
                                    text=f"✅ Спасибо за покупку! Ваша автовыдача по заказу #{order_id}:\n\n{content_text}"
                                )
                            elif file_path and os.path.exists(file_path):
                                ext = os.path.splitext(file_path)[1].lower()
                                if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                                    await bot.send_photo(
                                        chat_id=telegram_id,
                                        photo=FSInputFile(file_path),
                                        caption=f"✅ Спасибо за покупку! Ваша автовыдача по заказу #{order_id}"
                                    )
                                else:
                                    await bot.send_document(
                                        chat_id=telegram_id,
                                        document=FSInputFile(file_path),
                                        caption=f"✅ Спасибо за покупку! Ваша автовыдача по заказу #{order_id}"
                                    )
                            
                            # Отмечаем заказ как доставленный
                            conn = sqlite3.connect(DB_PATH)
                            cur = conn.cursor()
                            cur.execute("UPDATE purchases SET status = 'delivered' WHERE id = ?", (order_id,))
                            conn.commit()
                            conn.close()
                        except Exception as e:
                            logging.error(f"Error delivering autodelivery for order {order_id}: {e}")
                    else:
                        # Если нет автодоставки, просто отмечаем как доставленный
                        conn = sqlite3.connect(DB_PATH)
                        cur = conn.cursor()
                        cur.execute("UPDATE purchases SET status = 'delivered' WHERE id = ?", (order_id,))
                        conn.commit()
                        conn.close()
                except Exception as e:
                    logging.error(f"Error processing delivery for order {order_id}: {e}")
            
            # Проверяем каждые 5 секунд
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Error in process_pending_deliveries: {e}")
            await asyncio.sleep(5)

async def main():
    logging.info("Bot started...")
    logging.info(f"Using database: {DB_PATH}")
    
    # Запускаем фоновую задачу обработки доставок
    delivery_task = asyncio.create_task(process_pending_deliveries())
    
    try:
        await dp.start_polling(bot)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logging.info("Polling cancelled / interrupted.")
        delivery_task.cancel()
    except Exception:
        logging.exception("Unexpected error while polling:")
        delivery_task.cancel()
    finally:
        try:
            delivery_task.cancel()
        except Exception:
            pass
        
        try:
            if hasattr(dp, "shutdown"):
                dp.shutdown()
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
