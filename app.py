# coding: utf-8
import os
import logging
import sys
import time
import io
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import Groq
from gtts import gTTS

# --------------------------------------------------------------------------
# 1. 設定 & 初期化
# --------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s')
logger = logging.getLogger(__name__)

def load_secret(name, default_value=None):
    secret_file = f'/etc/secrets/{name}'
    try:
        with open(secret_file, 'r') as f: return f.read().strip()
    except (FileNotFoundError, IOError):
        return os.environ.get(name, default_value)

DATABASE_URL = load_secret('DATABASE_URL')
GROQ_API_KEY = load_secret('GROQ_API_KEY')

if not all([DATABASE_URL, GROQ_API_KEY]):
    logger.error("FATAL: 必須設定(DB, Groq)が不足しています。")
    sys.exit(1)

app = Flask(__name__)
CORS(app)
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    logger.info("Groq APIクライアント初期化成功。")
except Exception as e:
    logger.error(f"Groq APIクライアント初期化失敗: {e}")
    groq_client = None

Base = declarative_base()
class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False)
    user_name = Column(String(255), nullable=False)
    interaction_count = Column(Integer, default=0)
    last_interaction = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# --------------------------------------------------------------------------
# 2. コア機能 (AI応答, 音声生成)
# --------------------------------------------------------------------------
def get_or_create_user(session, user_uuid, user_name):
    user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if user:
        user.interaction_count += 1
    else:
        user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
        session.add(user)
    session.commit()
    return user

def generate_ai_text_response(user, user_message):
    if not groq_client:
        return f"申し訳ありません、{user.user_name}様。現在システムが応答できない状態です。"

    system_prompt = f"""
あなたは「もちこ」という名の、知的で親切なAIアシスタントです。
# あなたへの指示:
- ユーザー「{user.user_name}」さんに対し、常に敬意を払い、「です・ます」調の丁寧な言葉遣いで話してください。
- ユーザーの質問や発言の意図を正確に読み取り、その要点をまとめてください。
- あなたの回答は、その要点を基に **50文字以内** で、可能な限り分かりやすく説明する形で作成してください。
- 単なる相槌や短い返事ではなく、必ず何らかの情報や説明を含めるように心がけてください。
"""
    try:
        completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message or "こんにちは"}
            ],
            model="llama3-8b-8192",
            temperature=0.7,
            max_tokens=100,
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return "申し訳ありません、考えがまとまりませんでした。"

def generate_voice_gtts(text, lang='ja'):
    try:
        logger.info(f"gTTSによる音声生成を開始: '{text[:30]}...'")
        mp3_fp = io.BytesIO()
        tts = gTTS(text=text, lang=lang)
        tts.write_to_fp(mp3_fp)
        mp3_fp.seek(0)
        voice_data = mp3_fp.read()
        logger.info(f"✅ gTTS音声生成成功 (サイズ: {len(voice_data)} bytes)")
        return voice_data
    except Exception as e:
        logger.error(f"❌ gTTSでの音声生成中にエラーが発生しました: {e}")
        return None

# --------------------------------------------------------------------------
# 3. API エンドポイント
# --------------------------------------------------------------------------
@app.route('/chat', methods=['POST'])
def chat():
    session = Session()
    try:
        data = request.json
        user_uuid = data.get('user_uuid')
        user_name = data.get('user_name')
        user_message = data.get('message', '')

        if not (user_uuid and user_name):
            return jsonify(error='user_uuid and user_name are required'), 400

        user = get_or_create_user(session, user_uuid, user_name)
        ai_response_text = generate_ai_text_response(user, user_message)
        voice_data = generate_voice_gtts(ai_response_text)

        response_data = {
            'text': ai_response_text,
            'has_voice': voice_data is not None
        }
        if voice_data:
            filename = f"voice_{user_uuid}_{int(time.time())}.mp3"
            with open(os.path.join('/tmp', filename), 'wb') as f:
                f.write(voice_data)
            response_data['voice_url'] = f'/voice/{filename}'
        
        return jsonify(response_data)
    except Exception as e:
        logger.error(f"チャットエンドポイントエラー: {e}", exc_info=True)
        return jsonify(error='Internal server error'), 500
    finally:
        session.close()

@app.route('/voice/<filename>')
def serve_voice(filename):
    # /tmp ディレクトリはRenderなどの多くのホスティングサービスで一時的な書き込みが許可されています
    return send_from_directory('/tmp', filename)

@app.route('/status')
def status():
    return jsonify({
        "status": "ok",
        "tts_engine": "gTTS",
        "groq_api_status": "ok" if groq_client else "error",
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
