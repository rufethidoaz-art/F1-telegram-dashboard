from __future__ import annotations

from typing import Optional, Union

from telegram import Update, Message, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes


def get_chat_id(update: Update) -> Optional[int]:
    """Safely get the chat_id from an update object."""
    if update.effective_chat:
        return update.effective_chat.id
    return None


def get_message(update: Update) -> Optional[Message]:
    """Safely get the message from an update object, whether it's a command or callback."""
    return update.effective_message


async def safe_reply(
    update: Update,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    **kwargs
) -> Optional[Message]:
    """
    Safely reply to a message or edit the message from a callback query.
    This prevents errors when a button is pressed.
    """
    message = get_message(update)
    if not message:
        return None

    try:
        if update.callback_query:
            return await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup, **kwargs)
        else:
            return await message.reply_text(text=text, reply_markup=reply_markup, **kwargs)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            # Ignore errors where the message content is the same
            pass
        else:
            # For other errors, you might want to log them
            print(f"Error sending message: {e}")
            return None