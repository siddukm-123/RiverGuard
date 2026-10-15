```dockerfile
# RiverGuard backend — deployed to Railway.
# Use Docker explicitly because the repository also contains a
# package.json for the separate Vercel frontend.

FROM python:3.11.9-slim

# Install system libraries required by GeoPandas, GDAL, Shapely,
# OpenCV, and Python packages that compile C extensions.
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

# Install Python dependencies first to improve Docker build caching.
COPY backend/requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application files.
COPY backend ./backend
COPY frontend ./frontend
COPY models ./models

# Download default YOLO weights during the build.
# If the download fails, continue the build and allow runtime fallback.
RUN cd /app && python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" \
    || echo "YOLO weights prefetch skipped; runtime download may be required."

# Configure the startup script.
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8000

# start.sh uses Railway's PORT environment variable.
CMD ["./start.sh"]
```
