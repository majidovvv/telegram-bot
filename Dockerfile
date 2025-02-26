# Use a minimal Python image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Install system dependencies for OpenCV & barcode scanning
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot script
COPY bot.py .

# Expose the port for Flask
EXPOSE 5000

# Start the bot using Gunicorn
CMD ["gunicorn", "-b", "0.0.0.0:5000", "bot:app"]
