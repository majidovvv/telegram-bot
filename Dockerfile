# Dockerfile

FROM python:3.11-slim

# Install system dependencies needed by:
# - pyzbar (zbar)
# - tesseract-ocr
# - OpenCV (libGL, libglib, etc.)
RUN apt-get update && apt-get install -y \
    libzbar0 \
    tesseract-ocr \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy your code
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Start the bot
CMD ["python", "bot.py"]
