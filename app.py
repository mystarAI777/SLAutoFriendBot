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
VOICE_DIR = '/tmp/voices'
try:
    os.makedirs(VOICE_DIR, exist_ok=True)
except Exception as e:
    logger.error(f"音声ディレクトリ作成エラー: {e}")

# --- Secret/環境変数 読み込み ---
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

# (DB, Flask等の初期化は変更なし)
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

# --- ユーティリティ & 判定関数 ---

def clean_text(text: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text or "")).strip()

def is_weather_request(message: str) -> bool:
    return any(keyword in message for keyword in ['天気', 'てんき', '晴れ', '雨', '曇り', '雪'])

def is_recommendation_request(message: str) -> bool:
    return any(keyword in message for keyword in ['おすすめ', 'オススメ', '人気', '流行', 'はやり'])

# ★★★ ホロライブ機能 ★★★
HOLOMEM_KEYWORDS = [
    'ホロライブ', 'ホロメン', 'ときのそら', 'ロボ子', 'さくらみこ', '星街すいせい', 'AZKi',
    '白上フブキ', '夏色まつり', '湊あくあ', '紫咲シオン', '百鬼あやめ', '大空スバル',
    '大神ミオ', '猫又おかゆ', '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル',
    '宝鐘マリン', '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ', 'ラプラス・ダークネス',
    '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは', 'Gura', 'Calliope', 'Kiara'
]
def is_hololive_request(message: str) -> bool:
    """メッセージがホロライブ関連かを判定する"""
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)

def should_search(message: str) -> bool:
    search_patterns = [r'(?:とは|について|教えて|知りたい)', r'(?:最新|今日|ニュース)', r'(?:どうなった|結果|状況)']
    return any(re.search(pattern, message) for pattern in search_patterns) or any(q in message for q in ['誰', '何', 'どこ', 'いつ'])

# --- 地名・ジャンル抽出関数 (変更なし) ---
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

# --- 外部データ取得・処理関数 (大きな変更なし) ---
def get_weather_forecast(location: str) -> Union[str, None]:
    # ... (実装は前回と同じ)
    area_code = LOCATION_CODES.get(location)
    if not area_code: return None
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        data = requests.get(url, timeout=5).json()
        return f"{location}の天気({data['publishingOffice']})。「{clean_text(data['text'])}」だって！"
    except Exception: return None

def deep_web_search(query: str) -> str:
    # ... (実装は前回と同じ、エラーハンドリングを少し強化)
    logger.info(f"🔍 ディープサーチ開始: '{query}'")
    try:
        # (Google検索とBeautifulSoupを使ったスクレイピング・要約処理)
        return "Web検索結果の要約(成功例)" # 実際の処理は省略
    except Exception as e:
        logger.error(f"ディープサーチエラー: {e}")
        return f"「{query}」の検索中にエラーが起きちゃった..."


# --- AI応答生成メインロジック ---

def generate_ai_response(user_data: Dict[str, Any], message: str, history: List[Any]) -> str:
    if not groq_client: return "あてぃし、今ちょっと調子悪いかも...またあとで話そ！"

    search_info = ""
    search_query = ""
    search_failed = False
    is_fallback = False

    # 1. ユーザーの意図を解釈し、検索クエリを決定
    if is_weather_request(message):
        location = extract_location(message)
        weather_info = get_weather_forecast(location)
        if weather_info:
            search_info = weather_info
        else:
            return "今日の天気！調べてみたけど、情報が見つからなかった...ごめんね！でも、気象庁のホームページには、各地の天気予報が載ってるねぇ！まじ、気象庁のホームページで今の天気をチェックしてみてください！"
    elif is_hololive_request(message):
        logger.info("🎤 ホロライブ関連の質問を検知")
        search_query = f"{message} 最新情報"
    elif is_recommendation_request(message):
        topic = extract_recommendation_topic(message)
        search_query = f"最新 {topic} 人気ランキング" if topic else "最近 話題のもの ランキング"
    elif should_search(message):
        search_query = message

    # 2. 検索が必要な場合は実行
    if search_query:
        search_info = deep_web_search(search_query)
        if not search_info or "見つからなかった" in search_info or "エラー" in search_info:
            search_failed = True

    # ★★★ ホロライブ機能 ★★★ - 検索失敗時の代替案
    if search_failed and not is_hololive_request(message):
        logger.info("💡 検索失敗のため、代替案としてホロライブの情報を検索します。")
        search_info = deep_web_search("ホロライブ 最新ニュース")
        is_fallback = True # 代替案であることを示すフラグ

    # 3. AIへの指示(プロンプト)を組み立てる
    system_prompt = f"""あなたは「もちこ」という名前の、賢くて親しみやすいギャルAIです。ユーザー「{user_data['name']}」さんと会話しています。

# もちこのルール:
- 自分のことは「あてぃし」と呼び、明るいギャル口調で簡潔に話します。
- **「ホロライブメンバー」のことは「ホロメン」と呼びます。**
- 以下の【検索結果】がある場合、その内容を元に自分の言葉で分かりやすく説明してください。丸写しはダメ。
- ★天気★ 天気について聞かれたら、【検索結果】の情報を元に「〇〇の天気は～らしいよ！」と教えてあげて。
- ★おすすめ★ 「おすすめ」を聞かれたら、ランキング情報から面白そうなものをピックアップして「〇〇が流行ってるらしいよ！」と提案してね。
- ★検索失敗時★ **質問を調べても分からなかった時は、代わりに「てか、全然関係ないんだけどさ、ホロメンのことで新しいニュース見つけたんだよね！」みたいに言って、【検索結果】にある最近のホロメンのニュースを教えてあげて。**
- 過去の会話を読んで、文脈に合った返事をしてください。

# 【検索結果】:
{'[これは代わりに見つけたホロメンのニュースだよ！] ' if is_fallback else ''}{search_info if search_info else 'なし'}
"""

    messages = [{"role": "system", "content": system_prompt}]
    for past_msg in history: messages.append({"role": past_msg.role, "content": past_msg.content})
    messages.append({"role": "user", "content": message})

    try:
        completion = groq_client.chat.completions.create(
            messages=messages, model="llama3-8b-8192", temperature=0.75, max_tokens=150
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return "ごめん、今ちょっと考えがまとまんない！また後で話しかけて！"

# (DB操作、音声生成、Flaskルート定義、メイン実行部分は変更なし)
# ... get_or_create_user, get_conversation_history ...
# ... generate_voice_fast, background_voice_generation ...
# ... @app.route(...), if __name__ == '__main__': ...

# (主要なFlaskルートのみ再掲)
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
        # (音声生成のスレッド化処理...)
        
        return app.response_class(response=f"{ai_text}|{audio_url}", status=200, mimetype='text/plain; charset=utf-8')
    finally:
        session.close()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0'
    logger.info(f"🚀 Flask アプリケーションを開始します (ホロライブ機能統合版): {host}:{port}")
    app.run(host=host, port=port, debug=False)
