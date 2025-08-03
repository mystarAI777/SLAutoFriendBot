import os
import requests
import logging
import sys
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, safe_join
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool
from groq import Groq
from openai import OpenAI

# ログ設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ▼▼▼ Secret Fileからの設定読み込み ▼▼▼ ---

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
    logger.error(f"Secret Fileが見つかりません: {GROQ_API_KEY_SECRET_FILE}")
except Exception as e:
    logger.error(f"APIキーの読み込み中に予期せぬエラー: {e}")

# --- 【修正箇所】VOICEVOX_URL ---
VOICEVOX_URL = 'http://localhost:50021' # デフォルト値を設定
VOICEVOX_URL_SECRET_FILE = '/etc/secrets/VOICEVOX_URL'
try:
    with open(VOICEVOX_URL_SECRET_FILE, 'r') as f:
        VOICEVOX_URL = f.read().strip()
    logger.info("Secret FileからVOICEVOX_URLを読み込みました。")
except FileNotFoundError:
    logger.warning(f"Secret File '{VOICEVOX_URL_SECRET_FILE}' が見つかりません。環境変数またはデフォルト値を使用します。")
    VOICEVOX_URL = os.environ.get('VOICEVOX_URL', 'http://localhost:50021')
except Exception as e:
    logger.error(f"VOICEVOX_URLの読み込み中に予期せぬエラー: {e}")

# --- ▲▲▲ 設定読み込みはここまで ▲▲▲ ---


# --- 必須変数のチェック ---
if not DATABASE_URL:
    logger.error("DATABASE_URLが設定されていません。")
    sys.exit(1)
if not GROQ_API_KEY:
    logger.error("GROQ_API_KEYが設定されていません。")
    sys.exit(1)

app = Flask(__name__)
CORS(app, origins=["*"], methods=["GET", "POST", "OPTIONS"],
     allow_headers=["Content-Type", "Authorization"])

# --- Groqクライアントの初期設定 ---
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    use_openai_compatible = False
    logger.info("Groq native クライアントの初期設定が完了しました。")
    test_response = groq_client.chat.completions.create(
        messages=[{"role": "user", "content": "test"}], model="llama3-8b-8192", max_tokens=5)
    logger.info("Groq APIキーの検証が成功しました。")
except Exception as e:
    logger.error(f"Groq nativeクライアントでのエラー、OpenAI互換にフォールバック: {e}")
    try:
        groq_client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY)
        use_openai_compatible = True
        logger.info("Groq OpenAI互換クライアントの初期設定が完了しました。")
        test_response = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": "test"}], model="llama3-8b-8192", max_tokens=5)
        logger.info("Groq OpenAI互換APIキーの検証が成功しました。")
    except Exception as final_error:
        logger.error(f"OpenAI互換クライアントでもエラー: {final_error}")
        groq_client = None

Base = declarative_base()

class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False)
    user_name = Column(String(255), nullable=False)
    personality_notes = Column(String(2000), default='')
    favorite_topics = Column(String(2000), default='')
    interaction_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_interaction = Column(DateTime, default=datetime.utcnow)

engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
logger.info(f"データベース接続が完了しました。URL: {DATABASE_URL[:20]}...")

class UserDataContainer:
    def __init__(self, user_uuid, user_name, personality_notes='', favorite_topics='',
                 interaction_count=0, created_at=None, last_interaction=None):
        self.user_uuid = user_uuid
        self.user_name = user_name
        self.personality_notes = personality_notes
        self.favorite_topics = favorite_topics
        self.interaction_count = interaction_count
        self.created_at = created_at or datetime.utcnow()
        self.last_interaction = last_interaction or datetime.utcnow()

def get_or_create_user(user_uuid, user_name):
    session = Session()
    try:
        user_memory = session.query(UserMemory).filter(UserMemory.user_uuid == user_uuid).first()
        if user_memory:
            user_memory.interaction_count += 1
            user_memory.last_interaction = datetime.utcnow()
            session.commit()
            return UserDataContainer(user_uuid=user_memory.user_uuid, user_name=user_memory.user_name, personality_notes=user_memory.personality_notes or '', favorite_topics=user_memory.favorite_topics or '', interaction_count=user_memory.interaction_count, created_at=user_memory.created_at, last_interaction=user_memory.last_interaction)
        else:
            new_user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1, created_at=datetime.utcnow(), last_interaction=datetime.utcnow())
            session.add(new_user)
            session.commit()
            return UserDataContainer(user_uuid=new_user.user_uuid, user_name=new_user.user_name, interaction_count=1, created_at=new_user.created_at, last_interaction=new_user.last_interaction)
    except Exception as e:
        logger.error(f"ユーザーデータの取得/作成エラー: {e}")
        session.rollback()
        return UserDataContainer(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
    finally:
        session.close()

def generate_ai_response(user_data, message=""):
    if groq_client is None: return f"こんにちは、{user_data.user_name}さん！現在システムメンテナンス中ですが、お話しできて嬉しいです。"
    system_prompt = ""
    if user_data.interaction_count == 1:
        system_prompt = f"あなたは「もちこ」という名前の、優しくて親しみやすいAIアシスタントです。今、{user_data.user_name}さんという方に初めてお会いしました。以下のキャラクター設定を厳守して、60文字以内の自然で親しみやすい初対面の挨拶をしてください。\n- 敬語は使わず、親しみやすい「タメ口」で話します。\n- 少し恥ずかしがり屋ですが、フレンドリーです。\n- 相手に興味津々です。"
    else:
        days_since_last = (datetime.utcnow() - user_data.last_interaction).days
        situation = "継続的な会話" if days_since_last <= 1 else ("数日ぶりの再会" if days_since_last <= 7 else "久しぶりの再会")
        system_prompt = f"あなたは「もちこ」という名前の、優しくて親しみやすいAIアシスタントです。{user_data.user_name}さんとは{user_data.interaction_count}回目のお話です。\n状況: {situation}。\n過去のメモ: {user_data.personality_notes}\n好きな話題: {user_data.favorite_topics}\n以下のキャラクター設定を厳守し、この状況にふさわしい返事を60文字以内で作成してください。\n- 敬語は使わず、親しみやすい「タメ口」で話します。\n- 過去の会話を覚えている、親しい友人として振る舞います。"
    try:
        chat_completion = groq_client.chat.completions.create(messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": message or "こんにちは"}], model="llama3-8b-8192", temperature=0.7, max_tokens=150)
        return chat_completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return f"ごめんなさい、{user_data.user_name}さん。ちょっと考えがまとまらないや…。"

def generate_voice(text, speaker_id=1):
    try:
        audio_query_response = requests.post(f"{VOICEVOX_URL}/audio_query", params={"text": text, "speaker": speaker_id})
        audio_query_response.raise_for_status()
        synthesis_response = requests.post(f"{VOICEVOX_URL}/synthesis", headers={"Content-Type": "application/json"}, params={"speaker": speaker_id}, json=audio_query_response.json())
        synthesis_response.raise_for_status()
        return synthesis_response.content
    except Exception as e:
        logger.error(f"音声生成エラー: {e}")
        return None

@app.route('/chat', methods=['POST', 'OPTIONS'])
def chat():
    if request.method == 'OPTIONS': return jsonify(status='ok')
    try:
        data = request.json
        user_uuid = data.get('user_uuid') or data.get('uuid')
        user_name = data.get('user_name') or data.get('name')
        if not user_uuid or not user_name: return jsonify(error='user_uuid and user_name are required'), 400
        user_data = get_or_create_user(user_uuid, user_name)
        ai_response = generate_ai_response(user_data, data.get('message', ''))
        voice_data = generate_voice(ai_response)
        response_data = {'text': ai_response, 'response': ai_response, 'interaction_count': user_data.interaction_count, 'has_voice': voice_data is not None}
        if voice_data:
            voice_filename = f"voice_{user_uuid}_{datetime.now().timestamp()}.wav"
            voice_path = os.path.join('/tmp', voice_filename)
            with open(voice_path, 'wb') as f: f.write(voice_data)
            response_data['voice_url'] = f'/voice/{voice_filename}'
            response_data['audio_url'] = f'/voice/{voice_filename}'
        return jsonify(response_data)
    except Exception as e:
        logger.error(f"チャットエラー: {e}")
        return jsonify(error='Internal server error'), 500

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json
        user_uuid = data.get('uuid')
        user_name = data.get('name')
        message = data.get('message', '')
        if not user_uuid or not user_name: return "Error: user_uuid and user_name are required", 400
        user_data = get_or_create_user(user_uuid, user_name)
        if not user_data: return "Error: Failed to get user data", 500
        ai_response = generate_ai_response(user_data, message)
        voice_data = generate_voice(ai_response)
        audio_url_part = ""
        if voice_data:
            voice_filename = f"voice_{user_uuid}_{datetime.now().timestamp()}.wav"
            voice_path = os.path.join('/tmp', voice_filename)
            with open(voice_path, 'wb') as f: f.write(voice_data)
            audio_url_part = f'/voice/{voice_filename}'
        response_text = f"{ai_response}|{audio_url_part}"
        response = app.response_class(response=response_text, status=200, mimetype='text/plain; charset=utf-8')
        return response
    except Exception as e:
        logger.error(f"LSLチャットエラー: {e}")
        return "Error: Internal server error", 500

@app.route('/voice/<filename>')
def serve_voice(filename):
    directory = '/tmp'
    try:
        safe_path = safe_join(directory, filename)
        if os.path.exists(safe_path):
            return send_from_directory(directory, filename)
        else:
            logger.error(f"音声ファイルが見つかりません: {safe_path}")
            return "File not found", 404
    except Exception as e:
        logger.error(f"音声ファイル提供エラー: {e}")
        return "Server error", 500

@app.route('/health')
def health_check():
    return jsonify({'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
