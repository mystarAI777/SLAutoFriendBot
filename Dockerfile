# Python公式の軽量イメージをベースにする
# バージョンはご自身のプロジェクトに合わせてください（例: 3.10, 3.11など）
FROM python:3.10-slim

# 作業ディレクトリを設定
WORKDIR /app

# requirements.txtを先にコピーしてライブラリをインストールする
# （これにより、コードを変更しただけの再ビルドが高速になる）
COPY requirements.txt .
# Python公式イメージでは `pip3` ではなく `pip` を使うのが一般的
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションのコードをすべてコピー
COPY . .

# アプリケーションの実行コマンド（例）
CMD ["python", "app.py"]
