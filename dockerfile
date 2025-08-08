# ----------- Base Image -----------
FROM python:3.10-slim

# ----------- System Dependencies -----------
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    git \
    cron \
    && rm -rf /var/lib/apt/lists/*

# ----------- Encoding für saubere Logs -----------
ENV PYTHONIOENCODING=UTF-8

# ----------- Arbeitsverzeichnis -----------
WORKDIR /app

# ----------- Requirements installieren -----------
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ----------- Projektdateien kopieren -----------
COPY . .

# ----------- Cronjob Setup -----------
COPY cronjob.txt /etc/cron.d/bot-cron
RUN chmod 0644 /etc/cron.d/bot-cron && touch /var/log/cron.log
RUN crontab /etc/cron.d/bot-cron
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]