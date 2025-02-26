import os
import json
import re
import cv2
import numpy as np
from datetime import datetime, timedelta

from flask import Flask, request, abort
import telebot

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from pyzbar.pyzbar import decode
import pytesseract
from thefuzz import process
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

########################################
# ENV
########################################
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
ASSET_TAB_NAME = "Asset Data"

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

########################################
# GSheets
########################################
creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets"
]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(credentials)
main_sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
print("Connected to main sheet:", main_sheet.title)

try:
    asset_worksheet = gc.open_by_key(SPREADSHEET_ID).worksheet(ASSET_TAB_NAME)
    asset_data = asset_worksheet.col_values(1)
    if asset_data and asset_data[0].lower().startswith("asset"):
        asset_data.pop(0)
except Exception as e:
    print("Error loading asset data:", e)
    asset_data = []

print(f"Loaded {len(asset_data)} asset names from {ASSET_TAB_NAME}.")

########################################
# In-memory session data
########################################
user_data = {}  # chat_id -> {...}
user_state = {} # chat_id -> state string

STATE_IDLE = "idle"
STATE_WAIT_PHOTO = "wait_photo"
STATE_WAIT_LOCATION = "wait_location"
STATE_WAIT_ASSET = "wait_asset"
STATE_WAIT_ASSET_PICK = "wait_asset_pick"
STATE_WAIT_QUANTITY = "wait_quantity"

def init_session(chat_id):
    now = datetime.now()
    user_data[chat_id] = {
        "mode": "single",
        "location": "",
        "location_timestamp": now - timedelta(days=1), # so we definitely ask on first go
        "barcodes": [],
        "index": 0,
        "asset_name": "",
        "qty": 0
    }
    user_state[chat_id] = STATE_IDLE

########################################
# Webhook
########################################
def setup_webhook():
    bot.remove_webhook()
    domain = os.getenv("RENDER_EXTERNAL_HOSTNAME") or "https://yourapp.onrender.com"
    url = f"{domain}/webhook/{TELEGRAM_BOT_TOKEN}"
    bot.set_webhook(url=url)
    print("Webhook set to:", url)

########################################
# Check if location is needed
########################################
def need_location(chat_id):
    """
    Returns True if it's been 30+ minutes since last location,
    or if the date changed (midnight).
    """
    now = datetime.now()
    last_time = user_data[chat_id]["location_timestamp"]
    if (now - last_time) > timedelta(minutes=30):
        # or check if day changed
        if now.date() != last_time.date():
            return True
        return True
    return False

########################################
# Fuzzy Suggest
########################################
def fuzzy_suggest(query, data, limit=3):
    if not data:
        return []
    results = process.extract(query, data, limit=limit)
    return results

########################################
# Barcode Multi Detection
########################################
def detect_multi_barcodes(np_img):
    """
    Attempts a morphological approach + multiple angles to find all barcodes.
    Filters out non-AZT codes if you want only those.
    """
    gray = cv2.cvtColor(np_img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3,3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)

    inv = 255 - thresh
    # bigger kernel to unify close bars
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11,4))
    morph = cv2.morphologyEx(inv, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(morph, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    found_codes = []
    angles = list(range(0, 360, 15))  # try every 15° for better coverage

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w > 20 and h > 20:
            region = np_img[y:y+h, x:x+w]
            decoded = try_decode_region(region, angles)
            if decoded:
                for code in decoded:
                    if is_our_barcode(code):
                        found_codes.append(code)
    # also try entire image
    entire = try_decode_region(np_img, angles)
    if entire:
        for c in entire:
            if is_our_barcode(c) and c not in found_codes:
                found_codes.append(c)

    print("DEBUG: Found barcodes ->", found_codes)
    return found_codes

def try_decode_region(np_img, angles):
    results = []
    for angle in angles:
        rot = rotate_image(np_img, angle)
        code = decode_zbar(rot)
        if code and code not in results:
            results.append(code)
    return results

def rotate_image(cv_img, angle):
    (h, w) = cv_img.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(cv_img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return rotated

def decode_zbar(cv_img):
    from PIL import Image
    pil_img = Image.fromarray(cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB))
    codes = decode(pil_img)
    if codes:
        return codes[0].data.decode("utf-8")
    return None

def is_our_barcode(code):
    # Example: only accept if it matches "AZT" pattern
    pattern = r'^AZT\d+'
    return bool(re.match(pattern, code))

########################################
# ASK LOCATION if needed
########################################
def check_location(chat_id):
    if need_location(chat_id):
        # ask user to confirm or update location
        user_state[chat_id] = STATE_WAIT_LOCATION
        bot.send_message(chat_id,
            "Görünür 30+ dəqiqə keçib və ya yeni gün başlayıb.\n"
            "Zəhmət olmasa yeni lokasiyanı daxil edin (örn: 'Anbar 3').")
        return True
    return False

########################################
# MAIN FLOW: daily start replaced with location check
########################################
@bot.message_handler(commands=['start'])
def cmd_start(m):
    chat_id = m.chat.id
    # If brand new
    if chat_id not in user_data:
        init_session(chat_id)

    # Otherwise, we check location
    if not user_data[chat_id]["location"] or need_location(chat_id):
        user_state[chat_id] = STATE_WAIT_LOCATION
        bot.send_message(chat_id,
            "Günü yenidən başlamaq istəyirsiniz? Lokasiyanı daxil edin. "
            "Məsələn: 'Anbar A'.")
    else:
        # location is still valid
        # ask single vs multi
        show_mode_keyboard(chat_id)

@bot.message_handler(commands=['cancel'])
def cmd_cancel(m):
    chat_id = m.chat.id
    init_session(chat_id)
    bot.send_message(chat_id,
        "Cari proses ləğv edildi. /start yaza bilərsiniz yenidən başlamaq üçün.")

@bot.message_handler(func=lambda msg: user_state.get(msg.chat.id)==STATE_WAIT_LOCATION, content_types=['text'])
def handle_location(msg):
    chat_id = msg.chat.id
    loc = msg.text.strip()
    user_data[chat_id]["location"] = loc
    user_data[chat_id]["location_timestamp"] = datetime.now()
    show_mode_keyboard(chat_id)

def show_mode_keyboard(chat_id):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("Tək Barkod", callback_data="MODE_SINGLE"),
        InlineKeyboardButton("Çox Barkod", callback_data="MODE_MULTI")
    )
    loc = user_data[chat_id]["location"]
    bot.send_message(chat_id,
        f"Lokasiya: <b>{loc}</b>.\n"
        "Skan rejimini seçin:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data in ["MODE_SINGLE", "MODE_MULTI"])
def pick_mode(call):
    chat_id = call.message.chat.id
    mode = call.data
    user_data[chat_id]["mode"] = ("single" if mode == "MODE_SINGLE" else "multi")
    user_data[chat_id]["barcodes"] = []
    user_data[chat_id]["index"] = 0

    if mode == "MODE_SINGLE":
        bot.send_message(chat_id,
            "Tək barkod rejimi. Zəhmət olmasa barkodun şəklini göndərin.")
    else:
        bot.send_message(chat_id,
            "Çox barkod rejimi.\nBirdən çox barkod şəkli göndərin.\n"
            "Bitirdikdə 'Bitir' düyməsini basın.")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("Bitir", callback_data="FINISH_MULTI"))
        bot.send_message(chat_id,
            "Skanı bitirmək üçün bu düymədən istifadə edin.",
            reply_markup=kb
        )

    user_state[chat_id] = STATE_WAIT_PHOTO

@bot.callback_query_handler(func=lambda c: c.data == "FINISH_MULTI")
def finish_multi_callback(call):
    chat_id = call.message.chat.id
    if not user_data[chat_id]["barcodes"]:
        bot.send_message(chat_id,
            "Heç bir barkod yoxdur. Yenidən şəkil göndərin.")
        return
    user_data[chat_id]["index"] = 0
    bc_list = user_data[chat_id]["barcodes"]
    first_bc = bc_list[0]
    bot.send_message(chat_id,
        f"{len(bc_list)} barkod aşkar olunub.\n"
        f"1-ci barkod: <b>{first_bc}</b>\n"
        "Məhsul adını (tam və ya qismən) daxil edin."
    )
    user_state[chat_id] = STATE_WAIT_ASSET

########################################
# Photo => multi barcodes
########################################
@bot.message_handler(content_types=['photo'])
def handle_photo(msg):
    chat_id = msg.chat.id
    if user_state.get(chat_id, STATE_IDLE) != STATE_WAIT_PHOTO:
        bot.send_message(chat_id, "Hazırda şəkil qəbul edilmir. /start edin.")
        return

    file_id = msg.photo[-1].file_id
    info = bot.get_file(file_id)
    downloaded = bot.download_file(info.file_path)
    np_img = np.frombuffer(downloaded, np.uint8)
    cv_img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

    found = detect_multi_barcodes(cv_img)
    if not found:
        bot.send_message(chat_id,
            "Heç bir barkod tapılmadı. Zəhmət olmasa daha aydın və yaxın şəkil çəkin.")
        return

    if user_data[chat_id]["mode"] == "single":
        # pick the first code from the image
        bc = found[0]
        user_data[chat_id]["barcodes"] = [bc]
        if len(found) > 1:
            bot.send_message(chat_id,
                f"{len(found)} barkod aşkar olundu, birincisi götürülür: <b>{bc}</b>.")
        else:
            bot.send_message(chat_id,
                f"Barkod: <b>{bc}</b>")
        bot.send_message(chat_id,
            "Məhsul adını (tam və ya qismən) daxil edin.")
        user_state[chat_id] = STATE_WAIT_ASSET
    else:
        # multi mode => add all
        user_data[chat_id]["barcodes"].extend(found)
        bot.send_message(chat_id,
            f"Barkod(lar): {', '.join(found)}\n"
            "Daha şəkil göndərə ya da 'Bitir' düyməsini basın."
        )

########################################
# Wait for Asset Name
########################################
@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_ASSET, content_types=['text'])
def handle_asset(m):
    chat_id = m.chat.id
    q = m.text.strip()

    suggestions = fuzzy_suggest(q, asset_data, limit=3)
    if not suggestions:
        # no suggestions => accept as custom
        user_data[chat_id]["asset_name"] = q
        ask_quantity(chat_id)
        return

    kb = InlineKeyboardMarkup()
    for (name, score) in suggestions:
        kb.add(InlineKeyboardButton(
            text=f"{name} ({score}%)",
            callback_data=f"ASSET_PICK|{name}"
        ))
    kb.add(InlineKeyboardButton(
        text="Custom Name", callback_data=f"ASSET_CUSTOM|{q}"
    ))
    bot.send_message(chat_id,
        "Tapdığım uyğun adlar. Seçin və ya yeni ad yazın:",
        reply_markup=kb
    )
    user_state[chat_id] = STATE_WAIT_ASSET_PICK

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_ASSET_PICK, content_types=['text'])
def handle_asset_retry(m):
    chat_id = m.chat.id
    q = m.text.strip()
    suggestions = fuzzy_suggest(q, asset_data, limit=3)
    if not suggestions:
        user_data[chat_id]["asset_name"] = q
        ask_quantity(chat_id)
        return

    kb = InlineKeyboardMarkup()
    for (name, score) in suggestions:
        kb.add(InlineKeyboardButton(
            text=f"{name} ({score}%)",
            callback_data=f"ASSET_PICK|{name}"
        ))
    kb.add(InlineKeyboardButton(
        text="Custom Name", callback_data=f"ASSET_CUSTOM|{q}"
    ))
    bot.send_message(chat_id,
        "Seçin ya başqa ad yazın:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ASSET_PICK|"))
def cb_asset_pick(call):
    chat_id = call.message.chat.id
    chosen = call.data.split("|")[1]
    user_data[chat_id]["asset_name"] = chosen
    ask_quantity(chat_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ASSET_CUSTOM|"))
def cb_asset_custom(call):
    chat_id = call.message.chat.id
    custom = call.data.split("|")[1]
    user_data[chat_id]["asset_name"] = custom
    ask_quantity(chat_id)

########################################
# Ask Quantity
########################################
def ask_quantity(chat_id):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("1", callback_data="QTY|1"),
        InlineKeyboardButton("2", callback_data="QTY|2"),
        InlineKeyboardButton("3", callback_data="QTY|3"),
        InlineKeyboardButton("Other", callback_data="QTY|OTHER")
    )
    asset = user_data[chat_id]["asset_name"]
    bot.send_message(chat_id,
        f"Seçdiyiniz ad: <b>{asset}</b>\nMiqdarı seçin:",
        reply_markup=kb
    )
    user_state[chat_id] = STATE_WAIT_QUANTITY

@bot.callback_query_handler(func=lambda c: c.data.startswith("QTY|"))
def cb_qty_pick(call):
    chat_id = call.message.chat.id
    pick = call.data.split("|")[1]
    if pick == "OTHER":
        bot.send_message(chat_id, "Zəhmət olmasa miqdarı rəqəm kimi daxil edin.")
        user_state[chat_id] = STATE_WAIT_QUANTITY
        return
    user_data[chat_id]["qty"] = int(pick)
    finalize_barcode(chat_id)

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_QUANTITY, content_types=['text'])
def handle_qty_text(m):
    chat_id = m.chat.id
    txt = m.text.strip()
    try:
        val = int(txt)
    except:
        bot.send_message(chat_id, "Zəhmət olmasa düzgün rəqəm.")
        return
    user_data[chat_id]["qty"] = val
    finalize_barcode(chat_id)

def finalize_barcode(chat_id):
    data = user_data[chat_id]
    idx = data["index"]
    bc_list = data["barcodes"]
    if idx >= len(bc_list):
        bot.send_message(chat_id, "Xəta: barkod siyahısında mövqedən kənardayıq.")
        user_state[chat_id] = STATE_IDLE
        return

    bc = bc_list[idx]
    desc = data["asset_name"]
    quantity = data["qty"]
    loc = data["location"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Save row: [time, location, bc, desc, qty]
    try:
        main_sheet.append_row([now, loc, bc, desc, quantity])
        bot.send_message(chat_id,
            f"✅ Qeyd:\nTarix: {now}\nLokasiya: {loc}\nBarkod: {bc}\nAd: {desc}\nSay: {quantity}"
        )
    except Exception as e:
        bot.send_message(chat_id, f"❌ Xəta cədvələ əlavə edərkən: {e}")

    if data["mode"] == "multi":
        data["index"] += 1
        if data["index"] < len(bc_list):
            next_bc = bc_list[data["index"]]
            bot.send_message(chat_id,
                f"{data['index']+1}-ci barkod: <b>{next_bc}</b>\n"
                "Məhsul adını daxil edin (tam ya qismən)."
            )
            user_state[chat_id] = STATE_WAIT_ASSET
        else:
            bot.send_message(chat_id,
                "Bütün barkodların məlumatı yazıldı! Sağ olun.")
            user_state[chat_id] = STATE_IDLE
    else:
        # single done
        bot.send_message(chat_id,
            "Tək barkod prosesi bitdi! /start yazın yeni gün üçün.")
        user_state[chat_id] = STATE_IDLE

########################################
# /cancel
########################################
@bot.message_handler(commands=['cancel'])
def cmd_cancel(m):
    chat_id = m.chat.id
    init_session(chat_id)
    bot.send_message(chat_id, "Cari proses ləğv edildi. /start yazın yenidən başlamaq üçün.")

########################################
# FLASK
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
    abort(403)

if __name__ == "__main__":
    setup_webhook()
    # Gunicorn runs 'app'
    pass
