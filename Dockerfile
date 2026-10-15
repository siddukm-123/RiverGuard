# RiverGuard backend — deployed to Railway.
# Explicit Dockerfile instead of Nixpacks: this repo also has a root
# package.json (for the separate Vercel frontend build), which made
# Nixpacks auto-detect Node and skip Python/pip entirely. A Dockerfile
# removes that guesswork completely.

FROM python:3.11.9-slim

# System libraries geopandas/fiona/shapely need to build/run against,
# plus gcc for any packages that build C extensions from source.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgdal-dev \
    gdal-bin \
    libgeos-dev \
    libproj-dev \
    && rm -rf /var/lib/apt/lists/*

ENV GDAL_CONFIG=/usr/bin/gdal-config

WORKDIR /app

# Install Python deps first so this layer is cached unless requirements.txt changes.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Now copy the rest of the app (backend + frontend, needed since Flask serves
# the frontend's static files directly — see FRONTEND_DIR in backend/app.py).
COPY backend ./backend
COPY frontend ./frontend
COPY models ./models

# Bake the default YOLO weights into the image so the first detection request never downloads them
# (a slow first request used to risk a proxy timeout). Failure here is non-fatal: it falls back to runtime download.
RUN cd /app && python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" || echo "weights prefetch skipped"

COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8000

CMD ["./start.sh"]
