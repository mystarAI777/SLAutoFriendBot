# ==============================================================================
# もちこAI - 究極の全機能統合版 (v19.2 - 複数エンジン統合要約版)
#
# v19.1をベースに、Web検索機能を「複数の検索エンジンから情報を収集し、
# AIが統合して要約する」という、より高度で堅牢な方式にアップグレードした最終版。
# ==============================================================================

# ===== ライブラリのインポート =====
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
from urllib.parse import quote_plus, urljoin
from functools import wraps
from threading import Lock

# --- サードパーティライブラリ ---
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import schedule
import signal
from groq import Groq

# ==============================================================================
# 基本設定とロギング
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==============================================================================
# 定数設定
# ==============================================================================
VOICE_DIR = '/tmp/voices'
SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', "http://localhost:5000")
background_executor = ThreadPoolExecutor(max_workers=5)
VOICEVOX_SPEAKER_ID = 20
HOLOLIVE_NEWS_URL = "https://hololive-tsuushin.com/category/holonews/"
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
]
LOCATION_CODES = { "東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000" }
SPECIALIZED_SITES = {
    'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー']},
    'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG', 'CG業界']},
    '脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学', '脳', '認知科学']},
    'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']},
}
HOLOMEM_KEYWORDS = [
    'ときのそら', 'ロボ子さん', 'さくらみこ', '星街すいせい', 'AZKi', '夜空メル', 'アキ・ローゼンタール', '赤井はあと', '白上フブキ', '夏色まつり', '湊あくあ', '紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル', '大神ミオ', '猫又おかゆ', '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン', '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん', '尾丸ポルカ', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは', '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら', 'ワトソン・アメリア', 'IRyS', 'セレス・ファウナ', 'オーロ・クロニー', '七詩ムメイ', 'ハコス・ベールズ', 'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト', 'フワワ・アビスガード', 'モココ・アビスガード', 'アユンダ・リス', 'ムーナ・ホシノヴァ', 'アイラニ・イオフィフティーン', 'クレイジー・オリー', 'アーニャ・メルフィッサ', 'パヴォリア・レイネ', '火威青', '音乃瀬奏', '一条莉々華', '儒烏風亭らでん', '轟はじめ', 'ホロライブ', 'ホロメン', 'hololive', 'YAGOO', '桐生ココ', '潤羽るしあ', '魔乃アロエ', '九十九佐命'
]

# ==============================================================================
# 秘密情報/環境変数 読み込み
# ==============================================================================
def get_secret(name):
    secret_file_path = f"/etc/secrets/{name}"
    if os.path.exists(secret_file_path):
        try:
            with open(secret_file_path, 'r') as f:
                return f.read().strip()
        except IOError: pass
    return os.environ.get(name)

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./mochiko.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# ==============================================================================
# AIクライアントとグローバル変数
# ==============================================================================
groq_client = None
VOICEVOX_ENABLED = True if VOICEVOX_URL_FROM_ENV else False
search_context_cache = {}
cache_lock = Lock()

# ==============================================================================
# Flask & データベース初期化
# ==============================================================================
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
CORS(app)

def create_db_engine_with_retry(max_retries=5, retry_delay=5):
    from sqlalchemy.exc import OperationalError
    for attempt in range(max_retries):
        try:
            connect_args = {'check_same_thread': False} if 'sqlite' in DATABASE_URL else {'connect_timeout': 10}
            engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300, connect_args=connect_args)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except OperationalError as e:
            if attempt < max_retries - 1:
                logger.warning(f"⚠️ DB接続失敗: {e}. {retry_delay}秒後にリトライ...")
                time.sleep(retry_delay)
            else:
                raise
        except Exception as e:
            raise

engine = create_db_engine_with_retry()
Base = declarative_base()

# ==============================================================================
# データベースモデル
# ==============================================================================
class UserMemory(Base): __tablename__ = 'user_memories'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False); user_name = Column(String(255), nullable=False); interaction_count = Column(Integer, default=0); last_interaction = Column(DateTime, default=datetime.utcnow)
class ConversationHistory(Base): __tablename__ = 'conversation_history'; id = Column(Integer, primary_key=True, autoincrement=True); user_uuid = Column(String(255), nullable=False, index=True); role = Column(String(10), nullable=False); content = Column(Text, nullable=False); timestamp = Column(DateTime, default=datetime.utcnow, index=True)
class HololiveNews(Base): __tablename__ = 'hololive_news'; id = Column(Integer, primary_key=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class SpecializedNews(Base): __tablename__ = 'specialized_news'; id = Column(Integer, primary_key=True); site_name = Column(String(100), nullable=False, index=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class BackgroundTask(Base): __tablename__ = 'background_tasks'; id = Column(Integer, primary_key=True); task_id = Column(String(255), unique=True, nullable=False); user_uuid = Column(String(255), nullable=False); task_type = Column(String(50), nullable=False); query = Column(Text, nullable=False); result = Column(Text); status = Column(String(20), default='pending'); created_at = Column(DateTime, default=datetime.utcnow); completed_at = Column(DateTime)
class HolomemWiki(Base):
    __tablename__ = 'holomem_wiki'
    id = Column(Integer, primary_key=True); member_name = Column(String(100), nullable=False, unique=True, index=True); description = Column(Text); debut_date = Column(String(100)); generation = Column(String(100)); tags = Column(Text)
    status = Column(String(50), default='現役', nullable=False); graduation_date = Column(String(100), nullable=True); graduation_reason = Column(Text, nullable=True); mochiko_feeling = Column(Text, nullable=True); last_updated = Column(DateTime, default=datetime.utcnow)
class FriendRegistration(Base): __tablename__ = 'friend_registrations'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), nullable=False, index=True); friend_uuid = Column(String(255), nullable=False); friend_name = Column(String(255), nullable=False); registered_at = Column(DateTime, default=datetime.utcnow); relationship_note = Column(Text)
class UserPsychology(Base):
    __tablename__ = 'user_psychology'
    id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False, index=True); user_name = Column(String(255), nullable=False)
    openness = Column(Integer, default=50); conscientiousness = Column(Integer, default=50); extraversion = Column(Integer, default=50); agreeableness = Column(Integer, default=50); neuroticism = Column(Integer, default=50)
    interests = Column(Text); favorite_topics = Column(Text); conversation_style = Column(String(100)); emotional_tendency = Column(String(100)); analysis_summary = Column(Text)
    total_messages = Column(Integer, default=0); avg_message_length = Column(Integer, default=0); analysis_confidence = Column(Integer, default=0); last_analyzed = Column(DateTime)
class NewsCache(Base): __tablename__ = 'news_cache'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), nullable=False, index=True); news_id = Column(Integer, nullable=False); news_number = Column(Integer, nullable=False); news_type = Column(String(50), nullable=False); created_at = Column(DateTime, default=datetime.utcnow)
class UserContext(Base):
    __tablename__ = 'user_context'
    id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False, index=True); last_context_type = Column(String(50), nullable=False); last_query = Column(Text, nullable=True); updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ==============================================================================
# ユーティリティ & ヘルパー関数
# ==============================================================================
def create_json_response(data, status=200): return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json; charset=utf-8', status=status)
def clean_text(text): return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text or "")).strip()
def get_japan_time(): return f"今は{datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分')}だよ！"
def create_news_hash(title, content): return hashlib.md5(f"{title}{content[:100]}".encode('utf-8')).hexdigest()

def is_what_is_request(message):
    match = re.search(r'(.+?)\s*(?:とは|って何|ってなに)\??$', message.strip())
    if match: return match.group(1).strip()
    return None
def is_time_request(message): return any(keyword in message for keyword in ['今何時', '時間', '時刻'])
def is_weather_request(message): return any(keyword in message for keyword in ['天気予報', '明日の天気は？', '今日の天気は？'])
def is_hololive_news_request(message): return 'ホロライブ' in message and any(kw in message for kw in ['ニュース', '最新', '情報'])
def is_friend_request(message): return any(fk in message for fk in ['友だち', '友達']) and any(ak in message for ak in ['登録', '誰', 'リスト'])
def is_anime_request(message): return any(kw in message for kw in ['アニメ', 'anime', 'あにめ'])
def detect_specialized_topic(message):
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in message for keyword in config['keywords']) and any(kw in message for kw in ['ニュース', '最新', '情報']):
            return topic
    return None
def is_explicit_search_request(message): return any(keyword in message for keyword in ['調べて', '検索して', '探して'])
def should_search(message):
    if is_short_response(message) or is_explicit_search_request(message) or is_number_selection(message) or is_hololive_news_request(message) or detect_specialized_topic(message) or is_what_is_request(message): return False
    if is_anime_request(message): return True
    for member in HOLOMEM_KEYWORDS:
        if member in message and not any(kw in message for kw in ['ニュース', '最新', '情報']):
            if len(message.replace(member, '').strip()) > 5: return True
    patterns = [r'(?:について|教えて)', r'(?:誰|何|どこ|いつ|なぜ|どう)']
    return any(re.search(pattern, message) for pattern in patterns)
def is_detailed_request(message): return any(keyword in message for keyword in ['詳しく', '詳細', '教えて', '説明して'])
def is_short_response(message): return len(message.strip()) <= 3 or message.strip() in ['うん', 'そう', 'はい', 'そっか', 'なるほど']
def extract_location(message):
    for location in LOCATION_CODES.keys():
        if location in message: return location
    return "東京"
def is_number_selection(message):
    match = re.search(r'^\s*([1-9]|[１-９])\s*$', message.strip())
    if match: return int(unicodedata.normalize('NFKC', match.group(1)))
    return None
def detect_db_correction_request(message):
    pattern = r"(.+?)(?:(?:の|に関する)(?:情報|データ))?(?:で|、|だけど|ですが)、?「(.+?)」は「(.+?)」が正しいよ"
    match = re.search(pattern, message)
    if match:
        member_name_raw, field_raw, value_raw = match.groups()
        member_name = member_name_raw.strip()
        field = field_raw.strip()
        value = value_raw.strip()
        if member_name in HOLOMEM_KEYWORDS and field in ['説明', 'デビュー日', '期', 'タグ', 'ステータス', '卒業日', 'もちこの気持ち']:
            return {'member_name': member_name, 'field': field, 'value': value}
    return None
def get_sakuramiko_special_responses():
    return {
        'にぇ': 'みこちの「にぇ」、まじかわいすぎじゃん!あの独特な口癖がエリートの証なんだって〜うける!',
        'エリート': 'みこちって自称エリートVTuberなんだけど、実際は愛されポンコツって感じでさ、それがまた最高なんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!「みこち建築」って呼ばれてんの知ってる?まじ個性的!',
        'FAQ': 'みこちのFAQってさ、実は本人が答えるんじゃなくてファンが質問するコーナーなの!面白いよね〜',
        'GTA': 'みこちのGTA配信、カオスすぎて最高!警察に追われたり変なことしたり、見てて飽きないんだよね〜'
    }

# ==============================================================================
# データベースとバックグラウンドタスク管理
# ==============================================================================
def get_or_create_user(session, uuid, name):
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != name: user.user_name = name
    else:
        user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
        session.add(user)
    session.commit()
    return user
def get_conversation_history(session, uuid, limit=8):
    history = session.query(ConversationHistory).filter_by(user_uuid=uuid).order_by(ConversationHistory.timestamp.desc()).limit(limit).all()
    return list(reversed(history))
def check_completed_tasks(user_uuid):
    with Session() as session:
        task = session.query(BackgroundTask).filter(BackgroundTask.user_uuid == user_uuid, BackgroundTask.status == 'completed').order_by(BackgroundTask.completed_at.desc()).first()
        if task:
            query_data = json.loads(task.query)
            result = {'query': query_data.get('query', query_data) , 'result': task.result, 'type': task.task_type}
            session.delete(task); session.commit()
            return result
    return None
def start_background_task(user_uuid, task_type, query_data):
    task_id = str(uuid.uuid4())[:8]
    with Session() as session:
        session.add(BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type=task_type, query=json.dumps(query_data, ensure_ascii=False)))
        session.commit()
    
    if task_type == 'search':
        background_executor.submit(background_deep_search, task_id, query_data['query'])
    elif task_type == 'db_correction':
        background_executor.submit(background_db_correction, task_id, query_data)
    elif task_type == 'psych_analysis':
        background_executor.submit(analyze_user_psychology, user_uuid)
    return task_id

# ==============================================================================
# AIモデル呼び出し関数
# ==============================================================================
def call_llama_advanced(prompt, history, system_prompt, max_tokens=1000):
    if not groq_client: return None
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-8:]:
        messages.append({"role": "user" if msg.role == "user" else "assistant", "content": msg.content})
    messages.append({"role": "user", "content": prompt})
    completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", temperature=0.7, max_tokens=max_tokens)
    return completion.choices[0].message.content.strip()

def generate_fallback_response(message, reference_info=""):
    if reference_info:
        return f"調べてきたよ！\n\n{reference_info[:200]}"
    greetings = { 'こんにちは': ['やっほー！', 'こんにちは〜！元気？'], 'おはよう': ['おはよ〜！今日もいい天気だね！', 'おっはよ〜！'], 'こんばんは': ['こんばんは！今日どうだった？', 'ばんは〜！'], 'ありがとう': ['どういたしまして！', 'いえいえ〜！'], }
    for keyword, responses in greetings.items():
        if keyword in message: return random.choice(responses)
    if '?' in message or '？' in message:
        return random.choice(["それ、気になるね！", "うーん、なんて言おうかな！", "まじ？どういうこと？"])
    return random.choice(["うんうん！", "なるほどね！", "そうなんだ！", "まじで？"])

# ==============================================================================
# 性格分析 & 活用関数
# ==============================================================================
def analyze_user_psychology(user_uuid):
    with Session() as session:
        try:
            history = session.query(ConversationHistory).filter_by(user_uuid=user_uuid, role='user').order_by(ConversationHistory.timestamp.desc()).limit(100).all()
            if len(history) < 10: return
            
            messages_text = "\n".join([f"- {h.content}" for h in reversed(history)])
            analysis_prompt = f"以下の会話履歴からユーザーの性格を分析しJSONで出力してください:\n\n会話履歴:\n{messages_text[:3000]}\n\nJSON形式:\n{{\"openness\":50,\"conscientiousness\":50,\"extraversion\":50,\"agreeableness\":50,\"neuroticism\":50,\"interests\":[],\"favorite_topics\":[],\"conversation_style\":\"\",\"emotional_tendency\":\"\",\"analysis_summary\":\"\",\"confidence\":75}}"
            
            response_text = call_llama_advanced(analysis_prompt, [], "あなたは心理学の専門家です。", 800)
            if not response_text: return
            result = json.loads(response_text)
            
            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            if not psych:
                psych = UserPsychology(user_uuid=user_uuid, user_name=user.user_name if user else "Unknown"); session.add(psych)
            
            for key, value in result.items():
                if hasattr(psych, key):
                    setattr(psych, key, json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value)
            psych.last_analyzed = datetime.utcnow(); psych.total_messages = len(history)
            session.commit()
            logger.info(f"✅ 性格分析完了 for {user_uuid}")
        except Exception as e:
            logger.error(f"❌ 性格分析エラー: {e}"); session.rollback()

def get_psychology_insight(user_uuid):
    with Session() as session:
        psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        if not psych or psych.analysis_confidence < 60: return ""
        insights = []
        if psych.extraversion > 70: insights.append("社交的な")
        if psych.openness > 70: insights.append("好奇心旺盛な")
        if psych.conversation_style: insights.append(f"{psych.conversation_style}スタイルの")
        favorite_topics = json.loads(psych.favorite_topics) if psych.favorite_topics else []
        if favorite_topics: insights.append(f"{'、'.join(favorite_topics[:2])}が好きな")
        return "".join(insights)

# ==============================================================================
# コア機能
# ==============================================================================
def get_weather_forecast(location):
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{LOCATION_CODES.get(location, '130000')}.json"
    try:
        response = requests.get(url, timeout=10); response.raise_for_status()
        return f"今の{location}の天気はね、「{clean_text(response.json().get('text', ''))}」って感じだよ！"
    except Exception as e:
        logger.error(f"天気APIエラー: {e}"); return "天気情報がうまく取れなかったみたい…"

def background_db_correction(task_id, correction):
    result = f"「{correction['member_name']}」ちゃんの情報修正、やってみたけど失敗しちゃった…。ごめん！"
    try:
        with Session() as session:
            wiki = session.query(HolomemWiki).filter_by(member_name=correction['member_name']).first()
            if wiki:
                field_map = {'説明': 'description', 'デビュー日': 'debut_date', '期': 'generation', 'タグ': 'tags', 'ステータス': 'status', '卒業日': 'graduation_date', 'もちこの気持ち': 'mochiko_feeling'}
                db_field = field_map.get(correction['field'])
                if db_field:
                    setattr(wiki, db_field, correction['value'])
                    wiki.last_updated = datetime.utcnow()
                    session.commit()
                    result = f"おっけー！「{correction['member_name']}」ちゃんの「{correction['field']}」の情報を「{correction['value']}」に更新しといたよ！教えてくれてまじ助かる！"
                else: result = f"ごめん、「{correction['field']}」っていう項目はないみたい…"
            else: result = f"ごめん、「{correction['member_name']}」ちゃんがデータベースに見つからなかった…"
    except Exception as e:
        logger.error(f"❌ DB Correction error: {e}")
        result = "ごめん、データベースの修正中にエラーが起きちゃった…"
    
    with Session() as session:
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = result; task.status = 'completed'; task.completed_at = datetime.utcnow()
            session.commit()

def save_user_context(session, user_uuid, context_type, query):
    context = session.query(UserContext).filter_by(user_uuid=user_uuid).first()
    if not context:
        context = UserContext(user_uuid=user_uuid, last_context_type=context_type, last_query=query)
        session.add(context)
    else:
        context.last_context_type = context_type
        context.last_query = query
    session.commit()

def get_user_context(session, user_uuid):
    context = session.query(UserContext).filter_by(user_uuid=user_uuid).first()
    if context and (datetime.utcnow() - context.updated_at).total_seconds() < 600:
        return {'type': context.last_context_type, 'query': context.last_query}
    return None

def save_news_cache(session, user_uuid, news_items, news_type):
    session.query(NewsCache).filter_by(user_uuid=user_uuid, news_type=news_type).delete()
    for i, news in enumerate(news_items, 1):
        session.add(NewsCache(user_uuid=user_uuid, news_id=news.id, news_number=i, news_type=news_type))
    session.commit()

def get_cached_news_detail(session, user_uuid, news_number, news_type):
    cache = session.query(NewsCache).filter_by(user_uuid=user_uuid, news_number=news_number, news_type=news_type).first()
    if not cache: return None
    
    model = HololiveNews if news_type == 'hololive' else SpecializedNews
    return session.query(model).filter_by(id=cache.news_id).first()

# ==============================================================================
# AI応答生成
# ==============================================================================
def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
    if not groq_client:
        return generate_fallback_response(message, reference_info)

    personality_context = get_psychology_insight(user_data['uuid'])
    system_prompt = f"あなたは「もちこ」というギャルAIです。{user_data['name']}さんと話しています。\n# 口調ルール\n- 一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」。口癖は「まじ」「てか」「うける」。\n# ユーザー情報\n- {user_data['name']}さんは「{personality_context}人」という印象だよ。この情報を会話に活かしてね。"
    if is_task_report:
        system_prompt += "\n# 今回のミッション\n- 「おまたせ！さっきの件だけど…」と切り出し、【参考情報】を元に質問に答えて。"
    system_prompt += f"\n## 【参考情報】:\n{reference_info if reference_info else '特になし'}"

    try:
        logger.info(f"🧠 Groq llama-3.1-8b-instant を使用")
        response = call_llama_advanced(message, history, system_prompt, 500 if is_detailed else 300)
        if response:
            return response
        else:
            logger.error("⚠️ Groq AIモデルが応答しませんでした。")
            return generate_fallback_response(message, reference_info)
    except Exception as e:
        logger.error(f"❌ AI応答生成が最終的に失敗しました: {e}")
        return "うぅ、AIの調子が悪いみたい…ごめんね！"

# ==============================================================================
# 外部情報検索機能
# ==============================================================================
def search_wikipedia(term):
    API_ENDPOINT = "https://ja.wikipedia.org/w/api.php"
    params = { 'action': 'query', 'format': 'json', 'titles': term, 'prop': 'extracts', 'exintro': True, 'explaintext': True, 'redirects': 1 }
    try:
        response = requests.get(API_ENDPOINT, params=params, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=10)
        response.raise_for_status()
        data = response.json()
        pages = data.get('query', {}).get('pages')
        if not pages:
            logger.warning(f"Wikipedia APIから予期せぬ応答: {data}")
            return None
            
        page_id = next(iter(pages))
        if page_id != "-1":
            extract = pages[page_id].get('extract', '')
            disambig_patterns = ['曖昧さ回避', 'この項目では', '他の用法については', 'Disambiguation']
            if extract and not any(pattern in extract for pattern in disambig_patterns):
                logger.info(f"✅ Wikipediaで「{term}」の情報を取得しました。")
                return extract
            else:
                logger.info(f"Wikipediaで「{term}」は見つかりましたが、曖昧さ回避ページまたは内容が空でした。")
        else:
            logger.info(f"Wikipediaに「{term}」の項目が見つかりませんでした。")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Wikipedia APIへのリクエスト中にエラーが発生: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"❌ Wikipedia APIの応答(JSON)の解析に失敗: {e}")
    except Exception as e:
        logger.error(f"❌ Wikipedia検索中に予期せぬエラーが発生: {e}")
    return None

def deep_web_search(query):
    logger.info(f"🔍 複数エンジンでのWeb検索を開始: {query}")
    all_snippets = []

    # --- DuckDuckGoでの検索 ---
    try:
        ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        response = requests.get(ddg_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        for elem in soup.select('div.result')[:2]:
            snippet = elem.select_one('a.result__snippet')
            if snippet: all_snippets.append(clean_text(snippet.get_text()))
        logger.info("✅ DuckDuckGoでの検索に成功。")
    except Exception as e:
        logger.warning(f"⚠️ DuckDuckGoでの検索中にエラー: {e}")

    # --- Bingでの検索 ---
    try:
        bing_url = f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP"
        response = requests.get(bing_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        for elem in soup.select('li.b_algo')[:2]:
            snippet = elem.select_one('div.b_caption p, .b_caption')
            if snippet: all_snippets.append(clean_text(snippet.get_text()))
        logger.info("✅ Bingでの検索に成功。")
    except Exception as e:
        logger.warning(f"⚠️ Bingでの検索中にエラー: {e}")
        
    if not all_snippets:
        logger.error(f"❌ 全てのエンジンで検索結果が見つかりませんでした: {query}")
        return f"ごめん、「{query}」について調べたけど、良い情報が見つからなかった…"
        
    summary_text = "\n".join(f"[情報{i+1}] {s}" for i, s in enumerate(all_snippets))
    
    if not groq_client: return f"検索結果だよ！\n{summary_text}"
    
    prompt = f"""以下の複数の情報源を元に、質問「{query}」に対する答えをギャル語で一つの自然な文章に要約して：
{summary_text}\n\n回答の注意点:\n- 一人称は「あてぃし」、語尾は「〜じゃん」「〜的な？」、口癖は「まじ」「てか」「うける」。\n- 250文字以内で簡潔にまとめて。"""
    
    try:
        return call_llama_advanced(prompt, [], system_prompt="", max_tokens=300)
    except Exception as e:
        logger.error(f"❌ AIによる検索結果の要約中にエラー: {e}")
        return "検索はできたんだけど、うまくまとめるのに失敗しちゃった…ごめん！"


def background_deep_search(task_id, query):
    search_result = deep_web_search(query)
    with Session() as session:
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = search_result
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            session.commit()
            logger.info(f"✅ バックグラウンド検索タスク {task_id} が完了しました。")

# ==============================================================================
# Flask エンドポイント
# ==============================================================================
@app.route('/health')
def health_check():
    return create_json_response({'status': 'ok'})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    with Session() as session:
        try:
            data = request.json
            user_uuid, user_name, message = data['uuid'], data['name'], data['message'].strip()
            
            user_data_obj = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            
            ai_text = ""
            user_data = {'uuid': user_uuid, 'name': user_data_obj.user_name}

            completed_task = check_completed_tasks(user_uuid)
            if completed_task:
                query = completed_task['query']
                result = completed_task['result']
                ai_text = f"おまたせ！「{query}」について調べてきたよ！\n\n{result}"

            elif (what_is_term := is_what_is_request(message)):
                wikipedia_text = search_wikipedia(what_is_term)
                if wikipedia_text:
                    system_prompt = f"あなたは「もちこ」というギャルAIです。以下の【参考情報】を元に、「{what_is_term}とは？」という質問に対して、150文字程度で要約して答えてください。あなたの口調（一人称は「あてぃし」、語尾は「〜じゃん」「〜的な？」）を必ず守ってください。\n\n【参考情報】:\n{wikipedia_text[:1000]}"
                    ai_text = call_llama_advanced(message, [], system_prompt, 200)
                    if not ai_text:
                        ai_text = f"ごめん、{what_is_term}について調べてみたんだけど、うまくまとめられなかった…"
                else:
                    start_background_task(user_uuid, 'search', {'query': message}); ai_text = f"おっけー、「{message}」について詳しく調べてみるね！ちょい待ってて！"
            
            elif '性格分析' in message:
                start_background_task(user_uuid, 'psych_analysis', {}); ai_text = "おっけー！あなたの性格、分析してみるね！終わったら教えるから、ちょっと待ってて！"
            elif is_hololive_news_request(message):
                start_background_task(user_uuid, 'search', {'query': message}); ai_text = "ホロライブのニュースだね！調べてくるから待ってて！"
            elif is_weather_request(message): 
                location = extract_location(message)
                ai_text = get_weather_forecast(location)
            elif is_time_request(message):
                ai_text = get_japan_time()
            elif ('さくらみこ' in message or 'みこち' in message):
                for keyword, resp in get_sakuramiko_special_responses().items():
                    if keyword in message:
                        ai_text = resp; break
            
            if not ai_text and (should_search(message) or is_explicit_search_request(message)):
                start_background_task(user_uuid, 'search', {'query': message}); ai_text = f"おっけー、「{message}」について調べてみるね！ちょい待ってて！"
            
            if not ai_text:
                ai_text = generate_ai_response(user_data, message, history)
            
            if user_data_obj.interaction_count > 0 and user_data_obj.interaction_count % 50 == 0:
                start_background_task(user_uuid, 'psych_analysis', {})

            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
            session.commit()
            
            return Response(f"{ai_text}|", mimetype='text/plain; charset=utf-8', status=200)

        except Exception as e:
            logger.error(f"❌ Chatエラー: {e}", exc_info=True); session.rollback()
            return Response("ごめん、システムエラーが起きちゃった…|", mimetype='text/plain; charset=utf-8', status=500)

@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    data = request.json
    user_uuid = data.get('uuid', '')
    if not user_uuid: return create_json_response({'error': 'uuid is required'}, 400)
    completed_task = check_completed_tasks(user_uuid)
    if completed_task:
        return create_json_response({'status': 'completed', 'task': completed_task})
    return create_json_response({'status': 'pending'})

@app.route('/generate_voice', methods=['POST'])
def generate_voice_endpoint():
    if not VOICEVOX_ENABLED: return create_json_response({'error': 'Voice synthesis is not enabled.'}, 503)
    data = request.json
    text = data.get('text')
    if not text: return create_json_response({'error': 'Text is required.'}, 400)
    try:
        query_res = requests.post(f"{VOICEVOX_URL_FROM_ENV}/audio_query", params={"text": text, "speaker": VOICEVOX_SPEAKER_ID}, timeout=10)
        query_res.raise_for_status()
        synth_res = requests.post(f"{VOICEVOX_URL_FROM_ENV}/synthesis", params={"speaker": VOICEVOX_SPEAKER_ID}, json=query_res.json(), timeout=30)
        synth_res.raise_for_status()
        os.makedirs(VOICE_DIR, exist_ok=True)
        filename = f"voice_{uuid.uuid4()}.wav"
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f: f.write(synth_res.content)
        voice_url = urljoin(SERVER_URL, f'/voices/{filename}')
        return create_json_response({'status': 'success', 'url': voice_url})
    except Exception as e:
        logger.error(f"❌ VOICEVOXエラー: {e}")
        return create_json_response({'error': 'Voice generation failed.'}, 500)

@app.route('/voices/<filename>')
def serve_voice_file(filename):
    return send_from_directory(VOICE_DIR, filename)
    
# ==============================================================================
# 初期化とスケジューラー
# ==============================================================================
def initialize_groq_client():
    global groq_client
    if GROQ_API_KEY: groq_client = Groq(api_key=GROQ_API_KEY)

def cleanup_old_files():
    try:
        if not os.path.exists(VOICE_DIR): return
        cutoff = time.time() - (60 * 60)
        for filename in os.listdir(VOICE_DIR):
            file_path = os.path.join(VOICE_DIR, filename)
            if os.path.isfile(file_path) and os.path.getmtime(file_path) < cutoff:
                os.remove(file_path)
                logger.info(f"🗑️ 古い音声ファイルを削除しました: {filename}")
    except Exception as e:
        logger.error(f"❌ 音声ファイルのクリーンアップ中にエラー: {e}")

def initialize_app():
    logger.info("="*60 + "\n🔧 もちこAI 究極版 (v19.2) の初期化を開始...\n" + "="*60)
    
    initialize_groq_client()
    
    def run_scheduler():
        schedule.every(1).hour.do(cleanup_old_files)
        while True:
            schedule.run_pending()
            time.sleep(60)
            
    threading.Thread(target=run_scheduler, daemon=True).start()
    logger.info("⏰ スケジューラーを開始しました (ファイルクリーンアップ)")
    logger.info(f"🤖 利用可能なAIモデル: Llama (Groq)={'✅' if groq_client else '❌'}")
    logger.info("✅ 初期化完了！")

# ==============================================================================
# メイン実行
# ==============================================================================
if __name__
