import os
import json
import requests
import logging
import sys
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from sqlalchemy import create_engine, Column, String, DateTime, Integer, text
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
import google.generativeai as genai

# ログ設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 環境変数から設定を取得
DATABASE_URL = os.environ.get('DATABASE_URL')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
VOICEVOX_URL = os.environ.get('VOICEVOX_URL', 'http://localhost:50021')

# 必須の環境変数が設定されているかチェック
if not DATABASE_URL:
    logger.error("環境変数 'DATABASE_URL' が設定されていません。")
    sys.exit(1) # プログラムを終了

if not GEMINI_API_KEY:
    logger.error("環境変数 'GEMINI_API_KEY' が設定されていません。")
    sys.exit(1) # プログラムを終了

app = Flask(__name__)

# Gemini AI設定
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-pro')

# データベース設定
Base = declarative_base()

class UserMemory(Base):
    __tablename__ = 'user_memories'
    
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False)
    user_name = Column(String(255), nullable=False)
    first_met = Column(DateTime, default=datetime.utcnow)
    last_interaction = Column(DateTime, default=datetime.utcnow)
    interaction_count = Column(Integer, default=1)
    personality_notes = Column(String(2000), default='')
    favorite_topics = Column(String(1000), default='')

# データベース接続
engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

def get_or_create_user(user_uuid, user_name):
    """ユーザー情報を取得または新規作成"""
    session = Session()
    try:
        user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        
        if user:
            # 既存ユーザーの最終対話時間を更新
            user.last_interaction = datetime.utcnow()
            user.interaction_count += 1
            session.commit()
            logger.info(f"既存ユーザー {user_name} の記録を更新しました")
        else:
            # 新規ユーザーを作成
            user = UserMemory(
                user_uuid=user_uuid,
                user_name=user_name,
                first_met=datetime.utcnow(),
                last_interaction=datetime.utcnow(),
                interaction_count=1
            )
            session.add(user)
            session.commit()
            logger.info(f"新規ユーザー {user_name} を登録しました")
        
        return user
    except Exception as e:
        logger.error(f"データベースエラー: {e}")
        session.rollback()
        return None
    finally:
        session.close()

def generate_ai_response(user_data, message=""):
    """Gemini AIを使って応答を生成"""
    try:
        # ユーザーの状況に応じたプロンプトを作成
        if user_data.interaction_count == 1:
            # 初回の場合
            prompt = f"""
あなたは「もちこ」という名前の、優しくて親しみやすいAIアシスタントです。
今、{user_data.user_name}さんという方に初めてお会いしました。

以下のキャラクター設定で応答してください：
- 温かく親しみやすい口調
- 少し恥ずかしがり屋だけど、フレンドリー
- 相手のことを知りたがる好奇心旺盛な性格
- 敬語は使わず、親しみやすい話し方

初対面の挨拶として、自然で親しみやすい返事を60文字以内で作成してください。
ユーザーからのメッセージ: {message}
"""
        else:
            # 2回目以降の場合
            days_since_last = (datetime.utcnow() - user_data.last_interaction).days
            
            if days_since_last > 7:
                situation = "久しぶりの再会"
            elif days_since_last > 1:
                situation = "数日ぶりの再会"
            else:
                situation = "継続的な会話"
            
            prompt = f"""
あなたは「もちこ」という名前の、優しくて親しみやすいAIアシスタントです。
{user_data.user_name}さんとは{user_data.interaction_count}回目のお話です。
最後にお話ししたのは{days_since_last}日前です。

状況: {situation}
過去のメモ: {user_data.personality_notes}
好きな話題: {user_data.favorite_topics}

以下のキャラクター設定で応答してください：
- 温かく親しみやすい口調
- 相手との関係性を大切にする
- 過去の会話を覚えている
- 敬語は使わず、親しみやすい話し方

この状況にふさわしい返事を60文字以内で作成してください。
ユーザーからのメッセージ: {message}
"""
        
        response = model.generate_content(prompt)
        return response.text.strip()
        
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        # フォールバック応答
        if user_data.interaction_count == 1:
            return f"はじめまして、{user_data.user_name}さん！私、もちこって言います。よろしくね！"
        else:
            return f"おかえりなさい、{user_data.user_name}さん！また会えて嬉しいです♪"

def generate_voice(text, speaker_id=1):
    """VOICEVOXで音声を生成"""
    try:
        # 音声クエリを作成
        query_response = requests.post(
            f"{VOICEVOX_URL}/audio_query",
            params={"text": text, "speaker": speaker_id},
            timeout=10
        )
        
        if query_response.status_code != 200:
            logger.error(f"VOICEVOX query error: {query_response.status_code}")
            return None
        
        # 音声を合成
        synthesis_response = requests.post(
            f"{VOICEVOX_URL}/synthesis",
            params={"speaker": speaker_id},
            json=query_response.json(),
            timeout=30
        )
        
        if synthesis_response.status_code != 200:
            logger.error(f"VOICEVOX synthesis error: {synthesis_response.status_code}")
            return None
        
        # 音声ファイルを保存
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"voice_{timestamp}.wav"
        filepath = os.path.join("static", filename)
        
        # staticディレクトリが存在しない場合は作成
        os.makedirs("static", exist_ok=True)
        
        with open(filepath, "wb") as f:
            f.write(synthesis_response.content)
        
        logger.info(f"音声ファイルを生成しました: {filename}")
        return filename
        
    except Exception as e:
        logger.error(f"音声生成エラー: {e}")
        return None

@app.route('/chat', methods=['POST'])
def chat():
    """メインのチャット処理エンドポイント"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "JSONデータが必要です"}), 400
        
        user_uuid = data.get('uuid')
        user_name = data.get('name')
        message = data.get('message', '')
        
        if not user_uuid or not user_name:
            return jsonify({"error": "uuidとnameは必須です"}), 400
        
        logger.info(f"チャット要求: {user_name} ({user_uuid})")
        
        # ユーザー情報の取得・更新
        user_data = get_or_create_user(user_uuid, user_name)
        if not user_data:
            return jsonify({"error": "ユーザーデータの処理に失敗しました"}), 500
        
        # AI応答の生成
        ai_response = generate_ai_response(user_data, message)
        
        # 音声の生成
        audio_filename = generate_voice(ai_response)
        
        # レスポンスの構築
        response_data = {
            "text": ai_response,
            "audio_url": f"/static/{audio_filename}" if audio_filename else None,
            "user_info": {
                "interaction_count": user_data.interaction_count,
                "first_met": user_data.first_met.isoformat(),
                "is_new_user": user_data.interaction_count == 1
            }
        }
        
        logger.info(f"応答完了: {user_name}")
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"チャット処理エラー: {e}")
        return jsonify({"error": "内部サーバーエラー"}), 500

@app.route('/static/<filename>')
def serve_audio(filename):
    """静的ファイル（音声）の配信"""
    return send_from_directory('static', filename)

@app.route('/health')
def health_check():
    """ヘルスチェック用エンドポイント"""
    try:
        # データベース接続テスト
        session = Session()
        session.execute(text('SELECT 1'))
        session.close()
        
        # VOICEVOX接続テスト
        voicevox_response = requests.get(f"{VOICEVOX_URL}/version", timeout=5)
        voicevox_ok = voicevox_response.status_code == 200
        
        return jsonify({
            "status": "healthy",
            "database": "connected",
            "voicevox": "connected" if voicevox_ok else "disconnected",
            "timestamp": datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        logger.error(f"ヘルスチェックエラー: {e}")
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }), 500

@app.route('/')
def index():
    """基本情報の表示"""
    return jsonify({
        "service": "SL Auto Friend Bot",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "chat": "/chat (POST)",
            "health": "/health (GET)",
            "audio": "/static/<filename> (GET)"
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
