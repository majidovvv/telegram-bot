import os
import re
import cv2
import numpy as np
from datetime import datetime
import json
import time

from flask import Flask, request, abort
import telebot

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from pyzbar.pyzbar import decode, ZBarSymbol
import pytesseract
from thefuzz import process
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ENV VARIABLES
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON", "")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# GSheets Setup: Lazy-loading to avoid rate-limit errors
creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/spreadsheets"]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(credentials)

_main_sheet = None
def get_main_sheet():
    global _main_sheet
    if _main_sheet is None:
        tries = 0
        while tries < 3:
            try:
                print("Attempting to open Google Sheet by key...")
                spreadsheet = gc.open_by_key(SPREADSHEET_ID)
                _main_sheet = spreadsheet.sheet1
                print("Google Sheet Açıldı:", _main_sheet.title)
                break
            except gspread.exceptions.APIError as e:
                tries += 1
                print(f"APIError while opening sheet (attempt {tries}): {e}")
                time.sleep(5)
        if _main_sheet is None:
            raise RuntimeError("Could not open main sheet due to repeated API errors.")
    return _main_sheet

# We keep track of user sessions in memory for each chat_id
user_sessions = {}  # { chat_id: { "location": str, "inventory_code": str, ... } }

def get_session(chat_id):
    """Return the session dictionary for a given chat_id, creating if missing."""
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {
            "location": None,
            "inventory_code": None,
            "mode": None,              # 'single' or 'multi'
            "pending_barcodes": [],    # barcodes from the last scanned photo in multi mode
            "current_idx": 0,         # index of the barcode we're currently editing in multi mode
            "entries": [],            # all confirmed entries for this session
            "awaiting_name": False,   # are we waiting for the user to input name?
            "awaiting_quantity": False,# are we waiting for the user to input quantity?
            "temp_barcode": None,     # store the barcode we’re editing
            "temp_name": None,        # store the name for the current barcode
        }
    return user_sessions[chat_id]

def reset_session(chat_id):
    """Reset the user session data, typically on /finish or /start of new day."""
    user_sessions[chat_id] = {
        "location": None,
        "inventory_code": None,
        "mode": None,
        "pending_barcodes": [],
        "current_idx": 0,
        "entries": [],
        "awaiting_name": False,
        "awaiting_quantity": False,
        "temp_barcode": None,
        "temp_name": None,
    }

# START command
@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    reset_session(chat_id)
    bot.send_message(chat_id, "Günə başlayırıq! Zəhmət olmasa lokasiyanı daxil edin:")
    # Next message is location

@bot.message_handler(commands=["finish"])
def cmd_finish(message):
    chat_id = message.chat.id
    reset_session(chat_id)
    bot.send_message(chat_id, "Günü bitirdiniz. Sabah yenidən /start yaza bilərsiniz.")

@bot.message_handler(func=lambda m: get_session(m.chat.id)["location"] is None, content_types=["text"])
def handle_location(message):
    """First text after /start is location."""
    chat_id = message.chat.id
    session = get_session(chat_id)
    session["location"] = message.text.strip()
    # Ask for inventory code (or user can say 'Heç kod yoxdur')
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Heç kod yoxdur", callback_data="no_code"))
    bot.send_message(chat_id, "İnventar kodunu yazın (ya da düyməni seçin):", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data == "no_code")
def handle_no_code(call):
    chat_id = call.message.chat.id
    session = get_session(chat_id)
    session["inventory_code"] = None
    bot.send_message(chat_id, "Kod seçilmədi.")
    ask_single_or_multi(chat_id)

@bot.message_handler(func=lambda m: get_session(m.chat.id)["inventory_code"] is None and get_session(m.chat.id)["location"] is not None, content_types=["text"])
def handle_inventory_code(message):
    """User typed an inventory code or something else while we haven't set inventory_code yet."""
    chat_id = message.chat.id
    session = get_session(chat_id)
    session["inventory_code"] = message.text.strip()
    bot.send_message(chat_id, f"Inventar kodu seçildi: {session['inventory_code']}")
    ask_single_or_multi(chat_id)

def ask_single_or_multi(chat_id):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Tək Barkod", callback_data="single_mode"))
    kb.add(InlineKeyboardButton("Çox Barkod", callback_data="multi_mode"))
    bot.send_message(chat_id, "Zəhmət olmasa barkod rejimini seçin:", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data in ["single_mode","multi_mode"])
def choose_mode(call):
    chat_id = call.message.chat.id
    session = get_session(chat_id)
    mode_choice = call.data
    session["mode"] = "single" if mode_choice == "single_mode" else "multi"
    bot.send_message(chat_id, f"<b>{'Tək' if session['mode']=='single' else 'Çox'} barkod</b> rejimi seçildi!\nİndi barkod şəklini göndərin...")

# Handle photos
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    chat_id = message.chat.id
    session = get_session(chat_id)

    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    # Convert to numpy array for CV2
    np_arr = np.frombuffer(downloaded_file, np.uint8)
    img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    # Detect barcodes
    barcodes = decode(img, symbols=[ZBarSymbol.CODE128])
    recognized = []
    for bc in barcodes:
        val = bc.data.decode("utf-8")
        # If we only want barcodes that match ^AZT\d+ we can do:
        if re.match(r"^AZT\d+", val):
            recognized.append(val)

    if len(recognized) == 0:
        bot.send_message(chat_id, "Barkod tapılmadı. Yenidən cəhd edin.")
        return

    if session["mode"] == "single":
        # Process single barcode directly
        # We'll just take the first recognized (assuming user sends one barcode)
        bc_val = recognized[0]
        session["temp_barcode"] = bc_val
        # Now prompt for Asset Name
        bot.send_message(chat_id, f"Barkod: <b>{bc_val}</b>\nAdı daxil edin (və ya seçin):")
        session["awaiting_name"] = True
        session["pending_barcodes"] = []
        session["current_idx"] = 0
    else:
        # MULTI MODE: store all recognized barcodes
        session["pending_barcodes"] = recognized
        session["current_idx"] = 0
        if len(recognized) == 1:
            bot.send_message(chat_id, f"1 barkod tapıldı. Adını daxil edin (və ya seçin).")
        else:
            bot.send_message(chat_id, f"{len(recognized)} barkod tapıldı.\nİlk barkod üçün məlumat daxil edək.")
        # Start with the first barcode
        next_barcode_prompt(chat_id)

def next_barcode_prompt(chat_id):
    """
    Prompt for the current barcode's name. If we've handled all pending_barcodes, 
    we either finalize or ask user to confirm.
    """
    session = get_session(chat_id)
    if session["current_idx"] >= len(session["pending_barcodes"]):
        # We've prompted all barcodes. Summarize or let them confirm.
        finalize_multi_entries(chat_id)
        return

    bc_val = session["pending_barcodes"][session["current_idx"]]
    session["temp_barcode"] = bc_val
    session["temp_name"] = None
    session["awaiting_name"] = True
    session["awaiting_quantity"] = False

    bot.send_message(chat_id, f"Barkod: <b>{bc_val}</b>\nAdı daxil edin (və ya seçin):")

def finalize_multi_entries(chat_id):
    """When all multi barcodes have been processed, we show a summary and let user confirm or reset."""
    session = get_session(chat_id)
    if not session["entries"]:
        bot.send_message(chat_id, "Heç bir barkod təsdiqlənmədi. Yeni barkod göndərin və ya /finish.")
        return

    summary_lines = []
    for i, e in enumerate(session["entries"], start=1):
        summary_lines.append(f"{i}) {e['barcode']} / {e['name']} / {e['quantity']}")
    txt = "\n".join(summary_lines)
    bot.send_message(chat_id, f"Siyahı:\n{txt}")

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Bitir və Yadda saxla", callback_data="multi_confirm"))
    kb.add(InlineKeyboardButton("Barkodları yenidən daxil et", callback_data="multi_reset"))
    bot.send_message(chat_id, "Yadda saxlamaq və Google Sheets-ə əlavə etmək üçün <b>Bitir və Yadda saxla</b> basın.", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data in ["multi_confirm", "multi_reset"])
def multi_finalize_cb(call):
    chat_id = call.message.chat.id
    session = get_session(chat_id)
    if call.data == "multi_confirm":
        # Append to Sheets
        sheet = get_main_sheet()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        loc = session["location"] or ""
        inv = session["inventory_code"] or ""

        for e in session["entries"]:
            row_data = [now_str, loc, inv, e["barcode"], e["name"], e["quantity"]]
            try:
                sheet.append_row(row_data)
            except gspread.exceptions.APIError as ex:
                bot.send_message(chat_id, f"Google Sheets APIError: {ex}")
                return  # or handle partial success, etc.

        bot.send_message(chat_id, "Bütün barkodlar uğurla Google Sheets-ə əlavə olundu! Yeni barkod göndərə və ya /finish yaza bilərsiniz.")
        # Reset multi session except location/inventory so they can continue scanning
        session["pending_barcodes"] = []
        session["current_idx"] = 0
        session["entries"] = []
    else:
        # multi_reset
        bot.send_message(chat_id, "Çox barkod siyahısı sıfırlandı. Yenidən şəkil göndərin.")
        session["pending_barcodes"] = []
        session["current_idx"] = 0
        session["entries"] = []

# TEXT HANDLER: for name or quantity inputs
@bot.message_handler(func=lambda m: True, content_types=["text"])
def generic_text_handler(message):
    chat_id = message.chat.id
    session = get_session(chat_id)
    text = message.text.strip()

    # If we are awaiting name input
    if session["awaiting_name"]:
        session["temp_name"] = text
        session["awaiting_name"] = False
        # Next, ask for quantity
        session["awaiting_quantity"] = True
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("1", callback_data="qty_1"),
               InlineKeyboardButton("2", callback_data="qty_2"),
               InlineKeyboardButton("3", callback_data="qty_3"),
               InlineKeyboardButton("Digər", callback_data="qty_other"))
        bot.send_message(chat_id, f"Ad seçildi: <b>{text}</b>. Miqdarı seçin:", reply_markup=kb)
        return

    # If we are awaiting quantity and user typed a custom value
    if session["awaiting_quantity"]:
        # This means user typed a number directly
        if text.isdigit():
            finalize_barcode_entry(chat_id, int(text))
        else:
            bot.send_message(chat_id, "Zəhmət olmasa miqdarı rəqəmlə daxil edin.")
        return

    # Otherwise, it's a text that doesn't match a known state
    bot.send_message(chat_id, "Sizin məlumatınızı başa düşmədim. Xahiş olunur, düzgün mərhələdə cavab verin.")

# Callback for quantity
@bot.callback_query_handler(func=lambda call: call.data.startswith("qty_"))
def quantity_cb(call):
    chat_id = call.message.chat.id
    session = get_session(chat_id)
    if not session["awaiting_quantity"]:
        return

    data = call.data
    if data in ["qty_1", "qty_2", "qty_3"]:
        qty = int(data.split("_")[1])
        finalize_barcode_entry(chat_id, qty)
    elif data == "qty_other":
        bot.send_message(chat_id, "Miqdarı daxil edin:")
        # We'll handle it as typed text in the generic_text_handler
    else:
        bot.send_message(chat_id, "Seçim başa düşülmədi.")

def finalize_barcode_entry(chat_id, quantity):
    session = get_session(chat_id)
    bc = session["temp_barcode"]
    name = session["temp_name"]
    session["awaiting_quantity"] = False

    if session["mode"] == "single":
        # Single mode: append directly to Sheets
        sheet = get_main_sheet()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        loc = session["location"] or ""
        inv = session["inventory_code"] or ""
        row_data = [now_str, loc, inv, bc, name, quantity]
        try:
            sheet.append_row(row_data)
            bot.send_message(chat_id, f"<b>{bc}</b> -> <i>{name}</i> x {quantity} Sheets-ə əlavə olundu.")
        except gspread.exceptions.APIError as ex:
            bot.send_message(chat_id, f"Google Sheets APIError: {ex}")
        # Reset temp variables so user can scan next
        session["temp_barcode"] = None
        session["temp_name"] = None
        bot.send_message(chat_id, "Yeni barkod göndərə və ya /finish yaza bilərsiniz.")
    else:
        # Multi mode: store it in 'entries'
        session["entries"].append({
            "barcode": bc,
            "name": name,
            "quantity": quantity
        })
        bot.send_message(chat_id, f"<b>{bc}</b> -> <i>{name}</i> x {quantity} əlavə olundu.\nNövbəti barkod üçün məlumat daxil edilir...")
        # Move to next barcode
        session["current_idx"] += 1
        session["temp_barcode"] = None
        session["temp_name"] = None
        session["awaiting_name"] = False
        session["awaiting_quantity"] = False

        next_barcode_prompt(chat_id)

# Flask webhook (if you are using set_webhook)
@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def receive_update():
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ""
    else:
        abort(403)

def setup_webhook():
    url = os.environ.get("WEBHOOK_URL", "")
    if url:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=url + f"/{TELEGRAM_BOT_TOKEN}")
        print("Webhook set to:", url)

if __name__ == "__main__":
    setup_webhook()
