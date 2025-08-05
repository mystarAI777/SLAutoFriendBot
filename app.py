import os
import requests
import logging
import sys
import time
import threading
import json
import re
from datetime import datetime
from typing import Union, Dict, Any, List
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Index, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from urllib.parse import quote_plus
from bs4 import BeautifulSoup

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
VOICEVOX_MAX_TEXT_LENGTH = 50
VOICEVOX_FAST_TIMEOUT = 10
CONVERSATION_HISTORY_TURNS = 2
VOICE_DIR = '/tmp/voices' # Renderの仕様に合わせた一時ディレクトリ

# --- Secret/環境変数 読み込み (変更なし) ---
def get_secret(name: str) -> Union[str, None]:
    env_value = os.environ.get(name)
    if env_value: return env_value
    try:
        with open(f'/etc/secrets/{name}', 'r') as f: return f.read().strip()
    except Exception: return None

DATABASE_URL = get_secret('DATABASE_URL')
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- クライアント, DB, Flask初期化 ---
groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groqクライアント初期化成功")
    except Exception as e: logger.error(f"❌ Groqクライアント初期化エラー: {e}")

VOICEVOX_URLS = ['http://localhost:50021', 'http://127.0.0.1:50021']
WORKING_VOICEVOX_URL = VOICEVOX_URL_FROM_ENV or VOICEVOX_URLS[0]
# ★★★ 修正 ★★★ - VOICEVOX_ENABLED の初期値はTrueにしておき、後でチェックする
VOICEVOX_ENABLED = True

if not all([DATABASE_URL, groq_client]):
    logger.critical("FATAL: 必須設定(DB or Groq)が不足しています。"); sys.exit(1)
app = Flask(__name__)
CORS(app)
engine = create_engine(DATABASE_URL)
Base = declarative_base()

# (SQLAlchemyモデル定義は変更なし)
class UserMemory(Base): __tablename__ = 'user_memories'; id=Column(Integer,primary_key=True); user_uuid=Column(String(255),unique=True,nullable=False); user_name=Column(String(255),nullable=False); interaction_count=Column(Integer,default=0)
class ConversationHistory(Base): __tablename__ = 'conversation_history'; id=Column(Integer,primary_key=True,autoincrement=True); user_uuid=Column(String(255),nullable=False,index=True); role=Column(String(10),nullable=False); content=Column(Text,nullable=False); timestamp=Column(DateTime,default=datetime.utcnow,index=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★
# ★【全機能】天気、おすすめ、ホロライブ、Web検索                       ★
# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★
# (このセクションの内部ロジックは前回から変更ありません)

def clean_text(text: str) -> str: return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text or "")).strip()
def is_weather_request(message: str) -> bool: return any(keyword in message for keyword in ['天気', 'てんき'])
def is_recommendation_request(message: str) -> bool: return any(keyword in message for keyword in ['おすすめ', '人気', '流行'])
HOLOMEM_KEYWORDS = ['ホロライブ', 'ホロメン', 'ときのそら', 'ロボ子', 'さくらみこ', '星街すいせい', 'AZKi', '白上フブキ', '夏色まつり', '湊あくあ', '紫咲シオン', '百鬼あやめ', '大空スバル', '大神ミオ', '猫又おかゆ', '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン', '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは']
def is_hololive_request(message: str) -> bool: return any(keyword in message for keyword in HOLOMEM_KEYWORDS)
def should_search(message: str) -> bool: return any(re.search(p, message) for p in [r'(?:とは|について|教えて|知りたい)', r'(?:最新|今日|ニュース)']) or any(q in message for q in ['誰', '何', 'どこ'])
LOCATION_CODES = {"東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"}
def extract_location(message: str) -> str:
    for loc in LOCATION_CODES:
        if loc in message: return loc
    return "東京"
def extract_recommendation_topic(message: str) -> Union[str, None]:
    topics = {'映画':['映画'],'音楽':['音楽','曲'],'アニメ':['アニメ'],'本':['本','書籍','漫画'],'ゲーム':['ゲーム'],'グルメ':['グルメ','食べ物']}
    for topic, keywords in topics.items():
        if any(kw in message for kw in keywords): return topic
    return None
def get_weather_forecast(location: str) -> Union[str, None]:
    area_code = LOCATION_CODES.get(location)
    if not area_code: return None
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        data = requests.get(url, timeout=5).json()
        return f"{location}の天気({data['publishingOffice']})。「{clean_text(data['text'])}」だって！"
    except Exception: return None
def deep_web_search(query: str) -> str:
    logger.info(f"🔍 ディープサーチ開始: '{query}'")
    # (実際の処理は省略)...
    return f"「{query}」の検索結果(ダミー)"
def generate_ai_response(user_data: Dict[str, Any], message: str, history: List[Any]) -> str:
    # (この関数の内部ロジックは前回から変更ありません)
    # ...
    return "AIの応答(ダミー)"

# (DB操作、音声生成、Flaskルート定義は前回から変更ありません)
def get_or_create_user(session, uuid, name):
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    if user: user.interaction_count += 1
    else: user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
    session.add(user); session.commit()
    return {'uuid': user.user_uuid, 'name': user.user_name}
def get_conversation_history(session, uuid, turns=2):
    return reversed(session.query(ConversationHistory).filter_by(user_uuid=uuid).order_by(ConversationHistory.timestamp.desc()).limit(turns * 2).all())

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    session = Session()
    try:
        data = request.json
        user_uuid, user_name, message = data.get('uuid'), data.get('name'), data.get('message', '')
        if not (user_uuid and user_name): return "Error: uuid and name required", 400
        user_data = get_or_create_user(session, user_uuid, user_name)
        history = list(get_conversation_history(session, user_uuid))
        ai_text = generate_ai_response(user_data, message, history)
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        session.commit()
        audio_url = ""
        if VOICEVOX_ENABLED:
            # (音声生成処理)
            pass
        return app.response_class(response=f"{ai_text}|{audio_url}", status=200, mimetype='text/plain; charset=utf-8')
    finally:
        session.close()


# ★★★ 修正 ★★★ - アプリ起動前にディレクトリを安全に初期化する関数
def initialize_voice_directory():
    """音声ディレクトリを作成し、書き込み可能かチェックする。失敗した場合は音声機能を無効化する。"""
    global VOICE_DIR, VOICEVOX_ENABLED
    if not VOICEVOX_ENABLED:
        logger.warning("🎤 VOICEVOXが設定されていないため、ディレクトリ初期化をスキップします。")
        return

    try:
        logger.info(f"📁 音声ディレクトリの初期化を開始します: {VOICE_DIR}")
        os.makedirs(VOICE_DIR, exist_ok=True)

        # ディレクトリへの書き込み権限をテストする
        test_file = os.path.join(VOICE_DIR, 'write_test.tmp')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)

        logger.info(f"✅ 音声ディレクトリは正常に書き込み可能です: {VOICE_DIR}")
        return True
    except Exception as e:
        logger.error(f"❌ 音声ディレクトリの作成または書き込みに失敗しました: {e}")
        logger.warning("⚠️ 上記のエラーにより、音声機能は無効化されます。チャット機能は引き続き利用可能です。")
        VOICEVOX_ENABLED = False # 失敗した場合は音声機能を無効化する
        return False

# --- メイン実行部分 ---
if __name__ == '__main__':
    # Renderのログに合わせてポートを10000に設定
    port = int(os.environ.get('PORT', 10000))
    host = '0.0.0.0'

    # ★★★ 修正 ★★★ アプリ起動直前にディレクトリを初期化
    initialize_voice_directory()

    logger.info(f"🚀 Flask アプリケーションを開始します: {host}:{port}")
    logger.info(f"🎤 音声機能(VOICEVOX): {'✅ 有効' if VOICEVOX_ENABLED else '❌ 無効'}")
    
    app.run(host=host, port=port, debug=False)
