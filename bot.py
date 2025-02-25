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
    raise ValueError("Invalid Telegram bot token. Make sure TELEGRAM_BOT_TOKEN is set correctly.")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Authenticate Google Sheets
sheet = None
if SERVICE_ACCOUNT_JSON:
    creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets"
    ]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(credentials)
    sheet = client.open_by_key(SPREADSHEET_ID).sheet1
    print("Google Sheets connected successfully!")
else:
    print("Error: Google Sheets credentials not provided.")

# We'll maintain conversation states and data in dictionaries
user_state = {}  # chat_id -> 'waiting_for_photo' / 'waiting_for_description' / 'waiting_for_quantity'
user_data = {}   # chat_id -> { 'barcode': str, 'description': str, 'quantity': int }

# Helper constants for states
STATE_PHOTO = 'waiting_for_photo'
STATE_DESCRIPTION = 'waiting_for_description'
STATE_QUANTITY = 'waiting_for_quantity'


# --- START COMMAND HANDLER ---
@bot.message_handler(commands=['start'])
def start_message(message):
    chat_id = message.chat.id
    # Initialize conversation state and data
    user_state[chat_id] = STATE_PHOTO
    user_data[chat_id] = {'barcode': None, 'description': None, 'quantity': None}
    
    start_text = (
        "👋 Salam! Bu bot aktivlərinizi qeyd etmək üçün istifadə olunur. 📦\n\n"
        "📝 Format:\n"
        "1️⃣ Barkodlu məhsulun şəklini çəkin və göndərin (bot barkodu oxumağa çalışacaq).\n"
        "2️⃣ Aktivin adı ✍️\n"
        "3️⃣ Miqdar 🔢\n"
        "4️⃣ Əlavə qeydlər (əgər varsa) 📌\n\n"
        "📊 Daxil edilən məlumatlar avtomatik olaraq Google Sheets-də saxlanılır.\n\n"
        "Zəhmət olmasa ilk olaraq barkodun şəklini göndərin. ✅"
    )
    bot.send_message(chat_id, start_text)


# --- PHOTO HANDLER ---
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    # Check if we are actually waiting for a photo
    current_state = user_state.get(chat_id)

    if current_state != STATE_PHOTO:
        bot.send_message(
            chat_id,
            "Hazırda fotoya ehtiyac yoxdur. Zəhmət olmasa, əvvəlki mərhələyə uyğun addım atın."
        )
        return

    print("Received a photo from chat_id:", chat_id)
    
    # Download the photo
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
        bot.send_message(
            chat_id,
            f"✅ Barkod aşkarlandı: {barcode_data}\nİndi aktivin adını yazın ✍️"
        )
    else:
        user_data[chat_id]["barcode"] = "Barkod tapılmadı"
        bot.send_message(
            chat_id,
            "⚠ Barkod tapılmadı. Zəhmət olmasa aktivin adını yazın ✍️"
        )

    # Update state to waiting for description
    user_state[chat_id] = STATE_DESCRIPTION


# --- TEXT HANDLER ---
@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    text = message.text.strip()
    current_state = user_state.get(chat_id)

    if not current_state:
        # If user hasn't started or we lost the state for some reason
        bot.send_message(chat_id, "Zəhmət olmasa /start əmrini istifadə edin.")
        return

    # Depending on the state, do the appropriate action
    if current_state == STATE_PHOTO:
        # They should have sent a photo, but they sent text
        bot.send_message(
            chat_id,
            "Zəhmət olmasa barkodun şəklini göndərin. (Mətn göndərdiniz)"
        )
        return

    elif current_state == STATE_DESCRIPTION:
        # We expect an item description
        user_data[chat_id]["description"] = text
        user_state[chat_id] = STATE_QUANTITY
        bot.send_message(chat_id, "🔢 İndi miqdarı daxil edin (rəqəm).")

    elif current_state == STATE_QUANTITY:
        # We expect a numeric quantity
        try:
            quantity = int(text)
        except ValueError:
            bot.send_message(chat_id, "❌ Xəta! Zəhmət olmasa düzgün rəqəm daxil edin.")
            return
        
        user_data[chat_id]["quantity"] = quantity
        # Save data to Google Sheets
        saved = save_to_sheets(chat_id)
        if saved:
            bot.send_message(chat_id, "✅ Məlumat uğurla qeydə alındı!")
        else:
            bot.send_message(
                chat_id,
                "❌ Məlumatı saxlayarkən problem yarandı. Google Sheets qoşulmayıb?"
            )

        # Clear state + data or reset for next entry
        user_state[chat_id] = STATE_PHOTO  # or remove them if you want
        user_data[chat_id] = {'barcode': None, 'description': None, 'quantity': None}
        bot.send_message(
            chat_id,
            "Yeni barkod üçün yenidən şəkil göndərə bilərsiniz. 📷"
        )


# --- DECODE BARCODE ---
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


# --- SAVE TO SHEETS ---
def save_to_sheets(chat_id):
    if not sheet:
        return False
    try:
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        barcode = user_data[chat_id].get("barcode", "(Barkod yoxdur)")
        description = user_data[chat_id].get("description", "(Ad yoxdur)")
        quantity = user_data[chat_id].get("quantity", 0)

        row = [date_str, barcode, description, quantity]
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
