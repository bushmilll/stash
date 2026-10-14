FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY stash_app.py .
ENV STASH_DATA=/data STASH_HOST=0.0.0.0 STASH_NO_BROWSER=1
VOLUME /data
EXPOSE 8765
CMD ["python", "-m", "app"]
