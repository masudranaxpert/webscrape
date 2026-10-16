FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    CLOAKBROWSER_AUTO_UPDATE=false \
    CLOAKBROWSER_SUPPRESS_FONT_WARNING=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BROWSER_HEADLESS=false

USER root

# Base packages with tini for zombie process reaping
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    python3-venv \
    wget \
    curl \
    ca-certificates \
    xvfb \
    xdotool \
    openbox \
    tini \
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-ipafont-gothic \
    fonts-wqy-zenhei \
    fonts-unifont \
    fonts-freefont-ttf \
    fonts-tlwg-loma-otf \
    fonts-urw-base35 \
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

RUN (useradd -m -s /bin/bash ubuntu || true) && \
    mkdir -p /app /home/ubuntu/.cloakbrowser /tmp/.X11-unix && \
    chmod 1777 /tmp/.X11-unix && \
    chown -R ubuntu:ubuntu /app /home/ubuntu/.cloakbrowser

# 1. Install dependencies first (layer-cached across code changes)
COPY requirements.txt .

USER ubuntu

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Pre-download cloakbrowser chromium binary into ubuntu user cache
RUN python3 -c "import cloakbrowser; print(cloakbrowser.ensure_binary())"

# Bake the GeoLite2 database (~70 MB) used to match timezone/locale/WebRTC to the egress IP, so the first proxied
# launch does not stall on a download; non-fatal because CloakBrowser retries the download at runtime
RUN python3 -c "from cloakbrowser.geoip import _ensure_geoip_db; print('GeoIP DB:', _ensure_geoip_db())" || \
    echo "GeoIP DB pre-download failed; will download on first use"

USER root
# Symlink cloakbrowser binary to standard system paths
RUN CHROME_PATH=$(/app/venv/bin/python3 -c "import cloakbrowser; print(cloakbrowser.ensure_binary())") && \
    ln -sf "$CHROME_PATH" /usr/bin/google-chrome && \
    ln -sf "$CHROME_PATH" /usr/bin/google-chrome-stable && \
    chmod 755 "$CHROME_PATH" && \
    chmod -R 755 /home/ubuntu/.cloakbrowser

# 2. Copy application files (changes frequently without invalidating dependencies)
COPY . .
RUN chmod +x /app/docker-entrypoint.sh && \
    chown -R ubuntu:ubuntu /app

USER ubuntu

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
