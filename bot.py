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

# ... all your code, environment variables, etc. remain the same ...

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# Instead of opening the sheet here, we just build 'gc' but not do `open_by_key`.
creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/spreadsheets"]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(credentials)

# We'll store main_sheet in a global variable, but only fetch it when needed
_main_sheet = None
def get_main_sheet():
    """
    Lazy-load the main sheet.
    If you get an APIError 429 or 503, you can try sleeping or show a user-friendly error.
    """
    global _main_sheet
    if _main_sheet is None:
        tries = 0
        while tries < 3:  # try up to 3 times
            try:
                print("Attempting to open Google Sheet by key...")
                # fetch the spreadsheet
                spreadsheet = gc.open_by_key(SPREADSHEET_ID)
                _main_sheet = spreadsheet.sheet1
                print("Google Sheets açıldı:", _main_sheet.title)
                break
            except gspread.exceptions.APIError as e:
                tries += 1
                print(f"APIError while opening sheet (attempt {tries}): {e}")
                # If it's a 429 or 503, we can sleep a bit
                time.sleep(5)  # wait 5 sec
        if _main_sheet is None:
            # last attempt failed
            raise RuntimeError("Could not open main sheet due to repeated API errors.")
    return _main_sheet

# Next, whenever you'd do main_sheet.append_row(...),
# you call get_main_sheet().append_row(...)

# The rest of your bot code is the same, except for the place where you do the append_row.
# Instead of `main_sheet.append_row(...)`, do:
#
#   sheet = get_main_sheet()
#   sheet.append_row([...])

# For example:

def finalize_barcode_data(...):
    sheet = get_main_sheet()
    sheet.append_row([...])
    ...

# The rest of your code remains as is, with morphological barcode scanning, day flow, etc.
# Just ensure anywhere you used main_sheet, you call get_main_sheet() instead.

# For example:

def finalize_barcode_entry(...):
    try:
        sheet = get_main_sheet()
        sheet.append_row([timestamp, loc, inv, bc, desc, qty])
    except gspread.exceptions.APIError as e:
        # Show an error or re-try
        bot.send_message(chat_id, f"Google Sheets APIError: {e}")

# etc.


# Then your if __name__=="__main__": code remains the same:
if __name__=="__main__":
    setup_webhook()
    pass
