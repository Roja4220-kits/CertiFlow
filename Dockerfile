FROM python:3.12-slim

# tesseract = placeholder detection in image designs
# libfribidi0 + libharfbuzz0b = correct widths for Telugu/Hindi text
# gosu = start as root only to fix disk permissions, then run as a normal user
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr libfribidi0 libharfbuzz0b fonts-dejavu-core gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 certiflow && chown -R certiflow /app

ENV CERTIFLOW_DATA_DIR=/data \
    CERTIFLOW_TRUST_PROXY=1 \
    PORT=8000
EXPOSE 8000

# A mounted disk (e.g. Render) is owned by root, so fix its owner first, then drop to the app user.
# ONE worker + many threads: background jobs live inside the worker process.
CMD ["sh", "-c", "mkdir -p \"$CERTIFLOW_DATA_DIR\" && chown -R certiflow:certiflow \"$CERTIFLOW_DATA_DIR\" && exec gosu certiflow gunicorn app:app --workers 1 --threads 8 --timeout 120 --access-logfile - --error-logfile - --bind 0.0.0.0:${PORT:-8000}"]