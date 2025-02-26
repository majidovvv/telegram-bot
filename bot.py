import os
import re
import cv2
import numpy as np
from datetime import datetime, timedelta
import json

from flask import Flask, request, abort
import telebot

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from pyzbar.pyzbar import decode, ZBarSymbol
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

print(f"Loaded {len(asset_data)} asset names from '{ASSET_TAB_NAME}' tab.")

########################################
# SESSION
########################################
user_data = {}  # chat_id -> { mode, location, location_timestamp, barcodes, index, asset_name, qty }
user_state = {} # chat_id -> string state

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
        "location_timestamp": datetime.now() - timedelta(days=1), # so we definitely ask day 1
        "barcodes": [],
        "index": 0,
        "asset_name": "",
        "qty": 0
    }
    user_state[chat_id] = STATE_IDLE

########################################
# NEED LOCATION LOGIC
########################################
def need_location(chat_id):
    now = datetime.now()
    last_time = user_data[chat_id]["location_timestamp"]
    if (now - last_time) > timedelta(minutes=30):
        if now.date() != last_time.date():
            return True
        return True
    return False

########################################
# FUZZY SUGGEST
########################################
def fuzzy_suggest(query, data, limit=3):
    if not data:
        return []
    return process.extract(query, data, limit=limit)

########################################
# ENHANCED MULTI-BARCODE DETECTION
########################################
def detect_multi_barcodes(np_img):
    """
    1) morphological close with bigger kernel
    2) morphological open with smaller kernel
    3) findContours -> each region
    4) for each region, rotate in smaller increments, decode all barcodes found
    5) brightening if too dark
    6) deduplicate, filter out non ^AZT\d+ patterns
    """

    # Possibly do brightness check
    mean_val = np_img.mean()
    if mean_val < 60:
        # brighten
        alpha = 1.5  # scale
        beta = 30    # shift
        np_img = cv2.convertScaleAbs(np_img, alpha=alpha, beta=beta)

    # Pass1: bigger kernel to unify bar lines
    gray = cv2.cvtColor(np_img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3,3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)

    inv = 255 - thresh
    kernel_big = cv2.getStructuringElement(cv2.MORPH_RECT, (11,4))
    morph1 = cv2.morphologyEx(inv, cv2.MORPH_CLOSE, kernel_big)

    # Pass2: smaller kernel to separate adjacent codes
    kernel_small = cv2.getStructuringElement(cv2.MORPH_RECT, (3,3))
    morph2 = cv2.morphologyEx(morph1, cv2.MORPH_OPEN, kernel_small)

    contours, _ = cv2.findContours(morph2, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    found_codes = set()
    angles = list(range(0, 360, 10))  # every 10 degrees

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w > 20 and h > 20:
            region = np_img[y:y+h, x:x+w]
            # Attempt decode
            region_codes = try_decode_region(region, angles)
            for ccc in region_codes:
                if is_our_barcode(ccc):
                    found_codes.add(ccc)

    # Also decode entire image as fallback
    whole_image_codes = try_decode_region(np_img, angles)
    for ccc in whole_image_codes:
        if is_our_barcode(ccc):
            found_codes.add(ccc)

    # Return them in a consistent order
    result = list(found_codes)
    print("DEBUG: Found multi-barcodes =>", result)
    return result

def try_decode_region(np_img, angles):
    """
    Returns ALL barcodes found from multiple angles in a single region
    (not just the first).
    """
    results = set()
    for angle in angles:
        rot = rotate_image(np_img, angle)
        # decode_zbar_multi to get all codes
        codes = decode_zbar_multi(rot)
        for c in codes:
            results.add(c)
    return list(results)

def rotate_image(cv_img, angle):
    (h, w) = cv_img.shape[:2]
    center = (w//2, h//2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(cv_img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return rotated

def decode_zbar_multi(cv_img):
    """
    Return all barcodes from a region using pyzbar's decode() 
    since decode() can return multiple symbols at once.
    """
    from PIL import Image
    pil_img = Image.fromarray(cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB))
    barcodes = decode(pil_img, symbols=[ZBarSymbol.CODE128, ZBarSymbol.CODE39, ZBarSymbol.EAN13, ZBarSymbol.EAN8, ZBarSymbol.QRCODE])
    results = []
    for b in barcodes:
        code_str = b.data.decode("utf-8")
        results.append(code_str)
    return results

def is_our_barcode(code):
    # e.g. matches ^AZT\d+
    pattern = r'^AZT\d+'
    return bool(re.match(pattern, code))

########################################
# START
########################################
@bot.message_handler(commands=['start'])
def cmd_start(m):
    chat_id = m.chat.id
    if chat_id not in user_data:
        init_session(chat_id)
    # If location needed
    if not user_data[chat_id]["location"] or need_location(chat_id):
        user_state[chat_id] = STATE_WAIT_LOCATION
        bot.send_message(chat_id,
            "Yeni gün - Zəhmət olmasa lokasiyanı daxil edin (örn: 'Anbar 3').")
    else:
        show_mode_keyboard(chat_id)

@bot.message_handler(commands=['cancel'])
def cmd_cancel(m):
    chat_id = m.chat.id
    init_session(chat_id)
    bot.send_message(chat_id,
        "Cari proses ləğv edildi. /start yaza bilərsiniz yenidən başlamaq üçün.")

def show_mode_keyboard(chat_id):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("Tək Barkod", callback_data="MODE_SINGLE"),
        InlineKeyboardButton("Çox Barkod", callback_data="MODE_MULTI")
    )
    loc = user_data[chat_id]["location"]
    bot.send_message(chat_id,
        f"Lokasiya: <b>{loc}</b>.\nRejimi seçin:",
        reply_markup=kb
    )

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_LOCATION, content_types=['text'])
def handle_location(m):
    chat_id = m.chat.id
    loc = m.text.strip()
    user_data[chat_id]["location"] = loc
    user_data[chat_id]["location_timestamp"] = datetime.now()
    show_mode_keyboard(chat_id)

@bot.callback_query_handler(func=lambda c: c.data in ["MODE_SINGLE", "MODE_MULTI"])
def pick_mode(call):
    chat_id = call.message.chat.id
    mode = call.data
    user_data[chat_id]["mode"] = "single" if mode=="MODE_SINGLE" else "multi"
    user_data[chat_id]["barcodes"].clear()
    user_data[chat_id]["index"] = 0

    if mode=="MODE_SINGLE":
        bot.send_message(chat_id,
            "Tək barkod rejimi. Zəhmət olmasa barkodun şəklini göndərin.")
    else:
        # multi => add finish + stop
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("Bitir", callback_data="FINISH_MULTI"),
            InlineKeyboardButton("Stop/Restart", callback_data="STOP_RESTART")
        )
        bot.send_message(chat_id,
            "Çox barkod rejimi. Bir neçə şəkil göndərin.\n"
            "Bitirdikdən sonra 'Bitir' düyməsini basın.\n"
            "'Stop/Restart' ilə yenidən başlaya bilərsiniz.",
            reply_markup=kb
        )
    user_state[chat_id] = STATE_WAIT_PHOTO

@bot.callback_query_handler(func=lambda c: c.data=="STOP_RESTART")
def stop_restart_cb(call):
    chat_id = call.message.chat.id
    init_session(chat_id)
    bot.send_message(chat_id,
        "İş prosesi ləğv edildi və yenidən başlaya bilərik.\n"
        "Zəhmət olmasa /start yazın və ya lokasiyanı daxil edin.")

@bot.callback_query_handler(func=lambda c: c.data=="FINISH_MULTI")
def finish_multi_cb(call):
    chat_id = call.message.chat.id
    bcs = user_data[chat_id]["barcodes"]
    if not bcs:
        bot.send_message(chat_id, "Heç barkod yoxdur. Yenidən şəkil göndərin.")
        return
    user_data[chat_id]["index"] = 0
    first_bc = bcs[0]
    bot.send_message(chat_id,
        f"{len(bcs)} barkod aşkar olundu.\n"
        f"1-ci barkod: <b>{first_bc}</b>\n"
        "Məhsul adını (tam və ya qismən) daxil edin."
    )
    user_state[chat_id] = STATE_WAIT_ASSET

########################################
# Photo => multi detection
########################################
@bot.message_handler(content_types=['photo'])
def handle_photo(m):
    chat_id = m.chat.id
    if user_state.get(chat_id, STATE_IDLE) != STATE_WAIT_PHOTO:
        bot.send_message(chat_id, "Hazırda foto qəbul edilmir. /start edin.")
        return

    file_id = m.photo[-1].file_id
    info = bot.get_file(file_id)
    downloaded = bot.download_file(info.file_path)
    np_img = np.frombuffer(downloaded, np.uint8)
    cv_img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

    found = detect_multi_barcodes(cv_img)
    if not found:
        bot.send_message(chat_id, "Heç bir barkod tapılmadı. Daha yaxın/açıqlı şəkil çəkin.")
        return

    if user_data[chat_id]["mode"]=="single":
        # pick first if multiple
        bc = found[0]
        user_data[chat_id]["barcodes"] = [bc]
        if len(found)>1:
            bot.send_message(chat_id,
                f"{len(found)} barkod aşkarlandı, birincisi götürülür: <b>{bc}</b>")
        else:
            bot.send_message(chat_id,
                f"Barkod: <b>{bc}</b>")
        bot.send_message(chat_id,
            "Məhsul adını (tam və ya qismən) daxil edin.")
        user_state[chat_id] = STATE_WAIT_ASSET
    else:
        # multi
        user_data[chat_id]["barcodes"].extend(found)
        unique_bcs = list(set(user_data[chat_id]["barcodes"]))
        user_data[chat_id]["barcodes"] = unique_bcs  # dedupe
        bot.send_message(chat_id,
            f"Barkod(lar): {', '.join(unique_bcs)}\n"
            "Daha şəkil göndərə ya da 'Bitir' / 'Stop/Restart' basın."
        )

########################################
# Wait for Asset
########################################
@bot.message_handler(func=lambda x: user_state.get(x.chat.id)==STATE_WAIT_ASSET, content_types=['text'])
def handle_asset_name(x):
    chat_id = x.chat.id
    query = x.text.strip()

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
        "Tapdığım uyğun adlar, ya da 'Custom Name':",
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
        "Tapdığım uyğun adlar, ya da 'Custom Name':",
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
# ASK QTY
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
    if pick=="OTHER":
        bot.send_message(chat_id, "Zəhmət olmasa miqdarı rəqəm kimi daxil edin.")
        user_state[chat_id] = STATE_WAIT_QUANTITY
        return
    user_data[chat_id]["qty"] = int(pick)
    finalize_barcode(chat_id)

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_QUANTITY, content_types=['text'])
def handle_qty_text(m):
    chat_id = m.chat.id
    try:
        val = int(m.text.strip())
    except:
        bot.send_message(chat_id, "Zəhmət olmasa düzgün rəqəm.")
        return
    user_data[chat_id]["qty"] = val
    finalize_barcode(chat_id)

def finalize_barcode(chat_id):
    data = user_data[chat_id]
    idx = data["index"]
    bc_list = data["barcodes"]
    if idx>=len(bc_list):
        bot.send_message(chat_id, "Xəta: barkod siyahısı artıq bitti.")
        user_state[chat_id] = STATE_IDLE
        return

    bc = bc_list[idx]
    asset = data["asset_name"]
    qty = data["qty"]
    loc = data["location"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Save row [time, location, bc, asset, qty]
    try:
        main_sheet.append_row([now, loc, bc, asset, qty])
        bot.send_message(chat_id,
            f"✅ Qeyd:\nTarix: {now}\nLokasiya: {loc}\nBarkod: {bc}\nAd: {asset}\nSay: {qty}")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Xəta: {e}")

    if data["mode"]=="multi":
        data["index"]+=1
        if data["index"]<len(bc_list):
            next_bc = bc_list[data["index"]]
            bot.send_message(chat_id,
                f"{data['index']+1}-ci barkod: <b>{next_bc}</b>\n"
                "Məhsul adını daxil edin (tam ya qismən).")
            user_state[chat_id] = STATE_WAIT_ASSET
        else:
            bot.send_message(chat_id, "Bütün barkodlar tamamlandı! Sağolun.")
            user_state[chat_id] = STATE_IDLE
    else:
        # single
        bot.send_message(chat_id,
            "Tək barkod prosesi bitdi. /start yazıb yeni günə başlaya bilərsiniz.")
        user_state[chat_id] = STATE_IDLE

########################################
# FLASK
########################################
@app.route("/", methods=['GET'])
def home():
    return "Bot is running!", 200

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        data = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(data)
        bot.process_new_updates([update])
        return "ok", 200
    else:
        abort(403)

if __name__=="__main__":
    setup_webhook()
    # Gunicorn will run 'app'
    pass
