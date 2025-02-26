import os
import json
from datetime import datetime

import telebot
import gspread
from oauth2client.service_account import ServiceAccountCredentials

import cv2
import numpy as np
from pyzbar.pyzbar import decode
import pytesseract
from thefuzz import process  # for fuzzy matching

from flask import Flask, request, abort

########################################
# ENV & SETUP
########################################
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
ASSET_TAB_NAME = "Asset Data"  # The name of the sheet/tab that has your 1,000+ items

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")

app = Flask(__name__)

########################################
# Google Sheets: main sheet & asset data
########################################
creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/spreadsheets"]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(credentials)

# Main sheet to append final data
main_sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
print("Connected to main sheet:", main_sheet.title)

# Load asset names from "Asset Data" tab
try:
    asset_worksheet = gc.open_by_key(SPREADSHEET_ID).worksheet(ASSET_TAB_NAME)
    # Suppose asset names are in the first column
    asset_data = asset_worksheet.col_values(1)  # all values in col1
    # remove header if you have a header row
    if asset_data and asset_data[0].lower().startswith("asset"):
        asset_data.pop(0)
except Exception as e:
    print("Error loading asset data from tab:", ASSET_TAB_NAME, e)
    asset_data = []

print(f"Loaded {len(asset_data)} asset names from '{ASSET_TAB_NAME}' tab.")

########################################
# In-memory user session
########################################
user_state = {}
user_data = {}  # user_data[chat_id] -> { 'barcode': ..., 'asset_matches': ..., 'desc': ..., 'qty': ... }

STATE_IDLE = "idle"
STATE_WAIT_DESCRIPTION = "wait_description"
STATE_WAIT_DESC_PICK = "wait_desc_pick"
STATE_WAIT_QUANTITY = "wait_quantity"

def init_session(chat_id):
    user_data[chat_id] = {
        "barcode": "",
        "desc_matches": [],
        "final_desc": "",
        "qty": 0
    }
    user_state[chat_id] = STATE_IDLE

########################################
# 1) Basic Photo → Barcode
########################################
def preprocess_image_cv2(np_img):
    gray = cv2.cvtColor(np_img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3,3), 0)
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    return thresh

def scan_barcode_cv2(np_img):
    # Zbar decode first
    processed = preprocess_image_cv2(np_img)
    codes = decode(processed)
    if codes:
        return codes[0].data.decode('utf-8')
    # fallback: OCR
    text = pytesseract.image_to_string(processed, config='--psm 6')
    # strip non-alnum
    clean_text = "".join(filter(str.isalnum, text))
    return clean_text if clean_text else "Barkod tapılmadı"

########################################
# 2) Fuzzy Suggestions for Asset Name
########################################
def fuzzy_suggest(user_input, limit=3):
    if not asset_data:
        return []
    # returns list of (name, score) from best to worst
    results = process.extract(user_input, asset_data, limit=limit)
    return results  # e.g. [("Karel DS200 16/R", 90), ...]

########################################
# Bot Handlers
########################################
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    init_session(chat_id)
    bot.send_message(chat_id,
        "👋 Salam! Barkod şəkli göndərin, sonra məhsulun adını fuzzy search ilə seçək, sonra miqdar daxil edin."
    )

@bot.message_handler(commands=['help'])
def cmd_help(message):
    bot.send_message(message.chat.id, "Barkod şəkli göndərin, bot oxuyacaq. Sonra məhsul adını fuzzy search ilə tapın, sonra say daxil edin.")

@bot.message_handler(commands=['cancel'])
def cmd_cancel(message):
    chat_id = message.chat.id
    init_session(chat_id)
    bot.send_message(chat_id, "Əməliyyat ləğv edildi. /start ilə yenidən başlayın.")

########################################
# Photo Handler → Extract Barcode
########################################
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    init_session(chat_id)
    user_state[chat_id] = STATE_WAIT_DESCRIPTION

    # 1) Download photo
    file_id = message.photo[-1].file_id
    info = bot.get_file(file_id)
    downloaded = bot.download_file(info.file_path)
    np_img = np.frombuffer(downloaded, np.uint8)
    cv_img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

    # 2) Scan Barcode
    barcode_found = scan_barcode_cv2(cv_img)
    user_data[chat_id]["barcode"] = barcode_found

    bot.send_message(chat_id,
        f"📸 Barkod oxundu: <b>{barcode_found}</b>\nZəhmət olmasa məhsulun adını (tam və ya qismən) daxil edin."
    )

########################################
# 3) Wait Description (User types partial or full name)
########################################
@bot.message_handler(func=lambda msg: user_state.get(msg.chat.id)==STATE_WAIT_DESCRIPTION, content_types=['text'])
def handle_description_input(message):
    chat_id = message.chat.id
    user_input = message.text.strip()
    suggestions = fuzzy_suggest(user_input, limit=3)
    if not suggestions:
        bot.send_message(chat_id, "Heç bir uyğun ad tapılmadı. Yenidən cəhd edin.")
        return
    # store them
    user_data[chat_id]["desc_matches"] = suggestions
    user_state[chat_id] = STATE_WAIT_DESC_PICK
    text = "Mümkün uyğun adlar:\n"
    idx = 1
    for (name, score) in suggestions:
        text += f"{idx}) {name} (oxşarlıq: {score})\n"
        idx += 1
    text += "Hansını seçirsiniz? (1-3) və ya yenidən ad yazın."
    bot.send_message(chat_id, text)

########################################
# 4) Wait for user pick (1-3) or new input
########################################
@bot.message_handler(func=lambda msg: user_state.get(msg.chat.id)==STATE_WAIT_DESC_PICK, content_types=['text'])
def handle_desc_pick(message):
    chat_id = message.chat.id
    pick = message.text.strip()
    # if pick is digit, user picks an index
    if pick.isdigit():
        idx = int(pick) - 1
        matches = user_data[chat_id]["desc_matches"]
        if 0 <= idx < len(matches):
            chosen_name = matches[idx][0]
            user_data[chat_id]["final_desc"] = chosen_name
            # now ask for quantity
            user_state[chat_id] = STATE_WAIT_QUANTITY
            bot.send_message(chat_id, f"Seçilmiş ad: <b>{chosen_name}</b>\nMiqdarı (rəqəm) daxil edin.")
            return
        else:
            bot.send_message(chat_id, "Etibarsız seçim. Yenidən cəhd edin.")
    else:
        # user typed new partial name instead
        suggestions = fuzzy_suggest(pick, limit=3)
        if not suggestions:
            bot.send_message(chat_id, "Heç bir uyğun ad tapılmadı. Yenidən cəhd edin.")
            return
        user_data[chat_id]["desc_matches"] = suggestions
        text = "Mümkün uyğun adlar:\n"
        idx = 1
        for (name, score) in suggestions:
            text += f"{idx}) {name} (oxşarlıq: {score})\n"
            idx += 1
        text += "Hansını seçirsiniz? (1-3) və ya yenidən ad yazın."
        bot.send_message(chat_id, text)

########################################
# 5) Wait Quantity
########################################
@bot.message_handler(func=lambda msg: user_state.get(msg.chat.id)==STATE_WAIT_QUANTITY, content_types=['text'])
def handle_quantity_input(message):
    chat_id = message.chat.id
    pick = message.text.strip()
    try:
        qty = int(pick)
    except:
        bot.send_message(chat_id, "Zəhmət olmasa düzgün rəqəm daxil edin.")
        return

    user_data[chat_id]["qty"] = qty
    # Save to main sheet
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    barcode = user_data[chat_id]["barcode"]
    desc = user_data[chat_id]["final_desc"]
    quantity = qty

    try:
        main_sheet.append_row([now, barcode, desc, quantity])
        bot.send_message(chat_id,
            f"✅ Qeyd edildi:\nTarix: {now}\nBarkod: {barcode}\nAd: {desc}\nSayı: {quantity}"
        )
    except Exception as e:
        bot.send_message(chat_id, f"❌ Xəta! Cədvələ əlavə edə bilmədik: {e}")

    # Reset session
    init_session(chat_id)

########################################
# Flask route for health check
########################################
@app.route("/", methods=['GET'])
def home():
    return "Bot is running!", 200

# Webhook endpoint if needed (otherwise you might do bot.polling):
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
# Optionally set webhook programmatically
########################################
def setup_webhook():
    bot.remove_webhook()
    base_url = os.getenv("RENDER_EXTERNAL_HOSTNAME") or "https://yourapp.onrender.com"
    full_url = f"{base_url}/webhook/{TELEGRAM_BOT_TOKEN}"
    bot.set_webhook(url=full_url)
    print("Webhook set to:", full_url)

# If you'd like to do polling instead, remove the webhook approach
# and just call bot.polling() in __main__.

########################################
# Gunicorn entry point
########################################
if __name__ == "__main__":
    # Example: If you want to do polling (not recommended with Render free):
    # bot.polling(none_stop=True)

    # Or set up the webhook:
    setup_webhook()

    # We do NOT call app.run here because Gunicorn calls it
    pass
