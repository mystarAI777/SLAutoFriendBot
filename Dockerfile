# 起動スクリプトを作成
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
echo "=== Starting VOICEVOX Engine (Official Docker Image) ==="\n\
\n\
# VOICEVOXエンジンをバックグラウンドで起動\n\
# 公式イメージのENTRYPOINTやCMDに合わせた起動方法\n\
echo "🚀 Starting VOICEVOX engine on 0.0.0.0:50021..."\n\
/opt/voicevox_engine/run --host 0.0.0.0 --port 50021 &\n\
VOICEVOX_PID=$!\n\
\n\
# 起動を待つ（ヘルスチェック）\n\
echo "⏳ Waiting for VOICEVOX engine..."\n\
for i in {1..30}; do\n\
    if curl -s -f http://localhost:50021/version > /dev/null 2>&1; then\n\
        echo "✅ VOICEVOX engine is ready!"\n\
        echo "📋 Version info:"\n\
        curl -s http://localhost:50021/version | head -3\n\
        echo ""\n\
        break\n\
    fi\n\
    if [ $i -eq 30 ]; then\n\
        echo "⚠️  VOICEVOX engine startup timeout after 60 seconds"\n\
        echo "🔄 Proceeding without voice synthesis..."\n\
        kill $VOICEVOX_PID 2>/dev/null || true\n\
    fi\n\
    echo "   Attempt $i/30 - checking http://localhost:50021/version"\n\
    sleep 2\n\
done\n\
\n\
# API利用可能性の確認\n\
if curl -s -f http://localhost:50021/speakers > /dev/null 2>&1; then\n\
    echo "✅ VOICEVOX API is responding - checking available speakers..."\n\
    speaker_count=$(curl -s http://localhost:50021/speakers | grep -o \"name\" | wc -l)\n\
    echo "📢 Available speakers: $speaker_count"\n\
else\n\
    echo "⚠️  VOICEVOX API not responding, voice synthesis will be disabled"\n\
fi\n\
\n\
echo "=== Starting Flask Application ==="\n\
cd /app\n\
echo "🌶️  Starting Flask app..."\n\
exec python app.py\n\
' > /start.sh && chmod +x /start.sh
