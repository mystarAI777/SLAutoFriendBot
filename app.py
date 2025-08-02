import os
import requests
import logging
import sys
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool
from groq import Groq # <-- Geminiの代わりにGroqをインポート
# OpenAIクライアントも併用（バックアップ用）
from openai import OpenAI

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
    logger.info(f"GROQ_API_KEY loaded: {GROQ_API_KEY[:10]}...")  # 最初の10文字のみログ出力
else:
    GROQ_API_KEY = None
    logger.error("GROQ_API_KEY環境変数が見つかりません")
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

# CORS設定を追加
CORS(app, origins=["*"], methods=["GET", "POST", "OPTIONS"], 
     allow_headers=["Content-Type", "Authorization"])

# --- ▼▼▼ Groqクライアントの初期設定 ▼▼▼ ---
try:
    # まずGroqクライアントを試す
    groq_client = Groq(
        api_key=GROQ_API_KEY
    )
    use_openai_compatible = False
    logger.info("Groq native クライアントの初期設定が完了しました。")
except Exception as groq_error:
    logger.warning(f"Groq nativeクライアントでエラー: {groq_error}")
    try:
        # OpenAI互換クライアントを使用
        groq_client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=GROQ_API_KEY
        )
        use_openai_compatible = True
        logger.info("Groq OpenAI互換クライアントの初期設定が完了しました。")
    except Exception as openai_error:
        logger.error(f"OpenAI互換クライアントでもエラー: {openai_error}")
        sys.exit(1)
# --- ▲▲▲ ---

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

# データベース接続とテーブル作成
try:
    # PostgreSQLドライバーの動的インポートを試行
    try:
        import psycopg2
        logger.info("psycopg2 ドライバーを使用します。")
    except ImportError:
        try:
            import pg8000
            # pg8000を使用する場合、URLを調整
            if DATABASE_URL.startswith('postgresql://'):
                DATABASE_URL = DATABASE_URL.replace('postgresql://', 'postgresql+pg8000://')
            logger.info("pg8000 ドライバーを使用します。")
        except ImportError:
            logger.error("PostgreSQLドライバーが見つかりません。SQLiteにフォールバックします。")
            DATABASE_URL = 'sqlite:///./app.db'
    
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    logger.info(f"データベース接続が完了しました。URL: {DATABASE_URL[:20]}...")
except Exception as e:
    logger.error(f"データベース接続エラー: {e}")
    logger.error("SQLiteにフォールバックします。")
    try:
        DATABASE_URL = 'sqlite:///./app.db'
        engine = create_engine(DATABASE_URL)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        logger.info("SQLiteデータベースで起動しました。")
    except Exception as sqlite_error:
        logger.error(f"SQLiteでもエラーが発生しました: {sqlite_error}")
        sys.exit(1)

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
            # 既存ユーザーの場合、交流回数を増やして最終交流日時を更新
            user_memory.interaction_count += 1
            user_memory.last_interaction = datetime.utcnow()
            session.commit()
            
            user_data = UserDataContainer(
                user_uuid=user_memory.user_uuid,
                user_name=user_memory.user_name,
                personality_notes=user_memory.personality_notes,
                favorite_topics=user_memory.favorite_topics,
                interaction_count=user_memory.interaction_count,
                created_at=user_memory.created_at,
                last_interaction=user_memory.last_interaction
            )
        else:
            # 新規ユーザーの場合
            new_user = UserMemory(
                user_uuid=user_uuid,
                user_name=user_name,
                interaction_count=1,
                created_at=datetime.utcnow(),
                last_interaction=datetime.utcnow()
            )
            session.add(new_user)
            session.commit()
            
            user_data = UserDataContainer(
                user_uuid=user_uuid,
                user_name=user_name,
                interaction_count=1,
                created_at=new_user.created_at,
                last_interaction=new_user.last_interaction
            )
        
        return user_data
    except Exception as e:
        logger.error(f"ユーザーデータの取得/作成エラー: {e}")
        session.rollback()
        return None
    finally:
        session.close()

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
        # Groq API呼び出しの詳細ログ
        logger.info(f"Groq API呼び出し開始 - User: {user_data.user_name}, Model: llama3-8b-8192")
        
        # クライアントの種類に応じて適切なメソッドを使用
        if use_openai_compatible:
            # OpenAI互換クライアント使用
            chat_completion = groq_client.chat.completions.create(
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user", 
                        "content": message or "こんにちは",
                    }
                ],
                model="llama3-8b-8192",
                temperature=0.7,
                max_tokens=150,
            )
        else:
            # Groq nativeクライアント使用
            chat_completion = groq_client.chat.completions.create(
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user", 
                        "content": message or "こんにちは",
                    }
                ],
                model="llama3-8b-8192",
                temperature=0.7,
                max_tokens=150,
            )
        
        response_text = chat_completion.choices[0].message.content
        logger.info(f"Groq API呼び出し成功 - Response length: {len(response_text)}")
        return response_text.strip()
        
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        logger.error(f"エラータイプ: {type(e).__name__}")
        logger.error(f"エラー詳細: {str(e)}")
        return f"ごめんなさい、{user_data.user_name}さん。ちょっと考えがまとまらないや…。"
# --- ▲▲▲ 修正はここまで ▲▲▲ ---

def generate_voice(text, speaker_id=1):
    """VOICEVOX APIを使って音声を生成する"""
    try:
        # 音声クエリを作成
        audio_query_response = requests.post(
            f"{VOICEVOX_URL}/audio_query",
            params={"text": text, "speaker": speaker_id}
        )
        audio_query_response.raise_for_status()
        audio_query = audio_query_response.json()
        
        # 音声を合成
        synthesis_response = requests.post(
            f"{VOICEVOX_URL}/synthesis", 
            headers={"Content-Type": "application/json"},
            params={"speaker": speaker_id},
            json=audio_query
        )
        synthesis_response.raise_for_status()
        
        return synthesis_response.content
    except Exception as e:
        logger.error(f"音声生成エラー: {e}")
        return None

@app.route('/chat', methods=['POST', 'OPTIONS'])
def chat():
    # OPTIONSリクエスト（プリフライト）への対応
    if request.method == 'OPTIONS':
        response = jsonify({'status': 'ok'})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        return response
    
    try:
        data = request.json
        user_uuid = data.get('user_uuid')
        user_name = data.get('user_name')
        message = data.get('message', '')
        
        if not user_uuid or not user_name:
            return jsonify({'error': 'user_uuid and user_name are required'}), 400
        
        # ユーザーデータの取得または作成
        user_data = get_or_create_user(user_uuid, user_name)
        if not user_data:
            return jsonify({'error': 'Failed to get user data'}), 500
        
        # AI応答の生成
        ai_response = generate_ai_response(user_data, message)
        
        # 音声生成
        voice_data = generate_voice(ai_response)
        
        response_data = {
            'response': ai_response,
            'interaction_count': user_data.interaction_count,
            'has_voice': voice_data is not None
        }
        
        if voice_data:
            # 音声データを一時的に保存（実際の実装では適切な場所に保存）
            voice_filename = f"voice_{user_uuid}_{datetime.now().timestamp()}.wav"
            voice_path = os.path.join('/tmp', voice_filename)
            with open(voice_path, 'wb') as f:
                f.write(voice_data)
            response_data['voice_url'] = f'/voice/{voice_filename}'
        
        response = jsonify(response_data)
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response
        
    except Exception as e:
        logger.error(f"チャットエラー: {e}")
        response = jsonify({'error': 'Internal server error'})
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response, 500

@app.route('/voice/<filename>')
def serve_voice(filename):
    return send_from_directory('/tmp', filename)

@app.route('/health')
def health_check():
    return jsonify({'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
