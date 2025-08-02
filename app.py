import os
import requests
import uuid
import json
from flask import Flask, request, jsonify, send_from_directory
from sqlalchemy import create_engine, Column, String, Integer, Text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

app = Flask(__name__)
AUDIO_DIR = "static/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

# --- ★★★ Secret FileからデータベースURLを読み込む ★★★ ---
DATABASE_URL = ""
# RenderのSecret Fileは、この固定パスにマウントされます
secret_file_path = '/etc/secrets/database_internal_url'
if os.path.exists(secret_file_path):
    with open(secret_file_path, 'r') as f:
        DATABASE_URL = f.read().strip()
        # SQLAlchemyがSSL接続を正しく解釈できるように調整
        if "postgresql://" in DATABASE_URL and "?sslmode" not in DATABASE_URL:
             DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1) + "?sslmode=require"

if not DATABASE_URL:
    print("警告: データベースURLが見つかりません。データベース機能は無効になります。")
    engine = None
else:
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base = declarative_base()

    class User(Base):
        __tablename__ = "users"
        user_id = Column(PG_UUID(as_uuid=True), primary_key=True)
        user_name = Column(String)
    class ChatLog(Base):
        __tablename__ = "chat_logs"
        user_id = Column(PG_UUID(as_uuid=True), primary_key=True)
        user_name = Column(String)
        interaction_count = Column(Integer, default=1)
        
    Base.metadata.create_all(bind=engine)

VOICEVOX_URL = "http://127.0.0.1:50021"
SPEAKER_ID = 9 # もちこさん固定

@app.route('/healthz')
def health_check(): return "OK", 200

@app.route('/interact', methods=['POST'])
def interact():
    if not engine:
        return jsonify({"message": "データベースが接続されていません。", "audio_url": ""}), 503

    db = SessionLocal()
    data = request.get_json()
    user_id = uuid.UUID(data['userId'])
    user_name = data['userName']
    response_message = ""

    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        if user:
            response_message = f"おかえりなさい、{user.user_name}様！"
        else:
            chat_log = db.query(ChatLog).filter(ChatLog.user_id == user_id).first()
            if not chat_log:
                db.add(ChatLog(user_id=user_id, user_name=user_name, interaction_count=1))
            else:
                chat_log.interaction_count += 1
            
            db.commit()
            if chat_log and chat_log.interaction_count >= 5:
                db.add(User(user_id=user_id, user_name=user_name))
                db.delete(chat_log)
                db.commit()
                response_message = f"ありがとうございます、{user_name}様！今日からあなたの専属コンシェルジュになりますね。"
            else:
                response_message = f"こんにちは、{user_name}さん。"

        res_query = requests.post(f"{VOICEVOX_URL}/audio_query", params={'text': response_message, 'speaker': SPEAKER_ID}, timeout=10)
        res_query.raise_for_status()
        audio_query = res_query.json()
        res_synth = requests.post(f"{VOICEVOX_URL}/synthesis", params={'speaker': SPEAKER_ID}, json=audio_query, timeout=10)
        res_synth.raise_for_status()

        filename = f"{uuid.uuid4()}.wav"
        filepath = os.path.join(AUDIO_DIR, filename)
        with open(filepath, "wb") as f: f.write(res_synth.content)
        base_url = os.environ.get("RENDER_EXTERNAL_URL", request.url_root)
        audio_url = f"{base_url.rstrip('/')}/audio/{filename}"
        return jsonify({"message": response_message, "audio_url": audio_url})

    except Exception as e:
        db.rollback()
        print(f"エラー: {e}")
        return jsonify({"error": "サーバーでエラーが発生しました"}), 500
    finally:
        db.close()

@app.route('/audio/<filename>')
def serve_audio(filename): return send_from_directory(AUDIO_DIR, filename)
