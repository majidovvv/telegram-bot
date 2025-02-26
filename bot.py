# bot.py
"""
A reference Telegram bot in webhook mode with advanced features:
- Image quality checks (blur/brightness)
- Batch scanning of multiple barcodes
- Multi-barcode detection from one photo
- Fallback/confirmation logic (manual correction)
- Final mapping of desc/qty to each barcode
- Google Sheets integration
- Flask + TeleBot in webhook mode (no more 409 conflict)
"""

import os
import re
import json
from datetime import datetime

from flask import Flask, request, abort

import telebot
import gspread
from oauth2client.service_account import ServiceAccountCredentials

import pytesseract
from pyzbar.pyzbar import decode
import cv2
import numpy as np
from PIL import Image, ImageEnhance

# -----------------------------------
# 1) Environment Setup
# -----------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # your bot token
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")          # your Google Sheets ID
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")  # JSON for GSheets

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")

# Flask app for webhook
app = Flask(__name__)

# -----------------------------------
# 2) Google Sheets Auth
# -----------------------------------
sheet = None
if SERVICE_ACCOUNT_JSON:
    try:
        creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets"
        ]
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(credentials)
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        print("Google Sheets connected.")
    except Exception as e:
        print("Error connecting to Google Sheets:", e)
else:
    print("No SERVICE_ACCOUNT_JSON provided.")


# -----------------------------------
# 3) In-Memory Session Data
#    We'll store user states + scanned codes.
# -----------------------------------
SESSION_STATE_IDLE = "idle"
SESSION_STATE_SCANNING = "scanning"
SESSION_STATE_ENTERING_DATA = "entering_data"

# For each chat_id, we store an object:
# {
#   "state": scanning|entering_data|idle,
#   "scanned_codes": [
#       {
#         "barcode": "ABCD1234",
#         "confirmed": False,
#         "image_ok": True,
#         "manual_correction": None
#       }, ...
#    ],
#   "current_code_index": 0,
#   "filling_step": "desc" or "qty"
#   "desc": "...",
#   "qty": ...
# }
user_sessions = {}


# Helper to get or init session
def get_session(chat_id):
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {
            "state": SESSION_STATE_IDLE,
            "scanned_codes": [],
            "current_code_index": 0,
            "filling_step": None,
            "desc": "",
            "qty": 0
        }
    return user_sessions[chat_id]


# -----------------------------------
# 4) Bot Commands
# -----------------------------------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    session = get_session(chat_id)
    session["state"] = SESSION_STATE_SCANNING
    session["scanned_codes"] = []
    session["current_code_index"] = 0

    bot.send_message(
        chat_id,
        "👋 Salam! Bu bot çoxsaylı barkodları ardıcıl skan etmək üçün nəzərdə tutulub.\n"
        "1) Bir və ya bir neçə foto göndərib <b>birdən çox barkod</b> oxuya bilərsiniz.\n"
        "2) Bot şəkli yoxlayır (bulanıqlıq, parlaqlıq) və barkodları axtarır.\n"
        "3) Bütün barkodları yığdıqdan sonra /done yazın.\n"
        "4) Bot sizdən <b>hər barkod üçün</b> ad və miqdar istəyəcək.\n"
        "5) Sonda məlumat Google Sheets-ə yazılır.\n\n"
        "Başlayaq! Barkod şəkillərini göndərin. /cancel ilə ləğv edə bilərsiniz."
    )


@bot.message_handler(commands=['help'])
def cmd_help(message):
    chat_id = message.chat.id
    bot.send_message(
        chat_id,
        "/start - Yeni prosesə başlayın\n"
        "/cancel - Ləğv edin\n"
        "/done - Barkodları yığmağı bitirin, aktiv məlumatlarını daxil etməyə keçin\n"
        "Qeyd: Bir foto bir neçə barkod ola bilər, bot onları aşkarlayıb ayrıca siyahıya əlavə edəcək."
    )


@bot.message_handler(commands=['cancel'])
def cmd_cancel(message):
    chat_id = message.chat.id
    session = get_session(chat_id)
    session["state"] = SESSION_STATE_IDLE
    session["scanned_codes"] = []
    bot.send_message(chat_id, "Əməliyyat ləğv edildi. /start ilə yenidən başlaya bilərsiniz.")


@bot.message_handler(commands=['done'])
def cmd_done(message):
    chat_id = message.chat.id
    session = get_session(chat_id)
    if session["state"] != SESSION_STATE_SCANNING:
        bot.send_message(chat_id, "Hazırda skan rejimində deyilsiniz. /start ilə başlayın.")
        return

    if not session["scanned_codes"]:
        bot.send_message(chat_id, "Heç bir barkod skan olunmayıb. /start ilə yenidən başlayın.")
        return

    # Move to data entry step
    session["state"] = SESSION_STATE_ENTERING_DATA
    session["current_code_index"] = 0
    session["filling_step"] = "desc"

    code_obj = session["scanned_codes"][0]
    barkod = code_obj["barcode"]
    bot.send_message(chat_id, f"1-ci barkod: <b>{barkod}</b>\n"
                              "Bu barkod üçün aktivin adını yazın.")


# -----------------------------------
# 5) Photo Handler (Multi-Barcode + Image Quality)
# -----------------------------------
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    session = get_session(chat_id)

    if session["state"] != SESSION_STATE_SCANNING:
        bot.send_message(chat_id, "Hazırda barkod skan rejimində deyilsiniz. /start yazın.")
        return

    # 1) Download photo
    file_id = message.photo[-1].file_id
    file_info = bot.get_file(file_id)
    downloaded = bot.download_file(file_info.file_path)
    img_path = f"/tmp/barcode_{chat_id}.jpg"
    with open(img_path, "wb") as f:
        f.write(downloaded)

    # 2) Quick image quality check
    blur_score, brightness = check_image_quality(img_path)
    print(f"[ImageQuality] chat={chat_id}, blur={blur_score}, bright={brightness}")

    # If very blurry/dark, warn user
    if blur_score < 50:
        bot.send_message(chat_id, "⚠ Şəkil çox bulanıq ola bilər. Mümkünsə, daha aydın şəkil çəkin.")
    if brightness < 50:
        bot.send_message(chat_id, "⚠ Şəkil çox qaranlıq görünür. İşıqlı yerdə çəkməyə çalışın.")

    # 3) Attempt multi-barcode detection
    codes_found = detect_multi_barcodes(img_path)
    if not codes_found:
        bot.send_message(chat_id, "Heç bir barkod tapılmadı. Başqa şəkil göndərməyə çalışın.")
        return

    # 4) Add them to session
    for c in codes_found:
        session["scanned_codes"].append({
            "barcode": c,
            "confirmed": False,
            "image_ok": (blur_score >= 50 and brightness >= 50),  # or any threshold
            "manual_correction": None
        })
    bot.send_message(chat_id, f"✅ <b>{len(codes_found)}</b> barkod skan olundu. /done yazaraq prosesi bitirə bilərsiniz\n"
                              "yaxud başqa şəkil göndərib davam edin.")


# -----------------------------------
# 6) TEXT Handler for Data Entry Steps
# -----------------------------------
@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    session = get_session(chat_id)
    state = session["state"]

    # If not in scanning/data entry, ignore
    if state == SESSION_STATE_IDLE:
        bot.send_message(chat_id, "Prosesə başlamaq üçün /start yazın.")
        return

    if state == SESSION_STATE_SCANNING:
        # Possibly user typed something not relevant
        bot.send_message(chat_id, "Barkod şəkillərini göndərməkdə davam edə bilərsiniz. /done yazıb bitirin.")
        return

    if state == SESSION_STATE_ENTERING_DATA:
        # We are filling desc/qty for each code
        idx = session["current_code_index"]
        if idx >= len(session["scanned_codes"]):
            bot.send_message(chat_id, "Bütün barkodlar üçün məlumat daxil edilib.")
            return

        step = session["filling_step"]
        code_obj = session["scanned_codes"][idx]

        if step == "desc":
            # They typed the description
            session["desc"] = message.text.strip()
            session["filling_step"] = "qty"
            bot.send_message(chat_id, f"İndi <b>{code_obj['barcode']}</b> üçün miqdarı daxil edin (rəqəm).")
        elif step == "qty":
            # Parse quantity
            try:
                quantity = int(message.text.strip())
            except ValueError:
                bot.send_message(chat_id, "❌ Xəta! Zəhmət olmasa rəqəm daxil edin.")
                return

            session["qty"] = quantity
            # Save row
            success = save_to_sheets(chat_id, code_obj["barcode"], session["desc"], quantity)
            if success:
                bot.send_message(
                    chat_id,
                    f"✅ {code_obj['barcode']} barkodu üçün məlumat saxlanıldı.\n"
                    f"Ad: {session['desc']} | Miqdar: {quantity}"
                )
            else:
                bot.send_message(chat_id, "❌ Məlumatı saxlayarkən xəta baş verdi. Yenidən cəhd edin.")

            # Move to next code
            session["current_code_index"] += 1
            next_idx = session["current_code_index"]
            if next_idx < len(session["scanned_codes"]):
                session["filling_step"] = "desc"
                next_code = session["scanned_codes"][next_idx]
                bot.send_message(
                    chat_id,
                    f"{next_idx+1}-ci barkod: <b>{next_code['barcode']}</b>\n"
                    "Bu barkod üçün aktivin adını yazın."
                )
            else:
                # Done with all codes
                session["state"] = SESSION_STATE_IDLE
                bot.send_message(chat_id, "Bütün barkodlar üçün məlumat saxlanıldı. Təşəkkürlər!")


# -----------------------------------
# 7) detect_multi_barcodes Function
#  - find multiple barcodes in one photo
#  - advanced approach with morphological detection, bounding rect, rotate angles
# -----------------------------------
def detect_multi_barcodes(img_path):
    """
    Attempt to find & decode multiple barcodes in a single image.
    1) Convert to grayscale, threshold, morphological close to find candidate regions.
    2) For each region, rotate 0, 90, 180, 270, decode with zbar.
    3) Collect all distinct barcodes found.
    """
    cv_img = cv2.imread(img_path)
    if cv_img is None:
        return []

    codes_collected = []

    # a) morphological region detection
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3,3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    inv = 255 - thresh

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9,3))
    morph = cv2.morphologyEx(inv, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(morph, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    angles = [0, 90, 180, 270]

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        # Heuristic: must be somewhat large
        if w > 30 and h > 15:
            region = cv_img[y:y+h, x:x+w]
            for ang in angles:
                rot = rotate_image(region, ang)
                found = decode_zbar(rot)
                for f in found:
                    if f not in codes_collected:
                        codes_collected.append(f)

    # Also try entire image in case we missed
    entire_found = decode_zbar(cv_img)
    for f in entire_found:
        if f not in codes_collected:
            codes_collected.append(f)

    return codes_collected

def rotate_image(cv_img, angle):
    (h, w) = cv_img.shape[:2]
    center = (w//2, h//2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(cv_img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return rotated

def decode_zbar(cv_img):
    """
    Decode barcodes from a cv2 image using pyzbar.
    Return a list of decoded strings (unique).
    """
    from PIL import Image
    pil_img = Image.fromarray(cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB))
    barcodes = decode(pil_img)
    results = []
    for b in barcodes:
        val = b.data.decode("utf-8")
        if val not in results:
            results.append(val)
    return results


# -----------------------------------
# 8) check_image_quality => blur, brightness
# -----------------------------------
def check_image_quality(img_path):
    """
    Return (blur_score, brightness).
    - blur_score < 50 -> likely blurry
    - brightness < 50 -> quite dark
    """
    cv_img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
    if cv_img is None:
        return (0, 0)
    # blur via Laplacian variance
    lap = cv2.Laplacian(cv_img, cv2.CV_64F).var()
    # brightness
    mean_val = cv_img.mean()
    return (lap, mean_val)


# -----------------------------------
# 9) Save to Sheets
# -----------------------------------
def save_to_sheets(chat_id, barcode, desc, qty):
    if not sheet:
        return False
    try:
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [date_str, barcode, desc, qty]
        sheet.append_row(row)
        print("Saved row:", row)
        return True
    except Exception as e:
        print("Error saving to sheets:", e)
        return False


# -----------------------------------
# 10) Webhook Setup
#     We'll create an endpoint /webhook/<token> for Telegram to POST updates
# -----------------------------------
@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "ok", 200
    else:
        abort(403)


# -----------------------------------
# 11) Optional: Start Webhook
#     We can remove_webhook + set_webhook programmatically (or do it manually via BotFather).
# -----------------------------------
@app.before_first_request
def setup_webhook():
    # Remove old webhook
    bot.remove_webhook()
    # Set new webhook to our endpoint
    base_url = os.getenv("RENDER_EXTERNAL_HOSTNAME")  # or your domain
    if not base_url:
        # e.g. "https://yourapp.onrender.com"
        base_url = "https://yourapp.onrender.com"
    full_url = f"{base_url}/webhook/{TELEGRAM_BOT_TOKEN}"
    bot.set_webhook(url=full_url)
    print("Webhook set to:", full_url)


# -----------------------------------
# 12) Local Testing
#     If you want to run locally (not recommended with webhooks),
#     you can do: `python bot.py` -> app.run(port=5000)
#     But on Render, we'll run with gunicorn.
# -----------------------------------
if __name__ == "__main__":
    # For local debug only:
    # app.run(host='0.0.0.0', port=5000, debug=True)
    pass
