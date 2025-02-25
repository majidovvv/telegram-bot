# Dockerfile

FROM python:3.11-slim

# 1) Install system dependencies needed by:
#    - pyzbar (zbar)
#    - Tesseract OCR
#    - OpenCV (libGL, libglib, etc.)
RUN apt-get update && apt-get install -y \
    libzbar0 \
    tesseract-ocr \
    libgl1 \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2) Copy project files
COPY . /app

# 3) Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 4) Start the bot
CMD ["python", "bot.py"]
