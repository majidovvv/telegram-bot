# Use an official lightweight Python image
FROM python:3.11-slim

# Install system dependencies (zbar)
RUN apt-get update && apt-get install -y libzbar0

# Create app directory
WORKDIR /app

# Copy all files (including requirements.txt and bot.py)
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run the bot
CMD ["python", "bot.py"]
