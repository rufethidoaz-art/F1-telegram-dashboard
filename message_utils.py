from __future__ import annotations

from typing import Optional, Union, cast, Any
from telegram import Update, Message, CallbackQuery, InlineKeyboardMarkup
from telegram.constants import ParseMode

def get_message(update: Update) -> Optional[Message]:
    """Get the message object from an update, handling both direct messages and callbacks."""
    if update.message:
        return update.message
    if update.callback_query and update.callback_query.message:
        return cast(Message, update.callback_query.message)
    return None

def get_chat_id(update: Update) -> Optional[int]:
    """Get chat ID from an update, handling both direct messages and callbacks."""
    message = get_message(update)
    return message.chat.id if message and message.chat else None

async def reply_text(
    message: Message,
    text: str,
    parse_mode: str = ParseMode.HTML,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    **kwargs: Any,
) -> Optional[Message]:
    """Safely reply to a message with proper error handling.

    Accepts additional keyword arguments (like reply_markup) and forwards them
    to telegram's reply_text so callers can include inline keyboards.
    """
    if hasattr(message, "reply_text"):
        try:
            return await message.reply_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup, **kwargs
            )
        except Exception as e:
            print(f"Error sending message: {e}")
    return None

async def safe_reply(
    update: Update,
    text: str,
    parse_mode: str = ParseMode.HTML,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    **kwargs: Any,
) -> Optional[Message]:
    """Safely reply to an update, handling both messages and callback queries.

    Forwards optional reply_markup and extra kwargs to the underlying reply_text.
    """
    message = get_message(update)
    if message:
        return await reply_text(
            message, text, parse_mode=parse_mode, reply_markup=reply_markup, **kwargs
        )
    return None