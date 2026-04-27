FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TG_SESSIONS_DIR=/app/sessions

WORKDIR /app

# Build deps for cryptg (needs gcc) and runtime libs for pillow.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY auth.py ./auth.py

# Sessions volume must be mounted at /app/sessions so logins persist.
RUN mkdir -p /app/sessions

EXPOSE 8000

CMD ["python", "-m", "app.main"]
