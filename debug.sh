#!/bin/bash

echo "=== .NET バージョン確認 ==="
dotnet --version
dotnet --list-runtimes

echo ""
echo "=== プロジェクト情報 ==="
dotnet --info

echo ""
echo "=== パッケージ復元 ==="
dotnet restore --verbosity normal

echo ""
echo "=== ビルド実行 ==="
dotnet build --configuration Release --verbosity normal

echo ""
echo "=== 発行 (Publish) ==="
dotnet publish -c Release -o ./publish --verbosity normal

echo ""
echo "=== 発行されたファイル一覧 ==="
ls -la ./publish/

echo ""
echo "=== DLL 確認 ==="
file ./publish/SLAutoFriendBot.dll

echo ""
echo "=== 依存関係確認 ==="
dotnet ./publish/SLAutoFriendBot.dll --help 2>&1 || echo "DLLの実行テスト完了"