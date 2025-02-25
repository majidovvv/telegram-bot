print("Starting bot...")

import telebot
print("Telebot imported successfully!")

bot = telebot.TeleBot("YOUR_BOT_TOKEN")

print("Bot is about to start polling...")
bot.polling(none_stop=True, interval=1, timeout=20)

print("Bot is running!")

import logging
import os
import json
import requests
import telebot
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from pyzbar.pyzbar import decode
from PIL import Image

# Load environment variables (replace with your actual Telegram Bot Token)
TELEGRAM_BOT_TOKEN = "7430353636:AAFnka_jjxF317mo-DHKxfHhDyZFdsYg2ss"
SPREADSHEET_ID = "1Nzrbl2gF1CxP52ebKSo1SC45Aq25BYoWvrd8V5dRyPw"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Google Sheets API Setup
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SERVICE_ACCOUNT_FILE = "telegram-asset-bot-949e419b88dd.json"

creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
service = build("sheets", "v4", credentials=creds)
sheet = service.spreadsheets()

# Temporary storage for user inputs
user_data = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "Hello! Send a photo of the asset.")
    user_data[message.chat.id] = {}

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    file_id = message.photo[-1].file_id
    file_info = bot.get_file(file_id)
    file_path = file_info.file_path
    downloaded_file = bot.download_file(file_path)
    
    img_path = f"temp_{chat_id}.jpg"
    with open(img_path, "wb") as f:
        f.write(downloaded_file)
    
    if "asset_photo" not in user_data[chat_id]:
        user_data[chat_id]["asset_photo"] = img_path
        bot.send_message(chat_id, "Now send a photo of the barcode.")
    else:
        barcode_number = extract_barcode(img_path)
        if barcode_number:
            user_data[chat_id]["barcode"] = barcode_number
            bot.send_message(chat_id, f"Detected barcode: {barcode_number}\nNow, enter the asset description.")
        else:
            bot.send_message(chat_id, "Couldn't read the barcode. Try again.")

@bot.message_handler(func=lambda message: message.text and message.chat.id in user_data and "barcode" in user_data[message.chat.id])
def handle_description(message):
    chat_id = message.chat.id
    user_data[chat_id]["description"] = message.text
    save_to_sheets(chat_id)
    bot.send_message(chat_id, "Data saved successfully!")
    user_data.pop(chat_id, None)


def extract_barcode(image_path):
    try:
        img = Image.open(image_path)
        barcodes = decode(img)
        if barcodes:
            return barcodes[0].data.decode("utf-8")
    except Exception as e:
        print("Error decoding barcode:", e)
    return None


def save_to_sheets(chat_id):
    data = user_data.get(chat_id, {})
    if data:
        values = [[data.get("barcode", ""), data.get("description", "")]]
        request = sheet.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Sheet1!A:B",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        request.execute()


if __name__ == "__main__":
    bot.polling(none_stop=True)
