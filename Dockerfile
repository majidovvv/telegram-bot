# Dockerfile

# Use a lightweight Python image
FROM python:3.11-slim

# Install system dependencies needed by pyzbar (zbar) and Tesseract OCR
RUN apt-get update && apt-get install -y libzbar0 tesseract-ocr

# Create app directory
WORKDIR /app

# Copy all project files into /app
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run the bot when the container starts
CMD ["python", "bot.py"]
