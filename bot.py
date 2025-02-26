# bot.py

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
from datetime import datetime

########################################
# 1) Flask App + Bot Initialization
########################################
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

########################################
# 2) Google Sheets Connection
########################################
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

########################################
# 3) Session Data In-Memory
########################################
SESSION_STATE_IDLE = "idle"
SESSION_STATE_SCANNING = "scanning"
SESSION_STATE_ENTERING_DATA = "entering_data"

user_sessions = {}  # chat_id -> { session data }

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

########################################
# 4) Bot Commands
########################################
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
        "/done - Barkodları yığmağı bitirin, aktiv məlumatlarını daxil etməyə keçin"
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
        bot.send_message(chat_id, "Heç bir barkod skan olunmayıb.")
        return

    # Move to data entry
    session["state"] = SESSION_STATE_ENTERING_DATA
    session["current_code_index"] = 0
    session["filling_step"] = "desc"

    first_code = session["scanned_codes"][0]["barcode"]
    bot.send_message(chat_id, f"1-ci barkod: <b>{first_code}</b>\nBu barkod üçün aktivin adını yazın.")

########################################
# 5) PHOTO Handler
########################################
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    session = get_session(chat_id)
    if session["state"] != SESSION_STATE_SCANNING:
        bot.send_message(chat_id, "Hazırda barkod skan rejimində deyilsiniz. /start yazın.")
        return

    # Download
    file_id = message.photo[-1].file_id
    info = bot.get_file(file_id)
    downloaded = bot.download_file(info.file_path)
    img_path = f"/tmp/{chat_id}_barcode.jpg"
    with open(img_path, "wb") as f:
        f.write(downloaded)

    # Check image quality
    blur_score, brightness = check_image_quality(img_path)
    if blur_score < 50:
        bot.send_message(chat_id, "⚠ Şəkil çox bulanıq ola bilər.")
    if brightness < 50:
        bot.send_message(chat_id, "⚠ Şəkil çox qaranlıq görünür.")

    # Detect multi barcodes
    codes_found = detect_multi_barcodes(img_path)
    if not codes_found:
        bot.send_message(chat_id, "Heç bir barkod tapılmadı bu şəkildən.")
        return

    for c in codes_found:
        session["scanned_codes"].append({
            "barcode": c,
            "confirmed": False
        })
    bot.send_message(chat_id, f"✅ Bu şəkildə <b>{len(codes_found)}</b> barkod tapıldı. Başqa şəkil göndərə və ya /done yazıb bitirə bilərsiniz.")

########################################
# 6) TEXT Handler for data
########################################
@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    session = get_session(chat_id)
    state = session["state"]

    if state == SESSION_STATE_IDLE:
        bot.send_message(chat_id, "Prosesə başlamaq üçün /start yazın.")
        return

    if state == SESSION_STATE_SCANNING:
        # Possibly typed something while scanning
        bot.send_message(chat_id, "Barkod şəkillərini göndərməyə davam edin və ya /done yazın.")
        return

    if state == SESSION_STATE_ENTERING_DATA:
        idx = session["current_code_index"]
        if idx >= len(session["scanned_codes"]):
            bot.send_message(chat_id, "Bütün barkodlar üçün məlumat daxil edilib.")
            return

        step = session["filling_step"]
        code_obj = session["scanned_codes"][idx]
        if step == "desc":
            session["desc"] = message.text.strip()
            session["filling_step"] = "qty"
            bot.send_message(chat_id, f"İndi <b>{code_obj['barcode']}</b> üçün miqdar daxil edin (rəqəm).")
        elif step == "qty":
            try:
                quantity = int(message.text.strip())
            except ValueError:
                bot.send_message(chat_id, "❌ Zəhmət olmasa rəqəm daxil edin.")
                return

            # Save row
            success = save_to_sheets(chat_id, code_obj["barcode"], session["desc"], quantity)
            if success:
                bot.send_message(chat_id, f"✅ {code_obj['barcode']} üçün saxlanıldı: {session['desc']} / {quantity}")
            else:
                bot.send_message(chat_id, "❌ Xəta, saxlanmadı.")

            session["current_code_index"] += 1
            if session["current_code_index"] < len(session["scanned_codes"]):
                session["filling_step"] = "desc"
                next_code = session["scanned_codes"][session["current_code_index"]]["barcode"]
                bot.send_message(chat_id, f"{session['current_code_index']+1}-ci barkod: <b>{next_code}</b>\n"
                                          "Bu barkod üçün aktivin adını yazın.")
            else:
                session["state"] = SESSION_STATE_IDLE
                bot.send_message(chat_id, "Bütün barkodlar üçün məlumat saxlanıldı. Təşəkkürlər!")


########################################
# 7) detect_multi_barcodes
########################################
def detect_multi_barcodes(img_path):
    cv_img = cv2.imread(img_path)
    if cv_img is None:
        return []

    codes_collected = []
    # Morph approach
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
        if w > 30 and h > 15:
            region = cv_img[y:y+h, x:x+w]
            for ang in angles:
                rot = rotate_image(region, ang)
                found = decode_zbar(rot)
                for f in found:
                    if f not in codes_collected:
                        codes_collected.append(f)

    # Also try entire image
    entire = decode_zbar(cv_img)
    for e in entire:
        if e not in codes_collected:
            codes_collected.append(e)
    return codes_collected

def rotate_image(cv_img, angle):
    (h, w) = cv_img.shape[:2]
    center = (w//2, h//2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(cv_img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return rotated

def decode_zbar(cv_img):
    from PIL import Image
    pil_img = Image.fromarray(cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB))
    barcodes = decode(pil_img)
    results = []
    for b in barcodes:
        val = b.data.decode("utf-8")
        if val not in results:
            results.append(val)
    return results

########################################
# 8) check_image_quality
########################################
def check_image_quality(img_path):
    cv_img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
    if cv_img is None:
        return (0, 0)
    lap = cv2.Laplacian(cv_img, cv2.CV_64F).var()
    mean_val = cv_img.mean()
    return (lap, mean_val)

########################################
# 9) save_to_sheets
########################################
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

########################################
# 10) Webhook Endpoint
########################################
@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "ok", 200
    return abort(403)

########################################
# 11) Set Webhook Programmatically
########################################
def setup_webhook():
    bot.remove_webhook()
    base_url = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if not base_url:
        base_url = "https://<yourapp>.onrender.com"  # or your custom domain
    full_url = f"{base_url}/webhook/{TELEGRAM_BOT_TOKEN}"
    bot.set_webhook(url=full_url)
    print("Webhook set to:", full_url)

setup_webhook()

########################################
# 12) Gunicorn Entry
########################################
# Gunicorn will look for 'app' (Flask) so we do not run app.run() here.
# Everything is ready for the server to handle requests.
