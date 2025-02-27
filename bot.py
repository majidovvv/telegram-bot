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
# Ətraf Mühit Dəyişənləri
########################################
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
ASSET_TAB_NAME = "Asset Data"  # Aktiv adları olan sheet

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN tapılmadı!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

########################################
# Google Sheets Konfiqurasiyası
########################################
creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets"
]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(credentials)

main_sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
print("Google Sheets bağlantısı uğurlu:", main_sheet.title)

try:
    asset_worksheet = gc.open_by_key(SPREADSHEET_ID).worksheet(ASSET_TAB_NAME)
    asset_data = asset_worksheet.col_values(1)
    if asset_data and asset_data[0].lower().startswith("asset"):
        asset_data.pop(0)
except Exception as e:
    print("Aktiv adları yüklənərkən xəta:", e)
    asset_data = []

print(f"'{ASSET_TAB_NAME}' səhifəsindən {len(asset_data)} ad yükləndi.")

########################################
# Sessiya Məlumatları
########################################
user_data = {}  # chat_id -> {...}
user_state = {} # chat_id -> state (str)

# Mümkün states
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
        "mode": "single",   # tək və ya multi
        "barcodes": [],     # tapılan barkodlar
        "index": 0,         # çox barkodda hansındayıq
        "asset_name": "",   # fuzzy seçilmiş ad
        "qty": 0,           # miqdar
        "pending_barcode": None,
        "pending_asset": None,
        "pending_qty": None
    }
    user_state[chat_id] = STATE_IDLE

########################################
# Webhook Qurulması
########################################
def setup_webhook():
    bot.remove_webhook()
    domain = os.getenv("RENDER_EXTERNAL_HOSTNAME") or "https://yourapp.onrender.com"
    wh_url = f"{domain}/webhook/{TELEGRAM_BOT_TOKEN}"
    bot.set_webhook(url=wh_url)
    print("Webhook quruldu:", wh_url)

########################################
# /start, /finish və /cancel Komandaları
########################################
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    init_session(chat_id)

    guide_text = (
        "👋 Salam! Bu botla günə başlayırsınız.\n"
        "🔹 Adımlar:\n"
        "1) 'Yer Daxil Et' düyməsinə klikləyib olduğunuz məkanı qeyd edin.\n"
        "2) İstəsəniz inventar kodunu daxil edin, ya da 'Heç inventar kodu yoxdur' deyin.\n"
        "3) Tək veya çox barkod rejimini seçin.\n"
        "4) Barkodlu şəkilləri göndərin, bot skan edəcək.\n"
        "5) Məhsul adını (fuzzy search), sayını daxil edin.\n"
        "6) Sonda Edit/Delete/Confirm ilə təsdiqləyin.\n"
        "✅ Günü bitirmək üçün /finish yazın.\n"
    )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Yer Daxil Et", callback_data="ENTER_LOCATION"))
    bot.send_message(chat_id, guide_text, reply_markup=kb)

@bot.message_handler(commands=['finish'])
def cmd_finish(message):
    chat_id = message.chat.id
    init_session(chat_id)
    bot.send_message(chat_id, "Günü bitirdiniz. Növbəti gün /start yaza bilərsiniz.")

@bot.message_handler(commands=['cancel'])
def cmd_cancel(message):
    chat_id = message.chat.id
    init_session(chat_id)
    bot.send_message(chat_id, "Cari əməliyyat ləğv edildi. Yeni gün üçün /start yazın.")

########################################
# Lokasiyanı Daxil Et
########################################
@bot.callback_query_handler(func=lambda c: c.data == "ENTER_LOCATION")
def cb_enter_location(call):
    chat_id = call.message.chat.id
    user_state[chat_id] = STATE_WAIT_LOCATION
    bot.send_message(chat_id, "Olduğunuz məkanı daxil edin (məs: 'Anbar A').")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == STATE_WAIT_LOCATION, content_types=['text'])
def handle_location_input(m):
    chat_id = m.chat.id
    loc = m.text.strip()
    user_data[chat_id]["location"] = loc

    # Sonra inventar kodu
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("İnventar Kodunu Daxil Et", callback_data="INVENTORY_ENTER"),
        InlineKeyboardButton("Heç inventar kodu yoxdur", callback_data="INVENTORY_NONE")
    )
    bot.send_message(chat_id,
        f"Lokasiya '{loc}' olaraq qəbul edildi.\nİnventar kodu daxil etmək istəyirsiniz?",
        reply_markup=kb
    )

########################################
# İnventar Kodu
########################################
@bot.callback_query_handler(func=lambda c: c.data in ["INVENTORY_ENTER", "INVENTORY_NONE"])
def cb_inventory_choice(call):
    chat_id = call.message.chat.id
    if call.data == "INVENTORY_ENTER":
        user_state[chat_id] = STATE_WAIT_INVENTORY_INPUT
        bot.send_message(chat_id, "İnventar kodunu əl ilə yazın.")
    else:
        user_data[chat_id]["inventory_code"] = ""
        show_mode_keyboard(chat_id)

@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == STATE_WAIT_INVENTORY_INPUT, content_types=['text'])
def handle_inventory_input(m):
    chat_id = m.chat.id
    inv = m.text.strip()
    user_data[chat_id]["inventory_code"] = inv
    bot.send_message(chat_id,
        f"İnventar kodu qəbul edildi: <b>{inv}</b>\nRejim seçməyə keçək.")
    show_mode_keyboard(chat_id)

########################################
# Barkod Rejimi Seç
########################################
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

@bot.callback_query_handler(func=lambda c: c.data in ["MODE_SINGLE","MODE_MULTI"])
def pick_mode_cb(call):
    chat_id = call.message.chat.id
    if call.data=="MODE_SINGLE":
        user_data[chat_id]["mode"] = "single"
        bot.send_message(chat_id, "Tək barkod rejimi seçildi. Zəhmət olmasa barkod şəkli göndərin.")
    else:
        user_data[chat_id]["mode"] = "multi"
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("Bitir", callback_data="FINISH_MULTI"),
            InlineKeyboardButton("Stop/Restart", callback_data="STOP_RESTART")
        )
        bot.send_message(chat_id,
            "Çox barkod rejimi. Bir neçə barkodlu şəkil göndərə bilərsiniz.\n"
            "Bitirəndə 'Bitir', ləğv üçün 'Stop/Restart' düyməsinə basın.",
            reply_markup=kb
        )

    user_data[chat_id]["barcodes"].clear()
    user_data[chat_id]["index"] = 0
    user_state[chat_id] = STATE_WAIT_PHOTO

@bot.callback_query_handler(func=lambda c: c.data=="STOP_RESTART")
def cb_stop_restart(call):
    chat_id = call.message.chat.id
    init_session(chat_id)
    bot.send_message(chat_id,
        "Mövcud proses ləğv edildi. Yeni gün üçün /start yazın.")

@bot.callback_query_handler(func=lambda c: c.data=="FINISH_MULTI")
def cb_finish_multi(call):
    chat_id = call.message.chat.id
    bcs = user_data[chat_id]["barcodes"]
    if not bcs:
        bot.send_message(chat_id,"Heç barkod yoxdur. Yenidən şəkil göndərin.")
        return
    user_data[chat_id]["index"] = 0
    first_bc = bcs[0]
    bot.send_message(chat_id,
        f"{len(bcs)} barkod aşkarlandı.\n"
        f"1-ci barkod: <b>{first_bc}</b>\n"
        "Məhsul adını (tam və ya qismən) daxil edin."
    )
    user_state[chat_id] = STATE_WAIT_ASSET

########################################
# Barkodları Tanıma (Çoxsaylı)
########################################
def detect_multi_barcodes(np_img):
    # 1) Parlaqlıq artırmaq (əgər çox qaranlıqdırsa)
    mean_val = np_img.mean()
    if mean_val < 60:
        np_img = cv2.convertScaleAbs(np_img, alpha=1.5, beta=30)

    # 2) Morphological pass
    gray = cv2.cvtColor(np_img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3,3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    inv = 255 - thresh

    # Birinci pass: böyük kernel
    kernel_big = cv2.getStructuringElement(cv2.MORPH_RECT, (11,4))
    morph1 = cv2.morphologyEx(inv, cv2.MORPH_CLOSE, kernel_big)

    # İkinci pass: kiçik kernel
    kernel_small = cv2.getStructuringElement(cv2.MORPH_RECT, (3,3))
    morph2 = cv2.morphologyEx(morph1, cv2.MORPH_OPEN, kernel_small)

    contours, _ = cv2.findContours(morph2, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    found_codes = set()
    angles = range(0, 360, 10)  # 10 dərəcədənsə 360

    for c in contours:
        x,y,w,h = cv2.boundingRect(c)
        if w>20 and h>20:
            region = np_img[y:y+h, x:x+w]
            region_codes = try_decode_region(region, angles)
            for cd in region_codes:
                if is_our_barcode(cd):
                    found_codes.add(cd)

    # Bütün şəkil üçün də cəhd edirik
    whole_codes = try_decode_region(np_img, angles)
    for ccc in whole_codes:
        if is_our_barcode(ccc):
            found_codes.add(ccc)

    return list(found_codes)

def try_decode_region(np_img, angles):
    results = set()
    for ang in angles:
        rot = rotate_image(np_img, ang)
        multi = decode_zbar_multi(rot)
        for code_ in multi:
            results.add(code_)
    return list(results)

def rotate_image(cv_img, angle):
    (h, w) = cv_img.shape[:2]
    center = (w//2,h//2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(cv_img, M, (w,h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return rotated

def decode_zbar_multi(cv_img):
    from PIL import Image
    pil_img = Image.fromarray(cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB))
    barcodes = decode(pil_img, symbols=[
        ZBarSymbol.CODE128, ZBarSymbol.CODE39, ZBarSymbol.EAN13,
        ZBarSymbol.EAN8, ZBarSymbol.QRCODE
    ])
    out = []
    for b in barcodes:
        out.append(b.data.decode("utf-8"))
    return out

def is_our_barcode(code):
    # Məsələn, '^AZT\\d+'
    pattern = r'^AZT\d+'
    return bool(re.match(pattern, code))

########################################
# Foto Göndərilən Zaman
########################################
@bot.message_handler(content_types=['photo'])
def handle_photo(m):
    chat_id = m.chat.id
    st = user_state.get(chat_id, STATE_IDLE)
    if st!=STATE_WAIT_PHOTO:
        bot.send_message(chat_id,"Hazırda şəkil qəbul olunmur. /start ilə başlayın.")
        return

    file_id = m.photo[-1].file_id
    info = bot.get_file(file_id)
    downloaded = bot.download_file(info.file_path)
    np_img = np.frombuffer(downloaded, np.uint8)
    cv_img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

    found = detect_multi_barcodes(cv_img)
    if not found:
        bot.send_message(chat_id,"Heç barkod aşkar edilmədi. Daha aydın şəkil çəkin.")
        return

    if user_data[chat_id]["mode"]=="single":
        bc = found[0]
        user_data[chat_id]["barcodes"] = [bc]
        if len(found)>1:
            bot.send_message(chat_id,
                f"{len(found)} barkod tapıldı, birincisi götürülür: <b>{bc}</b>")
        else:
            bot.send_message(chat_id, f"Barkod: <b>{bc}</b>")
        bot.send_message(chat_id, "Məhsul adını (tam və ya qismən) daxil edin.")
        user_state[chat_id] = STATE_WAIT_ASSET
    else:
        # multi
        existing = set(user_data[chat_id]["barcodes"])
        for c in found:
            existing.add(c)
        user_data[chat_id]["barcodes"] = list(existing)
        bot.send_message(chat_id,
            f"Barkod(lar): {', '.join(found)}\n"
            "Daha şəkil göndərməyə davam edin və ya 'Bitir' / 'Stop/Restart'."
        )

########################################
# Fuzzy axtarış
########################################
def fuzzy_suggest(query, data, limit=3):
    if not data:
        return []
    return process.extract(query, data, limit=limit)

@bot.message_handler(func=lambda x: user_state.get(x.chat.id)==STATE_WAIT_ASSET, content_types=['text'])
def handle_asset_name(x):
    chat_id = x.chat.id
    q = x.text.strip()
    suggestions = fuzzy_suggest(q, asset_data, limit=3)
    if not suggestions:
        finalize_asset_info(chat_id, q)
        return

    kb = InlineKeyboardMarkup()
    for (name_,score_) in suggestions:
        kb.add(InlineKeyboardButton(
            text=f"{name_} ({score_}%)", callback_data=f"ASSET_PICK|{name_}"
        ))
    kb.add(InlineKeyboardButton(
        text="Custom Name", callback_data=f"ASSET_CUSTOM|{q}"
    ))
    bot.send_message(chat_id,"Aşağıdakı adlardan birini seçin və ya 'Custom Name':", reply_markup=kb)
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
    for (name_,score_) in suggestions:
        kb.add(InlineKeyboardButton(
            text=f"{name_} ({score_}%)", callback_data=f"ASSET_PICK|{name_}"
        ))
    kb.add(InlineKeyboardButton(
        text="Custom Name", callback_data=f"ASSET_CUSTOM|{q}"
    ))
    bot.send_message(chat_id,"Aşağıdakı adlardan birini seçin və ya 'Custom Name':", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ASSET_PICK|"))
def cb_asset_pick(call):
    chat_id = call.message.chat.id
    picked = call.data.split("|")[1]
    finalize_asset_info(chat_id, picked)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ASSET_CUSTOM|"))
def cb_asset_custom(call):
    chat_id = call.message.chat.id
    custom_ = call.data.split("|")[1]
    finalize_asset_info(chat_id, custom_)

def finalize_asset_info(chat_id, name_):
    user_data[chat_id]["asset_name"] = name_
    ask_quantity(chat_id)

########################################
# Miqdar girişi
########################################
def ask_quantity(chat_id):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("1", callback_data="QTY|1"),
        InlineKeyboardButton("2", callback_data="QTY|2"),
        InlineKeyboardButton("3", callback_data="QTY|3"),
        InlineKeyboardButton("Other", callback_data="QTY|OTHER")
    )
    n = user_data[chat_id]["asset_name"]
    bot.send_message(chat_id,
        f"Məhsul adı: <b>{n}</b>\nMiqdarı seçin:",
        reply_markup=kb
    )
    user_state[chat_id] = STATE_WAIT_QUANTITY

@bot.callback_query_handler(func=lambda c: c.data.startswith("QTY|"))
def cb_qty(call):
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
        val = int(m.text.strip())
        user_data[chat_id]["qty"] = val
        show_entry_summary(chat_id)
    except:
        bot.send_message(chat_id, "Zəhmət olmasa düzgün rəqəm.")

########################################
# Edit / Delete / Confirm Step
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

    data["pending_barcode"] = bc
    data["pending_asset"] = desc
    data["pending_qty"] = qty

    text = (
        "📋 Girişə Baxış:\n"
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
    d = user_data[chat_id]

    if choice=="ENTRY_EDIT":
        # yenidən məhsul adı + miqdar
        bot.send_message(chat_id,
            "Məhsul adını yenidən daxil edin (tam və ya qismən).")
        user_state[chat_id] = STATE_WAIT_ASSET

    elif choice=="ENTRY_DELETE":
        bot.send_message(chat_id,
            "Bu barkod silindi. Başqa barkod varsa foto göndərin ya da 'Bitir' düyməsi.")
        if d["mode"]=="multi":
            d["index"]+=1
            if d["index"]<len(d["barcodes"]):
                next_bc = d["barcodes"][d["index"]]
                bot.send_message(chat_id,
                    f"{d['index']+1}-ci barkod: <b>{next_bc}</b>\n"
                    "Məhsul adını (tam/qismən) daxil edin.")
                user_state[chat_id] = STATE_WAIT_ASSET
            else:
                bot.send_message(chat_id,
                    "Bütün barkodlar üçün məlumat tamamlandı! /finish yaza bilərsiniz.")
                user_state[chat_id] = STATE_IDLE
        else:
            bot.send_message(chat_id,
                "Tək barkod prosesi bitdi! /finish ilə günü bitirə bilərsiniz.")
            user_state[chat_id] = STATE_IDLE

    else:
        # ENTRY_CONFIRM
        bc = d["pending_barcode"]
        desc = d["pending_asset"]
        qty = d["pending_qty"]
        loc = d["location"]
        inv = d["inventory_code"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            main_sheet.append_row([now, loc, inv, bc, desc, qty])
            bot.send_message(chat_id, "✅ Məlumat Cədvələ Əlavə Olundu.")
        except Exception as e:
            bot.send_message(chat_id, f"❌ Xəta: {e}")

        if d["mode"]=="multi":
            d["index"]+=1
            if d["index"]<len(d["barcodes"]):
                nb = d["barcodes"][d["index"]]
                bot.send_message(chat_id,
                    f"{d['index']+1}-ci barkod: <b>{nb}</b>\n"
                    "Məhsul adını daxil edin (tam/qismən).")
                user_state[chat_id] = STATE_WAIT_ASSET
            else:
                bot.send_message(chat_id,
                    "Bütün barkodlar bitdi! /finish yaza bilərsiniz.")
                user_state[chat_id] = STATE_IDLE
        else:
            bot.send_message(chat_id,
                "Tək barkod prosesi bitdi. /finish yaza bilərsiniz.")
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
