# ベースイメージ
FROM ubuntu:22.04

# 環境変数の設定
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# システムの更新と必要なツールのインストール
RUN apt-get update && apt-get install -y \
    python3.10 \
    python3-pip \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Pythonのエイリアスを設定 (pythonコマンドがpython3.10を指すように)
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.10 1 \
    && update-alternatives --install /usr/bin/pip pip /usr/bin/pip3 1

# 作業ディレクトリの設定
WORKDIR /app

# VOICEVOXエンジンのダウンロードとセットアップ
# 安定版の最新バージョン (例: 0.14.11) を使用。
# VOICEVOX GitHub Releasesページで最新の 'voicevox_engine-linux-cpu-*.zip' を確認してください。
RUN wget -O voicevox.zip https://github.com/VOICEVOX/voicevox_engine/releases/download/0.14.11/voicevox_engine-linux-cpu-0.14.11.zip \
    && unzip voicevox.zip \
    && mv voicevox_engine /opt/voicevox_engine \
    && rm voicevox.zip

# VOICEVOX実行に必要なシステムライブラリのインストール
# VOICEVOX公式のDockerイメージやドキュメントを参考にしています。
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
# バックグラウンドで実行し、ログをリダイレクト
RUN echo '#!/bin/bash\ncd /opt/voicevox_engine && ./run --host 0.0.0.0 --port 50021 > /var/log/voicevox_engine.log 2>&1' > /usr/local/bin/run_voicevox \
    && chmod +x /usr/local/bin/run_voicevox

# Pythonの依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードのコピー
COPY . .

# staticディレクトリが存在することを確認 (音声ファイル保存用)
RUN mkdir -p static

# ポートの公開 (Flaskアプリ: 5000, VOICEVOXエンジン: 50021)
EXPOSE 5000
EXPOSE 50021

# アプリケーションの起動コマンド
# VOICEVOXエンジンをバックグラウンドで起動し、その後Flaskアプリを起動
CMD ["bash", "-c", "/usr/local/bin/run_voicevox & exec python app.py"]
