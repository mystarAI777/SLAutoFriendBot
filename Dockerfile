# マルチステージビルド: .NET アプリケーション
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src

# プロジェクトファイルをコピーして依存関係を復元
COPY *.csproj ./
RUN dotnet restore

# ソースコードをコピーしてビルド
COPY . ./
RUN dotnet publish -c Release -o /app/publish --no-restore

# 実行環境: VoiceVoxエンジンと.NETアプリケーション
FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS runtime
WORKDIR /app

# 必要なパッケージをインストール
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# VoiceVoxエンジンをダウンロードして解凍
RUN wget -O voicevox_engine.tar.gz https://github.com/VOICEVOX/voicevox_engine/releases/download/0.21.1/voicevox_engine-linux-cpu-0.21.1.tar.gz \
    && tar -xzf voicevox_engine.tar.gz \
    && mv voicevox_engine /opt/voicevox_engine \
    && rm voicevox_engine.tar.gz

# VoiceVoxエンジンの実行権限を設定
RUN chmod +x /opt/voicevox_engine/run

# .NETアプリケーションをコピー
COPY --from=build /app/publish .

# 静的ファイル用ディレクトリを作成
RUN mkdir -p /app/static/audio

# 起動スクリプトを作成
RUN echo '#!/bin/bash\n\
echo "VoiceVoxエンジンを起動中..."\n\
cd /opt/voicevox_engine\n\
./run --host 0.0.0.0 --port 50021 &\n\
VOICEVOX_PID=$!\n\
echo "VoiceVoxエンジンのPID: $VOICEVOX_PID"\n\
\n\
# VoiceVoxエンジンの起動を待機\n\
echo "VoiceVoxエンジンの起動を待機中..."\n\
for i in {1..30}; do\n\
    if curl -s http://localhost:50021/version > /dev/null; then\n\
        echo "VoiceVoxエンジンが起動しました"\n\
        break\n\
    fi\n\
    echo "待機中... ($i/30)"\n\
    sleep 2\n\
done\n\
\n\
echo ".NETアプリケーションを起動中..."\n\
cd /app\n\
dotnet SLAutoFriendBot.dll' > /app/start.sh && chmod +x /app/start.sh

# ポートを公開
EXPOSE 5000 50021

# 環境変数を設定
ENV ASPNETCORE_URLS=http://+:5000
ENV ASPNETCORE_ENVIRONMENT=Production
ENV VOICEVOX_URL=http://localhost:50021

# 起動スクリプトを実行
ENTRYPOINT ["/app/start.sh"]
