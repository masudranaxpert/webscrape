FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CLOAKBROWSER_AUTO_UPDATE=false

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    ca-certificates \
    xvfb \
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-ipafont-gothic \
    fonts-wqy-zenhei \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libx11-6 \
    libxcb1 \
    libxext6 \
    libgtk-3-0 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download cloakbrowser chromium
RUN python3 -c "import cloakbrowser; print(cloakbrowser.ensure_binary())"

COPY . .

EXPOSE 8000

CMD ["xvfb-run", "-a", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
