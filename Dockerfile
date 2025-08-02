FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY *.csproj .
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app/publish

FROM voicevox/voicevox_engine:latest
WORKDIR /app
COPY --from=build /app/publish .
CMD ["/bin/bash", "-c", "./SLAutoFriendBot --urls http://0.0.0.0:8080 & /usr/local/bin/python3 run.py --host 127.0.0.1"]
