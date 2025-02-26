"""
bot.py - A comprehensive Telegram bot with:
- Advanced barcode detection (region-based, angle increments).
- OCR fallback for "AZT..." codes.
- Multi-scan workflow (scan multiple items).
- Manual entry fallback if scanning fails repeatedly.
- /help, /cancel commands for user-friendly flow.
- Enhanced error handling & logs.
"""

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

# --- 1) Environment Variables ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise ValueError("Invalid Telegram bot token. Make sure TELEGRAM_BOT_TOKEN is set.")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# --- 2) Google Sheets Setup ---
sheet = None
if SERVICE_ACCOUNT_JSON:
    try:
        creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
        scope = ["https://spreadsheets.google.com/feeds",
                 "https://www.googleapis.com/auth/spreadsheets"]
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(credentials)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        print("Google Sheets connected successfully!")
    except Exception as e:
        print("Error connecting to Google Sheets:", e)
else:
    print("Error: SERVICE_ACCOUNT_JSON not provided.")

# --- 3) Conversation States ---
STATE_IDLE = 'idle'
STATE_WAITING_PHOTO = 'waiting_for_photo'
STATE_WAITING_DESCRIPTION = 'waiting_for_description'
STATE_WAITING_QUANTITY = 'waiting_for_quantity'
STATE_MANUAL_BARCODE = 'manual_barcode'

user_state = {}  # chat_id -> current state
user_data = {}   # chat_id -> { 'barcode': str, 'description': str, 'quantity': int }

# We'll allow multiple scans in a single session. They can type /done to finish.


# --- 4) HELPER: Initialize user session ---
def init_user_session(chat_id):
    user_state[chat_id] = STATE_WAITING_PHOTO
    user_data[chat_id] = {'barcode': None, 'description': None, 'quantity': None}


# --- 5) /start command ---
@bot.message_handler(commands=['start'])
def handle_start(message):
    chat_id = message.chat.id
    init_user_session(chat_id)

    text = (
        "👋 Salam! Bu bot aktivlərinizi qeyd etmək üçün istifadə olunur. 📦\n\n"
        "📝 Məlumat gedişatı:\n"
        "1️⃣ Barkodun şəklini göndərin (zbar ilə tanımağa çalışacaq, uğursuz olarsa OCR tətbiq edəcək).\n"
        "2️⃣ Bot barkodu (və ya AZT kodunu) göstərir.\n"
        "3️⃣ Sizin göndərəcəyiniz aktivin adı ✍️\n"
        "4️⃣ Miqdar 🔢\n"
        "✅ Sonda Google Sheets-də saxlanılır.\n\n"
        "Birdən çox mal əlavə etmək üçün hər barkod üçün şəkil göndərməyə davam edin. /done yazaraq bitirə bilərsiniz.\n"
        "Hər hansı köməyə ehtiyac olarsa /help yazın.\n"
        "Prosesi ləğv etmək üçün /cancel yazın.\n\n"
        "İlk olaraq barkodun şəklini göndərin. ✅"
    )
    bot.send_message(chat_id, text)


# --- 6) /help command ---
@bot.message_handler(commands=['help'])
def handle_help(message):
    chat_id = message.chat.id
    text = (
        "ℹ️ Kömək:\n"
        "- /start: Yeni prosesə başlayır.\n"
        "- /done: Bütün məhsulları əlavə etməyi bitirir.\n"
        "- /cancel: Mövcud əməliyyatı ləğv edir.\n"
        "- Barkod şəkli göndərin -> Bot oxumağa çalışacaq.\n"
        "- Barkod oxunmazsa, bot OCR ilə 'AZT...' axtaracaq və ya əl ilə barkod daxil edə bilərsiniz.\n"
        "- Sonra aktivin adını və miqdarını daxil edirsiniz.\n"
        "- Məlumat Google Sheets-ə yazılır.\n"
        "Qeyd: Sualınız varsa, burada botu yoxlayın və ya komanda rəhbərinizlə əlaqə saxlayın."
    )
    bot.send_message(chat_id, text)


# --- 7) /cancel command ---
@bot.message_handler(commands=['cancel'])
def handle_cancel(message):
    chat_id = message.chat.id
    user_state[chat_id] = STATE_IDLE
    bot.send_message(chat_id, "Əməliyyat ləğv edildi. Yeni proses üçün /start yazın.")


# --- 8) /done command (finish multi-scan) ---
@bot.message_handler(commands=['done'])
def handle_done(message):
    chat_id = message.chat.id
    user_state[chat_id] = STATE_IDLE
    bot.send_message(chat_id, "Bütün barkodlar əlavə olundu. Təşəkkürlər! Yeni proses üçün /start yazın.")


# --- 9) PHOTO HANDLER ---
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    state = user_state.get(chat_id, STATE_IDLE)

    if state == STATE_IDLE:
        bot.send_message(chat_id, "Prosesə başlamaq üçün /start yazın.")
        return

    if state != STATE_WAITING_PHOTO:
        bot.send_message(chat_id, "Hazırda fotoya ehtiyac yoxdur. Zəhmət olmasa addımları izləyin.")
        return

    # Download the photo
    file_id = message.photo[-1].file_id
    file_info = bot.get_file(file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    img_path = f"temp_{chat_id}.jpg"
    with open(img_path, "wb") as f:
        f.write(downloaded_file)

    # Attempt to decode barcode
    barcode_value = decode_barcode(img_path)

    if barcode_value:
        user_data[chat_id]['barcode'] = barcode_value
        bot.send_message(chat_id, f"✅ Barkod aşkarlandı: {barcode_value}\nİndi aktivin adını yazın.")
        user_state[chat_id] = STATE_WAITING_DESCRIPTION
    else:
        # Offer fallback: manual input
        bot.send_message(
            chat_id,
            "⚠ Barkod/QR tapılmadı. Daha yaxın/gözəl şəkil çəkin,\n"
            "yaxud /cancel yazaraq ləğv edin,\n"
            "və ya əl ilə barkod daxil etmək üçün /manual yazın."
        )
        # We'll remain in WAITING_PHOTO until user decides:
        # either retake photo or type /manual, or /cancel, or /done.


# --- 10) /manual command -> fallback manual barcode
@bot.message_handler(commands=['manual'])
def handle_manual(message):
    chat_id = message.chat.id
    state = user_state.get(chat_id, STATE_IDLE)

    if state != STATE_WAITING_PHOTO:
        bot.send_message(chat_id, "Bu mərhələdə əl ilə barkod daxil etməyə icazə verilmir.")
        return

    user_state[chat_id] = STATE_MANUAL_BARCODE
    bot.send_message(chat_id, "ℹ️ Barkodu əl ilə daxil edin (məsələn: AZT10013025).")


# --- 11) TEXT HANDLER (for description, quantity, or manual barcode)
@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    text = message.text.strip()
    state = user_state.get(chat_id, STATE_IDLE)

    # If they're idle or something
    if state == STATE_IDLE:
        bot.send_message(chat_id, "Prosesə başlamaq üçün /start yazın.")
        return

    if state == STATE_WAITING_PHOTO:
        # Possibly they typed something while we want a photo
        bot.send_message(chat_id, "Zəhmət olmasa barkodun şəklini göndərin və ya /manual yazın.")
        return

    if state == STATE_MANUAL_BARCODE:
        # The user is entering the barcode manually
        user_data[chat_id]['barcode'] = text
        bot.send_message(chat_id, f"Əl ilə barkod götürüldü: {text}\nİndi aktivin adını yazın.")
        user_state[chat_id] = STATE_WAITING_DESCRIPTION
        return

    if state == STATE_WAITING_DESCRIPTION:
        user_data[chat_id]['description'] = text
        bot.send_message(chat_id, "🔢 İndi miqdarı daxil edin (rəqəm).")
        user_state[chat_id] = STATE_WAITING_QUANTITY
        return

    if state == STATE_WAITING_QUANTITY:
        # parse quantity
        try:
            qty = int(text)
            user_data[chat_id]['quantity'] = qty
        except ValueError:
            bot.send_message(chat_id, "❌ Xəta! Zəhmət olmasa miqdarı rəqəm kimi daxil edin.")
            return

        # Save to sheets
        if save_to_sheets(chat_id):
            # Summarize to user
            bcode = user_data[chat_id]['barcode']
            desc = user_data[chat_id]['description']
            bot.send_message(
                chat_id,
                f"✅ Məlumat qeydə alındı:\nBarkod: {bcode}\nAd: {desc}\nMiqdar: {qty}\n"
                "Yeni barkod üçün yenidən şəkil göndərin və ya /done yazın bitirmək üçün."
            )
        else:
            bot.send_message(chat_id, "❌ Məlumatı saxlayarkən problem yarandı. Xahiş edirəm yenidən cəhd edin.")

        # Reset for next item
        user_state[chat_id] = STATE_WAITING_PHOTO
        user_data[chat_id] = {'barcode': None, 'description': None, 'quantity': None}


# --- 12) decode_barcode (Region-based detection + angle increments + fallback OCR) ---
def decode_barcode(image_path):
    # Step A: Try advanced region-based detection with zbar
    cv_img = cv2.imread(image_path)
    if cv_img is None:
        print("OpenCV failed to load image.")
        return None

    # Attempt robust detection
    zbar_result = robust_zbar_decode(cv_img)
    if zbar_result:
        return zbar_result

    # Step B: OCR fallback for "AZT..." or others
    fallback_text = do_ocr_fallback(image_path)
    return fallback_text


def robust_zbar_decode(cv_img):
    """
    1) find_barcode_regions -> crop candidate regions
    2) rotate angles: 0,90,180,270
    3) decode with zbar
    Return first successful decode or None
    """
    regions = find_barcode_regions(cv_img)
    # If no candidate region, try entire image as last resort
    if not regions:
        entire_result = try_zbar_on_image(cv_img)
        return entire_result

    angles = [0, 90, 180, 270]
    for (x, y, w, h, crop) in regions:
        for ang in angles:
            rotated = rotate_image(crop, ang)
            result = try_zbar_on_image(rotated)
            if result:
                return result

    return None


def find_barcode_regions(cv_img):
    """
    Convert to grayscale, threshold, morphological close.
    Return boundingRect for candidate barcode regions.
    """
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3,3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    # invert so bars are white
    inv = 255 - thresh

    # Morph close horizontally
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9,3))
    morph = cv2.morphologyEx(inv, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(morph, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    regions = []
    h_img, w_img = gray.shape[:2]

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        # Heuristic filters
        if w > 40 and h > 15:  # tweak as needed
            # Extract region
            region = cv_img[y:y+h, x:x+w]
            regions.append((x, y, w, h, region))

    return regions


def rotate_image(cv_img, angle):
    (h, w) = cv_img.shape[:2]
    center = (w//2, h//2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(cv_img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return rotated


def try_zbar_on_image(cv_img):
    """
    Convert OpenCV image to PIL, decode with pyzbar
    """
    from PIL import Image
    pil_img = Image.fromarray(cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB))
    barcodes = decode(pil_img)
    if barcodes:
        return barcodes[0].data.decode("utf-8")
    return None


# --- 13) OCR Fallback ---
def do_ocr_fallback(image_path):
    """
    If zbar fails, do Tesseract for 'AZT...' or other text codes.
    e.g. if your code is 'AZT\d+'
    """
    # Basic deskew or contrast can be added
    cv_img = cv2.imread(image_path)
    if cv_img is None:
        return None

    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    text = pytesseract.image_to_string(gray, lang='eng')
    # Look for AZT pattern
    match = re.search(r'(AZT\d+)', text.upper())
    if match:
        return match.group(1)
    # else return None, or entire text if needed
    return None


# --- 14) Save to Google Sheets ---
def save_to_sheets(chat_id):
    if not sheet:
        return False
    try:
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # fetch user data
        # your user_data structure
        barcode = user_data[chat_id].get('barcode', '(Barkod yoxdur)')
        desc = user_data[chat_id].get('description', '(Ad yoxdur)')
        qty = user_data[chat_id].get('quantity', 0)

        row = [date_str, barcode, desc, qty]
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
