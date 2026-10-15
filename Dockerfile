
FROM python:3.11.9-slim

# Install system dependencies for GeoPandas, GDAL, Shapely, and OpenCV.
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

# Install Python dependencies.
COPY backend/requirements.txt ./requirements.txt

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application files.
COPY backend ./backend
COPY frontend ./frontend
COPY models ./models

# Download YOLO weights during the build; continue if prefetch fails.
RUN cd /app && python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" || echo "YOLO weights prefetch skipped"

# Configure startup.
COPY start.sh ./start.sh
RUN chmod +x ./start.sh

EXPOSE 8000

CMD ["./start.sh"]
