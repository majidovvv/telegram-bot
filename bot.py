import os
import json
import cv2
import numpy as np
from datetime import datetime

from flask import Flask, request, abort
import telebot

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from pyzbar.pyzbar import decode
import pytesseract
from thefuzz import process
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

########################################
# Environment Variables
########################################
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
ASSET_TAB_NAME = "Asset Data"  # Name of the sheet containing asset list

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set")

# Initialize bot & Flask
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

########################################
# Google Sheets Setup
########################################
creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets"
]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(credentials)

# Main sheet for final data
main_sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
print("Connected to main sheet:", main_sheet.title)

# Asset Data from "Asset Data" tab
try:
    asset_worksheet = gc.open_by_key(SPREADSHEET_ID).worksheet(ASSET_TAB_NAME)
    asset_data = asset_worksheet.col_values(1)  # all values in first column
    # remove header if present
    if asset_data and asset_data[0].lower().startswith("asset"):
        asset_data.pop(0)
except Exception as e:
    print("Error loading asset data from tab:", ASSET_TAB_NAME, e)
    asset_data = []

print(f"Loaded {len(asset_data)} asset names from '{ASSET_TAB_NAME}' tab.")

########################################
# In-memory session data
########################################
user_mode = {}   # chat_id -> "single" or "multi"
user_data = {}   # chat_id -> { "barcode_list": [...], "index": int, "asset_name": str, "qty": int }
user_state = {}  # chat_id -> states

# Possible states
STATE_IDLE = "idle"
STATE_WAIT_PHOTO = "wait_photo"
STATE_WAIT_ASSET = "wait_asset"
STATE_WAIT_ASSET_PICK = "wait_asset_pick"
STATE_WAIT_QUANTITY = "wait_quantity"

def init_session(chat_id):
    user_mode[chat_id] = "single"
    user_data[chat_id] = {
        "barcode_list": [],
        "index": 0,
        "asset_name": "",
        "qty": 0
    }
    user_state[chat_id] = STATE_IDLE

########################################
# Fuzzy Suggest
########################################
def fuzzy_suggest(query, data, limit=3):
    if not data:
        return []
    results = process.extract(query, data, limit=limit)
    # e.g. [("Karel DS200 16/R", 90), ...]
    return results

########################################
# Barcode Scanning
########################################
def preprocess_image(np_img):
    gray = cv2.cvtColor(np_img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3,3), 0)
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    return thresh

def scan_barcode(np_img):
    # Attempt zbar
    processed = preprocess_image(np_img)
    codes = decode(processed)
    if codes:
        return codes[0].data.decode("utf-8")
    # fallback: OCR
    text = pytesseract.image_to_string(processed, config='--psm 6')
    cleaned = "".join(filter(str.isalnum, text))
    return cleaned if cleaned else "Barkod tapılmadı"

########################################
# Single vs. Multi mode
########################################
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    init_session(chat_id)

    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("Tək Barkod", callback_data="MODE_SINGLE"),
        InlineKeyboardButton("Çox Barkod", callback_data="MODE_MULTI")
    )
    bot.send_message(chat_id,
        "👋 Salam! Bu bot anbar aktivlərinin qeydiyyatı üçündür.\n"
        "Zəhmət olmasa rejimi seçin:\n"
        "- Tək Barkod: Bir barkod üçün sürətli emal.\n"
        "- Çox Barkod: Bir neçə barkod şəkli göndərib sonda hamısını əlavə edin.",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data in ["MODE_SINGLE", "MODE_MULTI"])
def pick_mode(call):
    chat_id = call.message.chat.id
    mode = call.data
    user_mode[chat_id] = ("single" if mode == "MODE_SINGLE" else "multi")
    user_data[chat_id]["barcode_list"] = []
    user_data[chat_id]["index"] = 0

    if mode == "MODE_SINGLE":
        bot.send_message(chat_id,
            "Tək barkod rejimi seçildi. Zəhmət olmasa barkodun şəklini göndərin.\n"
            "İstənilən vaxt /cancel yaza bilərsiniz.")
        user_state[chat_id] = STATE_WAIT_PHOTO
    else:
        bot.send_message(chat_id,
            "Çox barkod rejimi seçildi.\n"
            "Bir neçə barkod şəkli göndərə bilərsiniz.\n"
            "Bitirəndə 'Bitir' düyməsini basın.")
        user_state[chat_id] = STATE_WAIT_PHOTO

        # Provide a "Bitir" button
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("Bitir", callback_data="FINISH_MULTI"))
        bot.send_message(chat_id,
            "Barkod şəkillərini göndərməyə başlayın.\n"
            "Sonda bitirmək üçün aşağıdakı düyməni basın.",
            reply_markup=kb
        )

@bot.callback_query_handler(func=lambda c: c.data == "FINISH_MULTI")
def finish_multi_mode(call):
    chat_id = call.message.chat.id
    barcodes = user_data[chat_id]["barcode_list"]
    if not barcodes:
        bot.send_message(chat_id, "Heç barkod qəbul etmədik. Yenidən şəkil göndərməyə cəhd edin.")
        return
    # Now we proceed to name/qty for the first barcode
    user_data[chat_id]["index"] = 0
    first_bc = barcodes[0]
    bot.send_message(chat_id,
        f"1-ci barkod: <b>{first_bc}</b>\n"
        "Məhsul adını (tam və ya qismən) daxil edin."
    )
    user_state[chat_id] = STATE_WAIT_ASSET

########################################
# Photo Handler
########################################
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    state = user_state.get(chat_id, STATE_IDLE)

    if state != STATE_WAIT_PHOTO:
        # Possibly multi mode accepting photos, or invalid state
        if user_mode.get(chat_id) == "multi":
            # Add the barkod to barcode_list
            file_id = message.photo[-1].file_id
            info = bot.get_file(file_id)
            downloaded = bot.download_file(info.file_path)
            np_img = np.frombuffer(downloaded, np.uint8)
            cv_img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

            bc = scan_barcode(cv_img)
            user_data[chat_id]["barcode_list"].append(bc)
            bot.send_message(chat_id,
                f"Barkod tapıldı: <b>{bc}</b>\nDaha şəkil göndərməyi davam edin, və ya 'Bitir' düyməsi.")
        else:
            bot.send_message(chat_id,
                "Hazırda foto qəbul edilmir. /start və ya /cancel istifadə edin.")
        return

    # If we are in WAIT_PHOTO and single mode
    if user_mode[chat_id] == "single":
        # decode the single barkod
        file_id = message.photo[-1].file_id
        info = bot.get_file(file_id)
        downloaded = bot.download_file(info.file_path)
        np_img = np.frombuffer(downloaded, np.uint8)
        cv_img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

        bc = scan_barcode(cv_img)
        user_data[chat_id]["barcode_list"] = [bc]
        bot.send_message(chat_id,
            f"Barkod: <b>{bc}</b>\nZəhmət olmasa məhsul adını (tam və ya qismən) daxil edin.")
        user_state[chat_id] = STATE_WAIT_ASSET

########################################
# Wait for Asset Name
########################################
@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_ASSET, content_types=['text'])
def handle_asset_name_input(m):
    chat_id = m.chat.id
    query = m.text.strip()

    suggestions = fuzzy_suggest(query, asset_data, limit=3)
    if not suggestions:
        bot.send_message(chat_id, "Heç bir uyğun ad tapılmadı. Yenidən cəhd edin.")
        return

    # Build inline keyboard
    kb = InlineKeyboardMarkup()
    for (name, score) in suggestions:
        kb.add(InlineKeyboardButton(
            text=f"{name} ({score}%)",
            callback_data=f"ASSET_PICK|{name}"
        ))
    bot.send_message(chat_id,
        "Uyğun ola biləcək adlar, aşağıdan seçin və ya başqa ad yazın:",
        reply_markup=kb
    )

    user_state[chat_id] = STATE_WAIT_ASSET_PICK

# If user types another partial name instead of tapping a button
@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_ASSET_PICK, content_types=['text'])
def handle_asset_retry(m):
    chat_id = m.chat.id
    query = m.text.strip()
    suggestions = fuzzy_suggest(query, asset_data, limit=3)
    if not suggestions:
        bot.send_message(chat_id, "Heç bir uyğun ad tapılmadı. Yenidən cəhd edin.")
        return

    kb = InlineKeyboardMarkup()
    for (name, score) in suggestions:
        kb.add(InlineKeyboardButton(
            text=f"{name} ({score}%)",
            callback_data=f"ASSET_PICK|{name}"
        ))
    bot.send_message(chat_id,
        "Uyğun ola biləcək adlar, aşağıdan seçin və ya başqa ad yazın:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ASSET_PICK|"))
def pick_asset_callback(call):
    chat_id = call.message.chat.id
    chosen_name = call.data.split("|")[1]

    user_data[chat_id]["asset_name"] = chosen_name
    bot.send_message(chat_id,
        f"Seçdiyiniz ad: <b>{chosen_name}</b>\nZəhmət olmasa miqdar (rəqəm) daxil edin.")
    user_state[chat_id] = STATE_WAIT_QUANTITY

########################################
# Wait for Quantity
########################################
@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == STATE_WAIT_QUANTITY, content_types=['text'])
def handle_quantity(m):
    chat_id = m.chat.id
    pick = m.text.strip()
    try:
        q = int(pick)
    except:
        bot.send_message(chat_id, "Zəhmət olmasa düzgün rəqəm daxil edin.")
        return

    user_data[chat_id]["qty"] = q
    # We finalize for the current barkod
    bc_list = user_data[chat_id]["barcode_list"]
    idx = user_data[chat_id]["index"]

    if idx >= len(bc_list):
        bot.send_message(chat_id, "Xəta: barkod siyahısı tükəndi.")
        user_state[chat_id] = STATE_IDLE
        return

    bc = bc_list[idx]
    desc = user_data[chat_id]["asset_name"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Save to main sheet
    try:
        main_sheet.append_row([now, bc, desc, q])
        bot.send_message(chat_id,
            f"✅ Qeyd edildi:\nTarix: {now}\nBarkod: {bc}\nAd: {desc}\nSay: {q}"
        )
    except Exception as e:
        bot.send_message(chat_id, f"❌ Xəta cədvələ əlavə edilərkən: {e}")

    # Next step if multi
    if user_mode[chat_id] == "multi":
        user_data[chat_id]["index"] += 1
        if user_data[chat_id]["index"] < len(bc_list):
            # go next
            next_bc = bc_list[user_data[chat_id]["index"]]
            bot.send_message(chat_id,
                f"{user_data[chat_id]['index']+1}-ci barkod: <b>{next_bc}</b>\n"
                "Məhsul adını (tam və ya qismən) daxil edin."
            )
            user_state[chat_id] = STATE_WAIT_ASSET
        else:
            bot.send_message(chat_id,
                "Bütün barkodların məlumatı əlavə edildi! Təşəkkürlər.")
            user_state[chat_id] = STATE_IDLE
    else:
        # single mode done
        bot.send_message(chat_id,
            "Tək barkod prosesi tamamlandı! /start ilə yenidən başlaya bilərsiniz.")
        user_state[chat_id] = STATE_IDLE

########################################
# /cancel to abort
########################################
@bot.message_handler(commands=['cancel'])
def cmd_cancel(m):
    chat_id = m.chat.id
    init_session(chat_id)
    bot.send_message(chat_id, "Əməliyyat ləğv edildi. /start ilə yenidən başlaya bilərsiniz.")

########################################
# Flask routes
########################################
@app.route("/", methods=['GET'])
def home():
    return "Bot is running!", 200

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "ok", 200
    else:
        abort(403)

########################################
# Auto-set Webhook
########################################
def setup_webhook():
    # remove old
    bot.remove_webhook()
    domain = os.getenv("RENDER_EXTERNAL_HOSTNAME") or "https://yourapp.onrender.com"
    full_url = f"{domain}/webhook/{TELEGRAM_BOT_TOKEN}"
    bot.set_webhook(url=full_url)
    print("Webhook set to:", full_url)

if __name__ == "__main__":
    # we let gunicorn run 'app'
    setup_webhook()
    # do not call bot.polling(), we rely on the webhook
    pass
