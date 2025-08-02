# .NET 8.0 SDK を使用してビルド
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src

# プロジェクトファイルをコピーして依存関係を復元
COPY *.csproj ./
RUN dotnet restore

# ソースコードをコピーしてビルド
COPY . ./
RUN dotnet publish -c Release -o /app/publish --no-restore

# .NET 8.0 ランタイムを使用して実行
FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS runtime
WORKDIR /app

# 必要なパッケージをインストール (curl等)
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ビルド成果物をコピー
COPY --from=build /app/publish .

# 静的ファイル用ディレクトリを作成
RUN mkdir -p /app/static/audio

# ポート5000を公開
EXPOSE 5000

# 環境変数を設定
ENV ASPNETCORE_URLS=http://+:5000
ENV ASPNETCORE_ENVIRONMENT=Production

# アプリケーションを起動
ENTRYPOINT ["dotnet", "SLAutoFriendBot.dll"]
