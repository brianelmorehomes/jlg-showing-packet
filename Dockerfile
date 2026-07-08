FROM python:3.11-slim

# System libraries WeasyPrint needs for PDF/print rendering (Pango, Cairo,
# GDK-Pixbuf) -- this is exactly why we can't run this on a plain serverless
# function; a real container gives us apt-get.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=10000
EXPOSE 10000

# 2 workers is plenty for single-agent, once-a-day use; keep it light so it
# fits comfortably in Render's free-tier memory limit. Timeout is longer
# than the flyer app's because building a packet chains together several
# PDF renders plus (if the map is on) one geocoding call per stop at ~1
# request/second.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
