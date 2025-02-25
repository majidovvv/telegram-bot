import os
import telebot

print("Starting bot...")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise ValueError("Invalid Telegram bot token. Make sure TELEGRAM_BOT_TOKEN is set correctly in environment variables.")

print("Telebot imported successfully!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

print("Bot is about to start polling...")
try:
    bot.polling(none_stop=True, interval=1, timeout=20)
except Exception as e:
    print(f"Bot crashed with error: {e}")

print("Bot is running!")
