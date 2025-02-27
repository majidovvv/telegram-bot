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

# Load asset data
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
user_data = {}  # chat_id -> {}
user_state = {} # chat_id -> str state

STATE_IDLE = "idle"
STATE_WAIT_LOCATION = "wait_location"
STATE_WAIT_INVENTORY_CHOICE = "wait_inventory_choice"
STATE_WAIT_INVENTORY_INPUT = "wait_inventory_input"
STATE_WAIT_PHOTO = "wait_photo"
STATE_WAIT_ASSET = "wait_asset"
STATE_WAIT_ASSET_PICK = "wait_asset_pick"
STATE_WAIT_QUANTITY = "wait_quantity"
STATE_WAIT_CONFIRM = "wait_confirm"

def init_session(chat_id):
    user_data[chat_id] = {
        "location": "",
        "inventory_code": "",
        "location_timestamp": datetime.now() - timedelta(days=1),
        "mode": "single",
        "barcodes": [],
        "index": 0,
        "asset_name": "",
        "qty": 0,
        "pending_barcode": None, # for final confirm
        "pending_asset": None,
        "pending_qty": None
    }
    user_state[chat_id] = STATE_IDLE

def need_location(chat_id):
    now = datetime.now()
    last = user_data[chat_id]["location_timestamp"]
    if (now - last)>timedelta(minutes=30):
        if now.date()!=last.date():
            return True
        return True
    return False

########################################
# WEBHOOK
########################################
def setup_webhook():
    bot.remove_webhook()
    domain = os.getenv("RENDER_EXTERNAL_HOSTNAME") or "https://yourapp.onrender.com"
    wh_url = f"{domain}/webhook/{TELEGRAM_BOT_TOKEN}"
    bot.set_webhook(url=wh_url)
    print("Webhook set to:", wh_url)

########################################
# /start & /finish
########################################
@bot.message_handler(commands=['start'])
def cmd_start(msg):
    chat_id = msg.chat.id
    init_session(chat_id)
    # Show a short guide with emojis, plus an "Enter Location" button
    guide_text = (
        "👋 Salam, xoş gəldiniz!\n"
        "🔹 1) 'Enter Location' düyməsinə basın və yerləşdiyiniz yeri daxil edin.\n"
        "🔹 2) İnventar kodu (olsa) daxil edin və ya 'No inventory code' seçin.\n"
        "🔹 3) Tək barkod skan ya çox barkod skan rejimini seçin.\n"
        "🔹 4) Barkodun şəklini göndərin, bot onu tanıyacaq.\n"
        "🔹 5) Məhsulun adını fuzzy search ilə seçin və say daxil edin.\n"
        "🔹 6) Təsdiqlədikdən sonra Google Sheets-ə yazılacaq.\n"
        "💡 Sonda /finish yaza bilərsiniz günü bitirmək üçün.\n"
    )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Enter Location", callback_data="ENTER_LOCATION"))
    bot.send_message(chat_id, guide_text, reply_markup=kb)

@bot.message_handler(commands=['finish'])
def cmd_finish(msg):
    chat_id = msg.chat.id
    init_session(chat_id)
    bot.send_message(chat_id,
        "Günü bitirdiniz. Bütün məlumat tamamlandı. Sabah yenidən /start yaza bilərsiniz.")

@bot.message_handler(commands=['cancel'])
def cmd_cancel(msg):
    chat_id = msg.chat.id
    init_session(chat_id)
    bot.send_message(chat_id, "Cari proses ləğv edildi. /start yaza bilərsiniz yenidən.")

########################################
# Callback: Enter Location
########################################
@bot.callback_query_handler(func=lambda c: c.data=="ENTER_LOCATION")
def cb_enter_location(call):
    chat_id = call.message.chat.id
    user_state[chat_id] = STATE_WAIT_LOCATION
    bot.send_message(chat_id,
        "Zəhmət olmasa mövcud lokasiyanızı daxil edin (örn: 'Anbar 2').")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_LOCATION, content_types=['text'])
def handle_location(m):
    chat_id = m.chat.id
    loc = m.text.strip()
    user_data[chat_id]["location"] = loc
    user_data[chat_id]["location_timestamp"] = datetime.now()
    # Next: inventory code
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("Enter Inventory Code", callback_data="INVENTORY_ENTER"),
        InlineKeyboardButton("No inventory code", callback_data="INVENTORY_NONE")
    )
    bot.send_message(chat_id,
        f"Lokasiya: <b>{loc}</b>\nİnventar kodu əlavə etmək istəyirsiniz?",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data in ["INVENTORY_ENTER", "INVENTORY_NONE"])
def cb_inventory_choice(call):
    chat_id = call.message.chat.id
    if call.data=="INVENTORY_ENTER":
        user_state[chat_id] = STATE_WAIT_INVENTORY_INPUT
        bot.send_message(chat_id, "Zəhmət olmasa inventar kodunu əl ilə daxil edin.")
    else:
        # no inventory code
        user_data[chat_id]["inventory_code"] = ""
        show_mode_keyboard(chat_id)

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_INVENTORY_INPUT, content_types=['text'])
def handle_inventory_input(m):
    chat_id = m.chat.id
    inv_code = m.text.strip()
    user_data[chat_id]["inventory_code"] = inv_code
    bot.send_message(chat_id, f"İnventar kodu qəbul edildi: <b>{inv_code}</b>")
    show_mode_keyboard(chat_id)

def show_mode_keyboard(chat_id):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("Tək Barkod", callback_data="MODE_SINGLE"),
        InlineKeyboardButton("Çox Barkod", callback_data="MODE_MULTI")
    )
    bot.send_message(chat_id,
        "Rejimi seçin:\n - Tək barkod\n - Çox barkod",
        reply_markup=kb
    )
    user_state[chat_id] = STATE_WAIT_PHOTO

@bot.callback_query_handler(func=lambda c: c.data in ["MODE_SINGLE", "MODE_MULTI"])
def pick_mode(call):
    chat_id = call.message.chat.id
    mode = call.data
    user_data[chat_id]["mode"] = "single" if mode=="MODE_SINGLE" else "multi"
    user_data[chat_id]["barcodes"].clear()
    user_data[chat_id]["index"] = 0

    if mode=="MODE_SINGLE":
        bot.send_message(chat_id,
            "Tək barkod rejimi seçildi.\nZəhmət olmasa barkodun şəklini göndərin.")
    else:
        # multi => add finish & stop/restart
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("Bitir", callback_data="FINISH_MULTI"),
            InlineKeyboardButton("Stop/Restart", callback_data="STOP_RESTART")
        )
        bot.send_message(chat_id,
            "Çox barkod rejimi. Bir neçə barkod şəkil göndərin.\n"
            "Bitirəndə 'Bitir', imtina üçün 'Stop/Restart'.",
            reply_markup=kb
        )

@bot.callback_query_handler(func=lambda c: c.data=="STOP_RESTART")
def cb_stop_restart(call):
    chat_id = call.message.chat.id
    init_session(chat_id)
    bot.send_message(chat_id,
        "Əvvəlki proses ləğv edildi.\nYeni gün üçün /start yaza bilərsiniz.")

@bot.callback_query_handler(func=lambda c: c.data=="FINISH_MULTI")
def cb_finish_multi(call):
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

@bot.message_handler(commands=['finish'])
def cmd_finish(m):
    chat_id = m.chat.id
    init_session(chat_id)
    bot.send_message(chat_id,
        "Günü bitirdiniz. Bütün proseslər tamamlandı. /start yaza bilərsiniz "
        "növbəti iş üçün.")

########################################
# MULTI BARCODE DETECTION
########################################
def detect_multi_barcodes(np_img):
    # Your morphological passes, angle increments, multi decode, pattern check...
    # omitted for brevity, but same as the final advanced approach
    # Return a list of codes
    return ["FAKE123"]  # placeholder, copy from previous code

########################################
# Photo Handler
########################################
@bot.message_handler(content_types=['photo'])
def handle_photo(m):
    chat_id = m.chat.id
    if user_state.get(chat_id, STATE_IDLE)!=STATE_WAIT_PHOTO:
        bot.send_message(chat_id, "Foto qəbul edilmir. /start edin.")
        return

    file_id = m.photo[-1].file_id
    info = bot.get_file(file_id)
    downloaded = bot.download_file(info.file_path)
    np_img = np.frombuffer(downloaded, np.uint8)
    cv_img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

    found_codes = detect_multi_barcodes(cv_img)  # advanced scanning
    if not found_codes:
        bot.send_message(chat_id, "Heç bir barkod tapılmadı. Daha aydın/diqqətli foto.")
        return

    if user_data[chat_id]["mode"]=="single":
        bc = found_codes[0]
        user_data[chat_id]["barcodes"] = [bc]
        if len(found_codes)>1:
            bot.send_message(chat_id,
                f"{len(found_codes)} barkod aşkar, birincisi seçildi: <b>{bc}</b>")
        else:
            bot.send_message(chat_id, f"Barkod: <b>{bc}</b>")
        bot.send_message(chat_id,
            "Məhsul adını (tam və ya qismən) daxil edin.")
        user_state[chat_id] = STATE_WAIT_ASSET
    else:
        # multi
        existing = set(user_data[chat_id]["barcodes"])
        for c in found_codes:
            existing.add(c)
        user_data[chat_id]["barcodes"] = list(existing)
        bot.send_message(chat_id,
            f"Barkod(lar): {', '.join(found_codes)}\n"
            "Daha şəkil göndərin və ya 'Bitir' / 'Stop/Restart'."
        )

########################################
# Fuzzy Name
########################################
def fuzzy_suggest(query, data, limit=3):
    # existing function or advanced from before
    return []

@bot.message_handler(func=lambda x: user_state.get(x.chat.id)==STATE_WAIT_ASSET, content_types=['text'])
def handle_asset_name(x):
    chat_id = x.chat.id
    query = x.text.strip()
    # do fuzzy
    suggestions = fuzzy_suggest(query, asset_data, limit=3)
    if not suggestions:
        # no suggestions => accept as custom
        finalize_asset_info(chat_id, query)
        return
    kb = InlineKeyboardMarkup()
    for (name,score) in suggestions:
        kb.add(InlineKeyboardButton(
            text=f"{name} ({score}%)",
            callback_data=f"ASSET_PICK|{name}"
        ))
    kb.add(InlineKeyboardButton(
        text="Custom Name", callback_data=f"ASSET_CUSTOM|{query}"
    ))
    bot.send_message(chat_id,
        "Uyğun adlar, ya 'Custom Name':",
        reply_markup=kb
    )
    user_state[chat_id] = STATE_WAIT_ASSET_PICK

@bot.message_handler(func=lambda x: user_state.get(x.chat.id)==STATE_WAIT_ASSET_PICK, content_types=['text'])
def handle_asset_retry(x):
    chat_id = x.chat.id
    q = x.text.strip()
    suggestions = fuzzy_suggest(q, asset_data, limit=3)
    if not suggestions:
        finalize_asset_info(chat_id, q)
        return

    kb = InlineKeyboardMarkup()
    for (name,score) in suggestions:
        kb.add(InlineKeyboardButton(
            text=f"{name} ({score}%)",
            callback_data=f"ASSET_PICK|{name}"
        ))
    kb.add(InlineKeyboardButton(
        text="Custom Name", callback_data=f"ASSET_CUSTOM|{q}"
    ))
    bot.send_message(chat_id,
        "Uyğun adlar, ya 'Custom Name':",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ASSET_PICK|"))
def cb_asset_pick(call):
    chat_id = call.message.chat.id
    name_ = call.data.split("|")[1]
    finalize_asset_info(chat_id, name_)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ASSET_CUSTOM|"))
def cb_asset_custom(call):
    chat_id = call.message.chat.id
    custom = call.data.split("|")[1]
    finalize_asset_info(chat_id, custom)

def finalize_asset_info(chat_id, name_):
    user_data[chat_id]["asset_name"] = name_
    ask_quantity(chat_id)

########################################
# QUANTITY
########################################
def ask_quantity(chat_id):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("1", callback_data="QTY|1"),
        InlineKeyboardButton("2", callback_data="QTY|2"),
        InlineKeyboardButton("3", callback_data="QTY|3"),
        InlineKeyboardButton("Other", callback_data="QTY|OTHER")
    )
    nm = user_data[chat_id]["asset_name"]
    bot.send_message(chat_id,
        f"Ad: <b>{nm}</b>\nMiqdarı seçin:",
        reply_markup=kb
    )
    user_state[chat_id] = STATE_WAIT_QUANTITY

@bot.callback_query_handler(func=lambda c: c.data.startswith("QTY|"))
def cb_qty_pick(call):
    chat_id = call.message.chat.id
    pick = call.data.split("|")[1]
    if pick=="OTHER":
        bot.send_message(chat_id, "Zəhmət olmasa miqdarı rəqəm kimi yazın.")
        return
    user_data[chat_id]["qty"] = int(pick)
    show_entry_summary(chat_id)

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)==STATE_WAIT_QUANTITY, content_types=['text'])
def handle_qty_text(m):
    chat_id = m.chat.id
    try:
        q = int(m.text.strip())
        user_data[chat_id]["qty"] = q
        show_entry_summary(chat_id)
    except:
        bot.send_message(chat_id,"Zəhmət olmasa düzgün rəqəm.")

########################################
# SHOW SUMMARY WITH EDIT/DELETE/CONFIRM
########################################
def show_entry_summary(chat_id):
    data = user_data[chat_id]
    idx = data["index"]
    bc_list = data["barcodes"]
    bc = bc_list[idx]
    desc = data["asset_name"]
    qty = data["qty"]
    loc = data["location"]
    inv = data["inventory_code"]
    # store pending so we can do confirm later
    data["pending_barcode"] = bc
    data["pending_asset"] = desc
    data["pending_qty"] = qty

    text = (
        f"📋 Baxış:\n"
        f"Lokasiya: {loc}\n"
        f"İnventar kodu: {inv}\n"
        f"Barkod: {bc}\n"
        f"Ad: {desc}\n"
        f"Say: {qty}\n\n"
        "Düzdür?"
    )
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("Edit", callback_data="ENTRY_EDIT"),
        InlineKeyboardButton("Delete", callback_data="ENTRY_DELETE"),
        InlineKeyboardButton("Confirm", callback_data="ENTRY_CONFIRM")
    )
    bot.send_message(chat_id, text, reply_markup=kb)
    user_state[chat_id] = STATE_WAIT_CONFIRM

@bot.callback_query_handler(func=lambda c: c.data in ["ENTRY_EDIT","ENTRY_DELETE","ENTRY_CONFIRM"])
def cb_entry_decision(call):
    chat_id = call.message.chat.id
    choice = call.data
    data = user_data[chat_id]
    if choice=="ENTRY_EDIT":
        # re-run the product name/qty
        # user wants to fix the asset name or quantity
        # let's go back to handle_asset step
        bot.send_message(chat_id,
            "Məhsul adını yenidən daxil edin (tam və ya qismən).")
        user_state[chat_id] = STATE_WAIT_ASSET
    elif choice=="ENTRY_DELETE":
        # user doesn't want to store this item at all
        bot.send_message(chat_id, "Bu barkod məlumatı silindi.")
        # proceed if multi
        if data["mode"]=="multi":
            data["index"]+=1
            if data["index"]<len(data["barcodes"]):
                next_bc = data["barcodes"][data["index"]]
                bot.send_message(chat_id,
                    f"{data['index']+1}-ci barkod: <b>{next_bc}</b>\n"
                    "Məhsul adını (tam/qismən) daxil edin.")
                user_state[chat_id] = STATE_WAIT_ASSET
            else:
                bot.send_message(chat_id, "Bütün barkodlar tamamlandı!")
                user_state[chat_id] = STATE_IDLE
        else:
            # single mode done
            bot.send_message(chat_id, "Tək barkod prosesi bitdi. /start yazın yeni gün üçün.")
            user_state[chat_id] = STATE_IDLE
    else:
        # ENTRY_CONFIRM => append row
        bc = data["pending_barcode"]
        desc = data["pending_asset"]
        qty = data["pending_qty"]
        loc = data["location"]
        inv = data["inventory_code"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # store row => [time, location, inv, bc, desc, qty]
        try:
            main_sheet.append_row([now, loc, inv, bc, desc, qty])
            bot.send_message(chat_id, "✅ Məlumat cədvələ əlavə olundu.")
        except Exception as e:
            bot.send_message(chat_id, f"❌ Xəta: {e}")

        # proceed if multi
        if data["mode"]=="multi":
            data["index"]+=1
            if data["index"]<len(data["barcodes"]):
                nb = data["barcodes"][data["index"]]
                bot.send_message(chat_id,
                    f"{data['index']+1}-ci barkod: <b>{nb}</b>\n"
                    "Məhsul adını daxil edin.")
                user_state[chat_id] = STATE_WAIT_ASSET
            else:
                bot.send_message(chat_id, "Bütün barkodların məlumatı tamamlandı! /finish yaza bilərsiniz.")
                user_state[chat_id] = STATE_IDLE
        else:
            bot.send_message(chat_id, "Tək barkod prosesi bitdi! /finish yaza bilərsiniz.")
            user_state[chat_id] = STATE_IDLE

########################################
# FLASK
########################################
@app.route("/", methods=['GET'])
def home():
    return "Bot is running!", 200

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type')=="application/json":
        raw = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(raw)
        bot.process_new_updates([update])
        return "ok",200
    else:
        abort(403)

if __name__=="__main__":
    setup_webhook()
    pass
