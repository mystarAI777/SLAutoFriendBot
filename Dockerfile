# VOICEVOX ENGINEをベースイメージとして使用
FROM voicevox/voicevox_engine:latest

# 作業ディレクトリを設定
WORKDIR /app

# ビルド時に必要なツールをインストール
# これにより、psycopg2-binaryのビルドエラーを防ぐ
RUN apt-get update && apt-get install -y build-essential libpq-dev && rm -rf /var/lib/apt/lists/*

# 必要なPythonライブラリをインストール
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# アプリケーションのコードをコピー
COPY app.py .
COPY static /app/static

# アプリケーションとVOICEVOX ENGINEを省エネモードで起動
CMD ["/bin/bash", "-c", "/usr/local/bin/python3 run.py --host 127.0.0.1 --num_threads 1 & gunicorn --bind 0.0.0.0:10000 --workers 1 app:app"]
