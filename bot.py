import os
import json
from datetime import datetime

import telebot
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from pyzbar.pyzbar import decode
from PIL import Image

print("Starting bot...")

# Load environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise ValueError("Invalid Telegram bot token. Make sure TELEGRAM_BOT_TOKEN is set correctly in environment variables.")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Authenticate Google Sheets
sheet = None
if SERVICE_ACCOUNT_JSON:
    creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
    scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/spreadsheets"]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(credentials)
    sheet = client.open_by_key(SPREADSHEET_ID).sheet1
    print("Google Sheets connected successfully!")
else:
    print("Error: Google Sheets credentials not provided.")

# We'll store user data (barcode, item description, quantity) in a dictionary.
user_data = {}

# Explain in Azerbaijani
@bot.message_handler(commands=['start'])
def start_message(message):
    start_text = (
        "👋 Salam! Bu bot aktivlərinizi qeyd etmək üçün istifadə olunur. 📦\n\n"
        "📝 Format:\n"
        "1️⃣ Barkodlu məhsulun şəklini çəkin və göndərin (bot barkodu oxumağa çalışacaq).\n"
        "2️⃣ Aktivin adı ✍️\n"
        "3️⃣ Miqdar 🔢\n"
        "4️⃣ Əlavə qeydlər (əgər varsa) 📌\n\n"
        "📊 Daxil edilən məlumatlar avtomatik olaraq Google Sheets-də saxlanılır."
    )
    bot.send_message(message.chat.id, start_text)
    # Initialize user data structure
    user_data[message.chat.id] = {
        "barcode": None,
        "description": None,
        "quantity": None
    }

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    print("Received a photo from chat_id:", chat_id)

    # Download photo
    file_id = message.photo[-1].file_id
    file_info = bot.get_file(file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    img_path = f"temp_{chat_id}.jpg"
    with open(img_path, "wb") as f:
        f.write(downloaded_file)

    # Try to decode barcode
    barcode_data = decode_barcode(img_path)
    if barcode_data:
        user_data[chat_id]["barcode"] = barcode_data
        bot.send_message(chat_id, f"Barkod aşkarlandı: {barcode_data}\nİndi aktivin adını yazın ✍️")
    else:
        user_data[chat_id]["barcode"] = "Barkod tapılmadı"  # fallback
        bot.send_message(chat_id, "Barkod tapılmadı. Zəhmət olmasa aktivin adını yazın ✍️")

    bot.register_next_step_handler(message, get_item_description)

# Step 2: Get item description

def get_item_description(message):
    chat_id = message.chat.id
    description = message.text.strip()
    user_data[chat_id]["description"] = description
    bot.send_message(chat_id, "🔢 İndi miqdarı daxil edin (rəqəm).")
    bot.register_next_step_handler(message, get_quantity)

# Step 3: Get quantity and save

def get_quantity(message):
    chat_id = message.chat.id
    try:
        quantity = int(message.text.strip())
        user_data[chat_id]["quantity"] = quantity
    except ValueError:
        bot.send_message(chat_id, "❌ Xəta! Zəhmət olmasa düzgün rəqəm daxil edin.")
        return bot.register_next_step_handler(message, get_quantity)

    # Data is ready, save to sheets
    saved = save_to_sheets(chat_id)
    if saved:
        bot.send_message(chat_id, "✅ Məlumat uğurla qeydə alındı!")
    else:
        bot.send_message(chat_id, "❌ Məlumatı saxlayarkən problem yarandı. Google Sheets qoşulmayıb?")

    # Clear user data
    if chat_id in user_data:
        user_data.pop(chat_id)

# Function to decode barcode from image

def decode_barcode(image_path):
    try:
        img = Image.open(image_path)
        barcodes = decode(img)
        if barcodes:
            # return first barcode found
            return barcodes[0].data.decode("utf-8")
    except Exception as e:
        print("Error decoding barcode:", e)
    return None

# Save data to Google Sheets
def save_to_sheets(chat_id):
    if not sheet:
        return False

    try:
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            date_str,
            user_data[chat_id].get("barcode", "(Barkod yoxdur)"),
            user_data[chat_id].get("description", "(Ad yoxdur)"),
            user_data[chat_id].get("quantity", 0)
        ]
        sheet.append_row(row)
        print("Data saved:", row)
        return True
    except Exception as e:
        print("Error saving to sheet:", e)
        return False

print("Bot is about to start polling...")
try:
    bot.polling(none_stop=True, interval=1, timeout=20)
except Exception as e:
    print(f"Bot crashed with error: {e}")

print("Bot is running!")
