
FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    gcc \
    pkg-config \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libavfilter-dev \
    libswscale-dev \
    libswresample-dev \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements into the image
COPY requirements.txt .

# Install build dependencies

RUN pip install --no-cache-dir --default-timeout=300 --retries=10 "setuptools<81" "wheel==0.45.1" "packaging==23.2" Cython

# Install CPU-only PyTorch
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 \
    torch==2.2.2 \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple

# Install Whisper using the existing PyTorch installation
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 \
    --no-build-isolation openai-whisper==20231117

# Install remaining dependencies
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 \
    --no-build-isolation -r requirements.txt

# Copy application code
COPY . .

# Expose API port
EXPOSE 8000

# Start the API
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
