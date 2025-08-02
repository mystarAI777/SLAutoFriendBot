# ベースイメージ
FROM ubuntu:22.04

# 環境変数の設定
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# システムの更新と、動的ダウンロードに必要なツールのインストール
RUN apt-get update && apt-get install -y \
    python3.10 \
    python3-pip \
    wget \
    curl \
    jq \
    p7zip-full \
    && rm -rf /var/lib/apt/lists/*

# Pythonのエイリアスを設定
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.10 1 \
    && update-alternatives --install /usr/bin/pip pip /usr/bin/pip3 1

# 作業ディレクトリの設定
WORKDIR /app

# --- ▼▼▼ ここからが修正箇所 ▼▼▼ ---

# GitHub APIを使ってVOICEVOXエンジンの最新版URLを動的に取得し、ダウンロード・解凍する
# /releases/latest の代わりに /releases を使い、結果の配列の先頭([0])を取得することでレートリミットを回避する
RUN LATEST_URL=$(curl -sL https://api.github.com/repos/VOICEVOX/voicevox_engine/releases | jq -r '.[0].assets[] | select(.name | contains("linux-cpu")) | .browser_download_url') \
    && echo "Downloading VOICEVOX from: $LATEST_URL" \
    && wget -O voicevox_engine.7z "$LATEST_URL" \
    && 7z x voicevox_engine.7z \
    && mv linux-cpu /opt/voicevox_engine \
    && rm voicevox_engine.7z

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
