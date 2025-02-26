# Dockerfile

FROM python:3.11-slim

# 1) Install system dependencies for:
#   - pyzbar (zbar)
#   - tesseract OCR
#   - OpenCV (libGL, etc.)
#   - For building some Python packages (e.g., pip's needs)
RUN apt-get update && apt-get install -y \
    build-essential \
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

# 2) Copy code
COPY . /app

# 3) Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 4) Expose port 5000 for the webhook
EXPOSE 5000

# 5) Start your Flask server via gunicorn
CMD ["gunicorn", "bot:app", "--bind", "0.0.0.0:5000", "--workers=1"]
