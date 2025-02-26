import os
import json
import re
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
# ENV & BOT SETUP
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
# GOOGLE SHEETS
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

# Load asset data for fuzzy matching
try:
    asset_worksheet = gc.open_by_key(SPREADSHEET_ID).worksheet(ASSET_TAB_NAME)
    asset_data = asset_worksheet.col_values(1)
    if asset_data and asset_data[0].lower().startswith("asset"):
        asset_data.pop(0)
except Exception as e:
    print("Error loading asset data from tab:", e)
    asset_data = []

print(f"Loaded {len(asset_data)} asset names from '{ASSET_TAB_NAME}' tab.")

########################################
# IN-MEMORY SESSION
########################################
user_data = {}  # chat_id -> {...} 
user_state = {} # chat_id -> string

STATE_IDLE = "idle"
STATE_WAIT_LOCATION = "wait_location"
STATE_WAIT_PHOTO = "wait_photo"
STATE_WAIT_ASSET = "wait_asset"
STATE_WAIT_ASSET_PICK = "wait_asset_pick"
STATE_WAIT_QUANTITY = "wait_quantity"

def init_session(chat_id):
    user_data[chat_id] = {
        "mode": "single",
        "location": "",
        "barcodes": [],
        "index": 0,
        "asset_name": "",
        "qty": 0
    }
    user_state[chat_id] = STATE_IDLE

########################################
# WEBHOOK SETUP
########################################
def setup_webhook():
    bot.remove_webhook()
    domain = os.getenv("RENDER_EXTERNAL_HOSTNAME") or "https://yourapp.onrender.com"
    url = f"{domain}/webhook/{TELEGRAM_BOT_TOKEN}"
    bot.set_webhook(url=url)
    print("Webhook set to:", url)

########################################
# UTILS: Fuzzy
########################################
def fuzzy_suggest(query, data, limit=3):
    if not data:
        return []
    results = process.extract(query, data, limit=limit)
    return results

########################################
# MULTI-BARCODE DETECTION
########################################
def detect_multi_barcodes(np_img):
    """
    Returns a list of barcodes found in one image,
    ignoring random text that doesn't match e.g. '^AZT...' pattern.
    """
    gray = cv2.cvtColor(np_img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3,3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)

    inv = 255 - thresh
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9,3))
    morph = cv2.morphologyEx(inv, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(morph, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found_codes = []

    angles = [0, 90, 180, 270]
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w > 20 and h > 20:
            region = np_img[y:y+h, x:x+w]
            matched = False
            for ang in angles:
                rotated = rotate_image(region, ang)
                code = decode_zbar(rotated)
                if code:
                    # Filter out random text
                    if is_our_barcode(code):
                        found_codes.append(code)
                    matched = True
                    break
            if not matched:
                # fallback OCR if you want
                # code_ocr = decode_ocr(region)
                # if code_ocr and is_our_barcode(code_ocr):
                #     found_codes.append(code_ocr)
                pass

    # Also try entire image as fallback
    entire = decode_zbar(np_img)
    if entire and is_our_barcode(entire) and entire not in found_codes:
        found_codes.append(entire)

    return found_codes

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
    if barcodes:
        return barcodes[0].data.decode("utf-8")
    return None

def is_our_barcode(code):
    """
    E.g., return True if code matches 'AZT\d{something}'
    or length or any other pattern.
    Adjust as you see fit.
    """
    # Example: if it starts with "AZT" and has digits after
    pattern = r'^AZT\d+'
    return bool(re.match(pattern, code))

########################################
# START DAY / CANCEL
########################################
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    init_session(chat_id)

    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("Start Day", callback_data="DAY_START"),
        InlineKeyboardButton("Cancel Day", callback_data="DAY_CANCEL")
    )
    bot.send_message(chat_id,
        "👋 Salam! Günü başlamaq üçün 'Start Day' düyməsinə basın, "
        "və ya 'Cancel Day' ilə imtina edin.",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data in ["DAY_START", "DAY_CANCEL"])
def day_start_cancel(call):
    chat_id = call.message.chat.id
    if call.data == "DAY_CANCEL":
        init_session(chat_id)
        bot.send_message(chat_id, "Gün ləğv edildi. /start ilə yenidən başlaya bilərsiniz.")
        return

    # pick location
    user_state[chat_id] = STATE_WAIT_LOCATION
    bot.send_message(chat_id,
        "Zəhmət olmasa çalışdığınız lokasiyanı daxil edin. Məsələn: 'Anbar 3'.")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_LOCATION, content_types=['text'])
def handle_location(m):
    chat_id = m.chat.id
    loc = m.text.strip()
    user_data[chat_id]["location"] = loc

    # Let user pick single vs multi
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("Tək Barkod", callback_data="MODE_SINGLE"),
        InlineKeyboardButton("Çox Barkod", callback_data="MODE_MULTI")
    )
    bot.send_message(chat_id,
        f"Lokasiya: <b>{loc}</b> qeyd edildi.\nRejimi seçin:",
        reply_markup=kb
    )
    user_state[chat_id] = STATE_WAIT_PHOTO  # We'll finalize mode after callback

@bot.callback_query_handler(func=lambda c: c.data in ["MODE_SINGLE", "MODE_MULTI"])
def pick_mode(call):
    chat_id = call.message.chat.id
    mode = call.data
    user_data[chat_id]["mode"] = ("single" if mode == "MODE_SINGLE" else "multi")
    user_data[chat_id]["barcodes"] = []
    user_data[chat_id]["index"] = 0

    if mode == "MODE_SINGLE":
        bot.send_message(chat_id,
            "Tək barkod rejimi. Zəhmət olmasa barkodun şəklini göndərin.\n"
            "Yenidən /start yaza bilərsiniz lazım olsa.")
    else:
        bot.send_message(chat_id,
            "Çox barkod rejimi.\nBir neçə barkod şəkli göndərə bilərsiniz.")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("Bitir", callback_data="FINISH_MULTI"))
        bot.send_message(chat_id,
            "Sonda 'Bitir' düyməsini basın.",
            reply_markup=kb
        )

    user_state[chat_id] = STATE_WAIT_PHOTO

@bot.callback_query_handler(func=lambda c: c.data == "FINISH_MULTI")
def finish_multi_callback(call):
    chat_id = call.message.chat.id
    barcodes = user_data[chat_id]["barcodes"]
    if not barcodes:
        bot.send_message(chat_id, "Heç bir barkod tapılmadı. Yenidən şəkil göndərin.")
        return
    user_data[chat_id]["index"] = 0
    first_bc = barcodes[0]
    bot.send_message(chat_id,
        f"{len(barcodes)} barkod aşkarlanıb.\n"
        f"1-ci barkod: <b>{first_bc}</b>\nMəhsul adını (tam və ya qismən) daxil edin."
    )
    user_state[chat_id] = STATE_WAIT_ASSET

########################################
# Photo => multi-barcodes
########################################
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    state = user_state.get(chat_id, STATE_IDLE)

    if state != STATE_WAIT_PHOTO:
        bot.send_message(chat_id,
            "Hazırda şəkil qəbul edilmir. /start ilə yenidən cəhd edin.")
        return

    # decode multi barcodes
    file_id = message.photo[-1].file_id
    info = bot.get_file(file_id)
    downloaded = bot.download_file(info.file_path)
    np_img = np.frombuffer(downloaded, np.uint8)
    cv_img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

    found = detect_multi_barcodes(cv_img)
    if not found:
        bot.send_message(chat_id,
            "Heç bir barkod tapılmadı. Daha yaxın və ya aydın şəkil çəkin.")
        return

    if user_data[chat_id]["mode"] == "single":
        # pick first or all?
        bc = found[0]
        user_data[chat_id]["barcodes"] = [bc]
        if len(found) > 1:
            bot.send_message(chat_id,
                f"{len(found)} barkod aşkarlandı, birincisi götürülür: <b>{bc}</b>")
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
            f"Barkod(lar) tapıldı: {', '.join(found)}\n"
            "Daha şəkil göndərin və ya 'Bitir' düyməsini basın."
        )

########################################
# Wait for Asset Name
########################################
@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_ASSET, content_types=['text'])
def handle_asset_name(m):
    chat_id = m.chat.id
    query = m.text.strip()
    suggestions = fuzzy_suggest(query, asset_data, limit=3)
    if not suggestions:
        # Accept as custom
        user_data[chat_id]["asset_name"] = query
        ask_quantity(chat_id)
        return

    # Build inline kb with suggestions + "Custom Name"
    kb = InlineKeyboardMarkup()
    for (name, score) in suggestions:
        kb.add(InlineKeyboardButton(
            text=f"{name} ({score}%)",
            callback_data=f"ASSET_PICK|{name}"
        ))
    kb.add(InlineKeyboardButton(
        text="Custom Name", callback_data=f"ASSET_CUSTOM|{query}"
    ))
    bot.send_message(chat_id,
        "Uyğun ola biləcək adlar. Aşağıdan seçin, ya öz adınızı təkrar yazın:",
        reply_markup=kb
    )
    user_state[chat_id] = STATE_WAIT_ASSET_PICK

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_ASSET_PICK, content_types=['text'])
def handle_asset_retry(m):
    chat_id = m.chat.id
    query = m.text.strip()
    suggestions = fuzzy_suggest(query, asset_data, limit=3)
    if not suggestions:
        user_data[chat_id]["asset_name"] = query
        ask_quantity(chat_id)
        return

    kb = InlineKeyboardMarkup()
    for (name, score) in suggestions:
        kb.add(InlineKeyboardButton(
            text=f"{name} ({score}%)",
            callback_data=f"ASSET_PICK|{name}"
        ))
    kb.add(InlineKeyboardButton(
        text="Custom Name", callback_data=f"ASSET_CUSTOM|{query}"
    ))
    bot.send_message(chat_id,
        "Aşağıdan uyğun adı seçin, ya başqa ad daxil edin:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ASSET_PICK|"))
def pick_asset_callback(call):
    chat_id = call.message.chat.id
    chosen_name = call.data.split("|")[1]
    user_data[chat_id]["asset_name"] = chosen_name
    ask_quantity(chat_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ASSET_CUSTOM|"))
def pick_asset_custom(call):
    chat_id = call.message.chat.id
    custom = call.data.split("|")[1]
    user_data[chat_id]["asset_name"] = custom
    ask_quantity(chat_id)

########################################
# Quantity with Inline Buttons
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
def pick_qty_button(call):
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
        bot.send_message(chat_id, "Zəhmət olmasa düzgün rəqəm daxil edin.")
        return
    user_data[chat_id]["qty"] = val
    finalize_barcode(chat_id)

def finalize_barcode(chat_id):
    data = user_data[chat_id]
    idx = data["index"]
    bc_list = data["barcodes"]
    if idx >= len(bc_list):
        bot.send_message(chat_id, "Xəta: barkod siyahısı tükəndi.")
        user_state[chat_id] = STATE_IDLE
        return

    bc = bc_list[idx]
    asset = data["asset_name"]
    quantity = data["qty"]
    location = data["location"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Save row => [time, location, bc, asset, qty]
    try:
        main_sheet.append_row([now, location, bc, asset, quantity])
        bot.send_message(chat_id,
            f"✅ Qeyd edildi:\nTarix: {now}\nLokasiya: {location}\nBarkod: {bc}\nAd: {asset}\nSay: {quantity}"
        )
    except Exception as e:
        bot.send_message(chat_id, f"❌ Cədvələ əlavə xətası: {e}")

    # Next if multi
    if data["mode"] == "multi":
        data["index"] += 1
        if data["index"] < len(bc_list):
            nxt = bc_list[data["index"]]
            bot.send_message(chat_id,
                f"{data['index']+1}-ci barkod: <b>{nxt}</b>\nMəhsul adını daxil edin:"
            )
            user_state[chat_id] = STATE_WAIT_ASSET
        else:
            bot.send_message(chat_id,
                "Bütün barkodların məlumatı əlavə olundu! Təşəkkürlər.")
            user_state[chat_id] = STATE_IDLE
    else:
        # single done
        bot.send_message(chat_id,
            "Tək barkod prosesi bitdi. Yeni gün üçün /start yaza bilərsiniz.")
        user_state[chat_id] = STATE_IDLE

########################################
# CANCEL approach
########################################
@bot.message_handler(commands=['cancel'])
def cmd_cancel(m):
    chat_id = m.chat.id
    init_session(chat_id)
    bot.send_message(chat_id, "Cari gün ləğv edildi. /start yazın yenidən başlamaq üçün.")

########################################
# FLASK ROUTES
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
# APP ENTRY
########################################
if __name__ == "__main__":
    setup_webhook()
    # Gunicorn will run 'app'
    pass
