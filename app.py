import sys
import os
import requests
import logging
import time
import threading
import json
import re
import random
import hashlib
from datetime import datetime, timedelta, timezone
from groq import Groq
from flask import Response
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import schedule
import signal
from threading import Lock

# --- 型ヒント (Python古いバージョン向け) ---
try:
    from typing import Union, Dict, Any, List, Optional
except ImportError:
    Dict, Any, List, Union, Optional = dict, object, list, object, object

from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean, text
from sqlalchemy.orm import declarative_base, sessionmaker

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 定数 ---
SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', 'http://localhost:10000')
SL_SAFE_CHAR_LIMIT = 250
USER_AGENTS = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36']
HOLOLIVE_NEWS_URL = "https://hololive-tsuushin.com/category/holonews/"
LOCATION_CODES = { "東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000" }

# --- グローバル変数 & アプリ設定 ---
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client, engine, Session = None, None, None
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
CORS(app)

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

Base = declarative_base()

# --- 秘密情報/環境変数 読み込み ---
def get_secret(name):
    path = f"/etc/secrets/{name}"
    if os.path.exists(path):
        try:
            with open(path, 'r') as f: return f.read().strip()
        except IOError: return None
    return os.environ.get(name)

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
ADMIN_TOKEN = get_secret('ADMIN_TOKEN')

# --- データベースモデル ---
class UserMemory(Base): 
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False)
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
    conversation_style = Column(String(50))
    analysis_summary = Column(Text)
    analysis_confidence = Column(Integer, default=0)
    last_analyzed = Column(DateTime)
    
class BackgroundTask(Base):
    __tablename__ = 'background_tasks'
    id = Column(Integer, primary_key=True)
    task_id = Column(String(255), unique=True, nullable=False)
    user_uuid = Column(String(255), nullable=False)
    task_type = Column(String(50), nullable=False)
    query = Column(Text, nullable=False)
    result = Column(Text)
    status = Column(String(20), default='pending')
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime)

# --- 初期化処理 ---
def create_optimized_db_engine():
    try:
        is_sqlite = 'sqlite' in DATABASE_URL
        connect_args = {'check_same_thread': False} if is_sqlite else {'connect_timeout': 10}
        engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        logger.info(f"✅ Database engine created ({'SQLite' if is_sqlite else 'PostgreSQL'})")
        return engine
    except Exception as e: 
        logger.error(f"❌ Failed to create database engine: {e}")
        raise

def initialize_groq_client():
    global groq_client
    try:
        if GROQ_API_KEY and len(GROQ_API_KEY) > 20:
            groq_client = Groq(api_key=GROQ_API_KEY)
            logger.info("✅ Groq client initialized")
        else: logger.warning("⚠️ GROQ_API_KEY is not set or too short.")
    except Exception as e: logger.error(f"❌ Groq initialization failed: {e}")

# --- ヘルパー関数 ---
def limit_text_for_sl(text, max_length=SL_SAFE_CHAR_LIMIT):
    if not text: return "..."
    if len(text) <= max_length: return text
    return text[:max_length-3] + "..."

def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def get_japan_time():
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    return f"今は{now.strftime('%Y年%m月%d日 %H時%M分')}だよ！"

### ▼▼▼ 追加 ▼▼▼ ###
# さくらみこ専用の応答を返すための辞書
def get_sakuramiko_special_responses():
    return {
        'にぇ': 'さくらみこちゃんの「にぇ」、まじかわいいよね!あの独特な口癖がエリートの証なんだって〜',
        'エリート': 'みこちは自称エリートVTuber!でも実際は愛されポンコツキャラって感じで、それがまた魅力的なんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!「みこち建築」って呼ばれてるの知ってる?',
        'FAQ': 'みこちのFAQ、実は本人が答えるんじゃなくてファンが質問するコーナーなんだよ〜面白いでしょ?',
        'GTA': 'みこちのGTA配信、カオスで最高!警察に追われたり、変なことしたり、見てて飽きないんだよね〜'
    }
### ▲▲▲ 追加 ▲▲▲ ###

def get_or_create_user(session, user_uuid, user_name):
    try:
        user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        if not user:
            user = UserMemory(user_uuid=user_uuid, user_name=user_name)
            session.add(user)
            session.flush()
            logger.info(f"✨ New user created: {user_name}")
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        user.user_name = user_name
        return user
    except Exception as e:
        logger.error(f"❌ Error in get_or_create_user: {e}")
        raise

def get_conversation_history(session, user_uuid, limit=10):
    try:
        history = session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(ConversationHistory.timestamp.desc()).limit(limit).all()
        return history[::-1]
    except Exception as e:
        logger.error(f"❌ Error fetching conversation history: {e}")
        return []
        
# --- 天気予報 (完全版) ---
def get_weather_forecast(location):
    area_code = LOCATION_CODES.get(location, "130000")
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        weather_text = clean_text(data.get('text', ''))
        if not weather_text: return f"{location}の天気情報が見つからなかった…"
        summary = f"今の{location}の天気はね、「{weather_text}」って感じだよ！"
        return limit_text_for_sl(summary, 200)
    except Exception as e:
        logger.error(f"❌ Weather API error for {location}: {e}")
        return "うぅ、天気情報がうまく取れなかったみたい…ごめんね！"

def is_weather_request(message):
    if any(t in message for t in ['天気', '気温']) and any(a in message for a in ['教えて', 'どう？', 'は？']):
        for loc in LOCATION_CODES:
            if loc in message: return loc
        return "東京"
    return None

def is_time_request(message):
    return any(kw in message for kw in ['今何時', '時間', '時刻'])
    
# --- Web検索機能 ---
def deep_web_search(query):
    logger.info(f"🔍 Starting deep web search for: {query}")
    try:
        search_url = f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP"
        response = requests.get(search_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=12)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        snippets = []
        for elem in soup.select('li.b_algo')[:3]:
            snippet = elem.select_one('div.b_caption p, .b_caption')
            if snippet: snippets.append(clean_text(snippet.get_text()))
        
        if not snippets: return f"「{query}」について調べたけど、情報が見つからなかったよ…"
        
        summary_text = "\n".join(f"[情報{i+1}] {s}" for i, s in enumerate(snippets))
        
        if not groq_client: return f"検索結果だよ！\n{summary_text}"
        
        prompt = f"""以下の検索結果を使い、質問「{query}」にギャル語で簡潔に答えて：
検索結果:\n{summary_text}\n\n回答の注意点:\n- 一人称は「あてぃし」、語尾は「〜じゃん」「〜的な？」、口癖は「まじ」「てか」「うける」。\n- 200文字以内で要約して。"""
        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}], model="llama-3.1-8b-instant",
            temperature=0.7, max_tokens=200
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Deep web search error: {e}")
        return f"検索中にエラーが起きちゃった…ごめんね！「{query}」についてもう一回聞いてみて？"

def background_deep_search(task_id, query):
    search_result = deep_web_search(query)
    with Session() as session:
        try:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.result = search_result
                task.status = 'completed'
                task.completed_at = datetime.utcnow()
                session.commit()
                logger.info(f"✅ Background search task {task_id} completed.")
        except Exception as e:
            logger.error(f"❌ Failed to save task result: {e}")
            session.rollback()

def start_background_search(user_uuid, query):
    task_id = hashlib.md5(f"{user_uuid}{query}{time.time()}".encode()).hexdigest()[:10]
    with Session() as session:
        try:
            task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=query)
            session.add(task)
            session.commit()
            background_executor.submit(background_deep_search, task_id, query)
            return True
        except Exception as e:
            logger.error(f"❌ Background task creation error: {e}")
            session.rollback()
            return False

# --- 心理分析 (高度化) ---
def analyze_user_psychology_advanced(user_uuid):
    logger.info(f"🧠 Starting advanced psychology analysis for {user_uuid}")
    with Session() as session:
        try:
            history = session.query(ConversationHistory).filter_by(user_uuid=user_uuid, role='user').order_by(ConversationHistory.timestamp.desc()).limit(50).all()
            if len(history) < 10:
                logger.warning(f"⚠️ Not enough data for psychology analysis: {len(history)} messages")
                return
            
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            if not user: return
            
            messages_text = "\n".join([h.content for h in reversed(history)])
            
            analysis_prompt = f"""あなたは心理学の専門家です。以下のユーザー「{user.user_name}」さんの過去の会話を分析し、以下のJSON形式で回答してください。

【会話履歴】
{messages_text[:2000]}

【分析項目とJSON形式】
{{
  "conversation_style": "<カジュアル/丁寧/熱心など>",
  "summary": "<200文字程度の人物像の要約>",
  "confidence": <分析の信頼度 0-100の数値>
}}"""
            # ビッグファイブはJSON出力が不安定な場合があるため、主要な項目に絞っています

            completion = groq_client.chat.completions.create(
                messages=[{"role": "user", "content": analysis_prompt}],
                model="llama-3.1-8b-instant",
                temperature=0.3, max_tokens=600,
                response_format={"type": "json_object"}
            )
            analysis_data = json.loads(completion.choices[0].message.content)
            
            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if not psych:
                psych = UserPsychology(user_uuid=user_uuid, user_name=user.user_name)
                session.add(psych)
            
            psych.conversation_style = analysis_data.get('conversation_style', '')
            psych.analysis_summary = analysis_data.get('summary', '')
            psych.analysis_confidence = analysis_data.get('confidence', len(history) * 2)
            psych.last_analyzed = datetime.utcnow()
            
            session.commit()
            logger.info(f"✅ Advanced psychology analysis completed for {user.user_name}")

        except Exception as e:
            logger.error(f"❌ Advanced psychology analysis error: {e}", exc_info=True)
            session.rollback()

# --- AI応答生成 (高度化) ---
def generate_ai_response(user_name, message, history, reference_info=""):
    if not groq_client: 
        return random.choice(["うんうん！", "なるほどね！", "そうなんだ！", "まじで？"])
    
    try:
        system_prompt_parts = [
            f"あなたは「もちこ」という明るいギャルAIです。{user_name}さんと話しています。",
            "- 一人称は「あてぃし」、語尾は「〜じゃん」「〜的な？」、口癖は「まじ」「てか」「うける」。",
            "- 相手の話に共感し、短くテンポよく返す。絵文字は使わない。",
        ]
        if reference_info: 
            system_prompt_parts.append(f"【参考情報】: {reference_info}")
        system_prompt = "\n".join(system_prompt_parts)
        
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history[-5:]:
            messages.append({"role": msg.role, "content": msg.content})
        messages.append({"role": "user", "content": message})
        
        completion = groq_client.chat.completions.create(
            messages=messages, 
            model="llama-3.1-8b-instant", 
            temperature=0.8, max_tokens=300
        )
        response = completion.choices[0].message.content.strip()
        return limit_text_for_sl(response)
    except Exception as e:
        logger.error(f"AI response error: {e}")
        return "うーん、ちょっと考えがまとまらないや…ごめんね！"
        
# --- Flask エンドポイント ---
def json_response(data, status=200):
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json; charset=utf-8', status=status)

@app.route('/health')
def health_check():
    db_ok = False
    try:
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        db_ok = True
    except: pass
    return json_response({'status': 'ok', 'db': 'ok' if db_ok else 'error', 'ai': 'ok' if groq_client else 'disabled'})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json
        if not data or not all(k in data for k in ['user_uuid', 'user_name', 'message']):
            return json_response({'error': 'Missing required fields'}, 400)
        
        user_uuid, user_name, message = data['user_uuid'], data['user_name'], data['message'].strip()
        if not message: return json_response({'error': 'Empty message'}, 400)
        
        with Session() as session:
            user = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            
            response_text = ""
            
            # --- コマンド & 状況判断 ---
            if '性格分析' in message or '心理分析' in message:
                background_executor.submit(analyze_user_psychology_advanced, user_uuid)
                response_text = "おっ、性格分析したいの？今分析してるから、終わったら「分析結果」って聞いてみて♪"
            
            elif '分析結果' in message:
                psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
                if psych and psych.analysis_confidence > 0:
                    response_text = f"あてぃしが見た{user_name}さんの性格はね…「{psych.analysis_summary}」って感じだよ！ (信頼度: {psych.analysis_confidence}%)"
                else: response_text = "まだ分析が終わってないか、データが足りないみたい。もう少し話してから試してみて！"

            ### ▼▼▼ 追加 ▼▼▼ ###
            elif 'さくらみこ' in message or 'みこち' in message:
                special_responses = get_sakuramiko_special_responses()
                for keyword, response in special_responses.items():
                    if keyword in message:
                        response_text = response
                        break # 一致したらループを抜ける
            ### ▲▲▲ 追加 ▲▲▲ ###
            
            elif is_time_request(message): response_text = get_japan_time()
            elif (location := is_weather_request(message)): response_text = get_weather_forecast(location)
            
            elif any(kw in message for kw in ['調べて', '教えて', 'とは？', 'って何']):
                # 「さくらみこ」に関する特別な応答が先に処理されるため、一般的な検索のみがここに到達する
                if start_background_search(user_uuid, message):
                    response_text = "おっけー、調べてみるね！ちょっと待ってて！終わったら教えるね！"
                else: response_text = "ごめん、今検索機能がうまく動いてないみたい…"
            
            # 通常会話
            if not response_text:
                response_text = generate_ai_response(user.user_name, message, history)
            
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
            session.commit()
            
            logger.info(f"✅ Response to {user_name}: {response_text[:50]}...")
            return json_response({'response': response_text})
        
    except Exception as e:
        logger.error(f"❌ Chat error: {e}", exc_info=True)
        return json_response({'error': 'Internal server error'}, 500)

@app.route('/analyze_psychology', methods=['POST'])
def analyze_psychology_endpoint():
    try:
        data = request.json
        if not data or 'user_uuid' not in data: return json_response({'error': 'Missing user_uuid'}, 400)
        background_executor.submit(analyze_user_psychology_advanced, data['user_uuid'])
        return json_response({'status': 'accepted', 'message': 'Analysis started'}), 202
    except Exception as e:
        logger.error(f"❌ Psychology analysis endpoint error: {e}")
        return json_response({'error': 'Internal server error'}, 500)

@app.route('/get_psychology', methods=['POST'])
def get_psychology_endpoint():
    try:
        data = request.json
        if not data or 'user_uuid' not in data: return json_response({'error': 'Missing user_uuid'}, 400)
        with Session() as session:
            psych = session.query(UserPsychology).filter_by(user_uuid=data['user_uuid']).first()
            if not psych or psych.analysis_confidence == 0:
                return json_response({'error': 'No analysis data available'}, 404)
            return json_response({
                'summary': psych.analysis_summary,
                'conversation_style': psych.conversation_style,
                'confidence': psych.analysis_confidence,
            })
    except Exception as e:
        logger.error(f"❌ Get psychology endpoint error: {e}")
        return json_response({'error': 'Internal server error'}, 500)

@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    try:
        data = request.json
        if not data or 'user_uuid' not in data: return json_response({'error': 'Missing user_uuid'}, 400)
        
        with Session() as session:
            task = session.query(BackgroundTask).filter_by(user_uuid=data['user_uuid'], status='completed').order_by(BackgroundTask.completed_at.desc()).first()
            if task:
                response_data = {'task': {'query': task.query, 'result': task.result}}
                session.delete(task)
                session.commit()
                logger.info(f"✅ Notifying user {data['user_uuid']} of completed task: {task.query}")
                return json_response({'status': 'completed', **response_data})
            else:
                return json_response({'status': 'no_tasks'})
    except Exception as e:
        logger.error(f"❌ Check task endpoint error: {e}")
        return json_response({'error': 'Internal server error'}, 500)

# --- アプリケーション起動 ---
def initialize_app():
    global engine, Session
    logger.info("="*30 + "\n🔧 Mochiko AI Starting Up...\n" + "="*30)
    
    if not DATABASE_URL: 
        logger.critical("🔥 FATAL: DATABASE_URL not set."); sys.exit(1)
    
    initialize_groq_client()
    
    try:
        engine = create_optimized_db_engine()
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        logger.info("✅ Database initialized")
    except Exception as e: 
        logger.critical(f"🔥 DB init failed: {e}"); raise
    
    logger.info("✅ Initialization Complete!\n" + "="*30)

application = None
try:
    initialize_app()
    application = app
except Exception as e:
    logger.critical(f"🔥 Fatal initialization error: {e}", exc_info=True)
    application = Flask(__name__)
    @application.route('/health')
    def failed_health():
        return jsonify({'status': 'error', 'message': 'Initialization failed', 'error': str(e)}), 500

def signal_handler(sig, frame):
    logger.info("🛑 Shutting down...")
    background_executor.shutdown(wait=True)
    if engine: engine.dispose()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == '__main__':
    if application: 
        port = int(os.environ.get('PORT', 10000))
        logger.info(f"🚀 Starting server on port {port}")
        application.run(host='0.0.0.0', port=port, debug=False)
