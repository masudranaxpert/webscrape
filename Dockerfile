FROM python:3.13-slim

# Install latest official Google Chrome Stable and stealth canvas font dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-ipafont-gothic \
    fonts-wqy-zenhei \
    libasound2 \
    libx11-xcb1 \
    libdbus-glib-1-2 \
    libnss3 \
    libgbm1 \
    && wget -q -O /tmp/chrome.deb "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" \
    && apt-get install -y /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Verify Google Chrome installation
RUN google-chrome --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BROWSER_HEADLESS=true \
    CHROME_BIN=/usr/bin/google-chrome

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
