# ステージ1: .NET SDKを使ってアプリケーションをビルド
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY *.csproj .
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app/publish

# ステージ2: VOICEVOX ENGINEイメージをベースに、ビルドしたアプリを配置
FROM voicevox/voicevox_engine:latest
WORKDIR /app
COPY --from=build /app/publish .

# ★★★【省エネ起動】★★★
# CMD命令を、省エネオプション付きで、より安定したものに変更
CMD ["/bin/bash", "-c", "/usr/local/bin/python3 run.py --host 127.0.0.1 --num_threads 1 & ./SLAutoFriendBot --urls http://0.0.0.0:8080"]
