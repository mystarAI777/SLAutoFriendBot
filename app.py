# ======================================================================= #
#                           Application v5.1                              #
# ======================================================================= #

import os
import requests
import logging
import sys
import socket
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import Groq
from openai import OpenAI

# --- ログ設定 ---
# ログレベルをINFOに設定し、標準出力にログを出力します
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Secret Fileからの設定読み込み ---
# コンテナ内のSecretファイルから設定を安全に読み込み、見つからない場合は環境変数にフォールバックします

# --- DATABASE_URL ---
DATABASE_URL = None
DATABASE_URL_SECRET_FILE = '/etc/secrets/DATABASE_URL'
try:
    with open(DATABASE_URL_SECRET_FILE, 'r') as f:
        DATABASE_URL = f.read().strip()
    logger.info("Secret FileからDATABASE_URLを読み込みました。")
except FileNotFoundError:
    logger.warning(f"Secret File '{DATABASE_URL_SECRET_FILE}' が見つかりません。環境変数を試します。")
    DATABASE_URL = os.environ.get('DATABASE_URL')

# --- GROQ_API_KEY ---
GROQ_API_KEY = None
GROQ_API_KEY_SECRET_FILE = '/etc/secrets/GROQ_API_KEY'
try:
    with open(GROQ_API_KEY_SECRET_FILE, 'r') as f:
        GROQ_API_KEY = f.read().strip()
    logger.info("Secret FileからGROQ_API_KEYを読み込みました。")
except FileNotFoundError:
    logger.warning(f"Secret File '{GROQ_API_KEY_SECRET_FILE}' が見つかりません。環境変数を試します。")
    GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
except Exception as e:
    logger.error(f"APIキーの読み込み中に予期せぬエラー: {e}")

# --- VOICEVOX_URL ---
# 複数の候補から接続可能なURLを自動で探します
VOICEVOX_URLS = [
    'http://localhost:50021',        # Dockerコンテナ内で同時に動くVOICEVOX
    'http://127.0.0.1:50021',         # ローカルループバック
    'http://voicevox-engine:50021',  # Docker Compose等で使われる名前
    'http://voicevox:50021',         # 別のDocker名
]

VOICEVOX_URL_FROM_ENV = os.environ.get('VOICEVOX_URL')

def find_working_voicevox_url():
    """利用可能なVOICEVOX URLを見つけます"""
    urls_to_test = []
    if VOICEVOX_URL_FROM_ENV:
        urls_to_test.append(VOICEVOX_URL_FROM_ENV)
    urls_to_test.extend([url for url in VOICEVOX_URLS if url != VOICEVOX_URL_FROM_ENV])

    for url in urls_to_test:
        try:
            logger.info(f"🔍 VOICEVOX接続テスト中: {url}")
            response = requests.get(f"{url}/version", timeout=3)
            if response.status_code == 200:
                logger.info(f"✅ VOICEVOX接続成功: {url}, Version: {response.json()}")
                return url
        except requests.exceptions.RequestException as e:
            logger.warning(f"❌ VOICEVOX接続失敗: {url} - {e}")
            continue
    
    logger.error("❌ 利用可能なVOICEVOXエンジンが見つかりませんでした。音声機能は無効になります。")
    return None

# 起動時にVOICEVOX接続をテスト
WORKING_VOICEVOX_URL = find_working_voicevox_url()

# --- 必須変数のチェック ---
# アプリケーションの動作に不可欠な変数が設定されているか確認します
if not DATABASE_URL:
    logger.critical("FATAL: DATABASE_URLが設定されていません。アプリケーションを終了します。")
    sys.exit(1)
if not GROQ_API_KEY:
    logger.critical("FATAL: GROQ_API_KEYが設定されていません。アプリケーションを終了します。")
    sys.exit(1)

# --- Flaskアプリケーションの初期化 ---
app = Flask(__name__)
CORS(app) # CORSを有効化

# --- Groqクライアントの初期化 ---
# APIキーを使ってGroqクライアントを初期化し、接続をテストします
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    groq_client.chat.completions.create(messages=[{"role": "user", "content": "test"}], model="llama3-8b-8192", max_tokens=5)
    logger.info("✅ Groq APIクライアントの初期化とテスト接続に成功しました。")
except Exception as e:
    logger.error(f"❌ Groq APIクライアントの初期化に失敗しました: {e}")
    groq_client = None

# --- データベース設定 (SQLAlchemy) ---
Base = declarative_base()

class UserMemory(Base):
    """ユーザーの記憶を保存するデータベースモデル"""
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)
    personality_notes = Column(String(2000), default='')
    favorite_topics = Column(String(2000), default='')
    interaction_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_interaction = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

try:
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    logger.info("✅ データベース接続とテーブル作成が完了しました。")
except Exception as e:
    logger.critical(f"FATAL: データベース接続に失敗しました: {e}")
    sys.exit(1)

# --- ビジネスロジック ---

def get_or_create_user(user_uuid, user_name):
    """ユーザー情報を取得または新規作成します"""
    session = Session()
    try:
        user = session.query(UserMemory).filter(UserMemory.user_uuid == user_uuid).first()
        if user:
            user.interaction_count += 1
            user.last_interaction = datetime.utcnow()
        else:
            logger.info(f"新規ユーザーを作成します: {user_name} ({user_uuid})")
            user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
            session.add(user)
        session.commit()
        return user
    except Exception as e:
        logger.error(f"ユーザーデータの取得/作成エラー: {e}")
        session.rollback()
        return None
    finally:
        session.close()

def generate_ai_response(user_data, message=""):
    """ユーザー情報に基づいてAIの応答を生成します"""
    if not groq_client:
        return f"{user_data.user_name}さん、こんにちは！ ちょっとシステムがお休み中みたいだけど、話せて嬉しいな。"

    if user_data.interaction_count == 1:
        system_prompt = f"あなたは「もちこ」という名の、親しみやすいAIアシスタントです。{user_data.user_name}さんと初めて話します。フレンドリーなタメ口で、相手に興味を示すような、60文字以内の短い挨拶をしてください。"
    else:
        system_prompt = f"あなたは「もちこ」という名のAIアシスタントです。親友の{user_data.user_name}さんと{user_data.interaction_count}回目の会話です。過去のメモ({user_data.personality_notes})を参考に、タメ口で親しみを込めて60文字以内で応答してください。"

    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": message or "やあ！"}],
            model="llama3-8b-8192", temperature=0.8, max_tokens=150
        )
        return chat_completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return "ごめんね、今ちょっと考えがまとまらないや…。"

def generate_voice(text, speaker_id=3): # デフォルトは「ずんだもん」に設定
    """テキストから音声を生成します"""
    if not WORKING_VOICEVOX_URL:
        logger.warning("VOICEVOXが無効なため音声生成をスキップします。")
        return None
    
    try:
        # 1. audio_queryの作成
        params = {'text': text, 'speaker': speaker_id}
        res_query = requests.post(f"{WORKING_VOICEVOX_URL}/audio_query", params=params, timeout=10)
        res_query.raise_for_status()
        
        # 2. 音声合成
        res_synth = requests.post(f"{WORKING_VOICEVOX_URL}/synthesis", params={'speaker': speaker_id}, json=res_query.json(), timeout=15)
        res_synth.raise_for_status()

        logger.info(f"✅ 音声合成成功: '{text[:20]}...'")
        return res_synth.content
    except Exception as e:
        logger.error(f"❌ VOICEVOX音声合成エラー: {e}")
        return None

# --- APIエンドポイント ---

@app.route('/chat', methods=['POST'])
def chat():
    """メインのチャットエンドポイント"""
    try:
        data = request.json
        user_uuid = data.get('user_uuid')
        user_name = data.get('user_name')
        if not user_uuid or not user_name:
            return jsonify(error='user_uuid and user_name are required'), 400

        user_data = get_or_create_user(user_uuid, user_name)
        if not user_data:
            return jsonify(error='Failed to process user data'), 500
            
        ai_response = generate_ai_response(user_data, data.get('message', ''))
        voice_data = generate_voice(ai_response)
        
        response_data = {'text': ai_response, 'interaction_count': user_data.interaction_count}
        
        if voice_data:
            voice_filename = f"voice_{user_uuid}_{int(datetime.now().timestamp())}.wav"
            voice_path = os.path.join('/tmp', voice_filename)
            with open(voice_path, 'wb') as f:
                f.write(voice_data)
            response_data['voice_url'] = f'/voice/{voice_filename}'
        
        return jsonify(response_data)
    except Exception as e:
        logger.error(f"チャットエンドポイントエラー: {e}")
        return jsonify(error='Internal server error'), 500

@app.route('/voice/<filename>')
def serve_voice(filename):
    """生成された音声ファイルを提供します"""
    return send_from_directory('/tmp', filename)

@app.route('/health')
def health_check():
    """サービスの稼働状態を確認するヘルスチェックエンドポイント"""
    return jsonify({
        'status': 'healthy', 
        'timestamp': datetime.utcnow().isoformat(),
        'voicevox_status': 'available' if WORKING_VOICEVOX_URL else 'unavailable',
        'voicevox_url': WORKING_VOICEVOX_URL
    })

# --- アプリケーションの実行 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"🚀 Flaskアプリケーションをポート {port} で起動します...")
    app.run(host='0.0.0.0', port=port)
