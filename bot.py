import os
import json
import cv2
import numpy as np
from datetime import datetime

import telebot
from flask import Flask, request, abort

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

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

########################################
# Google Sheets Setup
########################################
creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/spreadsheets"]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(credentials)

# Main sheet for storing final data
main_sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
print("Connected to main sheet:", main_sheet.title)

# Load asset data from "Asset Data" tab
try:
    asset_worksheet = gc.open_by_key(SPREADSHEET_ID).worksheet(ASSET_TAB_NAME)
    asset_data = asset_worksheet.col_values(1)  # all values in first column
    # remove header if present
    if asset_data and asset_data[0].lower().startswith("asset"):
        asset_data.pop(0)
except Exception as e:
    print("Error loading asset data:", e)
    asset_data = []

print(f"Loaded {len(asset_data)} asset names from '{ASSET_TAB_NAME}' tab.")

########################################
# In-memory user session
########################################
user_mode = {}   # chat_id -> "single" or "multi"
user_data = {}   # chat_id -> { "barcode_list": [...], "current_index": int, ... }
user_state = {}  # chat states

STATE_IDLE = "idle"
STATE_WAIT_PHOTO = "wait_photo"
STATE_WAIT_ASSET = "wait_asset"
STATE_WAIT_ASSET_PICK = "wait_asset_pick"
STATE_WAIT_QUANTITY = "wait_quantity"

def init_session(chat_id):
    user_mode[chat_id] = "single"  # default
    user_data[chat_id] = {
        "barcode_list": [],
        "current_index": 0,
        "asset_name": "",
        "qty": 0
    }
    user_state[chat_id] = STATE_IDLE

########################################
# Fuzzy Suggest Helper
########################################
def fuzzy_suggest(query, assets, limit=3):
    if not assets:
        return []
    return process.extract(query, assets, limit=limit)  # [(name, score), ...]

########################################
# Barcode Scanning (Advanced)
########################################
def preprocess_image(np_img):
    gray = cv2.cvtColor(np_img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3,3), 0)
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    return thresh

def scan_barcode(np_img):
    # Attempt zbar first
    processed = preprocess_image(np_img)
    codes = decode(processed)
    if codes:
        return codes[0].data.decode('utf-8')
    # fallback: OCR
    text = pytesseract.image_to_string(processed, config='--psm 6')
    cleaned = "".join(filter(str.isalnum, text))
    return cleaned if cleaned else "Barkod tapılmadı"

########################################
# Inline Keyboard: Single vs Multiple
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
        "👋 Salam! Yeni barkod prosesinə başlamaq üçün aşağıdan birini seçin:\n"
        " - Tək Barkod: Yalnız bir barkod əlavə edəcəksiniz.\n"
        " - Çox Barkod: Birdən çox barkod əlavə edə bilərsiniz.",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data in ["MODE_SINGLE", "MODE_MULTI"])
def pick_mode(call):
    chat_id = call.message.chat.id
    mode = call.data
    user_mode[chat_id] = "single" if mode == "MODE_SINGLE" else "multi"
    user_state[chat_id] = STATE_WAIT_PHOTO
    if mode == "MODE_SINGLE":
        bot.send_message(chat_id,
            "Tək barkod rejimi seçildi. Zəhmət olmasa barkodun şəklini göndərin.\n"
            "Yalnış daxil etsəniz /cancel yaza bilərsiniz.")
    else:
        user_data[chat_id]["barcode_list"] = []
        bot.send_message(chat_id,
            "Çox barkod rejimi seçildi. Birdən çox şəkil göndərə bilərsiniz.\n"
            "Bütün barkodları göndərdikdən sonra 'Bitir' düyməsini sıxın.")
        # Let's add a "Bitir" button
        finish_kb = InlineKeyboardMarkup()
        finish_kb.add(InlineKeyboardButton("Bitir", callback_data="FINISH_MULTI"))
        bot.send_message(chat_id,
            "Göndərməyə başlayın. Sonda 'Bitir' düyməsi ilə tamamlayın.",
            reply_markup=finish_kb
        )

@bot.callback_query_handler(func=lambda c: c.data == "FINISH_MULTI")
def finish_multi_mode(call):
    chat_id = call.message.chat.id
    barcodes = user_data[chat_id]["barcode_list"]
    if not barcodes:
        bot.send_message(chat_id, "Heç bir barkod qəbul olunmadı. Yenidən yoxlayın.")
        return
    # Now we handle them one by one
    user_data[chat_id]["current_index"] = 0
    user_state[chat_id] = STATE_WAIT_ASSET
    first_bc = barcodes[0]
    bot.send_message(chat_id,
        f"1-ci barkod: <b>{first_bc}</b>\n"
        "Məhsul adını (tam və ya qismən) daxil edin."
    )

########################################
# Photo Handler
########################################
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    state = user_state.get(chat_id, STATE_IDLE)
    if state != STATE_WAIT_PHOTO:
        # If multi mode, we might accept photos anytime
        if user_mode[chat_id] == "multi":
            # We'll add the barkod to barcode_list
            # 1) Download
            file_id = message.photo[-1].file_id
            info = bot.get_file(file_id)
            downloaded = bot.download_file(info.file_path)
            np_img = np.frombuffer(downloaded, np.uint8)
            cv_img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

            bc = scan_barcode(cv_img)
            user_data[chat_id]["barcode_list"].append(bc)
            bot.send_message(chat_id, f"Barkod tapıldı: <b>{bc}</b>\nDaha şəkil göndərə ya da 'Bitir' düyməsini vurun.")
        else:
            # single mode or something else but in the wrong state
            bot.send_message(chat_id, "Hazırda şəkil qəbul edilmir. /start və ya /cancel ilə yenidən cəhd edin.")
        return

    # Single mode flow: user is in WAIT_PHOTO
    if user_mode[chat_id] == "single":
        # 1) Download & decode
        file_id = message.photo[-1].file_id
        info = bot.get_file(file_id)
        downloaded = bot.download_file(info.file_path)
        np_img = np.frombuffer(downloaded, np.uint8)
        cv_img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

        barcode_found = scan_barcode(cv_img)
        user_data[chat_id]["barcode_list"] = [barcode_found]
        # Next we ask for the asset name
        user_state[chat_id] = STATE_WAIT_ASSET
        bot.send_message(chat_id,
            f"Barkod: <b>{barcode_found}</b>\nZəhmət olmasa məhsul adını (tam və ya qismən) daxil edin."
        )

########################################
# Wait for Asset Name
########################################
@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == STATE_WAIT_ASSET, content_types=['text'])
def handle_asset_name(m):
    chat_id = m.chat.id
    user_input = m.text.strip()
    suggestions = fuzzy_suggest(user_input, asset_data, limit=3)
    if not suggestions:
        bot.send_message(chat_id, "Heç bir uyğun ad tapılmadı. Yenidən cəhd edin.")
        return

    # Build inline keyboard for suggestions
    kb = InlineKeyboardMarkup()
    for (name, score) in suggestions:
        kb.add(InlineKeyboardButton(
            text=f"{name} ({score}%)",
            callback_data=f"ASSET_PICK|{name}"
        ))
    bot.send_message(chat_id,
        "Aşağıdakı uyğun adlardan birini seçin və ya yenidən başqa ad yazın.",
        reply_markup=kb
    )

    user_state[chat_id] = STATE_WAIT_ASSET_PICK

@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == STATE_WAIT_ASSET_PICK, content_types=['text'])
def handle_asset_renegotiate(m):
    # If user typed a new partial name instead of tapping a button
    chat_id = m.chat.id
    user_input = m.text.strip()
    suggestions = fuzzy_suggest(user_input, asset_data, limit=3)
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
        "Aşağıdakı uyğun adlardan birini seçin və ya yenidən başqa ad yazın.",
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
        qty = int(pick)
    except:
        bot.send_message(chat_id, "Zəhmət olmasa düzgün rəqəm daxil edin.")
        return

    user_data[chat_id]["qty"] = qty
    # We finalize the current barcode
    index = user_data[chat_id]["current_index"]
    bc_list = user_data[chat_id]["barcode_list"]
    bc = bc_list[index]
    desc = user_data[chat_id]["asset_name"]

    # Save to sheet
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        main_sheet.append_row([now, bc, desc, qty])
        bot.send_message(chat_id,
            f"✅ Qeyd edildi:\nTarix: {now}\nBarkod: {bc}\nAd: {desc}\nSayı: {qty}"
        )
    except Exception as e:
        bot.send_message(chat_id, f"❌ Cədvələ əlavə etmə xətası: {e}")

    # Move to next barcode if multi
    if user_mode[chat_id] == "multi":
        user_data[chat_id]["current_index"] += 1
        if user_data[chat_id]["current_index"] < len(bc_list):
            idx = user_data[chat_id]["current_index"]
            next_bc = bc_list[idx]
            bot.send_message(chat_id,
                f"{idx+1}-ci barkod: <b>{next_bc}</b>\nMəhsul adını (tam və ya qismən) daxil edin."
            )
            user_state[chat_id] = STATE_WAIT_ASSET
        else:
            bot.send_message(chat_id,
                "Bütün barkodlar üçün məlumat daxil edildi! Təşəkkürlər.")
            user_state[chat_id] = STATE_IDLE
    else:
        # single mode, we are done
        bot.send_message(chat_id, "Tək barkod prosesi tamamlandı! /start ilə yenidən başlayın.")
        user_state[chat_id] = STATE_IDLE

########################################
# /cancel command
########################################
@bot.message_handler(commands=['cancel'])
def cmd_cancel(m):
    chat_id = m.chat.id
    user_state[chat_id] = STATE_IDLE
    user_data[chat_id] = {
        "barcode_list": [],
        "current_index": 0,
        "asset_name": "",
        "qty": 0
    }
    bot.send_message(chat_id, "Əməliyyat ləğv edildi. /start ilə yenidən başlaya bilərsiniz.")

########################################
# Flask Health Check
########################################
@app.route("/", methods=['GET'])
def home():
    return "Bot is running!", 200

# Optional: Webhook endpoint
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
# Optionally set up webhook
########################################
def setup_webhook():
    bot.remove_webhook()
    base_url = os.getenv("RENDER_EXTERNAL_HOSTNAME") or "https://yourapp.onrender.com"
    full_url = f"{base_url}/webhook/{TELEGRAM_BOT_TOKEN}"
    bot.set_webhook(url=full_url)
    print("Webhook set to:", full_url)

if __name__ == "__main__":
    # If using polling:
    # bot.polling(none_stop=True)
    # If using webhook:
    setup_webhook()
    # Gunicorn calls app
    pass
