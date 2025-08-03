import os
import requests
import logging
import sys
import time
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import Groq
from openai import OpenAI

# ログ設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Secret Fileからの設定読み込み ---

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

# --- VOICEVOX_URL ---
# ▼▼▼【2024年版修正】最新のVOICEVOX接続設定 ▼▼▼
VOICEVOX_URLS = [
    'http://localhost:50021',        # ローカル環境
    'http://127.0.0.1:50021',        # ローカルループバック
    'http://voicevox-engine:50021',  # Docker Compose環境
    'http://voicevox:50021',         # 別のDocker名
    'http://host.docker.internal:50021',  # Docker Desktop環境
    'http://0.0.0.0:50021',          # 全インターフェース（最後に試行）
]

# VOICEVOXエンジンの推奨バージョン情報
RECOMMENDED_VOICEVOX_IMAGES = [
    "voicevox/voicevox_engine:cpu-0.19.1",
    "voicevox/voicevox_engine:latest",
    "voicevox/voicevox_engine:cpu-0.18.2"
]

VOICEVOX_URL = None
VOICEVOX_URL_SECRET_FILE = '/etc/secrets/VOICEVOX_URL'
try:
    with open(VOICEVOX_URL_SECRET_FILE, 'r') as f:
        VOICEVOX_URL = f.read().strip()
    logger.info("Secret FileからVOICEVOX_URLを読み込みました。")
except FileNotFoundError:
    logger.warning(f"Secret File '{VOICEVOX_URL_SECRET_FILE}' が見つかりません。環境変数を試します。")
    VOICEVOX_URL = os.environ.get('VOICEVOX_URL')

# VOICEVOX接続テスト（改善版）
def find_working_voicevox_url():
    """利用可能なVOICEVOX URLを見つける（2024年版強化）"""
    urls_to_test = []
    
    # Secret Fileまたは環境変数で指定されたURLがあれば最初に試す
    if VOICEVOX_URL:
        urls_to_test.append(VOICEVOX_URL)
    
    # その後、デフォルトのURLリストを試す
    urls_to_test.extend([url for url in VOICEVOX_URLS if url != VOICEVOX_URL])
    
    for url in urls_to_test:
        try:
            logger.info(f"🔍 Testing VOICEVOX at: {url}")
            
            # ステップ1: バージョン確認（タイムアウト短縮）
            version_response = requests.get(f"{url}/version", timeout=3)
            if version_response.status_code == 200:
                version_info = version_response.json()
                engine_version = version_info.get('version', 'unknown')
                logger.info(f"✅ VOICEVOX version確認成功: {url}")
                logger.info(f"📋 Engine version: {engine_version}")
                
                # バージョン警告チェック
                if engine_version != 'unknown':
                    try:
                        version_parts = engine_version.split('.')
                        major, minor = int(version_parts[0]), int(version_parts[1])
                        if major == 0 and minor < 18:
                            logger.warning(f"⚠️ 古いVOICEVOXバージョン検出: {engine_version}")
                            logger.warning(f"💡 推奨バージョン: {', '.join(RECOMMENDED_VOICEVOX_IMAGES)}")
                    except (ValueError, IndexError):
                        pass  # バージョン解析失敗は無視
                
                # ステップ2: スピーカー情報取得テスト
                try:
                    speakers_response = requests.get(f"{url}/speakers", timeout=3)
                    if speakers_response.status_code == 200:
                        speakers = speakers_response.json()
                        speaker_count = len(speakers) if isinstance(speakers, list) else "unknown"
                        logger.info(f"📢 Available speakers: {speaker_count}")
                        
                        # サポート機能確認
                        supported_features = version_info.get('supported_features', {})
                        if supported_features:
                            logger.info(f"🔧 Supported features: {list(supported_features.keys())[:3]}...")
                        
                        # ステップ3: 軽量な音声合成テスト
                        try:
                            test_text = "テスト"
                            # audio_queryテスト
                            query_response = requests.post(
                                f"{url}/audio_query",
                                params={'text': test_text, 'speaker': 1},
                                timeout=5
                            )
                            if query_response.status_code == 200:
                                query_data = query_response.json()
                                # クエリデータの妥当性確認
                                if query_data and 'accent_phrases' in query_data:
                                    logger.info(f"🎵 Audio query test successful")
                                    return url
                                else:
                                    logger.warning(f"⚠️ Invalid query response format")
                            else:
                                logger.warning(f"⚠️ Audio query failed: {query_response.status_code}")
                                
                        except Exception as synthesis_error:
                            logger.warning(f"⚠️ Synthesis test failed: {synthesis_error}")
                            # 基本接続が成功していればURLを返す
                            return url
                    else:
                        logger.warning(f"⚠️ Speakers endpoint failed: {speakers_response.status_code}")
                        # バージョン確認が成功していればURLを返す
                        return url
                except Exception as speakers_error:
                    logger.warning(f"⚠️ Speakers test failed: {speakers_error}")
                    # バージョン確認が成功していればURLを返す
                    return url
                
        except requests.exceptions.Timeout as e:
            logger.debug(f"⏰ VOICEVOX接続タイムアウト: {url} - {e}")
            continue
        except requests.exceptions.ConnectionError as e:
            logger.debug(f"🔌 VOICEVOX接続エラー: {url} - {e}")
            continue
        except Exception as e:
            logger.debug(f"❌ VOICEVOX接続失敗: {url} - {e}")
            continue
    
    logger.error("❌ 利用可能なVOICEVOXエンジンが見つかりませんでした。")
    logger.error("💡 解決方法:")
    logger.error("1. 正しいDockerイメージを使用してください:")
    for image in RECOMMENDED_VOICEVOX_IMAGES:
        logger.error(f"   docker run --rm -p 50021:50021 {image}")
    logger.error("2. 古いイメージを削除してください:")
    logger.error("   docker rmi voicevox/voicevox_engine:cpu-ubuntu20.04-latest")
    logger.error("3. ポート50021が利用可能か確認してください: lsof -i :50021")
    logger.error("4. ファイアウォールの設定を確認してください")
    return None

# 起動時にVOICEVOX接続をテスト（リトライ機能付き）
def initialize_voicevox_with_retry(max_retries=3, retry_delay=5):
    """VOICEVOXの初期化をリトライ機能付きで実行"""
    for attempt in range(max_retries):
        logger.info(f"VOICEVOX初期化試行 {attempt + 1}/{max_retries}")
        working_url = find_working_voicevox_url()
        if working_url:
            return working_url
        
        if attempt < max_retries - 1:
            logger.info(f"⏳ {retry_delay}秒後にリトライします...")
            time.sleep(retry_delay)
    
    return None

WORKING_VOICEVOX_URL = initialize_voicevox_with_retry()

# デバッグ情報をログに出力
logger.info(f"設定されたVOICEVOX_URL: {VOICEVOX_URL}")
logger.info(f"動作するVOICEVOX_URL: {WORKING_VOICEVOX_URL}")

# DNS解決テスト（強化版）
import socket
def test_dns_resolution():
    """DNS解決テスト"""
    hostnames_to_test = ['voicevox-engine', 'voicevox', 'localhost', 'host.docker.internal']
    for hostname in hostnames_to_test:
        try:
            ip = socket.gethostbyname(hostname)
            logger.info(f"DNS解決成功: {hostname} -> {ip}")
        except socket.gaierror as e:
            logger.debug(f"DNS解決失敗: {hostname} -> {e}")

test_dns_resolution()

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
            return UserDataContainer(
                user_uuid=user_memory.user_uuid, 
                user_name=user_memory.user_name, 
                personality_notes=user_memory.personality_notes or '', 
                favorite_topics=user_memory.favorite_topics or '', 
                interaction_count=user_memory.interaction_count, 
                created_at=user_memory.created_at, 
                last_interaction=user_memory.last_interaction
            )
        else:
            new_user = UserMemory(
                user_uuid=user_uuid, 
                user_name=user_name, 
                interaction_count=1, 
                created_at=datetime.utcnow(), 
                last_interaction=datetime.utcnow()
            )
            session.add(new_user)
            session.commit()
            return UserDataContainer(
                user_uuid=new_user.user_uuid, 
                user_name=new_user.user_name, 
                interaction_count=1, 
                created_at=new_user.created_at, 
                last_interaction=new_user.last_interaction
            )
    except Exception as e:
        logger.error(f"ユーザーデータの取得/作成エラー: {e}")
        session.rollback()
        return UserDataContainer(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
    finally:
        session.close()

def generate_ai_response(user_data, message=""):
    if groq_client is None: 
        return f"こんにちは、{user_data.user_name}さん！現在システムメンテナンス中ですが、お話しできて嬉しいです。"
    
    system_prompt = ""
    if user_data.interaction_count == 1:
        system_prompt = f"あなたは「もちこ」という名前の、優しくて親しみやすいAIアシスタントです。今、{user_data.user_name}さんという方に初めてお会いしました。以下のキャラクター設定を厳守して、60文字以内の自然で親しみやすい初対面の挨拶をしてください。\n- 敬語は使わず、親しみやすい「タメ口」で話します。\n- 少し恥ずかしがり屋ですが、フレンドリーです。\n- 相手に興味津々です。"
    else:
        days_since_last = (datetime.utcnow() - user_data.last_interaction).days
        situation = "継続的な会話" if days_since_last <= 1 else ("数日ぶりの再会" if days_since_last <= 7 else "久しぶりの再会")
        system_prompt = f"あなたは「もちこ」という名前の、優しくて親しみやすいAIアシスタントです。{user_data.user_name}さんとは{user_data.interaction_count}回目のお話です。\n状況: {situation}。\n過去のメモ: {user_data.personality_notes}\n好きな話題: {user_data.favorite_topics}\n以下のキャラクター設定を厳守し、この状況にふさわしい返事を60文字以内で作成してください。\n- 敬語は使わず、親しみやすい「タメ口」で話します。\n- 過去の会話を覚えている、親しい友人として振る舞います。"
    
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt}, 
                {"role": "user", "content": message or "こんにちは"}
            ], 
            model="llama3-8b-8192", 
            temperature=0.7, 
            max_tokens=150
        )
        return chat_completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return f"ごめんなさい、{user_data.user_name}さん。ちょっと考えがまとまらないや…。"

# ▼▼▼【改良版】VOICEVOX音声生成（エラーハンドリング強化） ▼▼▼
def generate_voice(text, speaker_id=1, retry_count=2):
    """音声を生成する（エラーハンドリング強化版）"""
    if not WORKING_VOICEVOX_URL:
        logger.warning("VOICEVOXエンジンが利用できないため、音声生成をスキップします。")
        return None
    
    # 長文は分割（公式READMEのパフォーマンス対策）
    original_text = text
    if len(text) > 100:
        text = text[:100] + "..."
        logger.info(f"テキストを100文字に制限: {original_text[:30]}... -> {text[:30]}...")
    
    for attempt in range(retry_count + 1):
        try:
            if attempt > 0:
                logger.info(f"🔄 VOICEVOX音声生成リトライ {attempt}/{retry_count}")
            
            # ステップ1: audio_query でクエリ作成（タイムアウト短縮）
            audio_query_params = {
                'text': text,
                'speaker': speaker_id
            }
            
            logger.debug(f"🔄 VOICEVOX audio_query: {WORKING_VOICEVOX_URL}/audio_query")
            query_response = requests.post(
                f"{WORKING_VOICEVOX_URL}/audio_query",
                params=audio_query_params,
                timeout=8  # タイムアウト短縮
            )
            query_response.raise_for_status()
            
            # レスポンスが空でないことを確認
            if not query_response.content:
                raise ValueError("Audio query returned empty response")
            
            query_data = query_response.json()
            if not query_data:
                raise ValueError("Audio query returned invalid JSON")
            
            # ステップ2: synthesis で音声合成
            synthesis_params = {'speaker': speaker_id}
            
            logger.debug(f"🔄 VOICEVOX synthesis: {WORKING_VOICEVOX_URL}/synthesis")
            synthesis_response = requests.post(
                f"{WORKING_VOICEVOX_URL}/synthesis",
                headers={"Content-Type": "application/json"},
                params=synthesis_params,
                json=query_data,
                timeout=12  # synthesisは少し長めに
            )
            synthesis_response.raise_for_status()
            
            # 音声データが有効であることを確認
            if not synthesis_response.content or len(synthesis_response.content) < 1000:
                raise ValueError(f"Invalid audio data: size={len(synthesis_response.content) if synthesis_response.content else 0}")
            
            # 成功ログ
            audio_size = len(synthesis_response.content)
            logger.info(f"✅ VOICEVOX音声合成成功: テキスト='{text[:30]}...', サイズ={audio_size}bytes")
            
            return synthesis_response.content
            
        except requests.exceptions.Timeout as e:
            logger.warning(f"⏰ VOICEVOX音声合成タイムアウト (試行{attempt+1}): {e}")
            if attempt < retry_count:
                time.sleep(1)  # 短い待機時間
                continue
        except requests.exceptions.HTTPError as e:
            error_detail = ""
            try:
                if e.response.content:
                    error_detail = e.response.text[:200]
            except:
                pass
            logger.error(f"🌐 VOICEVOX HTTP エラー (試行{attempt+1}): {e.response.status_code} - {error_detail}")
            if attempt < retry_count and e.response.status_code >= 500:
                time.sleep(2)  # サーバーエラーの場合は少し長めに待機
                continue
            break  # クライアントエラー（4xx）の場合はリトライしない
        except requests.exceptions.ConnectionError as e:
            logger.error(f"🔌 VOICEVOX接続エラー (試行{attempt+1}): {e}")
            if attempt < retry_count:
                time.sleep(2)
                continue
        except ValueError as e:
            logger.error(f"📊 VOICEVOX データエラー: {e}")
            break  # データエラーはリトライしても意味がない
        except Exception as e:
            logger.error(f"❌ VOICEVOX音声合成予期せぬエラー (試行{attempt+1}): {e}")
            if attempt < retry_count:
                time.sleep(1)
                continue
    
    logger.error(f"❌ VOICEVOX音声合成が{retry_count + 1}回の試行で失敗しました")
    return None

@app.route('/chat', methods=['POST', 'OPTIONS'])
def chat():
    if request.method == 'OPTIONS': 
        return jsonify(status='ok')
    
    try:
        data = request.json
        user_uuid = data.get('user_uuid') or data.get('uuid')
        user_name = data.get('user_name') or data.get('name')
        
        if not user_uuid or not user_name: 
            return jsonify(error='user_uuid and user_name are required'), 400
        
        user_data = get_or_create_user(user_uuid, user_name)
        ai_response = generate_ai_response(user_data, data.get('message', ''))
        voice_data = generate_voice(ai_response)
        
        response_data = {
            'text': ai_response, 
            'response': ai_response, 
            'interaction_count': user_data.interaction_count, 
            'has_voice': voice_data is not None
        }
        
        if voice_data:
            voice_filename = f"voice_{user_uuid}_{datetime.now().timestamp()}.wav"
            voice_path = os.path.join('/tmp', voice_filename)
            with open(voice_path, 'wb') as f: 
                f.write(voice_data)
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
        
        if not user_uuid or not user_name: 
            return "Error: user_uuid and user_name are required", 400
        
        user_data = get_or_create_user(user_uuid, user_name)
        if not user_data: 
            return "Error: Failed to get user data", 500
        
        ai_response = generate_ai_response(user_data, message)
        voice_data = generate_voice(ai_response)
        
        audio_url_part = ""
        if voice_data:
            voice_filename = f"voice_{user_uuid}_{datetime.now().timestamp()}.wav"
            voice_path = os.path.join('/tmp', voice_filename)
            with open(voice_path, 'wb') as f: 
                f.write(voice_data)
            audio_url_part = f'/voice/{voice_filename}'
            logger.info(f"音声ファイル生成成功: {audio_url_part}")
        else:
            logger.warning("音声データの生成に失敗しました")
        
        response_text = f"{ai_response}|{audio_url_part}"
        response = app.response_class(
            response=response_text, 
            status=200, 
            mimetype='text/plain; charset=utf-8'
        )
        return response
    except Exception as e:
        logger.error(f"LSLチャットエラー: {e}")
        return "Error: Internal server error", 500

@app.route('/voice/<filename>')
def serve_voice(filename):
    directory = '/tmp'
    try:
        filepath = os.path.join(directory, filename)
        if os.path.exists(filepath):
            return send_from_directory(directory, filename)
        else:
            logger.error(f"音声ファイルが見つかりません: {filepath}")
            return "File not found", 404
    except Exception as e:
        logger.error(f"音声ファイル提供エラー: {e}")
        return "Server error", 500

# ▼▼▼【強化版】VOICEVOX状態確認エンドポイント ▼▼▼
@app.route('/voicevox_status')
def voicevox_status():
    """VOICEVOXエンジンの詳細状態を確認する"""
    if WORKING_VOICEVOX_URL:
        try:
            # バージョン情報取得
            version_response = requests.get(f"{WORKING_VOICEVOX_URL}/version", timeout=5)
            if version_response.status_code == 200:
                version_info = version_response.json()
                
                # スピーカー情報取得
                speakers_response = requests.get(f"{WORKING_VOICEVOX_URL}/speakers", timeout=3)
                speakers_info = None
                if speakers_response.status_code == 200:
                    speakers_data = speakers_response.json()
                    speakers_info = {
                        'count': len(speakers_data) if isinstance(speakers_data, list) else 0,
                        'available': True
                    }
                
                # 音声合成テスト
                synthesis_test = False
                try:
                    test_query = requests.post(
                        f"{WORKING_VOICEVOX_URL}/audio_query",
                        params={'text': 'テスト', 'speaker': 1},
                        timeout=3
                    )
                    if test_query.status_code == 200:
                        synthesis_test = True
                except:
                    pass
                
                return jsonify({
                    'status': 'available',
                    'url': WORKING_VOICEVOX_URL,
                    'version': version_info,
                    'speakers': speakers_info,
                    'synthesis_test': synthesis_test,
                    'configured_url': VOICEVOX_URL,
                    'tested_urls': VOICEVOX_URLS
                })
        except Exception as e:
            logger.error(f"VOICEVOX状態確認エラー: {e}")
            return jsonify({
                'status': 'error',
                'url': WORKING_VOICEVOX_URL,
                'error': str(e),
                'configured_url': VOICEVOX_URL,
                'tested_urls': VOICEVOX_URLS
            })
    
    return jsonify({
        'status': 'unavailable',
