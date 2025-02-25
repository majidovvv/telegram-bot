# bot.py

import os
import json
import re
from datetime import datetime

import telebot
import gspread
from oauth2client.service_account import ServiceAccountCredentials

import pytesseract
from pyzbar.pyzbar import decode
from PIL import Image, ImageEnhance
import cv2
import numpy as np

print("Starting bot...")

# --- 1) Load environment variables ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise ValueError("Invalid Telegram bot token. Make sure TELEGRAM_BOT_TOKEN is set.")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# --- 2) Authenticate Google Sheets ---
sheet = None
if SERVICE_ACCOUNT_JSON:
    try:
        creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets"
        ]
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(credentials)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        print("Google Sheets connected successfully!")
    except Exception as e:
        print("Error connecting to Google Sheets:", e)
else:
    print("Error: Google Sheets credentials (SERVICE_ACCOUNT_JSON) not provided.")

# --- 3) Conversation states ---
user_state = {}  # chat_id -> 'waiting_for_photo' / 'waiting_for_description' / 'waiting_for_quantity'
user_data = {}   # chat_id -> { 'barcode': str, 'description': str, 'quantity': int }

STATE_PHOTO = 'waiting_for_photo'
STATE_DESCRIPTION = 'waiting_for_description'
STATE_QUANTITY = 'waiting_for_quantity'

# --- 4) START command ---
@bot.message_handler(commands=['start'])
def start_message(message):
    chat_id = message.chat.id
    user_state[chat_id] = STATE_PHOTO
    user_data[chat_id] = {'barcode': None, 'description': None, 'quantity': None}

    start_text = (
        "👋 Salam! Bu bot aktivlərinizi qeyd etmək üçün istifadə olunur. 📦\n\n"
        "📝 Format:\n"
        "1️⃣ Barkodun şəklini göndərin (bot zbar ilə standart barkod axtaracaq, "
        "alınmazsa OCR ilə 'AZT...' kodu axtaracaq).\n"
        "2️⃣ Aktivin adı ✍️\n"
        "3️⃣ Miqdar 🔢\n"
        "📊 Daxil edilən məlumatlar Google Sheets-də saxlanılır.\n\n"
        "Zəhmət olmasa ilk olaraq barkodun şəklini göndərin. ✅"
    )
    bot.send_message(chat_id, start_text)

# --- 5) PHOTO handler ---
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    current_state = user_state.get(chat_id)

    if current_state != STATE_PHOTO:
        bot.send_message(chat_id, "Hazırda fotoya ehtiyac yoxdur. Zəhmət olmasa, əvvəlki mərhələyə uyğun davranın.")
        return

    # Download photo
    file_id = message.photo[-1].file_id
    file_info = bot.get_file(file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    img_path = f"temp_{chat_id}.jpg"
    with open(img_path, "wb") as f:
        f.write(downloaded_file)

    # Decode barcode (zbar + OCR fallback)
    barcode_data = decode_barcode(img_path)
    if barcode_data:
        user_data[chat_id]["barcode"] = barcode_data
        bot.send_message(chat_id, f"✅ Barkod/Kod aşkarlandı: {barcode_data}\nİndi aktivin adını yazın ✍️")
    else:
        user_data[chat_id]["barcode"] = "Barkod tapılmadı"
        bot.send_message(chat_id, "⚠ Barkod və ya AZT kod aşkar edilmədi. İndi aktivin adını yazın ✍️")

    user_state[chat_id] = STATE_DESCRIPTION

# --- 6) TEXT handler ---
@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    text = message.text.strip()
    current_state = user_state.get(chat_id)

    if not current_state:
        bot.send_message(chat_id, "Zəhmət olmasa /start əmri ilə başlayın.")
        return

    if current_state == STATE_PHOTO:
        bot.send_message(chat_id, "Zəhmət olmasa barkodun şəklini göndərin (Mətn göndərdiniz).")
        return

    if current_state == STATE_DESCRIPTION:
        user_data[chat_id]["description"] = text
        user_state[chat_id] = STATE_QUANTITY
        bot.send_message(chat_id, "🔢 İndi miqdarı daxil edin (rəqəm).")
        return

    if current_state == STATE_QUANTITY:
        try:
            quantity = int(text)
            user_data[chat_id]["quantity"] = quantity
        except ValueError:
            bot.send_message(chat_id, "❌ Xəta! Zəhmət olmasa düzgün rəqəm daxil edin.")
            return

        saved = save_to_sheets(chat_id)
        if saved:
            bot.send_message(chat_id, "✅ Məlumat uğurla qeydə alındı!")
        else:
            bot.send_message(chat_id, "❌ Məlumatı saxlayarkən problem yarandı. Google Sheets qoşulmayıb?")

        # Reset for new cycle
        user_state[chat_id] = STATE_PHOTO
        user_data[chat_id] = {'barcode': None, 'description': None, 'quantity': None}
        bot.send_message(chat_id, "Yeni barkod üçün yenidən şəkil göndərə bilərsiniz. 📷")

# --- 7) decode_barcode function ---
def decode_barcode(image_path):
    """
    1) Try zbar (pyzbar) for standard barcodes
    2) If no result, advanced OpenCV preprocessing + Tesseract OCR for 'AZT\d+'
    """

    # Attempt #1: direct zbar
    try:
        pil_img = Image.open(image_path)
        raw_barcodes = decode(pil_img)
        if raw_barcodes:
            return raw_barcodes[0].data.decode("utf-8")
    except Exception as e:
        print("zbar decode error:", e)

    # Attempt #2: advanced OCR with OpenCV
    try:
        # Load via OpenCV
        img = cv2.imread(image_path)
        if img is None:
            print("OpenCV could not read image.")
            return None

        # Convert to grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Deskew
        deskewed = deskew_image(gray)

        # Morph close
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2,2))
        morph = cv2.morphologyEx(deskewed, cv2.MORPH_CLOSE, kernel, iterations=1)

        # Increase contrast
        pil_morph = Image.fromarray(morph)
        enhancer = ImageEnhance.Contrast(pil_morph)
        high_contrast = enhancer.enhance(2.0)

        final_img = np.array(high_contrast)

        text = pytesseract.image_to_string(final_img, lang='eng')

        # Look for AZT + digits
        match = re.search(r'(AZT\d+)', text.upper())
        if match:
            return match.group(1)

    except Exception as e:
        print("Tesseract OCR error:", e)

    return None

def deskew_image(gray):
    """
    Attempt to correct image rotation using OpenCV minAreaRect.
    """
    blur = cv2.GaussianBlur(gray, (3,3), 0)
    # Otsu threshold
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Invert so text is black on white (depending on your images)
    thresh_inv = 255 - thresh

    contours, _ = cv2.findContours(thresh_inv, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return gray

    # largest contour
    c = max(contours, key=cv2.contourArea)

    rect = cv2.minAreaRect(c)
    angle = rect[-1]
    if angle < -45:
        angle += 90
    angle = -angle  # correct direction

    (h, w) = gray.shape[:2]
    center = (w//2, h//2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return rotated

# --- 8) Save to sheets ---
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
    print("Bot crashed with error:", e)

print("Bot is running!")
