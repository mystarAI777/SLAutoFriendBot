# Python公式の軽量イメージをベースにする
FROM python:3.10-slim

# 常に最新のpipを利用するようにアップグレードする
RUN pip install --no-cache-dir --upgrade pip

# 環境変数設定
# - PYTHONUNBUFFERED: Pythonの出力がすぐにログに表示されるようにする
# - PIP_NO_CACHE_DIR: キャッシュを使わずイメージサイズを削減
# - PIP_DISABLE_PIP_VERSION_CHECK: pipのバージョンチェックを無効化しビルドを高速化
# - PIP_DEFAULT_TIMEOUT: タイムアウト時間を延長
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=on \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100

# ★ここからがユーザー作成のパート★
# 1. アプリケーション用のディレクトリを作成
WORKDIR /app

# 2. requirements.txtをコピーしてライブラリをインストール
#    この時点ではまだrootユーザーでOK
COPY requirements.txt .
RUN pip install -r requirements.txt

# 3. 'appuser'という名前で一般ユーザーを作成
RUN useradd --create-home --shell /bin/bash appuser

# 4. アプリケーションのコードをコピー
COPY . .

# 5. ファイルの所有者を新しいユーザーに変更
RUN chown -R appuser:appuser /app

# 6. これ以降のコマンドを実行するユーザーを'appuser'に切り替え
USER appuser
# ★ここまで★

# アプリケーションの実行コマンド（例）
CMD ["python", "app.py"]
