# ======================================================================= #
#                           Application v5.4 (Timeout Fix)                  #
# ======================================================================= #

import os
import requests
import logging
import sys
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import Groq
from openai import OpenAI

# (ログ設定、Secret読み込み、VOICEVOX接続テストの部分は変更なし)
# ... (前と同じコード) ...
# --- ログ設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Secret Fileからの設定読み込み ---
def get_secret(name):
    secret_file = f'/etc/secrets/{name}'
    try:
        with open(secret_file, 'r') as f:
            value = f.read().strip()
            logger.info(f"Secret Fileから {name} を読み込みました。")
            return value
    except FileNotFoundError:
        logger.warning(f"Secret File '{secret_file}' が見つかりません。環境変数 '{name}' を試します。")
        return os.environ.get(name)
    except Exception as e:
        logger.error(f"{name} の読み込み中に予期せぬエラー: {e}")
        return None

DATABASE_URL = get_secret('DATABASE_URL')
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- VOICEVOX接続テスト ---
VOICEVOX_URLS = ['http://localhost:50021', 'http://127.0.0.1:50021']
def find_working_voicevox_url():
    urls_to_test = []
    if VOICEVOX_URL_FROM_ENV: urls_to_test.insert(0, VOICEVOX_URL_FROM_ENV)
    urls_to_test.extend([url for url in VOICEVOX_URLS if url not in urls_to_test])
    for url in urls_to_test:
        try:
            logger.info(f"🔍 VOICEVOX接続テスト中: {url}")
            response = requests.get(f"{url}/version", timeout=3)
            response.raise_for_status()
            logger.info(f"✅ VOICEVOX接続成功: {url}, Version: {response.json()}")
            return url
        except requests.exceptions.RequestException:
            logger.warning(f"❌ VOICEVOX接続失敗: {url}")
            continue
    logger.error("❌ 利用可能なVOICEVOXエンジンが見つかりませんでした。")
    return None
WORKING_VOICEVOX_URL = find_working_voicevox_url()

# --- 必須変数のチェック ---
if not DATABASE_URL or not GROQ_API_KEY:
    logger.critical("FATAL: DATABASE_URLまたはGROQ_API_KEYが設定されていません。終了します。")
    sys.exit(1)

# --- Flaskアプリケーションの初期化 ---
app = Flask(__name__)
CORS(app)

# --- サービス初期化 ---
groq_client = None
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    groq_client.chat.completions.create(messages=[{"role": "user", "content": "test"}], model="llama3-8b-8192", max_tokens=5)
    logger.info("✅ Groq APIクライアントの初期化に成功しました。")
except Exception as e:
    logger.error(f"❌ Groq APIクライアントの初期化に失敗: {e}")

engine = None
Session = None
Base = declarative_base()
class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)
    interaction_count = Column(Integer, default=0)

try:
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    logger.info("✅ データベース接続とテーブル作成が完了しました。")
except Exception as e:
    logger.critical(f"FATAL: データベース接続に失敗: {e}")
    sys.exit(1)

class UserDataContainer:
    def __init__(self, user_uuid, user_name, interaction_count):
        self.user_uuid = user_uuid
        self.user_name = user_name
        self.interaction_count = interaction_count

def get_or_create_user(user_uuid, user_name):
    session = Session()
    try:
        user_memory = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        if user_memory: user_memory.interaction_count += 1
        else:
            logger.info(f"新規ユーザーを作成: {user_name} ({user_uuid})")
            user_memory = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
            session.add(user_memory)
        session.commit()
        return UserDataContainer(
            user_uuid=user_memory.user_uuid,
            user_name=user_memory.user_name,
            interaction_count=user_memory.interaction_count
        )
    except Exception as e:
        logger.error(f"ユーザーデータ処理エラー: {e}")
        session.rollback()
        return None
    finally:
        session.close()

# --- ビジネスロジック ---
def generate_ai_response(user_data, message):
    if not groq_client: return f"{user_data.user_name}さん、こんにちは！"
    system_prompt = f"あなたは「もちこ」というAIです。親友の{user_data.user_name}さんと丁寧な言葉でで話します。日本語でフレンドリーな返事を60文字程度で生成してください。語尾は「ですわ」「ますわ」、一人称は「あてぃし」"
    try:
        completion = groq_client.chat.completions.create(messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": message or "元気？"}], model="llama3-8b-8192")
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return "ごめん、ちょっと考え事してた！"

# ▼▼▼【ここが修正点】▼▼▼
def generate_voice(text, speaker_id=3):
    """テキストから音声を生成します。タイムアウト値を延長。"""
    if not WORKING_VOICEVOX_URL:
        return None
    try:
        # audio_queryのタイムアウトを15秒に延長
        res_query = requests.post(
            f"{WORKING_VOICEVOX_URL}/audio_query",
            params={'text': text, 'speaker': speaker_id},
            timeout=15
        )
        res_query.raise_for_status()
        
        # synthesisのタイムアウトも15秒に延長
        res_synth = requests.post(
            f"{WORKING_VOICEVOX_URL}/synthesis",
            params={'speaker': speaker_id},
            json=res_query.json(),
            timeout=15
        )
        res_synth.raise_for_status()
        
        logger.info(f"✅ 音声合成成功: '{text[:20]}...'")
        return res_synth.content
        
    except requests.exceptions.Timeout:
        logger.error(f"⏰ VOICEVOX音声合成がタイムアウトしました（{15}秒）。")
        return None
    except Exception as e:
        logger.error(f"❌ VOICEVOX音声合成で予期せぬエラー: {e}")
        return None
# ▲▲▲【修正はここまで】▲▲▲


# --- APIエンドポイント ---
@app.route('/')
def index():
    return "<h1>AI Chat API</h1><p>Service is running.</p>"

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    logger.info("✅ /chat_lsl エンドポイントへのリクエストを受信しました。")
    try:
        data = request.json or {}
        user_uuid, user_name, message = data.get('uuid'), data.get('name'), data.get('message', '')
        if not (user_uuid and user_name): return "Error: uuid and name are required", 400
        user_data = get_or_create_user(user_uuid, user_name)
        if not user_data: return "Error: Failed to process user data", 500
        ai_response = generate_ai_response(user_data, message)
        voice_data = generate_voice(ai_response)
        audio_url_part = ""
        if voice_data:
            filename = f"voice_{user_uuid}_{int(datetime.now().timestamp())}.wav"
            with open(os.path.join('/tmp', filename), 'wb') as f: f.write(voice_data)
            audio_url_part = f'/voice/{filename}'
            logger.info(f"音声ファイル生成成功: {audio_url_part}")
        response_text = f"{ai_response}|{audio_url_part}"
        return app.response_class(response=response_text, status=200, mimetype='text/plain; charset=utf-8')
    except Exception as e:
        logger.error(f"LSLチャットエンドポイントで予期せぬエラー: {e}")
        return "Error: Internal server error", 500

@app.route('/voice/<filename>')
def serve_voice(filename):
    return send_from_directory('/tmp', filename)

@app.route('/health')
def health_check():
    return jsonify({'status': 'healthy', 'voicevox': 'available' if WORKING_VOICEVOX_URL else 'unavailable'})

# --- アプリケーションの実行 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"🚀 Flaskアプリケーションをポート {port} で起動します...")
    app.run(host='0.0.0.0', port=port)
