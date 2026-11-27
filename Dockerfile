FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MEMEBOT_DATA=/data PANEL_HOST=0.0.0.0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY memebot ./memebot
COPY config ./config
RUN useradd -r -u 10001 memebot && mkdir -p /data && chown memebot /data
USER memebot
VOLUME ["/data"]
EXPOSE 8787
# PANEL_HOST=0.0.0.0 inside the container means PANEL_PASSWORD must be set (the bot refuses otherwise).
# Publish the port only on localhost unless you put it behind HTTPS: -p 127.0.0.1:8787:8787
CMD ["sh", "-c", "python -m memebot init --data /data >/dev/null && exec python -m memebot run --data /data"]
