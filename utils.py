import os
from aiogram import Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, FSInputFile

last_message = {}

async def send_or_edit(bot: Bot, chat_id: int, source_obj, text: str = None, photo_path: str = None,
                       reply_markup: InlineKeyboardMarkup = None, parse_mode: str = None):
    """
    Удаляет предыдущее сообщение бота и отправляет новое.
    """
    prev_mid = last_message.get(chat_id)

    # Удаляем старое сообщение если оно есть
    if prev_mid:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=prev_mid)
        except Exception:
            pass
        last_message.pop(chat_id, None)

    # Определяем, нужно ли отправлять ответом на сообщение пользователя
    reply_to = None
    try:
        if isinstance(source_obj, Message):
            reply_to = source_obj.message_id
    except Exception:
        reply_to = None

    sent = None
    try:
        if photo_path:
            if reply_to:
                sent = await bot.send_photo(chat_id=chat_id, photo=FSInputFile(photo_path),
                                            caption=text, reply_markup=reply_markup,
                                            parse_mode=parse_mode, reply_to_message_id=reply_to)
            else:
                sent = await bot.send_photo(chat_id=chat_id, photo=FSInputFile(photo_path),
                                            caption=text, reply_markup=reply_markup,
                                            parse_mode=parse_mode)
        else:
            if reply_to:
                sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                              parse_mode=parse_mode, reply_to_message_id=reply_to)
            else:
                sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                              parse_mode=parse_mode)
    except Exception:
        try:
            sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            sent = None

    if sent:
        last_message[chat_id] = sent.message_id
