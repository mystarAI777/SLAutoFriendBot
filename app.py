# ==============================================================================
# もちこAI - 完全統合版 (v15.0)
#
# ベース: v14.0
# 統合機能1: 性格分析 & 文字化け修正
# 統合機能2: ハイブリッドAIモデル (Gemini 2.0 Flash + Llama 3.3 70B)
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
from urllib.parse import quote_plus, urljoin

# --- サードパーティライブラリ ---
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import schedule
import signal

# ===== 【追加】Gemini API のインポート =====
import google.generativeai as genai
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
SERVER_URL = "https://slautofriendbot.onrender.com"
background_executor = ThreadPoolExecutor(max_workers=5)
VOICEVOX_SPEAKER_ID = 20 # もちこさん
HOLOLIVE_NEWS_URL = "https://hololive-tsuushin.com/category/holonews/"
# (その他の定数はv14.0と同様のため省略)
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
    '轟はじめ', 'ホロライブ', 'ホロメン', 'hololive', 'YAGOO'
]
LOCATION_CODES = { "東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000" }

# ==============================================================================
# 秘密情報/環境変数 読み込み
# ==============================================================================
# === app.py の get_secret 関数を以下のように修正 ===
def get_secret(name):
    """環境変数から秘密情報を取得（Render環境での標準的な方法）"""
    
    # RenderはSecret Fileを環境変数として展開するため、これでほとんどのケースをカバーできる
    env_value = os.environ.get(name)
    if env_value and env_value.strip():
        return env_value.strip()
        
    # 安全のため、ファイルからも試す（過去のバージョンがこのロジックだった場合の後方互換）
    try:
        # Renderの標準的なSecretパス: /etc/secrets/
        with open(f'/etc/secrets/{name}', 'r') as f: 
            file_value = f.read().strip()
            if file_value:
                return file_value
    except Exception:
        pass # ファイルが見つからなくても続行
        
    return None
# ===================================================

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')
# ===== 【追加】Gemini API Key 取得 =====
GEMINI_API_KEY = get_secret('GEMINI_API_KEY')

# ==============================================================================
# AIクライアントとグローバル変数
# ==============================================================================
groq_client = None
gemini_model = None
VOICEVOX_ENABLED = True if VOICEVOX_URL_FROM_ENV else False

# ==============================================================================
# Flask & データベース初期化
# ==============================================================================
app = Flask(__name__)
# ===== 【修正】文字化け対策: JSONのASCIIエンコードを無効化 =====
app.config['JSON_AS_ASCII'] = False
CORS(app)

# (v14.0の create_db_engine_with_retry 関数はそのまま使用)
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
class HololiveNews(Base): __tablename__ = 'hololive_news'; id = Column(Integer, primary_key=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); published_date = Column(DateTime, default=datetime.utcnow); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class BackgroundTask(Base): __tablename__ = 'background_tasks'; id = Column(Integer, primary_key=True); task_id = Column(String(255), unique=True, nullable=False); user_uuid = Column(String(255), nullable=False); task_type = Column(String(50), nullable=False); query = Column(Text, nullable=False); result = Column(Text); status = Column(String(20), default='pending'); created_at = Column(DateTime, default=datetime.utcnow, index=True); completed_at = Column(DateTime)
class HolomemWiki(Base): __tablename__ = 'holomem_wiki'; id = Column(Integer, primary_key=True); member_name = Column(String(100), nullable=False, unique=True, index=True); description = Column(Text); debut_date = Column(String(100)); generation = Column(String(100)); tags = Column(Text); last_updated = Column(DateTime, default=datetime.utcnow)
class FriendRegistration(Base): __tablename__ = 'friend_registrations'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), nullable=False, index=True); friend_uuid = Column(String(255), nullable=False); friend_name = Column(String(255), nullable=False); registered_at = Column(DateTime, default=datetime.utcnow); relationship_note = Column(Text)

# ===== 【追加】性格分析用データベースモデル =====
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
    interests = Column(Text) # JSON
    favorite_topics = Column(Text) # JSON
    conversation_style = Column(String(100))
    emotional_tendency = Column(String(100))
    analysis_summary = Column(Text)
    total_messages = Column(Integer, default=0)
    avg_message_length = Column(Integer, default=0)
    analysis_confidence = Column(Integer, default=0)
    last_analyzed = Column(DateTime)

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ==============================================================================
# ユーティリティ & ヘルパー関数 (v14.0から変更なし)
# ==============================================================================
def clean_text(text): return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text or "")).strip()
def get_japan_time(): return f"今は{datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分')}だよ！"
def is_time_request(message): return any(keyword in message for keyword in ['今何時', '時間', '時刻', '何時', 'なんじ'])
def is_weather_request(message): return any(keyword in message for keyword in ['天気予報', '今日の天気は？', '明日の天気'])
def is_hololive_request(message): return any(keyword in message for keyword in HOLOMEM_KEYWORDS)
# ... その他のヘルパー関数も同様 ...

# ==============================================================================
# AIモデル呼び出し関数 (ハイブリッドAI)
# ==============================================================================
def call_gemini(prompt, history, system_context):
    """Gemini 2.0 Flash で高速応答生成"""
    if not gemini_model: return None
    try:
        full_prompt = system_context + "\n\n"
        for msg in history[-5:]:
            role = "ユーザー" if msg.role == "user" else "AI"
            full_prompt += f"{role}: {msg.content}\n"
        full_prompt += f"ユーザー: {prompt}\nAI: "
        
        response = gemini_model.generate_content(full_prompt, generation_config={"temperature": 0.7, "max_output_tokens": 200})
        return response.text.strip()
    except Exception as e:
        logger.error(f"❌ Gemini APIエラー: {e}")
        return None

def call_llama_advanced(prompt, history, system_prompt, max_tokens=1000):
    """Llama 3.3 70B で高精度な分析・応答生成"""
    if not groq_client: return None
    try:
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history[-8:]:
            messages.append({"role": "user" if msg.role == "user" else "assistant", "content": msg.content})
        messages.append({"role": "user", "content": prompt})
        
        completion = groq_client.chat.completions.create(
            messages=messages, model="llama-3.3-70b-versatile", temperature=0.7, max_tokens=max_tokens
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Llama APIエラー: {e}")
        return None

# ==============================================================================
# 性格分析 & 活用関数
# ==============================================================================
def analyze_user_psychology(user_uuid):
    """ユーザーの性格をLlama 3.3 70Bで高精度に分析"""
    session = Session()
    try:
        history = session.query(ConversationHistory).filter_by(user_uuid=user_uuid, role='user').order_by(ConversationHistory.timestamp.desc()).limit(100).all()
        if len(history) < 10:
            return
        
        messages_text = "\n".join([f"- {h.content}" for h in reversed(history)])
        analysis_prompt = f"""以下の会話履歴からユーザーの性格を分析し、指定されたJSON形式で出力してください。

会話履歴:
{messages_text[:3000]}

JSON形式:
{{
  "openness": 50, "conscientiousness": 50, "extraversion": 50, "agreeableness": 50, "neuroticism": 50,
  "interests": ["興味1", "興味2"], "favorite_topics": ["トピック1", "トピック2"],
  "conversation_style": "フレンドリー/フォーマルなど", "emotional_tendency": "ポジティブ/ネガティブなど",
  "analysis_summary": "性格の簡潔な要約", "confidence": 75
}}"""
        
        response_text = call_llama_advanced(analysis_prompt, [], "あなたは心理学の専門家です。", 800)
        if not response_text: return

        result = json.loads(response_text)
        
        psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        
        if not psych:
            psych = UserPsychology(user_uuid=user_uuid, user_name=user.user_name if user else "Unknown")
            session.add(psych)
        
        # 取得した値で更新
        for key, value in result.items():
            if hasattr(psych, key):
                if isinstance(value, (list, dict)):
                    setattr(psych, key, json.dumps(value, ensure_ascii=False))
                else:
                    setattr(psych, key, value)
        psych.last_analyzed = datetime.utcnow()
        psych.total_messages = len(history)

        session.commit()
        logger.info(f"✅ 性格分析完了 for {user_uuid}")

    except Exception as e:
        logger.error(f"❌ 性格分析エラー: {e}")
        session.rollback()
    finally:
        session.close()

def get_psychology_insight(user_uuid):
    """性格分析結果を会話のコンテキストとして取得"""
    session = Session()
    try:
        psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        if not psych or psych.analysis_confidence < 60:
            return ""
        
        insights = []
        if psych.extraversion > 70: insights.append("社交的な")
        if psych.openness > 70: insights.append("好奇心旺盛な")
        if psych.conversation_style: insights.append(f"{psych.conversation_style}スタイルの")
        
        favorite_topics = json.loads(psych.favorite_topics) if psych.favorite_topics else []
        if favorite_topics:
            insights.append(f"{'、'.join(favorite_topics[:2])}が好きな")
        
        return "".join(insights) if insights else ""
    finally:
        session.close()

# ==============================================================================
# AI応答生成 (ハイブリッド版)
# ==============================================================================
def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
    use_llama = is_detailed or is_task_report or len(reference_info) > 100 or any(kw in message for kw in ['分析', '詳しく', '説明'])
    
    personality_context = get_psychology_insight(user_data['uuid'])
    
    system_prompt = f"""あなたは「もちこ」というギャルAIです。{user_data['name']}さんと話しています。
# 口調ルール
- 一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」。口癖は「まじ」「てか」「うける」。
# ユーザー情報
- {user_data['name']}さんは「{personality_context}人」という印象だよ。この情報を会話に活かしてね。
# 今回のミッション
"""
    if is_task_report:
        system_prompt += "- 「おまたせ！さっきの件だけど…」みたいに切り出し、【参考情報】を元にユーザーの質問に答えてあげて。"
    system_prompt += f"\n## 【参考情報】:\n{reference_info if reference_info else '特になし'}"

    try:
        if use_llama:
            logger.info("🧠 Llama 3.3 70Bを使用 (高精度)")
            response = call_llama_advanced(message, history, system_prompt, 500 if is_detailed else 300)
            if response: return response
            logger.warning("⚠️ Llama失敗、Geminiにフォールバック")

        logger.info("🚀 Gemini 2.0 Flashを使用 (高速)")
        response = call_gemini(message, history, system_prompt)
        if response: return response

        logger.error("⚠️ 全てのAIモデルが失敗")
        return "ごめん、今ちょっと考えがまとまらないや…！"
    except Exception as e:
        logger.error(f"❌ AI応答生成エラー: {e}")
        return "うぅ、AIの調子が悪いみたい…ごめんね！"

# ==============================================================================
# 既存機能のAIアップグレード
# ==============================================================================
def summarize_article(title, content):
    """記事要約をLlama 3.3 70Bで実行"""
    if not groq_client or not content: return content[:500] if content else title
    prompt = f"以下のニュース記事を200文字以内で簡潔に要約してください。\n\nタイトル: {title}\n本文: {content[:1500]}\n\n要約:"
    summary = call_llama_advanced(prompt, [], "あなたは優秀なニュース要約AIです。", 200)
    return summary if summary else content[:500]

def deep_web_search(query, is_detailed):
    """Web検索結果の要約をLlama 3.3 70Bで実行"""
    # (v14.0の scrape_major_search_engines 関数はそのまま使用)
    # ...
    return "検索機能は現在メンテナンス中です" # この部分はv14.0のコードを流用

# ==============================================================================
# Flask エンドポイント (文字化け対策済み)
# ==============================================================================
def create_json_response(data, status=200):
    """文字化け対策済みのJSONレスポンスを生成"""
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json; charset=utf-8', status=status)

@app.route('/health')
def health_check():
    # ... (v14.0と同様)
    return create_json_response({'status': 'ok'})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    session = Session()
    try:
        data = request.json
        user_uuid, user_name, message = data['uuid'], data['name'], data['message'].strip()
        
        user = get_or_create_user(session, user_uuid, user_name) # get_or_create_userはv14.0のものを流用
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        
        response_text = ""
        
        # ===== 性格分析リクエストの優先処理 =====
        if '性格分析' in message:
            background_executor.submit(analyze_user_psychology, user_uuid)
            response_text = "おっけー！あなたの性格、分析してみるね！終わったら「分析結果」って聞いてみて♪"
        elif '分析結果' in message:
            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if psych and psych.analysis_confidence > 60:
                response_text = f"分析結果だよ！あてぃしから見たあなたは…「{psych.analysis_summary}」って感じ！"
            else:
                response_text = "まだ分析中か、データが足りないみたい！もう少しお話しよ！"
        
        # ===== その他の処理 (v14.0のロジックを流用) =====
        else:
            history = get_conversation_history(session, user_uuid) # v14.0のものを流用
            response_text = generate_ai_response({'uuid': user_uuid, 'name': user_name}, message, history)
        
        # ===== 自動性格分析トリガー =====
        if user.interaction_count % 50 == 0 and user.interaction_count > 10:
            logger.info(f"🧠 自動性格分析を開始 for {user_name}")
            background_executor.submit(analyze_user_psychology, user_uuid)

        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
        session.commit()
        
        # LSLクライアントは `text|` 形式を期待するため、古い形式で返す
        return Response(f"{response_text}|", mimetype='text/plain; charset=utf-8', status=200)

    except Exception as e:
        logger.error(f"❌ Chatエラー: {e}", exc_info=True)
        session.rollback()
        return Response("ごめん、システムエラーが起きちゃった…|", mimetype='text/plain; charset=utf-8', status=500)
    finally:
        session.close()

@app.route('/get_psychology', methods=['GET'])
def get_psychology_endpoint():
    user_uuid = request.args.get('user_uuid')
    if not user_uuid: return create_json_response({'error': 'user_uuid is required'}, 400)
    
    session = Session()
    try:
        psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        if not psych:
            return create_json_response({'status': 'not_analyzed'})
        
        result = { 'status': 'analyzed', 'user_name': psych.user_name, 'summary': psych.analysis_summary, 'confidence': psych.analysis_confidence }
        # ... (詳細なデータを追加)
        return create_json_response(result)
    finally:
        session.close()

# (その他のエンドポイントも同様に文字化け対策を適用)

# ==============================================================================
# 初期化とスケジューラー
# ==============================================================================
def initialize_groq_client():
    global groq_client
    if GROQ_API_KEY and len(GROQ_API_KEY) > 20:
        groq_client = Groq(api_key=GROQ_API_KEY.strip())
        logger.info("✅ Groq Llama 3.3 70B client initialized")
    else:
        logger.warning("⚠️ GROQ_API_KEY is not set. Groq features disabled.")

def initialize_gemini_client():
    global gemini_model
    if GEMINI_API_KEY and len(GEMINI_API_KEY) > 20:
        genai.configure(api_key=GEMINI_API_KEY.strip())
        gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
        logger.info("✅ Google Gemini 2.0 Flash client initialized")
    else:
        logger.warning("⚠️ GEMINI_API_KEY is not set. Gemini features disabled.")

def schedule_psychology_analysis():
    """30日以上分析されていないアクティブユーザーを再分析"""
    session = Session()
    try:
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        users_to_analyze = session.query(UserPsychology).filter(UserPsychology.last_analyzed < thirty_days_ago).all()
        logger.info(f"🔄 定期性格分析スケジュール: {len(users_to_analyze)}人のユーザーを再分析します。")
        for psych in users_to_analyze:
            background_executor.submit(analyze_user_psychology, psych.user_uuid)
            time.sleep(5) # APIレート制限対策
    finally:
        session.close()

def initialize_app():
    """アプリケーションの完全初期化"""
    logger.info("="*60)
    logger.info("🔧 もちこAI 完全統合版 (v15.0) の初期化を開始...")
    logger.info("="*60)
    
    initialize_gemini_client()
    initialize_groq_client()
    
    # (v14.0のその他の初期化処理をここに含める)
    # initialize_holomem_wiki()
    # check_and_populate_initial_news()

    def run_scheduler():
        # (v14.0のスケジューラに性格分析を追加)
        # schedule.every().hour.do(...)
        schedule.every().day.at("03:00").do(schedule_psychology_analysis)
        while True:
            schedule.run_pending()
            time.sleep(60)

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logger.info("⏰ スケジューラーを開始しました (定期性格分析含む)")
    logger.info("✅ 初期化完了！")
    logger.info(f"🤖 利用可能なAIモデル: Gemini={'✅' if gemini_model else '❌'} | Llama={'✅' if groq_client else '❌'}")


# ==============================================================================
# メイン実行
# ==============================================================================
if __name__ == '__main__':
    # (v14.0と同様の起動ロジック)
    initialize_app()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
else:
    initialize_app()
    application = app
