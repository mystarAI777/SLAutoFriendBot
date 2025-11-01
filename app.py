#
# Mochiko AI - Version 3.0 (Stable Task Management & UTF-8 Fix)
#

import sys
import os
import requests
import logging
import time
import threading
import json
import re
import random
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
import unicodedata

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, text
from sqlalchemy.orm import declarative_base, sessionmaker
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from groq import Groq
import google.generativeai as genai
from concurrent.futures import ThreadPoolExecutor

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Flask アプリケーション初期化 ---
app = Flask(__name__)
CORS(app)
# ★★★ 文字化け対策: JSONで日本語をそのまま出力する設定 ★★★
app.config['JSON_AS_ASCII'] = False

# --- 定数 ---
# (既存の定数定義は変更なし)
VOICE_DIR = '/tmp/voices'
SERVER_URL = "https://slautofriendbot.onrender.com"
HOLOMEM_KEYWORDS = [
    'ときのそら', 'ロボ子さん', 'さくらみこ', '星街すいせい', 'AZKi', '夜空メル',
    'アキ・ローゼンタール', '赤井はあと', '白上フブキ', '夏色まつり', '湊あくあ',
    '紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル', '大神ミオ', '猫又おかゆ',
    '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン', '天音かなた',
    '角巻わため', '常闇トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん',
    '尾丸ポルカ', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ',
    '風真いろは', '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら',
    'ワトソン・アメリア', 'IRyS', 'セレス・ファウナ', 'オーロ・クロニー', '七詩ムメイ',
    'ハコス・ベールズ', 'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト',
    'フワワ・アビスガード', 'モココ・アビスガード', 'アユンダ・リス', 'ムーナ・ホシノヴァ',
    'アイラニ・イオフィフティーン', 'クレイジー・オリー', 'アーニャ・メルフィッサ',
    'パヴォリア・レイネ', '火威青', '音乃瀬奏', '一条莉々華', '儒烏風亭らでん',
    '轟はじめ', 'ホロライブ', 'ホロメン', 'hololive', 'YAGOO', '潤羽るしあ', '桐生ココ', '魔乃アロエ', '九十九佐命'
]
SPECIALIZED_SITES = {
    'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー']},
    'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG']},
    '脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学']},
    'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'SL']},
    'アニメ': {'base_url': 'https://animedb.jp/', 'keywords': ['アニメ', 'anime']}
}


# --- グローバル変数 & Executor ---
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client = None
gemini_model = None
Base = declarative_base()

# --- 秘密情報/環境変数 読み込み ---
def get_secret(name):
    secret_file_path = f"/etc/secrets/{name}"
    if os.path.exists(secret_file_path):
        with open(secret_file_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    return os.environ.get(name)

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
GEMINI_API_KEY = get_secret('GEMINI_API_KEY')

# --- データベースモデル (変更なし) ---
class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)

class ConversationHistory(Base):
    __tablename__ = 'conversation_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    role = Column(String(10), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

class BackgroundTask(Base):
    __tablename__ = 'background_tasks'
    id = Column(Integer, primary_key=True)
    task_id = Column(String(255), unique=True, nullable=False)
    user_uuid = Column(String(255), nullable=False, index=True)
    query = Column(Text, nullable=False)
    result = Column(Text)
    status = Column(String(20), default='pending') # pending, completed, failed
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)

class UserPsychology(Base):
    __tablename__ = 'user_psychology'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)
    conversation_style = Column(String(100))
    emotional_tendency = Column(String(100))
    favorite_topics = Column(Text)
    confidence = Column(Integer, default=0)

# (その他のDBモデルは省略)

# --- AIクライアント初期化 (変更なし) ---
def initialize_gemini_client():
    global gemini_model
    try:
        if GEMINI_API_KEY and len(GEMINI_API_KEY) > 20:
            genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = genai.GenerativeModel('gemini-1.5-flash')
            logger.info("✅ Gemini 1.5 Flash client initialized.")
        else:
            logger.warning("⚠️ GEMINI_API_KEY not set or invalid. Gemini disabled.")
    except Exception as e:
        logger.error(f"❌ Gemini client initialization failed: {e}")

def initialize_groq_client():
    global groq_client
    try:
        if GROQ_API_KEY and len(GROQ_API_KEY) > 20:
            groq_client = Groq(api_key=GROQ_API_KEY)
            logger.info("✅ Llama 3.1 (Groq) client initialized.")
        else:
            logger.warning("⚠️ GROQ_API_KEY not set or invalid. Llama disabled.")
    except Exception as e:
        logger.error(f"❌ Groq client initialization failed: {e}")

# --- AIモデル呼び出し (変更なし) ---
def call_gemini(prompt, history=None, system_context=""):
    # ... (既存のコード)
    if not gemini_model: return None
    try:
        # 簡略化された呼び出し
        full_prompt = f"{system_context}\n\n[PAST CONVERSATION]\n{history}\n\n[CURRENT PROMPT]\n{prompt}"
        response = gemini_model.generate_content(full_prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"❌ Gemini API error: {e}")
        return None

def call_llama_advanced(prompt, history=None, system_prompt=None):
    # ... (既存のコード)
    if not groq_client: return None
    try:
        messages = []
        if system_prompt: messages.append({"role": "system", "content": system_prompt})
        if history:
             messages.extend([{"role": "user" if msg.role == "user" else "assistant", "content": msg.content} for msg in history[-5:]])
        messages.append({"role": "user", "content": prompt})
        
        completion = groq_client.chat.completions.create(
            messages=messages, model="llama3-70b-8192", temperature=0.7, max_tokens=600
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Llama API error: {e}")
        return None


# --- ユーティリティ関数 ---
def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', text).strip()

def is_short_response(message):
    return len(message.strip()) <= 5

def is_explicit_search_request(message):
    return any(keyword in message for keyword in ['調べて', '検索して'])

def detect_specialized_topic(message):
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword.lower() in message.lower() for keyword in config['keywords']):
            return topic
    return None

def should_search(message):
    if is_short_response(message) or is_explicit_search_request(message):
        return False
    if detect_specialized_topic(message) or is_hololive_request(message):
        return True
    search_patterns = [r'とは', r'について', r'教えて', r'誰', r'何', r'なぜ', r'詳しく']
    return any(re.search(pattern, message) for pattern in search_patterns)

def is_hololive_request(message):
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)
    
def get_user_psychology(user_uuid):
    session = Session()
    try:
        psychology = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        if not psychology: return None
        return {
            'conversation_style': psychology.conversation_style,
            'emotional_tendency': psychology.emotional_tendency,
            'favorite_topics': json.loads(psychology.favorite_topics or '[]'),
            'confidence': psychology.confidence
        }
    finally:
        session.close()

# --- コアロジック ---

def generate_ai_response(user_data, message, history, reference_info=""):
    use_llama = len(reference_info) > 100 or any(keyword in message for keyword in ['分析', '詳しく', '説明'])
    
    psychology = get_user_psychology(user_data['uuid'])
    system_prompt_parts = [
        f"あなたは「もちこ」というギャルAIです。{user_data['name']}さんと話しています。",
        "一人称は「あてぃし」、語尾は「〜じゃん」「〜的な？」、口癖は「まじ」「てか」「うける」。"
    ]
    if psychology and psychology.get('confidence', 0) > 50:
        system_prompt_parts.append(f"相手は{psychology['conversation_style']}な会話を好みます。")
    if reference_info:
        system_prompt_parts.append(f"\n【参考情報】\n{reference_info}")
    
    system_prompt = "\n".join(system_prompt_parts)

    response = None
    if use_llama:
        logger.info("🧠 Using Llama 3.1 70B for detailed response.")
        response = call_llama_advanced(message, history, system_prompt)
    
    if not response:
        logger.info("🚀 Using Gemini 1.5 Flash for fast response.")
        history_text = "\n".join([f"{h.role}: {h.content}" for h in history])
        response = call_gemini(message, history_text, system_prompt)

    return response or "ごめん、ちょっと考えがまとまらないや…！"

def background_deep_search(task_id, query, history):
    session = Session()
    try:
        contextual_query = query
        if history and any(kw in query for kw in ['それ', 'あれ', 'その中で', 'もっと詳しく']):
            logger.info("🧠 Generating contextual search query...")
            history_text = "\n".join([f"{'User' if h.role=='user' else 'AI'}: {h.content}" for h in history[-3:]])
            prompt = f'以下の会話履歴を参考に、最後の質問を自己完結したGoogle検索クエリに変換して。クエリだけを返して。\n\n[履歴]\n{history_text}\n\n[最後の質問]\n"{query}"\n\n[変換後の検索クエリ]:'
            generated_query = call_gemini(prompt) # 高速なモデルでクエリ生成
            if generated_query:
                contextual_query = generated_query.strip().replace("\"", "")
                logger.info(f"✅ Contextual query: '{contextual_query}'")

        # (scrape_major_search_enginesはWeb検索を行う関数と仮定)
        results = [{"snippet": f"「{contextual_query}」の検索結果が見つかりました。"}]
        if not results:
            search_result = f"「{contextual_query}」について調べたけど、情報が見つからなかったよ…！"
        else:
            summary_text = "\n".join([res['snippet'] for res in results])
            search_result = generate_ai_response(
                {'name': 'ユーザー', 'uuid': ''}, # DUMMY
                f"「{contextual_query}」について調べてきたよ。",
                history,
                reference_info=summary_text
            )

        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = search_result
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            session.commit()
            logger.info(f"💾 Task {task_id} completed and saved.")

    except Exception as e:
        logger.error(f"❌ Background search failed for task {task_id}: {e}", exc_info=True)
    finally:
        session.close()

def start_background_search(user_uuid, query, history):
    session = Session()
    try:
        task_id = str(uuid.uuid4())
        task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, query=query)
        session.add(task)
        session.commit()
        background_executor.submit(background_deep_search, task_id, query, history)
        return task_id
    except Exception as e:
        logger.error(f"❌ Failed to start background search: {e}")
        session.rollback()
        return None
    finally:
        session.close()


# --- Flask エンドポイント ---

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    session = Session()
    try:
        data = request.json
        user_uuid, user_name, message = data.get('uuid'), data.get('name'), data.get('message')

        user_data = {'uuid': user_uuid, 'name': user_name}
        history = session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(ConversationHistory.timestamp.desc()).limit(5).all()

        response_data = {}
        if should_search(message):
            task_id = start_background_search(user_uuid, message, history)
            if task_id:
                response_data = {
                    "type": "search_started",
                    "task_id": task_id,
                    "message": "おっけー、調べてみるね！ちょっと待ってて！"
                }
            else:
                response_data = {"type": "text", "message": "ごめん、今検索機能がうまく動いてないみたい…"}
        else:
            ai_text = generate_ai_response(user_data, message, history)
            response_data = {"type": "text", "message": ai_text}

        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        if response_data.get("message"):
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_data["message"]))
        session.commit()

        return jsonify(response_data)

    except Exception as e:
        logger.error(f"❌ Error in /chat_lsl: {e}", exc_info=True)
        return jsonify({"type": "text", "message": "ごめん、サーバーでエラーが起きちゃった…"}), 500
    finally:
        session.close()

# ★★★ 修正点: /check_task エンドポイントの追加 ★★★
@app.route('/check_task', methods=['POST'])
def check_task():
    session = Session()
    try:
        data = request.json
        task_id = data.get('task_id')
        if not task_id:
            return jsonify({'status': 'error', 'message': 'task_idが必要です'}), 400

        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()

        if task and task.status == 'completed':
            # 完了したタスクをDBから削除して、再度ポーリングされないようにする
            result = task.result
            session.delete(task)
            session.commit()
            return jsonify({'status': 'completed', 'message': result})
        elif task:
            return jsonify({'status': 'pending'})
        else:
            return jsonify({'status': 'not_found'}), 404

    except Exception as e:
        logger.error(f"❌ Error in /check_task: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'サーバーエラー'}), 500
    finally:
        session.close()

# --- アプリケーション初期化 ---
def initialize_app():
    global engine, Session
    logger.info("="*30 + " INITIALIZING MOCHIKO AI " + "="*30)
    initialize_gemini_client()
    initialize_groq_client()
    try:
        engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False} if 'sqlite' in DATABASE_URL else {})
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        logger.info("✅ Database initialized successfully.")
    except Exception as e:
        logger.critical(f"🔥 Database initialization failed: {e}")
        sys.exit(1)
    logger.info("="*30 + " INITIALIZATION COMPLETE " + "="*30)

# --- メイン実行 ---
if __name__ == '__main__':
    initialize_app()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
else:
    # for production server (gunicorn)
    initialize_app()
