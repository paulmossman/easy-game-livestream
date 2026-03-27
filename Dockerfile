FROM python:3.9-slim

# Install ffmpeg and an emoji-capable font for the mute icon overlay
RUN apt-get update && apt-get install -y ffmpeg fonts-noto-color-emoji && rm -rf /var/lib/apt/lists/*

# Set workdir
WORKDIR /app

# Copy requirements
COPY requirements.txt .

# Install Python deps
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src/ .

# Expose port
EXPOSE 5000

# Run
CMD ["python", "app.py"]
