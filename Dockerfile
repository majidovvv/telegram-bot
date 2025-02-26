# Dockerfile

FROM python:3.11-slim

WORKDIR /app

# 1) Install system dependencies needed for:
#    - OpenCV (libGL, etc.)
#    - zbar (for pyzbar)
#    - tesseract (for OCR fallback if you want)
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libzbar0 \
    tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot script
COPY bot.py .

# Expose port 5000 (Render expects a web service)
EXPOSE 5000

# Start Gunicorn on port 5000, referencing 'bot:app'
CMD ["gunicorn", "-b", "0.0.0.0:5000", "bot:app"]
