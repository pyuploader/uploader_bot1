# get_chat_id.py
import os
from telegram import Bot

bot = Bot(os.environ.get("TG_BOT_TOKEN"))
updates = bot.get_updates(limit=50)
for u in updates:
    if u.message:
        print("chat title:", getattr(u.message.chat, "title", None), "chat id:", u.message.chat.id)