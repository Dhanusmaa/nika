FROM python:3.12-slim

WORKDIR /app

# GIF/video-to-sticker conversion used by /kang needs the ffmpeg executable.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py bestpick_engine.py .

ENV PORT=8000

CMD ["python", "bot.py"]
