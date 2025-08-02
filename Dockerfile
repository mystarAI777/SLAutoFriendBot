FROM voicevox/voicevox_engine:latest
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static /app/static
CMD ["/bin/bash", "-c", "/usr/local/bin/python3 run.py --host 127.0.0.1 --num_threads 1 & gunicorn --bind 0.0.0.0:10000 --workers 1 app:app"]
