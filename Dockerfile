FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    CLOAKBROWSER_AUTO_UPDATE=false \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    python3-venv \
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
    libasound2t64 \
    libatspi2.0-0 \
    libx11-6 \
    libxcb1 \
    libxext6 \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy application files
COPY . .

RUN chmod +x /app/docker-entrypoint.sh && \
    (useradd -m -s /bin/bash ubuntu || true) && \
    chown -R ubuntu:ubuntu /app

USER ubuntu

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Pre-download cloakbrowser chromium binary into ubuntu user cache
RUN python3 -c "import cloakbrowser; print(cloakbrowser.ensure_binary())"

USER root
# Symlink cloakbrowser binary to standard system paths for Pydoll validation
RUN CHROME_PATH=$(/app/venv/bin/python3 -c "import cloakbrowser; print(cloakbrowser.ensure_binary())") && \
    ln -sf "$CHROME_PATH" /usr/bin/google-chrome && \
    ln -sf "$CHROME_PATH" /usr/bin/google-chrome-stable && \
    chmod 755 "$CHROME_PATH" && \
    chmod -R 755 /home/ubuntu/.cloakbrowser

USER ubuntu

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
