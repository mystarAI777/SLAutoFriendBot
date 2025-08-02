# ベースイメージ
FROM ubuntu:22.04

# 環境変数の設定
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# システムの更新と必要なツールのインストール
# 7zipを解凍するためにp7zip-fullを追加
RUN apt-get update && apt-get install -y \
    python3.10 \
    python3-pip \
    wget \
    p7zip-full \
    && rm -rf /var/lib/apt/lists/*

# Pythonのエイリアスを設定
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.10 1 \
    && update-alternatives --install /usr/bin/pip pip /usr/bin/pip3 1

# 作業ディレクトリの設定
WORKDIR /app

# --- ▼▼▼ ここからが修正箇所 ▼▼▼ ---

# VOICEVOXエンジンのダウンロードとセットアップ
# APIレートリミットを避けるため、最新の安定版(0.19.1)のURLを直接指定する
RUN wget https://github.com/VOICEVOX/voicevox_engine/releases/download/0.19.1/voicevox_engine-linux-cpu-0.19.1.7z.001 -O voicevox.7z.001 \
    && 7z x voicevox.7z.001 \
    && mv linux-cpu /opt/voicevox_engine \
    && rm voicevox.7z.001

# --- ▲▲▲ ここまでが修正箇所 ▲▲▲ ---

# VOICEVOX実行に必要なシステムライブラリのインストール
RUN apt-get update && apt-get install -y \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libasound2 \
    libglib2.0-0 \
    libssl-dev \
    libffi-dev \
    libpq-dev \
    build-essential \
    pkg-config \
    libusb-1.0-0-dev \
    && rm -rf /var/lib/apt/lists/*

# VOICEVOXエンジンの実行スクリプトを作成
RUN echo '#!/bin/bash\ncd /opt/voicevox_engine && ./run --host 0.0.0.0 --port 50021 > /var/log/voicevox_engine.log 2>&1' > /usr/local/bin/run_voicevox \
    && chmod +x /usr/local/bin/run_voicevox

# Pythonの依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードのコピー
COPY . .

# staticディレクトリが存在することを確認
RUN mkdir -p static

# ポートの公開
EXPOSE 5000
EXPOSE 50021

# アプリケーションの起動コマンド
CMD ["bash", "-c", "/usr/local/bin/run_voicevox & exec python app.py"]
