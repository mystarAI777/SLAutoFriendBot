import os
import requests
import logging
import sys
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from sqlalchemy import create_engine, Column, String, DateTime, Integer, text
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import Groq # <-- Geminiの代わりにGroqをインポート

# ログ設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 環境変数の読み込み ---
DATABASE_URL = None
DATABASE_URL_SECRET_FILE = '/etc/secrets/DATABASE_URL'
try:
    with open(DATABASE_URL_SECRET_FILE, 'r') as f:
        DATABASE_URL = f.read().strip()
    logger.info("Secret FileからDATABASE_URLを読み込みました。")
except FileNotFoundError:
    DATABASE_URL = os.environ.get('DATABASE_URL')

# --- ▼▼▼ 【最重要修正箇所】GroqのAPIキーを読み込む ▼▼▼ ---
raw_groq_key = os.environ.get('GROQ_API_KEY')
if raw_groq_key:
    GROQ_API_KEY = raw_groq_key.strip()
else:
    GROQ_API_KEY = None
# --- ▲▲▲ 修正はここまで ▲▲▲ ---

VOICEVOX_URL = os.environ.get('VOICEVOX_URL', 'http://localhost:50021')

# --- 必須変数のチェック ---
if not DATABASE_URL:
    logger.error("DATABASE_URLが設定されていません。")
    sys.exit(1)
if not GROQ_API_KEY:
    logger.error("GROQ_API_KEYが設定されていません。")
    sys.exit(1)

app = Flask(__name__)

# --- ▼▼▼ Groqクライアントの初期設定 ▼▼▼ ---
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    logger.info("Groq AIクライアントの初期設定が完了しました。")
except Exception as e:
    logger.error(f"Groq AIの初期設定中にエラーが発生しました: {e}")
    sys.exit(1)
# --- ▲▲▲ ---

Base = declarative_base()

class UserMemory(Base):
    __tablename__ = 'user_memories'
    # ... (変更なし) ...

engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

class UserDataContainer:
    # ... (変更なし) ...

def get_or_create_user(user_uuid, user_name):
    # ... (変更なし) ...

# --- ▼▼▼ 【最重要修正箇所】AI応答生成をGroqで行う ▼▼▼ ---
def generate_ai_response(user_data, message=""):
    """Groq AI (Llama 3)を使って応答を生成"""
    
    # AIに渡す「役割設定」と「過去の状況」
    system_prompt = ""
    if user_data.interaction_count == 1:
        system_prompt = f"""
あなたは「もちこ」という名前の、優しくて親しみやすいAIアシスタントです。今、{user_data.user_name}さんという方に初めてお会いしました。
以下のキャラクター設定を厳守して、60文字以内の自然で親しみやすい初対面の挨拶をしてください。
- 敬語は使わず、親しみやすい「タメ口」で話します。
- 少し恥ずかしがり屋ですが、フレンドリーです。
- 相手に興味津々です。
"""
    else:
        days_since_last = (datetime.utcnow() - user_data.last_interaction).days
        situation = "継続的な会話"
        if days_since_last > 7: situation = "久しぶりの再会"
        elif days_since_last > 1: situation = "数日ぶりの再会"
        
        system_prompt = f"""
あなたは「もちこ」という名前の、優しくて親しみやすいAIアシスタントです。{user_data.user_name}さんとは{user_data.interaction_count}回目のお話です。
状況: {situation}。
過去のメモ: {user_data.personality_notes}
好きな話題: {user_data.favorite_topics}
以下のキャラクター設定を厳守し、この状況にふさわしい返事を60文字以内で作成してください。
- 敬語は使わず、親しみやすい「タメ口」で話します。
- 過去の会話を覚えている、親しい友人として振る舞います。
"""
    
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": message,
                }
            ],
            model="llama3-8b-8192", # Llama 3の8Bモデルを使用
            temperature=0.7,
            max_tokens=150,
        )
        response_text = chat_completion.choices[0].message.content
        return response_text.strip()

    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return f"ごめんなさい、{user_data.user_name}さん。ちょっと考えがまとまらないや…。"
# --- ▲▲▲ 修正はここまで ▲▲▲ ---

def generate_voice(text, speaker_id=1):
    # ... (変更なし) ...

@app.route('/chat', methods=['POST'])
def chat():
    # ... (変更なし) ...

# ... (以降のコードもすべて変更なし) ...
