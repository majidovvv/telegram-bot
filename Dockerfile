# Dockerfile

FROM python:3.11-slim

# Install all OS dependencies needed by:
#  - pyzbar (zbar)
#  - tesseract OCR
#  - OpenCV (libGL.so.1)
RUN apt-get update && apt-get install -y \
    libzbar0 \
    tesseract-ocr \
    libgl1-mesa-glx \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy code
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Start the bot
CMD ["python", "bot.py"]
