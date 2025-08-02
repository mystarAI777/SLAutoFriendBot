# Python 3.11の公式イメージをベースとして使用
FROM python:3.11-slim

# 作業ディレクトリを設定
WORKDIR /app

# システムパッケージの更新と必要なツールをインストール
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# VOICEVOXエンジンのダウンロードとセットアップ
RUN wget -O voicevox.zip https://github.com/VOICEVOX/voicevox_engine/releases/download/0.14.4/voicevox_engine-linux-cpu-0.14.4.zip \
    && unzip voicevox.zip \
    && mv voicevox_engine /opt/voicevox_engine \
    && rm voicevox.zip

# VOICEVOXの実行権限を設定
RUN chmod +x /opt/voicevox_engine/run

# Pythonの依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションファイルをコピー
COPY app.py .

# 静的ファイル用のディレクトリを作成
RUN mkdir -p static

# ポートを公開（Renderが自動的に設定するPORTを使用）
EXPOSE $PORT

# 起動スクリプトを作成
RUN echo '#!/bin/bash\n\
# VOICEVOXエンジンをバックグラウンドで起動\n\
echo "VOICEVOXエンジンを起動中..."\n\
cd /opt/voicevox_engine\n\
./run --host 0.0.0.0 --port 50021 --cpu_num_threads 2 &\n\
\n\
# VOICEVOXの起動を待機\n\
echo "VOICEVOXの起動を待機中..."\n\
for i in {1..30}; do\n\
    if curl -s http://localhost:50021/version > /dev/null; then\n\
        echo "VOICEVOX起動完了"\n\
        break\n\
    fi\n\
    echo "待機中... ($i/30)"\n\
    sleep 2\n\
done\n\
\n\
# Flaskアプリケーションを起動\n\
echo "Flaskアプリケーションを起動中..."\n\
cd /app\n\
exec gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 120 app:app' > /app/start.sh

# 起動スクリプトに実行権限を付与
RUN chmod +x /app/start.sh

# 環境変数の設定
ENV VOICEVOX_URL=http://localhost:50021

# アプリケーションの起動
CMD ["/app/start.sh"]
