# ステージ1: VOICEVOXエンジンを含める（オプション）
FROM voicevox/voicevox_engine:cpu-latest as voicevox

# ステージ2: 最終的なアプリケーションイメージ
FROM python:3.9-slim

# 必要なシステムパッケージをインストール
RUN apt-get update && apt-get install -y \
    curl \
    procps \
    gcc \
    libmariadb-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# VOICEVOXエンジンを最初のステージからコピー（オプション）
COPY --from=voicevox /opt/voicevox_engine /opt/voicevox_engine

# アプリケーションディレクトリ
WORKDIR /app

# Python依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get update && \
    apt-get purge -y --auto-remove gcc libmariadb-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*

# アプリケーションファイルをコピー
COPY . .

# 音声ファイル用ディレクトリを作成
RUN mkdir -p /tmp/voices && chmod 755 /tmp/voices

# 環境変数を設定
ENV PYTHONUNBUFFERED=1
ENV PORT=5000

# 改良された起動スクリプトを作成
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
echo "=== Mochiko AI Assistant Startup ==="\n\
\n\
# 環境変数の確認\n\
echo "🔧 Environment check:"\n\
echo "   DATABASE_URL: ${DATABASE_URL:0:30}..." || echo "   DATABASE_URL: not set"\n\
echo "   GROQ_API_KEY: ${GROQ_API_KEY:0:8}..." || echo "   GROQ_API_KEY: not set"\n\
echo "   PORT: ${PORT:-5000}"\n\
\n\
# VOICEVOX実行ファイルの確認と起動（オプション）\n\
if [ -f "/opt/voicevox_engine/run" ]; then\n\
    VOICEVOX_CMD="/opt/voicevox_engine/run"\n\
elif [ -f "/opt/voicevox_engine/voicevox_engine" ]; then\n\
    VOICEVOX_CMD="/opt/voicevox_engine/voicevox_engine"\n\
else\n\
    echo "⚠️  VOICEVOX実行ファイルが見つかりません（音声機能は無効化されます）"\n\
    VOICEVOX_CMD=""\n\
fi\n\
\n\
if [ -n "$VOICEVOX_CMD" ]; then\n\
    echo "🚀 Starting VOICEVOX engine: $VOICEVOX_CMD"\n\
    echo "📡 Port: 50021, Host: 0.0.0.0"\n\
    \n\
    # VOICEVOXエンジンをバックグラウンドで起動\n\
    $VOICEVOX_CMD --host 0.0.0.0 --port 50021 &\n\
    VOICEVOX_PID=$!\n\
    echo "🔧 VOICEVOX PID: $VOICEVOX_PID"\n\
    \n\
    # 起動待機処理（タイムアウト短縮）\n\
    echo "⏳ Waiting for VOICEVOX engine startup..."\n\
    for i in {1..30}; do\n\
        if curl -s -f http://localhost:50021/version > /dev/null 2>&1; then\n\
            echo "✅ VOICEVOX engine is ready! (attempt $i)"\n\
            break\n\
        fi\n\
        \n\
        if [ $i -eq 30 ]; then\n\
            echo "⚠️  VOICEVOX engine startup timeout after 60 seconds"\n\
            echo "🔄 Proceeding without voice synthesis..."\n\
            kill $VOICEVOX_PID 2>/dev/null || true\n\
        fi\n\
        \n\
        echo "   Attempt $i/30 - checking VOICEVOX..."\n\
        sleep 2\n\
    done\n\
else\n\
    echo "⚠️  VOICEVOX engine not found - proceeding without voice synthesis"\n\
fi\n\
\n\
echo "=== Starting Flask Application ==="\n\
cd /app\n\
echo "🌶️  Starting Flask app on 0.0.0.0:${PORT:-5000}..."\n\
echo "📁 Voice directory: /tmp/voices"\n\
ls -la /tmp/voices || echo "Voice directory not accessible"\n\
\n\
# アプリケーション起動\n\
exec python app.py\n\
' > /start.sh && chmod +x /start.sh

# ポートを公開
EXPOSE 5000 50021

# ヘルスチェック追加
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT:-5000}/health || exit 1

# 起動コマンド
CMD ["/start.sh"]
