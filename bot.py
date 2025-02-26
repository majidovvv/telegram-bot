import os
import telebot
import json
import gspread
from flask import Flask, request
import cv2
import numpy as np
from pyzbar.pyzbar import decode
import pytesseract
from datetime import datetime

# Load environment variables
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

# Initialize Flask app
app = Flask(__name__)

# Initialize Telegram bot
bot = telebot.TeleBot(TOKEN)

# Google Sheets authentication
creds = json.loads(SERVICE_ACCOUNT_JSON)
client = gspread.service_account_from_dict(creds)
sheet = client.open_by_key(SPREADSHEET_ID).sheet1
print("Google Sheets connected.")

# Function to preprocess image for better barcode scanning
def preprocess_image(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)  # Convert to grayscale
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)  # Reduce noise
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)  # Improve contrast
    return thresh

# Function to scan barcode using ZBar first, fallback to OCR
def scan_barcode(image):
    preprocessed = preprocess_image(image)

    # Try ZBar first
    decoded_objects = decode(preprocessed)
    if decoded_objects:
        return decoded_objects[0].data.decode("utf-8")

    # Fallback: OCR if ZBar fails
    text = pytesseract.image_to_string(preprocessed, config='--psm 6')
    barcode = "".join(filter(str.isalnum, text))  # Clean unwanted characters
    return barcode if barcode else "Barkod tapılmadı"

# Handle /start command
@bot.message_handler(commands=['start'])
def start_message(message):
    bot.send_message(message.chat.id, "👋 Salam! Bu bot anbara daxil edilən malların qeydiyyatı üçün nəzərdə tutulub.\n📸 Şəkil çəkin və göndərin, bot avtomatik olaraq məlumatları emal edəcək.\n✅ Format: Tarix, Barkod, Məhsul adı, Say.")

# Handle received photos
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    try:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        # Convert image to OpenCV format
        np_arr = np.frombuffer(downloaded_file, np.uint8)
        image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

        # Scan the barcode
        barcode = scan_barcode(image)
        description = "Pano"  # Default description
        quantity = 1  # Default quantity

        # Save data to Google Sheets
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([now, barcode, description, quantity])
        bot.send_message(message.chat.id, f"📋 Məlumat qeydə alındı:\n🆔 Barkod: {barcode}\n📦 Məhsul: {description}\n🔢 Say: {quantity}")

    except Exception as e:
        bot.send_message(message.chat.id, "⚠️ Xəta baş verdi. Xahiş olunur şəkli düzgün çəkin və yenidən göndərin.")
        print(f"Error processing photo: {str(e)}")

# Flask route to keep the bot alive
@app.route('/', methods=['GET'])
def home():
    return "Bot is running!"

# Start Flask app
if __name__ == "__main__":
    bot.polling(none_stop=True)
    app.run(host="0.0.0.0", port=5000, debug=True)
