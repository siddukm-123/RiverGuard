
FROM python:3.11.9-slim

# System dependencies required by geospatial and OpenCV packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgdal-dev \
    gdal-bin \
    libgeos-dev \
    libproj-dev \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV GDAL_CONFIG=/usr/bin/gdal-config

WORKDIR /app

# Install dependencies separately for better layer caching.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files.
COPY backend ./backend
COPY frontend ./frontend
COPY models ./models

# Pre-fetch YOLO weights.
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" \
    || echo "YOLO weights prefetch skipped"

# Configure startup.
COPY start.sh ./start.sh
RUN chmod +x ./start.sh

EXPOSE 8000

CMD ["./start.sh"]
