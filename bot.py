import os
import telebot
from datetime import datetime
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials

print("Starting bot...")

# Load environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise ValueError("Invalid Telegram bot token. Make sure TELEGRAM_BOT_TOKEN is set correctly in environment variables.")

print("Telebot imported successfully!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Authenticate Google Sheets
if SERVICE_ACCOUNT_JSON:
    creds = json.loads(SERVICE_ACCOUNT_JSON)
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(credentials)
    sheet = client.open_by_key(SPREADSHEET_ID).sheet1
    print("Google Sheets connected successfully!")
else:
    sheet = None
    print("Error: Google Sheets credentials not provided.")

# Define the bot response to /start
@bot.message_handler(commands=['start'])
def start_message(message):
    start_text = (
        "👋 Salam! Bu bot aktivlərinizi qeyd etmək üçün istifadə olunur. 📦\n\n"
        "📝 Format:\n"
        "1️⃣ Barkod 📷 (Şəkil çəkin və göndərin)\n"
        "2️⃣ Aktivin adı ✍️\n"
        "3️⃣ Miqdar 🔢\n"
        "4️⃣ Əlavə qeydlər (əgər varsa) 📌\n\n"
        "📊 Daxil edilən məlumatlar avtomatik olaraq Google Sheets-də saxlanılır."
    )
    bot.send_message(message.chat.id, start_text)

# Handle photo uploads
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    print("Received a photo")
    bot.send_message(message.chat.id, "📸 Şəkil qəbul edildi. İndi aktivin adını yazın.")
    bot.register_next_step_handler(message, get_item_description)

# Get item description
def get_item_description(message):
    item_description = message.text
    bot.send_message(message.chat.id, "🔢 İndi miqdarı daxil edin.")
    bot.register_next_step_handler(message, get_quantity, item_description)

# Get quantity
def get_quantity(message, item_description):
    try:
        quantity = int(message.text)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Xəta! Zəhmət olmasa düzgün rəqəm daxil edin.")
        bot.register_next_step_handler(message, get_quantity, item_description)
        return
    
    bot.send_message(message.chat.id, "✅ Məlumat uğurla qəbul edildi və Google Sheets-ə göndərilir!")
    save_to_sheets(item_description, quantity)

# Save data to Google Sheets
def save_to_sheets(item_description, quantity):
    if sheet:
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = [date, "(Barkod burada olacaq)", item_description, quantity]
        sheet.append_row(data)
        print("Data saved to Google Sheets:", data)
    else:
        print("Error: Google Sheets is not connected.")

print("Bot is about to start polling...")
try:
    bot.polling(none_stop=True, interval=1, timeout=20)
except Exception as e:
    print(f"Bot crashed with error: {e}")

print("Bot is running!")
