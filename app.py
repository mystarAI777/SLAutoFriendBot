import os
import requests
import uuid
import json
from flask import Flask, request, jsonify, send_from_directory
from sqlalchemy import create_engine, Column, String, Integer, Text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.dialects.postgresql import UUID as PG_UUID  # UUIDの名称衝突を避ける

# --- 基本設定 ---
app = Flask(__name__)
AUDIO_DIR = "static/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)
FRIENDSHIP_THRESHOLD = 5 # お友達になる回数

# --- データベース接続設定 ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")

# RenderのPostgreSQLはSSL接続が必要なため、接続文字列を調整
if "postgresql://" in DATABASE_URL and "?sslmode" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    DATABASE_URL += "?sslmode=require"
elif "postgresql+psycopg2://" not in DATABASE_URL:
     DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)


engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- データベースのテーブル設計 ---
class User(Base):
    __tablename__ = "users"
    user_id = Column(PG_UUID(as_uuid=True), primary_key=True, index=True)
    user_name = Column(String)
    chat_history_json = Column(Text, default="[]")

class ChatLog(Base):
    __tablename__ = "chat_logs"
    user_id = Column(PG_UUID(as_uuid=True), primary_key=True, index=True)
    user_name = Column(String)
    interaction_count = Column(Integer, default=1)
    
Base.metadata.create_all(bind=engine)

# --- VOICEVOX設定 ---
VOICEVOX_URL = "http://127.0.0.1:50021"
SPEAKER_ID = 9 # もちこさん固定

# --- Flaskルート定義 ---
@app.route('/healthz')
def health_check():
    return "OK", 200

@app.route('/interact', methods=['POST'])
def interact():
    db = SessionLocal()
    data = request.get_json()
    user_id = uuid.UUID(data['userId'])
    user_name = data['userName']
    user_message = data['message']
    response_message = ""
    audio_url = ""

    try:
        user = db.query(User).filter(User.user_id == user_id).first()

        if user:
            response_message = f"（AIの応答）おかえりなさい、{user.user_name}様！"
        else:
            chat_log = db.query(ChatLog).filter(ChatLog.user_id == user_id).first()
            if not chat_log:
                chat_log = ChatLog(user_id=user_id, user_name=user_name, interaction_count=1)
                db.add(chat_log)
            else:
                chat_log.interaction_count += 1
            
            db.commit()

            if chat_log.interaction_count >= FRIENDSHIP_THRESHOLD:
                new_user = User(user_id=user_id, user_name=user_name)
                db.add(new_user)
                db.delete(chat_log)
                db.commit()
                response_message = f"いつもありがとうございます、{user_name}様！今日からあなたの専属コンシェルジュとして、会話を記憶させていただきますね。声はずっと、もちこが担当します。"
            else:
                response_message = f"（AIの応答）こんにちは、{user_name}さん。 （あと{FRIENDSHIP_THRESHOLD - chat_log.interaction_count}回で常連さんです！）"

        res_query = requests.post(f"{VOICEVOX_URL}/audio_query", params={'text': response_message, 'speaker': SPEAKER_ID}, timeout=10)
        res_query.raise_for_status()
        audio_query = res_query.json()
        res_synth = requests.post(f"{VOICEVOX_URL}/synthesis", params={'speaker': SPEAKER_ID}, json=audio_query, timeout=10)
        res_synth.raise_for_status()

        filename = f"{uuid.uuid4()}.wav"
        filepath = os.path.join(AUDIO_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(res_synth.content)
        
        base_url = os.environ.get("RENDER_EXTERNAL_URL", request.url_root)
        audio_url = f"{base_url.rstrip('/')}/audio/{filename}"

    except Exception as e:
        print(f"エラー: {e}")
        response_message = "申し訳ありません、サーバーでエラーが発生しました。"
        db.rollback()
    finally:
        db.close()
    
    return jsonify({"message": response_message, "audio_url": audio_url})

@app.route('/audio/<filename>')
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)
