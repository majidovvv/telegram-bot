# Dockerfile

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies needed for:
#   - OpenCV (libGL, etc.)
#   - zbar (for pyzbar)
#   - tesseract (optional if you want OCR fallback)
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

# Expose port 5000 (Render sees it as a Web Service)
EXPOSE 5000

# Start the bot with gunicorn on port 5000
CMD ["gunicorn", "-b", "0.0.0.0:5000", "bot:app"]
