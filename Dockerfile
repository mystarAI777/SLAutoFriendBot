FROM python:3.9-slim

# 必要なシステムパッケージをインストール
RUN apt-get update && apt-get install -y \
    wget \
    unzip \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# VOICEVOXエンジン（Linux CPU版）をダウンロード - 正しいURL使用
WORKDIR /opt
RUN wget -O voicevox-engine.zip https://github.com/VOICEVOX/voicevox_engine/releases/download/0.23.1/linux-cpu.zip \
    && unzip voicevox-engine.zip \
    && rm voicevox-engine.zip \
    && mv linux-cpu voicevox-engine \
    && chmod +x voicevox-engine/run

# アプリケーションディレクトリ
WORKDIR /app

# Python依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションファイルをコピー
COPY . .

# 起動スクリプトを作成（記事のアプローチを参考）
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
echo "=== Starting VOICEVOX Engine ==="\n\
# VOICEVOXエンジンをバックグラウンドで起動\n\
cd /opt/voicevox-engine\n\
./run --host 0.0.0.0 --port 50021 &\n\
VOICEVOX_PID=$!\n\
\n\
# VOICEVOXの起動を待つ（記事と同様のアプローチ）\n\
echo "Waiting for VOICEVOX engine to be ready..."\n\
for i in {1..30}; do\n\
    if curl -s http://localhost:50021/version > /dev/null 2>&1; then\n\
        echo "✅ VOICEVOX engine is ready!"\n\
        curl -s http://localhost:50021/version | head -1\n\
        break\n\
    fi\n\
    echo "⏳ Waiting... ($i/30)"\n\
    sleep 2\n\
done\n\
\n\
# 簡単な動作テスト（記事のAPIパターンに従う）\n\
echo "=== Testing VOICEVOX API ==="\n\
if curl -s "http://localhost:50021/audio_query?text=テスト&speaker=3" > /dev/null 2>&1; then\n\
    echo "✅ VOICEVOX API test successful"\n\
else\n\
    echo "⚠️  VOICEVOX API test failed, but continuing..."\n\
fi\n\
\n\
echo "=== Starting Flask Application ==="\n\
cd /app\n\
exec python app.py\n\
' > /start.sh && chmod +x /start.sh

# ポートを公開
EXPOSE 5000

# 起動コマンド
CMD ["/start.sh"]
