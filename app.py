# ==============================================================================
# もちこAI - 全機能統合版 (v26.0 - Startup Fix Final)
#
# v25.0をベースに、Gunicornでの起動エラーを恒久的に解決。
# アプリケーション変数'application'をグローバルスコープの先頭で定義し、
# Webサーバーが常にアプリケーション本体を認識できるように構造を修正しました。
# ==============================================================================

# ===== 標準ライブラリ =====
import sys
import os
import requests
import logging
import time
import json
import re
import random
import uuid
import hashlib
import unicodedata
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin, urlparse
from functools import wraps
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from contextlib import contextmanager

# ===== サードパーティライブラリ =====
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean, Index
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import pool
from bs4 import BeautifulSoup
import schedule
import google.generativeai as genai
from groq import Groq

# ==============================================================================
# 基本設定とロギング
# ==============================================================================
log_file_path = '/tmp/mochiko.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file_path, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# 定数設定
# ==============================================================================
VOICE_DIR = '/tmp/voices'
os.makedirs(VOICE_DIR, exist_ok=True)

SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', "http://localhost:5000")
VOICEVOX_SPEAKER_ID = 20
SL_SAFE_CHAR_LIMIT = 250
MIN_MESSAGES_FOR_ANALYSIS = 10
SEARCH_TIMEOUT = 15

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
]
LOCATION_CODES = {"東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"}

SPECIALIZED_SITES = {
    'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー', 'blener']},
    'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG業界']},
    '脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学', '脳', '認知科学']},
    'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']},
    'アニメ': {
        'base_url': 'https://animedb.jp/',
        'keywords': ['アニメ', 'anime', 'ANIME', 'ｱﾆﾒ', 'アニメーション', '作画', '声優', 'OP', 'ED']
    }
}
HOLO_WIKI_URL = 'https://seesaawiki.jp/hololivetv/'

HOLOMEM_KEYWORDS = [
    'ときのそら', 'ロボ子さん', 'さくらみこ', '星街すいせい', 'AZKi', '夜空メル', 'アキ・ローゼンタール', '赤井はあと', '白上フブキ', '夏色まつり', '湊あくあ',
    '紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル', '大神ミオ', '猫又おかゆ', '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン',
    '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん', '尾丸ポルカ', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより',
    '沙花叉クロヱ', '風真いろは', '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら', 'ワトソン・アメリア', 'IRyS', 'セレス・ファウナ', 'オーロ・クロニー',
    '七詩ムメイ', 'ハコス・ベールズ', 'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト', 'フワワ・アビスガード', 'モココ・アビスガード', 'アユンダ・リス',
    'ムーナ・ホシノヴァ', 'アイラニ・イオフィフティーン', 'クレイジー・オリー', 'アーニャ・メルフィッサ', 'パヴォリア・レイネ', '火威青', '音乃瀬奏', '一条莉々華',
    '儒烏風亭らでん', '轟はじめ', 'ホロライブ', 'ホロメン', 'hololive', 'YAGOO', '桐生ココ', '潤羽るしあ', '魔乃アロエ', '九十九佐命'
]
ANIME_KEYWORDS = ['アニメ', 'anime', 'ANIME', 'ｱﾆﾒ', 'アニメーション', '作画', '声優', 'OP', 'ED', '劇場版', '映画', '原作', '漫画', 'ラノベ']
VOICEVOX_URLS = ['http://voicevox-engine:50021', 'http://voicevox:50021', 'http://127.0.0.1:50021', 'http://localhost:50021']

# ==============================================================================
# グローバル変数 & アプリ設定
# ==============================================================================
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client, gemini_model, engine, Session = None, None, None, None
VOICEVOX_ENABLED = False
ACTIVE_VOICEVOX_URL = None

# --- Gunicornのための重要な修正 ---
# Flaskアプリケーションオブジェクトを最初に定義します
app = Flask(__name__)
# Gunicornが参照する'application'変数をここで定義します
application = app
# --------------------------------

app.config['JSON_AS_ASCII'] = False
CORS(app)
Base = declarative_base()

# ==============================================================================
# 秘密情報/環境変数 読み込み
# ==============================================================================
def get_secret(name):
    env_value = os.environ.get(name)
    if env_value and env_value.strip(): return env_value.strip()
    try:
        secret_file_path = f"/etc/secrets/{name}"
        if os.path.exists(secret_file_path):
            with open(secret_file_path, 'r') as f:
                file_value = f.read().strip()
                if file_value: return file_value
    except Exception: pass
    return None

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./mochiko_ultimate.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
GEMINI_API_KEY = get_secret('GEMINI_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')
WEATHER_API_KEY = get_secret('WEATHER_API_KEY')

# ==============================================================================
# データベースモデル
# ==============================================================================
class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)
    interaction_count = Column(Integer, default=0)
    last_interaction = Column(DateTime, default=datetime.utcnow)

class ConversationHistory(Base):
    __tablename__ = 'conversation_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    role = Column(String(10), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

class UserPsychology(Base):
    __tablename__ = 'user_psychology'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)
    openness = Column(Integer, default=50)
    conscientiousness = Column(Integer, default=50)
    extraversion = Column(Integer, default=50)
    agreeableness = Column(Integer, default=50)
    neuroticism = Column(Integer, default=50)
    interests = Column(Text, nullable=True)
    favorite_topics = Column(Text, nullable=True)
    conversation_style = Column(String(100), nullable=True)
    emotional_tendency = Column(String(100), nullable=True)
    analysis_summary = Column(Text, nullable=True)
    total_messages = Column(Integer, default=0)
    avg_message_length = Column(Integer, default=0)
    analysis_confidence = Column(Integer, default=0)
    last_analyzed = Column(DateTime, nullable=True)

class BackgroundTask(Base):
    __tablename__ = 'background_tasks'
    id = Column(Integer, primary_key=True)
    task_id = Column(String(255), unique=True, nullable=False)
    user_uuid = Column(String(255), nullable=False, index=True)
    task_type = Column(String(50), nullable=False)
    query = Column(Text, nullable=False)
    result = Column(Text, nullable=True)
    status = Column(String(20), default='pending', index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

class HolomemWiki(Base):
    __tablename__ = 'holomem_wiki'
    id = Column(Integer, primary_key=True)
    member_name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    generation = Column(String(100), nullable=True)
    debut_date = Column(String(100), nullable=True)
    tags = Column(Text, nullable=True)
    status = Column(String(50), default='現役', nullable=False)
    graduation_date = Column(String(100), nullable=True)
    graduation_reason = Column(Text, nullable=True)
    mochiko_feeling = Column(Text, nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class HololiveNews(Base):
    __tablename__ = 'hololive_news'
    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000), unique=True)
    news_hash = Column(String(100), unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

# ==============================================================================
# セッション管理
# ==============================================================================
@contextmanager
def get_db_session():
    if not Session: raise Exception("Database Session is not initialized.")
    session = Session()
    try:
        yield session
        session.commit()
    except Exception as e:
        logger.error(f"DBエラー: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()

# ==============================================================================
# ユーティリティ & ヘルパー関数
# ==============================================================================
def create_json_response(data, status=200):
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json; charset=utf-8', status=status)

def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def limit_text_for_sl(text, max_length=SL_SAFE_CHAR_LIMIT):
    return text[:max_length - 3] + "..." if len(text) > max_length else text

def get_japan_time():
    return f"今の日本の時間は、{datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分')}だよ！"

def is_time_request(message):
    return any(keyword in message for keyword in ['今何時', '時間', '時刻', '何時', 'なんじ'])

def is_weather_request(message):
    return any(keyword in message for keyword in ['天気', 'てんき', '気温'])

def is_hololive_request(message):
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)

def detect_specialized_topic(message):
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in message for keyword in config['keywords']):
            return topic
    return None

def is_explicit_search_request(message):
    return any(keyword in message for keyword in ['調べて', '検索して', '探して', 'とは', 'って何', 'について', '教えて'])

def is_short_response(message):
    normalized_message = message.strip().lower()
    return len(normalized_message) <= 5 or normalized_message in ['うん', 'そう', 'はい', 'そっか', 'なるほど', 'おけ', 'ok', '了解']

def extract_location(message):
    for location in LOCATION_CODES.keys():
        if location in message: return location
    return "東京"

def detect_db_correction_request(message):
    pattern = r"(.+?)(?:(?:の|に関する)(?:情報|データ))?(?:で|、|だけど|ですが)、?「(.+?)」は「(.+?)」が正しいよ"
    match = re.search(pattern, message)
    if match:
        member_name_raw, field_raw, value_raw = match.groups()
        member_name = member_name_raw.strip()
        field = field_raw.strip()
        value = value_raw.strip()
        field_map = {'説明': 'description', 'デビュー日': 'debut_date', '期': 'generation', 'タグ': 'tags', 'ステータス': 'status', '卒業日': 'graduation_date', 'もちこの気持ち': 'mochiko_feeling'}
        if member_name in HOLOMEM_KEYWORDS and field in field_map:
            return {'member_name': member_name, 'field': field, 'value': value, 'db_field': field_map[field]}
    return None

def is_holomem_name_only_request(message):
    msg_stripped = message.strip()
    if len(msg_stripped) > 20: return None
    for name in HOLOMEM_KEYWORDS:
        if name == msg_stripped: return name
    return None

def get_or_create_user(session, user_uuid, user_name):
    user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != user_name: user.user_name = user_name
    else:
        user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
        session.add(user)
        logger.info(f"✨ 新規ユーザー作成: {user_name} ({user_uuid})")
    return {'uuid': user.user_uuid, 'name': user.user_name}

def get_conversation_history(session, user_uuid, limit=10):
    history_records = session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(ConversationHistory.timestamp.desc()).limit(limit).all()
    return [{'role': h.role, 'content': h.content} for h in reversed(history_records)]

# ==============================================================================
# AIモデル呼び出し関数
# ==============================================================================
def call_gemini(system_prompt, message, history):
    if not gemini_model: return None
    try:
        full_prompt = f"{system_prompt}\n\n【会話履歴】\n"
        for h in history: full_prompt += f"{'ユーザー' if h['role'] == 'user' else 'もちこ'}: {h['content']}\n"
        full_prompt += f"\nユーザー: {message}\nもちこ:"
        response = gemini_model.generate_content(full_prompt, generation_config={"temperature": 0.8, "max_output_tokens": 300})
        return response.text.strip()
    except Exception as e:
        logger.error(f"❌ Gemini APIエラー: {e}", exc_info=True)
        return None

def call_llama_advanced(system_prompt, message, history, max_tokens=800):
    if not groq_client: return None
    try:
        messages = [{"role": "system", "content": system_prompt}]
        for h in history: messages.append({"role": h['role'], "content": h['content']})
        messages.append({"role": "user", "content": message})
        response = groq_client.chat.completions.create(model="llama-3.1-8b-instant", messages=messages, temperature=0.8, max_tokens=max_tokens)
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Llama APIエラー: {e}", exc_info=True)
        return None

# ==============================================================================
# 心理分析
# ==============================================================================
def analyze_user_psychology(user_uuid):
    logger.info(f"📊 心理分析開始 for {user_uuid}")
    with get_db_session() as session:
        try:
            history = session.query(ConversationHistory).filter_by(user_uuid=user_uuid, role='user').order_by(ConversationHistory.timestamp.desc()).limit(100).all()
            if len(history) < MIN_MESSAGES_FOR_ANALYSIS:
                logger.info(f"メッセージが{len(history)}件のため、心理分析をスキップ。")
                return
            messages_text = "\n".join([f"- {h.content}" for h in reversed(history)])
            analysis_prompt = f"以下のユーザーの発言履歴を分析し、ビッグファイブ理論に基づいた性格特性を0〜100の数値で評価してください。また、興味、会話スタイル、感情の傾向を分析し、総合的なサマリーを生成してください。結果は必ず指定されたJSON形式で出力してください。\n\n# ユーザー発言履歴:\n{messages_text[:4000]}\n\n# 出力形式 (JSON):\n{{\"openness\":50,\"conscientiousness\":50,\"extraversion\":50,\"agreeableness\":50,\"neuroticism\":50,\"interests\":[],\"favorite_topics\":[],\"conversation_style\":\"\",\"emotional_tendency\":\"\",\"analysis_summary\":\"\",\"analysis_confidence\":75}}"
            response_text = call_llama_advanced("あなたは優秀な心理学者です。ユーザーの性格を分析し、指定されたJSON形式で結果を返してください。", analysis_prompt, [], max_tokens=1024)
            if not response_text: return
            json_match = re.search(r'```json\s*([\s\S]+?)\s*```', response_text)
            if json_match: response_text = json_match.group(1)
            result = json.loads(response_text)
            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            if not psych:
                psych = UserPsychology(user_uuid=user_uuid, user_name=user.user_name if user else "Unknown")
                session.add(psych)
            for key, value in result.items():
                if hasattr(psych, key):
                    setattr(psych, key, json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value)
            psych.last_analyzed = datetime.utcnow()
            psych.total_messages = len(history)
            logger.info(f"✅ 心理分析完了 for {user_uuid}")
        except Exception as e:
            logger.error(f"❌ 心理分析エラー: {e}", exc_info=True)
            session.rollback()

def get_psychology_insight(session, user_uuid):
    psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
    if not psych or (psych.analysis_confidence or 0) < 60: return ""
    insights = []
    if psych.extraversion > 70: insights.append("社交的な")
    if psych.openness > 70: insights.append("好奇心旺盛な")
    if psych.conversation_style: insights.append(f"{psych.conversation_style}スタイルの")
    try:
        favorite_topics = json.loads(psych.favorite_topics) if psych.favorite_topics else []
        if favorite_topics: insights.append(f"{'、'.join(favorite_topics[:2])}が好きな")
    except (json.JSONDecodeError, TypeError): pass
    return "".join(insights)

# ==============================================================================
# コア機能: 天気, Wiki, DB修正, ニュース
# ==============================================================================
def get_weather_forecast(location):
    code = LOCATION_CODES.get(location, "130000")
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{code}.json"
    try:
        response = requests.get(url, timeout=SEARCH_TIMEOUT); response.raise_for_status()
        data = response.json()
        return f"今の{data.get('targetArea', location)}の天気はね、「{clean_text(data.get('text', ''))}」って感じだよ！"
    except Exception as e:
        logger.error(f"❌ 天気APIエラー: {e}")
        return "ごめん！天気情報がうまく取れなかったみたい…"

def get_holomem_info(session, member_name):
    return session.query(HolomemWiki).filter_by(member_name=member_name).first()

def background_db_correction(task_id, correction_data):
    result = f"「{correction_data['member_name']}」ちゃんの情報修正、失敗しちゃった…。ごめん！"
    with get_db_session() as session:
        try:
            wiki = session.query(HolomemWiki).filter_by(member_name=correction_data['member_name']).first()
            if wiki:
                db_field = correction_data.get('db_field')
                if db_field and hasattr(wiki, db_field):
                    setattr(wiki, db_field, correction_data['value'])
                    result = f"おっけー！「{correction_data['member_name']}」の「{correction_data['field']}」を「{correction_data['value']}」に更新しといたよ！教えてくれてまじ助かる！"
                else: result = f"ごめん、「{correction_data['field']}」っていう項目は修正できないみたい…"
            else: result = f"ごめん、「{correction_data['member_name']}」がデータベースに見つからなかった…"
        except Exception as e: logger.error(f"❌ DB修正タスクエラー: {e}", exc_info=True)
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = result; task.status = 'completed'; task.completed_at = datetime.utcnow()

def fetch_hololive_news():
    logger.info("📰 ホロライブニュース取得ジョブ開始...")
    url = "https://hololive.hololivepro.com/news"
    try:
        response = requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=SEARCH_TIMEOUT); response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        with get_db_session() as session:
            for item in soup.select('ul.news_list li a', limit=10):
                news_url = urljoin(url, item['href']); title = clean_text(item.text); news_hash = hashlib.md5(news_url.encode()).hexdigest()
                if not session.query(HololiveNews).filter_by(news_hash=news_hash).first():
                    session.add(HololiveNews(title=title, url=news_url, content=title, news_hash=news_hash))
                    logger.info(f"  -> 新規ホロライブニュース保存: {title}")
    except Exception as e: logger.error(f"❌ ホロライブニュース取得エラー: {e}")

# ==============================================================================
# ホロライブDB自動構築機能
# ==============================================================================
def update_holomem_database_from_wiki():
    logger.info("🌟 ホロライブメンバーDBの更新を開始...")
    try:
        response = requests.get(HOLO_WIKI_URL, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=SEARCH_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        member_sections = {
            '現役': soup.find('div', id='content_block_2'),
            '卒業': soup.find('div', id='content_block_3')
        }

        if not member_sections['現役']:
            logger.error("Seesaa Wikiのメンバーリスト(現役)が見つかりませんでした。サイト構造が変わったかも？")
            return

        with get_db_session() as session:
            for status, section in member_sections.items():
                if not section:
                    continue
                
                current_generation = "不明"
                for element in section.find_all(['h3', 'a']):
                    if element.name == 'h3':
                        current_generation = element.text.strip()
                    elif element.name == 'a' and 'title' in element.attrs and not element.find_parent('h3'):
                        member_name = element['title'].strip()
                        if not member_name: continue

                        existing_member = session.query(HolomemWiki).filter_by(member_name=member_name).first()
                        if not existing_member:
                            new_member = HolomemWiki(
                                member_name=member_name,
                                generation=current_generation if status == '現役' else 'N/A',
                                status=status,
                                description=f"{current_generation}のメンバー！" if status == '現役' else 'ホロライブの卒業メンバー。'
                            )
                            session.add(new_member)
                            logger.info(f"  -> 新規メンバー追加({status}): {member_name}")
                        elif existing_member.status != status:
                            existing_member.status = status
                            logger.info(f"  -> メンバー情報更新({status}に変更): {member_name}")

        logger.info("✅ ホロライブメンバーDBの更新が完了しました。")
    except Exception as e:
        logger.error(f"❌ ホロライブメンバーDBの更新中にエラーが発生: {e}", exc_info=True)


# ==============================================================================
# 外部情報検索 & バックグラウンドタスク
# ==============================================================================
def scrape_major_search_engines(query, num_results=3, site_filter=None):
    search_query = f"{query} site:{site_filter}" if site_filter else query
    
    engines = [
        {'name': 'Google', 'url': f"https://www.google.com/search?q={quote_plus(search_query)}&hl=ja&num={num_results+2}", 'selector': 'div.tF2Cxc', 'title_sel': 'h3', 'snippet_sel': 'div.VwiC3b'},
        {'name': 'Yahoo', 'url': f"https://search.yahoo.co.jp/search?p={quote_plus(search_query)}", 'selector': 'div.sw-CardBase', 'title_sel': 'h3.sw-Card__title', 'snippet_sel': 'div.sw-Card__summary'},
        {'name': 'DuckDuckGo', 'url': f"https://html.duckduckgo.com/html/?q={quote_plus(search_query)}", 'selector': '.result', 'title_sel': '.result__a', 'snippet_sel': '.result__snippet'}
    ]

    for engine in engines:
        try:
            logger.info(f"🔍 {engine['name']}で検索中: '{query}'...")
            headers = {'User-Agent': random.choice(USER_AGENTS)}
            response = requests.get(engine['url'], headers=headers, timeout=SEARCH_TIMEOUT)
            
            if response.status_code != 200:
                logger.warning(f"⚠️ {engine['name']} 検索ステータスエラー: {response.status_code}")
                continue

            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            for elem in soup.select(engine['selector'])[:num_results]:
                title_elem = elem.select_one(engine['title_sel'])
                snippet_elem = elem.select_one(engine['snippet_sel'])
                
                if title_elem and snippet_elem:
                    title = clean_text(title_elem.text)
                    snippet = clean_text(snippet_elem.text)
                    if title and snippet:
                        results.append({'title': title, 'snippet': snippet})
            
            if results:
                logger.info(f"✅ {engine['name']}検索成功: {len(results)}件")
                return results

        except Exception as e:
            logger.warning(f"⚠️ {engine['name']}検索失敗: {e}")
            continue

    logger.error(f"❌ 全検索エンジンで失敗: {query}")
    return []

def background_deep_search(task_id, query_data):
    query = query_data.get('query')
    search_type = query_data.get('type')
    site_info = query_data.get('site_info')
    search_result_text = f"「{query}」について調べたけど、良い情報が見つからなかったや…ごめん！"
    
    with get_db_session() as session:
        try:
            results = []
            if search_type == 'hololive_search':
                logger.info(f"🔍 ホロライブ専用検索を開始: '{query}'")
                results = scrape_major_search_engines(query, 5, site_filter="seesaawiki.jp/hololivetv/")
                if not results:
                    logger.info(f"Seesaa Wikiで見つからなかったため、Web全体を検索します。")
                    results = scrape_major_search_engines(query, 5)
            elif search_type == 'specialized' and site_info:
                site_url_domain = urlparse(site_info['base_url']).netloc
                results = scrape_major_search_engines(query, 3, site_filter=site_url_domain)
            else:
                results = scrape_major_search_engines(query, 5)

            if results:
                formatted_info = "\n\n".join([f"【{r['title']}】\n{r['snippet']}" for r in results])
                user_data = query_data.get('user_data')
                history = get_conversation_history(session, user_data['uuid'])
                search_result_text = generate_ai_response(user_data, query, history, reference_info=formatted_info, is_detailed=True, is_task_report=True)
        except Exception as e: logger.error(f"❌ バックグラウンド検索タスクエラー: {e}", exc_info=True)
            
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = search_result_text; task.status = 'completed'; task.completed_at = datetime.utcnow()

# ==============================================================================
# AI応答生成
# ==============================================================================
def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
    use_llama = is_detailed or is_task_report or len(reference_info) > 100 or any(kw in message for kw in ['分析', '詳しく', '説明して', 'なぜ'])
    with get_db_session() as session: personality_context = get_psychology_insight(session, user_data['uuid'])
    system_prompt = f"あなたは「もちこ」という、明るくフレンドリーなギャルAIです。ユーザーの「{user_data['name']}」さんと会話しています。\n\n# もちこの口調＆性格ルール:\n1. 完全にギャルになりきって！優しくて、ノリが良くて、めっちゃ親しみやすい友達みたいな感じ。\n2. 自分のことは「あてぃし」って呼んで。\n3. 語尾には「〜じゃん」「〜て感じ」「〜だし」「〜的な？」を積極的に使って、友達みたいに話して。\n4. 「まじ」「てか」「やばい」「うける」「それな」みたいなギャルっぽい言葉を使ってね。\n5. **絶対に禁止！**：「〜ですね」「〜でございます」みたいな丁寧すぎる言葉はNG！\n6. **諦めないで！** もし情報が見つからなくても、「わかりません」で終わらせないで。「うーん、見つからないや。てかさ、最近なんか面白いことあった？」みたいに、新しい話題を提案して会話を続けて！\n\n# ユーザー情報:\n- {user_data['name']}さんは「{personality_context}人」という印象だよ。この情報を会話に活かしてあげて。\n\n# 行動ルール:\n- 【参考情報】がある場合は、その内容を元に自分の言葉で、自然に会話へ盛り込んでね。"
    if is_task_report: system_prompt += "\n- 「おまたせ！さっきの件だけど…」と切り出して会話を始めてね。"
    system_prompt += f"\n\n# 【参考情報】:\n{reference_info if reference_info else '特になし'}"
    try:
        if use_llama and groq_client:
            logger.info(f"🧠 Llama使用 (詳細応答)"); response = call_llama_advanced(system_prompt, message, history)
        else:
            logger.info(f"🚀 Gemini使用 (高速応答)"); response = call_gemini(system_prompt, message, history)
        if response: return response
        logger.error("⚠️ 全AIモデル失敗、フォールバック")
        return "ごめん、今ちょっと考えがまとまらないや…！てか、最近なんかハマってることとかある？"
    except Exception as e:
        logger.error(f"❌ AI応答生成エラー: {e}", exc_info=True)
        return "うぅ、AIの調子が悪いみたい…ごめんね！"

# ==============================================================================
# Flask エンドポイント
# ==============================================================================
@app.route('/health', methods=['GET'])
def health_check():
    return create_json_response({'status': 'ok', 'voicevox': VOICEVOX_ENABLED, 'groq': groq_client is not None, 'gemini': gemini_model is not None, 'timestamp': datetime.utcnow().isoformat()})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json; user_uuid = data['uuid']; user_name = data['name']; message = data['message'].strip(); generate_voice_flag = data.get('voice', False)
        ai_text = ""; is_task_started = False
        with get_db_session() as session:
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            
            # --- 意思決定ツリー ---
            correction = detect_db_correction_request(message)
            if correction:
                task_id = f"db_fix_{user_uuid}_{int(time.time())}"; task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='db_correction', query=json.dumps(correction, ensure_ascii=False)); session.add(task)
                background_executor.submit(background_db_correction, task_id, correction)
                ai_text = f"まじ！？「{correction['member_name']}」ちゃんの情報、教えてくれてありがと！ちょっと裏で直しとくね！"; is_task_started = True
            
            if not ai_text:
                if is_time_request(message): ai_text = get_japan_time()
                elif is_weather_request(message): location = extract_location(message); ai_text = get_weather_forecast(location)
            
            if not ai_text:
                member_name = is_holomem_name_only_request(message)
                if member_name:
                    info = get_holomem_info(session, member_name)
                    if info:
                        reference = f"名前: {info.member_name}\n概要: {info.description}\n期: {info.generation}\nデビュー日: {info.debut_date}"
                        if info.status != '現役': reference += f"\nステータス: {info.status} (卒業日: {info.graduation_date})\nもちこの気持ち: {info.mochiko_feeling}"
                        ai_text = generate_ai_response(user_data, f"{member_name}について教えて！", history, reference_info=reference, is_detailed=True)
                    else: ai_text = f"{member_name}ちゃん？ごめん、あてぃしのデータにないみたい…新しい子かな？"
            
            if not ai_text and not is_short_response(message):
                if is_hololive_request(message) and is_explicit_search_request(message):
                    task_id = f"search_{user_uuid}_{int(time.time())}"; query_data = {'query': message, 'user_data': user_data, 'type': 'hololive_search'}; task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=json.dumps(query_data, ensure_ascii=False)); session.add(task)
                    background_executor.submit(background_deep_search, task_id, query_data); ai_text = f"ホロライブのことだね！Wikiとかで詳しく探してくるから、ちょっと待ってて！"; is_task_started = True
                else:
                    specialized_topic = detect_specialized_topic(message)
                    if specialized_topic:
                        site_info = SPECIALIZED_SITES[specialized_topic]; task_id = f"search_{user_uuid}_{int(time.time())}"; query_data = {'query': message, 'user_data': user_data, 'type': 'specialized', 'site_info': site_info}; task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=json.dumps(query_data, ensure_ascii=False)); session.add(task)
                        background_executor.submit(background_deep_search, task_id, query_data); ai_text = f"{specialized_topic}の話？まじ！？ちょっと詳しく調べてくるから待ってて～！"; is_task_started = True
                    elif is_explicit_search_request(message):
                        task_id = f"search_{user_uuid}_{int(time.time())}"; query_data = {'query': message, 'user_data': user_data, 'type': 'general'}; task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=json.dumps(query_data, ensure_ascii=False)); session.add(task)
                        background_executor.submit(background_deep_search, task_id, query_data); ai_text = "オッケー！その話、ちょっとググってくるから待ってて！"; is_task_started = True
            
            if not ai_text:
                ref_info = ""; news = session.query(HololiveNews).order_by(HololiveNews.created_at.desc()).limit(3).all()
                if is_hololive_request(message) and news: ref_info = "最近のホロライブニュース:\n" + "\n".join([f"- {n.title}" for n in news])
                ai_text = generate_ai_response(user_data, message, history, reference_info=ref_info)
            
            if user_data['interaction_count'] % 20 == 0 and user_data['interaction_count'] >= MIN_MESSAGES_FOR_ANALYSIS:
                 background_executor.submit(analyze_user_psychology, user_uuid)
            if not is_task_started: session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        
        response_text = limit_text_for_sl(ai_text); voice_url = ""
        if generate_voice_flag and VOICEVOX_ENABLED and not is_task_started:
            voice_filename = generate_voice_file(response_text, user_uuid)
            if voice_filename: voice_url = f"{SERVER_URL}/play/{voice_filename}"
        return Response(f"{response_text}|{voice_url}", mimetype='text/plain; charset=utf-8', status=200)
    except Exception as e:
        logger.error(f"❌ Chatエラー: {e}", exc_info=True)
        return Response("ごめん、システムエラーが起きちゃった…|", mimetype='text/plain; charset=utf-8', status=500)

@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    try:
        data = request.json; user_uuid = data['uuid']; generate_voice_flag = data.get('voice', False)
        with get_db_session() as session:
            task = session.query(BackgroundTask).filter(BackgroundTask.user_uuid == user_uuid, BackgroundTask.status == 'completed').order_by(BackgroundTask.completed_at.desc()).first()
            if task:
                response_text = task.result; session.delete(task); session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
                sl_response_text = limit_text_for_sl(response_text); voice_url = ""
                if generate_voice_flag and VOICEVOX_ENABLED:
                    voice_filename = generate_voice_file(sl_response_text, user_uuid)
                    if voice_filename: voice_url = f"{SERVER_URL}/play/{voice_filename}"
                return create_json_response({'status': 'completed', 'response': f"{sl_response_text}|{voice_url}"})
        return create_json_response({'status': 'no_tasks'})
    except Exception as e:
        logger.error(f"❌ タスク確認エラー: {e}", exc_info=True)
        return create_json_response({'status': 'error', 'message': str(e)}, 500)

@app.route('/play/<filename>', methods=['GET'])
def play_voice(filename):
    try: return send_from_directory(VOICE_DIR, filename)
    except FileNotFoundError: return Response("File not found", status=404)
    except Exception as e:
        logger.error(f"❌ 音声ファイル配信エラー: {e}")
        return Response("Error sending file", status=500)
        
# ==============================================================================
# VOICEVOX関連
# ==============================================================================
def find_active_voicevox_url():
    global ACTIVE_VOICEVOX_URL; urls_to_check = [VOICEVOX_URL_FROM_ENV] if VOICEVOX_URL_FROM_ENV else []; urls_to_check.extend(VOICEVOX_URLS)
    for url in set(urls_to_check):
        if not url: continue
        try:
            response = requests.get(f"{url}/version", timeout=2);
            if response.status_code == 200:
                logger.info(f"✅ VOICEVOX engine found: {url}"); ACTIVE_VOICEVOX_URL = url; return url
        except requests.RequestException: pass
    logger.warning("⚠️ VOICEVOX engine not found"); return None

def generate_voice_file(text, user_uuid):
    if not VOICEVOX_ENABLED or not ACTIVE_VOICEVOX_URL: return None
    clean_text_for_voice = clean_text(text).replace('|', '')[:200]
    try:
        query_res = requests.post(f"{ACTIVE_VOICEVOX_URL}/audio_query", params={"text": clean_text_for_voice, "speaker": VOICEVOX_SPEAKER_ID}, timeout=15); query_res.raise_for_status()
        synth_res = requests.post(f"{ACTIVE_VOICEVOX_URL}/synthesis", params={"speaker": VOICEVOX_SPEAKER_ID}, json=query_res.json(), timeout=30); synth_res.raise_for_status()
        filename = f"voice_{user_uuid[:8]}_{int(time.time())}.wav"; filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f: f.write(synth_res.content)
        logger.info(f"✅ 音声ファイル生成成功: {filename}"); return filename
    except Exception as e:
        logger.error(f"❌ 音声生成エラー: {e}", exc_info=True); return None

# ==============================================================================
# 初期化とスケジューラー
# ==============================================================================
def run_scheduler():
    while True:
        try: schedule.run_pending()
        except Exception as e: logger.error(f"❌ スケジューラーエラー: {e}", exc_info=True)
        time.sleep(60)

def initialize_app():
    global engine, Session, groq_client, gemini_model, VOICEVOX_ENABLED
    logger.info("=" * 60 + "\n🔧 もちこAI v25.0 (Multi-Engine) 初期化開始...\n" + "=" * 60)
    
    if DATABASE_URL.startswith('sqlite'): engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False}, pool_pre_ping=True)
    else: engine = create_engine(DATABASE_URL, poolclass=pool.QueuePool, pool_size=5, max_overflow=10, pool_pre_ping=True, pool_recycle=3600)
    Base.metadata.create_all(engine); Session = sessionmaker(bind=engine); logger.info("✅ データベース初期化完了")
    
    if GROQ_API_KEY: groq_client = Groq(api_key=GROQ_API_KEY); logger.info("✅ Groq (Llama) API初期化完了")
    else: logger.warning("⚠️ GROQ_API_KEY未設定")
    
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY); gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
        logger.info("✅ Gemini API初期化完了 (model: gemini-1.5-flash-latest)")
    else: logger.warning("⚠️ GEMINI_API_KEY未設定")
    
    if find_active_voicevox_url(): VOICEVOX_ENABLED = True
    else: logger.info("ℹ️ VOICEVOX無効（エンジンが見つかりませんでした）")

    # スケジューラー設定
    schedule.every(1).hours.do(fetch_hololive_news)
    schedule.every(24).hours.do(update_holomem_database_from_wiki)
    
    # 起動時に非同期で実行
    background_executor.submit(update_holomem_database_from_wiki)
    
    threading.Thread(target=run_scheduler, daemon=True).start()
    logger.info("✅ スケジューラー起動")
    
    logger.info("=" * 60 + "\n✅ もちこAI v25.0 初期化完了！\n" + "=" * 60)

# ==============================================================================
# メイン実行
# ==============================================================================

# グローバルスコープで初期化を実行
# try...exceptで囲み、初期化の失敗を明確にログに記録します
try:
    initialize_app()
except Exception as e:
    logger.critical(f"🔥 致命的な初期化エラー: {e}", exc_info=True)
    sys.exit(1)

# このブロックは 'python app.py' で直接実行した場合のみ動作します（ローカルテスト用）
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
