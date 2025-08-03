FROM python:3.9-slim

# 必要なシステムパッケージをインストール
RUN apt-get update && apt-get install -y \
    wget \
    unzip \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# VOICEVOXエンジンのダウンロード（複数のURLを試行）
WORKDIR /opt

# 複数のダウンロードURLを試行するスクリプト
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
# 試行するURL一覧（最新版から古い版まで）\n\
URLS=(\n\
    "https://github.com/VOICEVOX/voicevox_engine/releases/download/0.23.1/linux-cpu.zip"\n\
    "https://github.com/VOICEVOX/voicevox_engine/releases/download/0.23.0/linux-cpu.zip"\n\
    "https://github.com/VOICEVOX/voicevox_engine/releases/download/0.22.3/linux-cpu.zip"\n\
    "https://github.com/VOICEVOX/voicevox_engine/releases/download/0.22.2/linux-cpu.zip"\n\
    "https://github.com/VOICEVOX/voicevox_engine/releases/download/0.22.1/linux-cpu.zip"\n\
    "https://github.com/VOICEVOX/voicevox_engine/releases/download/0.22.0/linux-cpu.zip"\n\
)\n\
\n\
for url in "${URLS[@]}"; do\n\
    echo "🔄 Trying to download from: $url"\n\
    if wget -q --timeout=30 --tries=2 -O voicevox-engine.zip "$url" 2>/dev/null; then\n\
        echo "✅ Download successful from: $url"\n\
        if unzip -q voicevox-engine.zip; then\n\
            rm voicevox-engine.zip\n\
            if [ -d "linux-cpu" ]; then\n\
                mv linux-cpu voicevox-engine\n\
                chmod +x voicevox-engine/run\n\
                echo "✅ VOICEVOX engine setup complete"\n\
                ls -la voicevox-engine/\n\
                exit 0\n\
            fi\n\
        fi\n\
    fi\n\
    echo "❌ Failed to download/extract from: $url"\n\
    rm -f voicevox-engine.zip\n\
    rm -rf linux-cpu\n\
done\n\
\n\
echo "❌ All download attempts failed. App will run without voice support."\n\
mkdir -p voicevox-engine\n\
echo "#!/bin/bash" > voicevox-engine/run\n\
echo "echo \\"VOICEVOX engine not available\\"" >> voicevox-engine/run\n\
echo "exit 1" >> voicevox-engine/run\n\
chmod +x voicevox-engine/run\n\
exit 0\n\
' > /download_voicevox.sh && chmod +x /download_voicevox.sh

# スクリプトを実行してVOICEVOXをダウンロード
RUN /download_voicevox.sh

# アプリケーションディレクトリ
WORKDIR /app

# Python依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションファイルをコピー
COPY . .

# 堅牢な起動スクリプトを作成
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
echo "=== VOICEVOX Engine Startup ===" \n\
\n\
# VOICEVOXエンジンの存在確認\n\
if [ -f "/opt/voicevox-engine/run" ]; then\n\
    echo "📁 VOICEVOX engine binary found"\n\
    \n\
    # VOICEVOXエンジンをバックグラウンドで起動\n\
    cd /opt/voicevox-engine\n\
    echo "🚀 Starting VOICEVOX engine..."\n\
    ./run --host 0.0.0.0 --port 50021 &\n\
    VOICEVOX_PID=$!\n\
    \n\
    # VOICEVOXの起動を待つ（最大60秒）\n\
    echo "⏳ Waiting for VOICEVOX engine..."\n\
    for i in {1..30}; do\n\
        if curl -s -f http://localhost:50021/version > /dev/null 2>&1; then\n\
            echo "✅ VOICEVOX engine is ready!"\n\
            curl -s http://localhost:50021/version | head -1\n\
            break\n\
        fi\n\
        if [ $i -eq 30 ]; then\n\
            echo "⚠️  VOICEVOX engine startup timeout (60s)"\n\
            echo "🔄 Killing VOICEVOX process and continuing without voice..."\n\
            kill $VOICEVOX_PID 2>/dev/null || true\n\
        fi\n\
        echo "   Attempt $i/30..."\n\
        sleep 2\n\
    done\n\
else\n\
    echo "❌ VOICEVOX engine binary not found - running without voice"\n\
fi\n\
\n\
echo "=== Flask Application Startup ==="\n\
cd /app\n\
echo "🌶️  Starting Flask app..."\n\
exec python app.py\n\
' > /start.sh && chmod +x /start.sh

# ポートを公開
EXPOSE 5000

# 起動コマンド
CMD ["/start.sh"]
