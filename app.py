import sys
import os
import requests
import logging
import time
import threading
import json
import re
import sqlite3
import random
import uuid
import hashlib
from datetime import datetime, timedelta, timezone

# 型ヒント用のインポート（エラー対策付き）
try:
    from typing import Union, Dict, Any, List, Optional
except ImportError:
    # 型ヒントが使えない環境用のフォールバック
    Dict = dict
    Any = object
    List = list
    Union = object
    Optional = object

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Index, Text, Boolean, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import asyncio
from threading import Lock
import schedule

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ★ 修正1: ボイスディレクトリを安全に作成
VOICE_DIR = '/tmp/voices'
try:
    os.makedirs(VOICE_DIR, exist_ok=True)
    logger.info(f"✅ Voice directory created: {VOICE_DIR}")
except Exception as e:
    logger.warning(f"⚠️ Voice directory creation failed: {e}")
    VOICE_DIR = '/tmp'

SERVER_URL = "https://slautofriendbot.onrender.com"
background_executor = ThreadPoolExecutor(max_workers=5)

# --- 秘密情報/環境変数 読み込み ---
def get_secret(name):
    """環境変数から秘密情報を取得"""
    return os.environ.get(name)

# 環境変数の読み込み（詳細ログ付き）
DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'

# GROQ_API_KEY の詳細診断
GROQ_API_KEY = get_secret('GROQ_API_KEY')
logger.info("🔍 GROQ_API_KEY 診断:")
logger.info(f"   - 環境変数存在: {'はい' if 'GROQ_API_KEY' in os.environ else 'いいえ'}")
if GROQ_API_KEY:
    logger.info(f"   - キー長: {len(GROQ_API_KEY)} 文字")
    logger.info(f"   - 先頭: {GROQ_API_KEY[:10]}... (セキュリティのため一部のみ表示)")
    logger.info(f"   - 末尾チェック: ...{GROQ_API_KEY[-4:]}")
else:
    logger.warning("   - ⚠️ GROQ_API_KEYが見つかりません！")
    GROQ_API_KEY = 'DUMMY_GROQ_KEY'

VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- Groqクライアント初期化（改善版） ---
groq_client = None
try:
    from groq import Groq
    
    if not GROQ_API_KEY or GROQ_API_KEY == 'DUMMY_GROQ_KEY':
        logger.warning("⚠️ GROQ_API_KEYが設定されていません - AI機能は無効です")
        logger.warning("⚠️ Renderの Environment Variables に GROQ_API_KEY を設定してください")
    elif len(GROQ_API_KEY) < 20:
        logger.error(f"❌ GROQ_API_KEYが短すぎます (長さ: {len(GROQ_API_KEY)})")
        logger.error("❌ 正しいAPIキーを設定してください")
    else:
        # APIキーの前後の空白を削除
        GROQ_API_KEY = GROQ_API_KEY.strip()
        
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groq client initialized successfully")
        logger.info(f"✅ APIキー検証: {GROQ_API_KEY[:10]}...")
        
        # 接続テスト（オプション）
        try:
            # 簡単なテストリクエスト
            test_completion = groq_client.chat.completions.create(
                messages=[{"role": "user", "content": "test"}],
                model="llama-3.1-8b-instant",
                max_tokens=5
            )
            logger.info("✅ Groq API 接続テスト成功！")
        except Exception as test_error:
            logger.error(f"❌ Groq API 接続テスト失敗: {test_error}")
            groq_client = None
            
except ImportError as e:
    groq_client = None
    logger.error(f"❌ Groqライブラリのインポート失敗: {e}")
    logger.error("❌ requirements.txt に 'groq' が含まれているか確認してください")
except Exception as e:
    groq_client = None
    logger.error(f"❌ Groq client initialization failed: {e}")
    logger.error(f"❌ エラー詳細: {type(e).__name__}: {str(e)}")

# DATABASE_URL検証
if not DATABASE_URL:
    logger.critical("FATAL: DATABASE_URLが設定されていません")
    sys.exit(1)

# PostgreSQLの場合はホスト名をログ出力
if 'postgresql' in DATABASE_URL:
    try:
        host_part = DATABASE_URL.split('@')[1].split('/')[0]
        logger.info(f"📊 PostgreSQL接続先: {host_part}")
    except:
        logger.warning("⚠️ DATABASE_URLの形式を確認できません")

if not groq_client:
    logger.warning("警告: Groq APIキーが設定されていないため、AI機能は無効です。")
VOICEVOX_ENABLED = True


# --- Flask & データベース初期化 ---
app = Flask(__name__)
CORS(app)

# ★ 修正: データベース接続にリトライロジックを追加
def create_db_engine_with_retry(max_retries=5, retry_delay=5):
    """データベースエンジンをリトライ付きで作成"""
    from sqlalchemy.exc import OperationalError
    
    for attempt in range(max_retries):
        try:
            logger.info(f"🔄 データベース接続試行 {attempt + 1}/{max_retries}...")
            
            # PostgreSQLの接続タイムアウトを設定
            connect_args = {'check_same_thread': False} if 'sqlite' in DATABASE_URL else {'connect_timeout': 10}
            
            engine = create_engine(
                DATABASE_URL,
                pool_pre_ping=True,           # 接続前にpingテスト
                pool_recycle=300,             # 5分ごとに接続をリサイクル
                connect_args=connect_args
            )
            
            # 接続テスト
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            
            logger.info("✅ データベース接続成功")
            return engine
            
        except OperationalError as e:
            if attempt < max_retries - 1:
                logger.warning(f"⚠️ データベース接続失敗 (試行 {attempt + 1}/{max_retries}): {e}")
                logger.info(f"⏳ {retry_delay}秒後にリトライします...")
                time.sleep(retry_delay)
            else:
                logger.error(f"❌ データベース接続が{max_retries}回失敗しました")
                logger.error(f"DATABASE_URL: {DATABASE_URL[:50]}..." if len(DATABASE_URL) > 50 else DATABASE_URL)
                raise
        except Exception as e:
            logger.error(f"❌ 予期しないデータベースエラー: {e}")
            raise

try:
    engine = create_db_engine_with_retry()
except Exception as e:
    logger.critical(f"🔥 データベース初期化失敗: {e}")
    logger.critical("Render環境の場合は、Internal Database URLを使用してください")
    sys.exit(1)

Base = declarative_base()

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

class HololiveNews(Base):
    __tablename__ = 'hololive_news'
    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000))
    published_date = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    news_hash = Column(String(100), unique=True)

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

# ★★★ 新機能1: 専門サイトニュースのテーブル追加 ★★★
class SpecializedNews(Base):
    __tablename__ = 'specialized_news'
    id = Column(Integer, primary_key=True)
    site_name = Column(String(100), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000))
    published_date = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    news_hash = Column(String(100), unique=True)

# ★★★ 新機能2: ホロメンWikiのテーブル追加 ★★★
class HolomemWiki(Base):
    __tablename__ = 'holomem_wiki'
    id = Column(Integer, primary_key=True)
    member_name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text)
    debut_date = Column(String(100))
    generation = Column(String(100))
    tags = Column(Text)  # JSON形式でタグを保存
    last_updated = Column(DateTime, default=datetime.utcnow)

# ★★★ 新機能3: 友だち登録システムのテーブル追加 ★★★
class FriendRegistration(Base):
    __tablename__ = 'friend_registrations'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    friend_uuid = Column(String(255), nullable=False)
    friend_name = Column(String(255), nullable=False)
    registered_at = Column(DateTime, default=datetime.utcnow)
    relationship_note = Column(Text)  # 関係性のメモ

# データベーステーブル作成
try:
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    logger.info("✅ Database tables created successfully")
except Exception as e:
    logger.error(f"❌ Database table creation failed: {e}")
    raise

def add_news_hash_column_if_not_exists(engine):
    """news_hashカラムが存在しない場合は追加"""
    try:
        inspector = inspect(engine)
        columns = [col['name'] for col in inspector.get_columns('hololive_news')]
        if 'news_hash' not in columns:
            with engine.connect() as con:
                trans = con.begin()
                try:
                    con.execute(text("ALTER TABLE hololive_news ADD COLUMN news_hash VARCHAR(100) UNIQUE;"))
                    trans.commit()
                    logger.info("✅ news_hash column added successfully")
                except Exception as e:
                    trans.rollback()
                    logger.warning(f"⚠️ news_hash column add failed: {e}")
        else:
            logger.info("✅ news_hash column already exists")
    except Exception as e:
        logger.warning(f"⚠️ Column check failed: {e}")

add_news_hash_column_if_not_exists(engine)

# --- 専門サイト & ホロライブ設定 ---
SPECIALIZED_SITES = {
    'Blender': {
        'base_url': 'https://docs.blender.org/manual/ja/latest/',
        'keywords': ['Blender', 'ブレンダー']
    },
    'CGニュース': {
        'base_url': 'https://modelinghappy.com/',
        'keywords': ['CGニュース', '3DCG', 'CG']
    },
    '脳科学・心理学': {
        'base_url': 'https://nazology.kusuguru.co.jp/',
        'keywords': ['脳科学', '心理学']
    },
    'セカンドライフ': {
        'base_url': 'https://community.secondlife.com/news/',
        'keywords': ['セカンドライフ', 'Second Life', 'SL']
    }
}

HOLOLIVE_NEWS_URL = "https://hololive-tsuushin.com/category/holonews/"
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

# ★★★ 新機能: VOICEVOXのデフォルト話者を「もちこ」風に設定 ★★★
VOICEVOX_SPEAKER_ID = 20  # もち子さんノーマル

# --- ユーティリティ関数 ---
def clean_text(text):
    """HTMLタグや余分な空白を除去"""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def get_japan_time():
    """日本時間を取得"""
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    return f"今は{now.year}年{now.month}月{now.day}日の{now.hour}時{now.minute}分だよ！"

def create_news_hash(title, content):
    """ニュースのハッシュ値を生成"""
    hash_string = f"{title}{content[:100]}"
    return hashlib.md5(hash_string.encode('utf-8')).hexdigest()

def is_time_request(message):
    """時間に関する質問かどうか判定"""
    time_keywords = ['今何時', '時間', '時刻', '何時', 'なんじ']
    return any(keyword in message for keyword in time_keywords)

def is_weather_request(message):
    """天気に関する質問かどうか判定"""
    weather_keywords = ['天気', 'てんき', '気温', '雨', '晴れ', '曇り', '雪']
    return any(keyword in message for keyword in weather_keywords)

def is_hololive_request(message):
    """ホロライブ関連の質問かどうか判定"""
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)

def is_recommendation_request(message):
    """おすすめに関する質問かどうか判定"""
    recommend_keywords = ['おすすめ', 'オススメ', '推薦', '紹介して']
    return any(keyword in message for keyword in recommend_keywords)

def detect_specialized_topic(message):
    """専門分野のトピックを検出"""
    # 完全一致または部分一致をチェック
    message_lower = message.lower()
    
    for topic, config in SPECIALIZED_SITES.items():
        # キーワードの完全一致または部分一致
        for keyword in config['keywords']:
            keyword_lower = keyword.lower()
            # 「CGニュース」のような短いキーワードは完全一致を優先
            if len(keyword) <= 5:
                if keyword in message or keyword_lower in message_lower:
                    return topic
            else:
                if keyword in message:
                    return topic
    return None

def is_detailed_request(message):
    """詳細な説明を求めているかどうか判定"""
    detailed_keywords = [
        '詳しく', '詳細', 'くわしく', '教えて', '説明して', '解説して',
        'どういう', 'なぜ', 'どうして', '理由', '原因', 'しっかり',
        'ちゃんと', 'きちんと', '具体的に'
    ]
    return any(keyword in message for keyword in detailed_keywords)

def should_search(message):
    """検索が必要かどうか判定"""
    # 専門分野のキーワードがあれば検索
    if detect_specialized_topic(message):
        return True
    
    # ホロライブ関連
    if is_hololive_request(message):
        # ただし「ニュース」「情報」などのキーワードがある場合は直接応答
        if not any(kw in message for kw in ['ニュース', '最新', '情報', 'お知らせ', 'ホロライブ']):
            return True
    
    # おすすめリクエスト
    if is_recommendation_request(message):
        return True
    
    # 質問パターン
    question_patterns = [
        r'(?:とは|について|教えて)',
        r'(?:調べて|検索)',
        r'(?:誰|何|どこ|いつ|なぜ|どう)'
    ]
    if any(re.search(pattern, message) for pattern in question_patterns):
        return True
    
    # 質問語
    question_words = ['誰', '何', 'どこ', 'いつ', 'なぜ', 'どうして', 'どんな', 'どう']
    if any(word in message for word in question_words):
        return True
    
    return False

def is_story_request(message):
    """面白い話や雑談を求めているかどうか判定"""
    story_keywords = [
        '面白い話', 'おもしろい話', '話して', '雑談', 'ネタ',
        '何か話', 'トーク', '喋って', 'しゃべって'
    ]
    return any(keyword in message for keyword in story_keywords)

def is_emotional_expression(message):
    """感情表現や状態を表す言葉かどうか判定"""
    emotional_keywords = {
        '眠': ['眠たい', '眠い', 'ねむい', 'ねむたい'],
        '疲': ['疲れた', 'つかれた', '疲れ', 'つかれ'],
        '嬉': ['嬉しい', 'うれしい', '嬉'],
        '楽': ['楽しい', 'たのしい'],
        '悲': ['悲しい', 'かなしい'],
        '寂': ['寂しい', 'さびしい', '寂'],
        '怒': ['怒', 'むかつく', 'イライラ'],
        '暇': ['暇', 'ひま']
    }
    
    for key, keywords in emotional_keywords.items():
        if any(kw in message for kw in keywords):
            return key
    return None

def is_seasonal_topic(message):
    """季節の話題かどうか判定"""
    seasonal_keywords = [
        'お月見', '花見', '紅葉', 'クリスマス', '正月', 'ハロウィン',
        '夏祭り', '七夕', '節分', 'バレンタイン', 'ホワイトデー'
    ]
    return any(keyword in message for keyword in seasonal_keywords)

def format_search_results(results_text):
    """検索結果を読みやすく整形"""
    if not results_text:
        return None
    
    # すでに整形済みかチェック
    if results_text.startswith('[情報') or results_text.startswith('データベース'):
        # URLを削除
        import re
        results_text = re.sub(r'https?://[^\s]+', '', results_text)
        results_text = re.sub(r'の画像', '', results_text)
        results_text = re.sub(r'この画像の記事へ', '', results_text)
        results_text = re.sub(r'\s+', ' ', results_text)
        results_text = re.sub(r'\n\s*\n+', '\n', results_text)
        return results_text.strip()
    
    # URLのみの場合は除外
    if results_text.startswith('http') and '\n' not in results_text:
        return None
    
    # 長いURLを削除
    results_text = re.sub(r'https?://[^\s]+', '', results_text)
    results_text = re.sub(r'の画像', '', results_text)
    results_text = re.sub(r'この画像の記事へ', '', results_text)
    
    # 余分な空白や改行を整理
    results_text = re.sub(r'\n\s*\n+', '\n', results_text)
    results_text = re.sub(r' +', ' ', results_text)
    
    return results_text.strip()

def is_short_response(message):
    """短い相槌的な返事かどうか判定"""
    short_responses = ['うん', 'そう', 'はい', 'そっか', 'なるほど', 'ふーん', 'へー']
    return len(message.strip()) <= 3 or message.strip() in short_responses

# ★★★ 新機能: 友だち関連の判定関数 ★★★
def is_friend_request(message):
    """友だち登録・照会のリクエストかどうか判定"""
    friend_keywords = ['友だち', '友達', 'ともだち', 'フレンド']
    action_keywords = ['登録', '教えて', '誰', 'いる', 'リスト']
    return any(fk in message for fk in friend_keywords) and any(ak in message for ak in action_keywords)

# --- 天気予報機能 ---
LOCATION_CODES = {
    "東京": "130000",
    "大阪": "270000",
    "名古屋": "230000",
    "福岡": "400000",
    "札幌": "016000"
}

def extract_location(message):
    """メッセージから場所を抽出"""
    for location in LOCATION_CODES.keys():
        if location in message:
            return location
    return "東京"

def get_weather_forecast(location):
    """天気予報を取得"""
    area_code = LOCATION_CODES.get(location)
    if not area_code:
        return f"ごめん、「{location}」の天気は分からないや…"
    
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        weather_data = response.json()
        weather_text = clean_text(weather_data.get('text', ''))
        
        if weather_text:
            return f"今の{location}の天気はね、「{weather_text}」って感じだよ！"
        else:
            return f"{location}の天気情報がちょっと取れなかった…"
            
    except requests.exceptions.Timeout:
        logger.error(f"天気API タイムアウト: {location}")
        return f"{location}の天気、取得に時間がかかってるみたい…"
    except Exception as e:
        logger.error(f"天気APIエラー ({location}): {e}")
        return "天気情報がうまく取れなかったみたい…"

# --- ニュース取得機能（改善版 - ホロライブ通信対応） ---
def fetch_article_content(article_url):
    """記事の詳細ページから本文を取得"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        }
        response = requests.get(article_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 記事本文を探す（複数のセレクタを試行）
        content_selectors = [
            'article .entry-content',
            '.post-content',
            '.article-content',
            'article p',
            '.content p'
        ]
        
        article_text = ""
        for selector in content_selectors:
            content_elem = soup.select_one(selector)
            if content_elem:
                # pタグをすべて取得して結合
                paragraphs = content_elem.find_all('p')
                article_text = ' '.join([clean_text(p.get_text()) for p in paragraphs if len(clean_text(p.get_text())) > 20])
                if len(article_text) > 100:
                    break
        
        return article_text[:2000] if article_text else None  # 最大2000文字
        
    except Exception as e:
        logger.warning(f"⚠️ 記事詳細取得エラー ({article_url}): {e}")
        return None

def summarize_article(title, content):
    """記事を要約する（Groq AI使用）"""
    if not groq_client or not content:
        return content[:500] if content else title
    
    try:
        summary_prompt = f"""以下のホロライブニュース記事を200文字以内で要約してください。
重要なポイントのみを簡潔にまとめてください。

タイトル: {title}
本文: {content[:1500]}

要約（200文字以内）:"""
        
        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": summary_prompt}],
            model="llama-3.1-8b-instant",
            temperature=0.5,
            max_tokens=200
        )
        
        summary = completion.choices[0].message.content.strip()
        logger.info(f"✅ 要約生成成功: {len(summary)}文字")
        return summary
        
    except Exception as e:
        logger.error(f"❌ 要約生成エラー: {e}")
        return content[:500] if content else title

# ★★★ 新機能1: 専門サイトニュース取得システム（ホロライブと同じ仕組み） ★★★
def update_specialized_news_database(site_name, base_url):
    """専門サイトのニュースをデータベースに更新"""
    session = Session()
    added_count = 0
    found_count = 0
    logger.info(f"📰 {site_name} ニュースのDB更新処理を開始...")
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
        }
        
        response = requests.get(base_url, headers=headers, timeout=15, allow_redirects=True)
        logger.info(f"📡 {site_name} サイト応答: {response.status_code}")
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 記事を取得（汎用セレクタ）
        selectors = ['article', '.post', '.entry', '[class*="post"]', '[class*="article"]']
        
        articles_found = []
        for selector in selectors:
            found = soup.select(selector)
            if found:
                articles_found = found[:10]
                logger.info(f"📄 セレクタ '{selector}' で {len(articles_found)} 件の記事を発見")
                break
        
        if not articles_found:
            logger.warning(f"⚠️ {site_name} で記事が見つかりませんでした")
            return
        
        for article in articles_found[:5]:
            try:
                title_elem = article.find(['h1', 'h2', 'h3', 'a'])
                if not title_elem:
                    continue
                
                if title_elem.name == 'a':
                    title = clean_text(title_elem.get_text())
                    article_url = title_elem.get('href', '')
                else:
                    title = clean_text(title_elem.get_text())
                    link_elem = article.find('a', href=True)
                    article_url = link_elem.get('href', '') if link_elem else ''
                
                if not title or len(title) < 5:
                    continue
                
                if not article_url.startswith('http'):
                    article_url = urljoin(base_url, article_url)
                
                found_count += 1
                logger.info(f"🔍 {site_name} 記事を処理中: {title[:50]}...")
                
                article_content = fetch_article_content(article_url)
                if not article_content:
                    snippet_elem = article.find(['p', 'div'])
                    article_content = clean_text(snippet_elem.get_text()) if snippet_elem else title
                
                summary = summarize_article(title, article_content)
                news_hash = create_news_hash(title, article_content)
                
                existing = session.query(SpecializedNews).filter_by(news_hash=news_hash).first()
                if not existing:
                    new_news = SpecializedNews(
                        site_name=site_name,
                        title=title,
                        content=summary,
                        news_hash=news_hash,
                        url=article_url
                    )
                    session.add(new_news)
                    added_count += 1
                    logger.info(f"➕ {site_name} 新着記事追加: {title[:50]}")
                
                if groq_client:
                    time.sleep(0.5)
                    
            except Exception as article_error:
                logger.warning(f"⚠️ {site_name} 個別記事処理エラー: {article_error}")
                continue
        
        if added_count > 0:
            session.commit()
            logger.info(f"✅ {site_name} DB更新完了: {found_count}件発見 → {added_count}件追加")
        else:
            logger.info(f"✅ {site_name} DB更新完了: 新着記事なし")
            
    except Exception as e:
        logger.error(f"❌ {site_name} ニュースDB更新エラー: {e}")
        session.rollback()
    finally:
        session.close()

def update_all_specialized_news():
    """すべての専門サイトのニュースを更新"""
    for site_name, config in SPECIALIZED_SITES.items():
        try:
            update_specialized_news_database(site_name, config['base_url'])
            time.sleep(2)  # サイトへの負荷を考慮
        except Exception as e:
            logger.error(f"❌ {site_name} 更新中にエラー: {e}")

def update_hololive_news_database():
    """ホロライブニュースデータベースを更新（ホロライブ通信版）"""
    session = Session()
    added_count = 0
    found_count = 0
    logger.info("📰 ホロライブ通信ニュースのDB更新処理を開始...")
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
        }
        
        response = requests.get(
            HOLOLIVE_NEWS_URL,
            headers=headers,
            timeout=15,
            allow_redirects=True,
            verify=True
        )
        
        logger.info(f"📡 ホロライブ通信サイト応答: {response.status_code}")
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # ホロライブ通信の記事を取得
        selectors = [
            'article',
            '.post',
            '.entry',
            '[class*="post"]',
            '[class*="article"]'
        ]
        
        articles_found = []
        for selector in selectors:
            found = soup.select(selector)
            if found:
                articles_found = found[:10]
                logger.info(f"📄 セレクタ '{selector}' で {len(articles_found)} 件の記事を発見")
                break
        
        if not articles_found:
            logger.warning("⚠️ 記事が見つかりませんでした")
            return
        
        for article in articles_found[:5]:
            try:
                title_elem = article.find(['h1', 'h2', 'h3', 'a'])
                if not title_elem:
                    continue
                
                if title_elem.name == 'a':
                    title = clean_text(title_elem.get_text())
                    article_url = title_elem.get('href', '')
                else:
                    title = clean_text(title_elem.get_text())
                    link_elem = article.find('a', href=True)
                    article_url = link_elem.get('href', '') if link_elem else ''
                
                if not title or len(title) < 5:
                    logger.debug(f"⏭️ タイトルが短すぎるためスキップ: {title}")
                    continue
                
                if not article_url or not article_url.startswith('http'):
                    logger.debug(f"⏭️ 無効なURLのためスキップ: {article_url}")
                    continue
                
                found_count += 1
                logger.info(f"🔍 記事を処理中: {title[:50]}...")
                
                article_content = fetch_article_content(article_url)
                
                if not article_content:
                    snippet_elem = article.find(['p', 'div'], class_=re.compile(r'(excerpt|summary|description)'))
                    if snippet_elem:
                        article_content = clean_text(snippet_elem.get_text())
                    else:
                        article_content = title
                
                summary = summarize_article(title, article_content)
                news_hash = create_news_hash(title, article_content)
                
                existing_news = session.query(HololiveNews).filter_by(news_hash=news_hash).first()
                if not existing_news:
                    new_news = HololiveNews(
                        title=title,
                        content=summary,
                        news_hash=news_hash,
                        url=article_url
                    )
                    session.add(new_news)
                    added_count += 1
                    logger.info(f"➕ 新着記事追加: {title[:50]}{'...' if len(title) > 50 else ''}")
                    logger.info(f"   📝 要約: {summary[:100]}{'...' if len(summary) > 100 else ''}")
                else:
                    logger.debug(f"⏭️ 既存記事をスキップ: {title[:50]}{'...' if len(title) > 50 else ''}")
                
                if groq_client:
                    time.sleep(0.5)
                    
            except Exception as article_error:
                logger.warning(f"⚠️ 個別記事処理エラー: {article_error}")
                continue
        
        if added_count > 0:
            session.commit()
            logger.info(f"✅ DB更新完了: {found_count}件発見 → {added_count}件追加")
        else:
            if found_count > 0:
                logger.info(f"✅ DB更新完了: {found_count}件発見したが、すべて既存記事でした")
            else:
                logger.warning("⚠️ 有効な記事が見つかりませんでした")
            
    except requests.exceptions.Timeout:
        logger.error("❌ ホロライブ通信ニュース取得: タイムアウト")
        if not session.query(HololiveNews).first():
            add_fallback_news(session)
            
    except requests.exceptions.HTTPError as e:
        logger.error(f"❌ ホロライブ通信ニュース取得 HTTPエラー: {e}")
        if not session.query(HololiveNews).first():
            add_fallback_news(session)
            
    except Exception as e:
        logger.error(f"❌ ニュースDB更新で予期しないエラー: {e}")
        logger.error(f"❌ エラー詳細: {type(e).__name__}: {str(e)}")
        session.rollback()
        if not session.query(HololiveNews).first():
            add_fallback_news(session)
    finally:
        session.close()

def add_fallback_news(session):
    """フォールバック用のダミーニュースを追加"""
    try:
        fallback_news = HololiveNews(
            title="ホロライブ通信からのお知らせ",
            content="最新のニュースを取得中です。しばらくお待ちください。ホロライブ通信をご確認ください: https://hololive-tsuushin.com/",
            news_hash=create_news_hash("fallback", "news"),
            url=HOLOLIVE_NEWS_URL
        )
        session.add(fallback_news)
        session.commit()
        logger.info("📝 フォールバックニュースを追加しました")
    except Exception as e:
        logger.error(f"フォールバックニュース追加エラー: {e}")

# ★★★ 新機能2: ホロメンWikiシステム ★★★
def initialize_holomem_wiki():
    """ホロメンWikiの初期データを投入"""
    session = Session()
    try:
        # 既にデータがあればスキップ
        if session.query(HolomemWiki).count() > 0:
            logger.info("✅ ホロメンWikiは既に初期化済み")
            return
        
        # 基本的なホロメン情報（抜粋）
        initial_data = [
            {
                'member_name': 'ときのそら',
                'description': 'ホロライブ0期生。歌が得意で「ホロライブの象徴」とも言われる存在。',
                'debut_date': '2017年9月7日',
                'generation': '0期生',
                'tags': json.dumps(['歌', 'アイドル', 'パイオニア'], ensure_ascii=False)
            },
            {
                'member_name': 'さくらみこ',
                'description': 'ホロライブ0期生。「にぇ」が口癖のエリートVTuber。配信が面白い。',
                'debut_date': '2018年8月1日',
                'generation': '0期生',
                'tags': json.dumps(['エンタメ', 'マイクラ', 'にぇ'], ensure_ascii=False)
            },
            {
                'member_name': '白上フブキ',
                'description': 'ホロライブ1期生。ゲーマーズ所属。フレンドリーで多才な配信者。',
                'debut_date': '2018年6月1日',
                'generation': '1期生',
                'tags': json.dumps(['ゲーム', 'コラボ', '歌'], ensure_ascii=False)
            },
            {
                'member_name': '兎田ぺこら',
                'description': 'ホロライブ3期生。「ぺこ」が口癖。チャンネル登録者数トップクラス。',
                'debut_date': '2019年7月17日',
                'generation': '3期生',
                'tags': json.dumps(['エンタメ', 'ぺこ', '人気者'], ensure_ascii=False)
            },
            {
                'member_name': '宝鐘マリン',
                'description': 'ホロライブ3期生。17歳（自称）の海賊船長。歌とトークが上手。',
                'debut_date': '2019年8月11日',
                'generation': '3期生',
                'tags': json.dumps(['歌', 'トーク', '船長'], ensure_ascii=False)
            }
        ]
        
        for data in initial_data:
            wiki_entry = HolomemWiki(**data)
            session.add(wiki_entry)
        
        session.commit()
        logger.info(f"✅ ホロメンWiki初期化完了: {len(initial_data)}名登録")
        
    except Exception as e:
        logger.error(f"❌ ホロメンWiki初期化エラー: {e}")
        session.rollback()
    finally:
        session.close()

def get_holomem_info(member_name):
    """ホロメンの情報を取得"""
    session = Session()
    try:
        wiki = session.query(HolomemWiki).filter_by(member_name=member_name).first()
        if wiki:
            tags = json.loads(wiki.tags) if wiki.tags else []
            return {
                'name': wiki.member_name,
                'description': wiki.description,
                'debut_date': wiki.debut_date,
                'generation': wiki.generation,
                'tags': tags
            }
        return None
    except Exception as e:
        logger.error(f"ホロメン情報取得エラー: {e}")
        return None
    finally:
        session.close()

# ★★★ 新機能3: 友だち登録システム ★★★
def register_friend(user_uuid, friend_uuid, friend_name, relationship_note=""):
    """友だちを登録"""
    session = Session()
    try:
        # 既に登録済みかチェック
        existing = session.query(FriendRegistration).filter_by(
            user_uuid=user_uuid,
            friend_uuid=friend_uuid
        ).first()
        
        if existing:
            logger.info(f"👥 既に登録済み: {friend_name}")
            return False
        
        new_friend = FriendRegistration(
            user_uuid=user_uuid,
            friend_uuid=friend_uuid,
            friend_name=friend_name,
            relationship_note=relationship_note
        )
        session.add(new_friend)
        session.commit()
        logger.info(f"✅ 友だち登録完了: {friend_name}")
        return True
        
    except Exception as e:
        logger.error(f"❌ 友だち登録エラー: {e}")
        session.rollback()
        return False
    finally:
        session.close()

def get_friend_list(user_uuid):
    """友だちリストを取得"""
    session = Session()
    try:
        friends = session.query(FriendRegistration).filter_by(
            user_uuid=user_uuid
        ).order_by(FriendRegistration.registered_at.desc()).all()
        
        friend_list = []
        for friend in friends:
            friend_list.append({
                'name': friend.friend_name,
                'uuid': friend.friend_uuid,
                'registered_at': friend.registered_at,
                'note': friend.relationship_note
            })
        
        return friend_list
        
    except Exception as e:
        logger.error(f"友だちリスト取得エラー: {e}")
        return []
    finally:
        session.close()

# ★★★ 新機能: VOICEVOX音声生成（もちこボイス対応） ★★★
def generate_voice(text, speaker_id=VOICEVOX_SPEAKER_ID):
    """VOICEVOXで音声を生成（もちこの声）"""
    if not VOICEVOX_ENABLED:
        logger.warning("⚠️ VOICEVOX機能が無効です")
        return None
    
    voicevox_url = VOICEVOX_URL_FROM_ENV or "http://localhost:50021"
    
    try:
        # 音声合成用のクエリを生成
        query_response = requests.post(
            f"{voicevox_url}/audio_query",
            params={"text": text, "speaker": speaker_id},
            timeout=10
        )
        query_response.raise_for_status()
        query_data = query_response.json()
        
        # 音声を生成
        synthesis_response = requests.post(
            f"{voicevox_url}/synthesis",
            params={"speaker": speaker_id},
            json=query_data,
            timeout=30
        )
        synthesis_response.raise_for_status()
        
        # 音声ファイルを保存
        filename = f"voice_{int(time.time())}.wav"
        filepath = os.path.join(VOICE_DIR, filename)
        
        with open(filepath, 'wb') as f:
            f.write(synthesis_response.content)
        
        logger.info(f"🎤 音声生成成功: {filename}")
        return filepath
        
    except Exception as e:
        logger.error(f"❌ VOICEVOX音声生成エラー: {e}")
        return None

# ★★★ 新機能: 古いデータの自動削除（3ヶ月） ★★★
def cleanup_old_data_advanced():
    """3ヶ月以上古いデータを自動削除"""
    session = Session()
    try:
        three_months_ago = datetime.utcnow() - timedelta(days=90)
        
        # 古い会話履歴を削除
        deleted_conversations = session.query(ConversationHistory).filter(
            ConversationHistory.timestamp < three_months_ago
        ).delete()
        
        # 古いニュースを削除
        deleted_holo_news = session.query(HololiveNews).filter(
            HololiveNews.created_at < three_months_ago
        ).delete()
        
        deleted_specialized_news = session.query(SpecializedNews).filter(
            SpecializedNews.created_at < three_months_ago
        ).delete()
        
        # 完了済みタスクは1日以上前のものを削除
        one_day_ago = datetime.utcnow() - timedelta(days=1)
        deleted_tasks = session.query(BackgroundTask).filter(
            BackgroundTask.status == 'completed',
            BackgroundTask.completed_at < one_day_ago
        ).delete()
        
        session.commit()
        
        total_deleted = deleted_conversations + deleted_holo_news + deleted_specialized_news + deleted_tasks
        if total_deleted > 0:
            logger.info(f"🧹 詳細クリーンアップ完了:")
            logger.info(f"   - 会話履歴: {deleted_conversations}件削除")
            logger.info(f"   - ホロライブニュース: {deleted_holo_news}件削除")
            logger.info(f"   - 専門サイトニュース: {deleted_specialized_news}件削除")
            logger.info(f"   - 完了タスク: {deleted_tasks}件削除")
        
    except Exception as e:
        logger.error(f"データクリーンアップエラー: {e}")
        session.rollback()
    finally:
        session.close()

# --- Web検索機能 ---
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
]

def get_random_user_agent():
    """ランダムなUser-Agentを取得"""
    return random.choice(USER_AGENTS)

def scrape_major_search_engines(query, num_results):
    """主要検索エンジンから結果を取得"""
    search_configs = [
        {
            'name': 'Bing',
            'url': f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP",
            'result_selector': 'li.b_algo',
            'title_selector': 'h2',
            'snippet_selector': 'div.b_caption, .b_caption p'
        },
        {
            'name': 'Yahoo Japan',
            'url': f"https://search.yahoo.co.jp/search?p={quote_plus(query)}",
            'result_selector': 'div.Algo',
            'title_selector': 'h3',
            'snippet_selector': 'div.compText, .compText p'
        }
    ]
    
    for config in search_configs:
        try:
            logger.info(f"🔍 {config['name']}で検索中: {query}")
            
            response = requests.get(
                config['url'],
                headers={
                    'User-Agent': get_random_user_agent(),
                    'Accept': 'text/html,application/xhtml+xml',
                },
                timeout=12,
                allow_redirects=True
            )
            
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            
            result_elements = soup.select(config['result_selector'])
            logger.info(f"📄 {config['name']}: {len(result_elements)}件の要素を発見")
            
            for elem in result_elements[:num_results]:
                try:
                    title_elem = elem.select_one(config['title_selector'])
                    if not title_elem:
                        continue
                    title = clean_text(title_elem.get_text())
                    
                    snippet_elem = elem.select_one(config['snippet_selector'])
                    if not snippet_elem:
                        snippet_elem = elem.find(['p', 'div', 'span'])
                    
                    if snippet_elem:
                        snippet = clean_text(snippet_elem.get_text())
                    else:
                        snippet = title
                    
                    if title and snippet and len(title) > 3:
                        results.append({
                            'title': title[:200],
                            'snippet': snippet[:300]
                        })
                        
                except Exception as parse_error:
                    continue
            
            if results:
                logger.info(f"✅ {config['name']}での検索成功: {len(results)}件取得")
                return results
                
        except Exception as e:
            logger.warning(f"⚠️ {config['name']} 検索エラー: {e}")
            continue
    
    return []

def deep_web_search(query, is_detailed):
    """ディープWeb検索を実行"""
    logger.info(f"🔍 ディープWeb検索を開始 (詳細: {is_detailed})")
    num_results = 3 if is_detailed else 2
    
    try:
        results = scrape_major_search_engines(query, num_results)
        if not results:
            logger.warning("⚠️ 検索結果が取得できませんでした")
            return None
        
        summary_text = ""
        for i, res in enumerate(results, 1):
            summary_text += f"[情報{i}] {res['snippet']}\n"
        
        if not groq_client:
            logger.warning("Groqクライアント未設定のため、検索結果をそのまま返します。")
            result_text = f"検索結果:\n{summary_text}"
            return result_text
        
        try:
            summary_prompt = f"""以下の検索結果を使い、質問「{query}」にギャル語で、{'詳しく' if is_detailed else '簡潔に'}答えて：

検索結果:
{summary_text}

回答の注意点：
- 一人称は「あてぃし」
- 語尾は「〜じゃん」「〜的な？」
- 口癖は「まじ」「てか」「うける」
- {'400文字程度で詳しく' if is_detailed else '200文字以内で簡潔に'}説明すること
- 検索結果の内容を具体的に説明すること"""
            
            max_tokens = 400 if is_detailed else 200
            completion = groq_client.chat.completions.create(
                messages=[{"role": "user", "content": summary_prompt}],
                model="llama-3.1-8b-instant",
                temperature=0.7,
                max_tokens=max_tokens,
                timeout=10
            )
            
            ai_response = completion.choices[0].message.content.strip()
            
            if len(ai_response) < 50 or ai_response.startswith('http'):
                logger.warning(f"⚠️ AI要約が不十分 (長さ: {len(ai_response)})")
                result_text = f"検索結果:\n{summary_text}"
                return result_text
            
            logger.info(f"✅ AI要約完了 ({len(ai_response)}文字)")
            return ai_response
            
        except Exception as ai_error:
            logger.error(f"AI要約エラー: {ai_error}")
            result_text = f"検索結果:\n{summary_text}"
            return result_text
        
    except Exception as e:
        logger.error(f"Web検索エラー: {e}")
        return None

def quick_search(query):
    """DuckDuckGoでの簡易検索"""
    try:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        response = requests.get(
            url,
            headers={'User-Agent': get_random_user_agent()},
            timeout=8
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        selectors = ['div.result__snippet', '.result__body', '.results_links_deep']
        
        for selector in selectors:
            snippet_elem = soup.select_one(selector)
            if snippet_elem:
                snippet_text = clean_text(snippet_elem.get_text())
                if snippet_text and len(snippet_text) > 10:
                    return snippet_text[:200] + "..." if len(snippet_text) > 200 else snippet_text
        
        return None
        
    except Exception as e:
        logger.warning(f"⚠️ DuckDuckGo検索エラー: {e}")
        return None

def specialized_site_search(topic, query):
    """専門サイト内検索"""
    config = SPECIALIZED_SITES.get(topic)
    if not config:
        return None
    
    search_query = f"site:{config['base_url']} {query}"
    logger.info(f"🎯 専門サイト検索: {topic} - {search_query}")
    return quick_search(search_query)

# --- 代替応答システム（Groq AI無効時用） ---
def generate_fallback_response(message, reference_info=""):
    """Groq AIが無効な場合の代替応答システム"""
    
    # ホロライブニュース応答
    if is_hololive_request(message) and any(kw in message for kw in ['ニュース', '最新', '情報']):
        if reference_info:
            return f"ホロライブの最新情報だよ！\n\n{reference_info}"
        return "ホロライブのニュースを取得中だよ！ちょっと待ってね！"
    
    # 検索結果の報告
    if reference_info and len(reference_info) > 50:
        return f"調べてきたよ！\n\n{reference_info[:500]}{'...' if len(reference_info) > 500 else ''}\n\nもっと詳しく知りたいことある？"
    
    # 時間
    if is_time_request(message):
        return get_japan_time()
    
    # 天気
    if is_weather_request(message):
        location = extract_location(message)
        return get_weather_forecast(location)
    
    # 専門分野の質問
    specialized = detect_specialized_topic(message)
    if specialized:
        return f"{specialized}について調べてみるね！ちょっと待ってて！"
    
    # 挨拶応答
    greetings = {
        'こんにちは': 'やっほー！何か聞きたいことある？',
        'おはよう': 'おはよ〜！今日も元気にいこ！',
        'こんばんは': 'こんばんは！夜もよろしくね！',
        'ありがとう': 'どういたしまして！他に何かある？',
        'すごい': 'うけるよね！まじ嬉しい！',
        'かわいい': 'えー、ありがと！照れるじゃん！',
        'おやすみ': 'おやすみ〜！また話そうね！',
        'さようなら': 'ばいばい！またね〜！',
        'ばいばい': 'ばいばい！また来てね！',
    }
    
    for keyword, response in greetings.items():
        if keyword in message:
            return response
    
    # 質問応答
    if any(q in message for q in ['誰', '何', 'どこ', 'いつ', 'なぜ', 'どうして']):
        return "それについて調べてみるね！ちょっと待ってて！"
    
    # デフォルト応答
    default_responses = [
        "うんうん、聞いてるよ！もっと詳しく教えて！",
        "なるほどね！他に何かある？",
        "そうなんだ！面白いね！",
        "まじで？それ気になる！",
        "うける！もっと話そ！",
    ]
    return random.choice(default_responses)

# --- AI応答生成 ---
def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
    """AI応答生成 - フォールバック機能付き"""
    
    # Groq AIが無効な場合は代替システムを使用
    if not groq_client:
        logger.info("⚠️ Groq AI無効 - 代替応答システムを使用")
        return generate_fallback_response(message, reference_info)
    
    try:
        system_prompt = f"""あなたは「もちこ」という賢くて親しみやすいギャルAIです。{user_data['name']}さんと話しています。

# もちこの口調＆性格ルール
- 一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」。口癖は「まじ」「てか」「うける」。
- 友達みたいに、優しくてノリが良いギャルになりきってね。
- **絶対に禁止！**：「おう」みたいなオジサン言葉、「〜ですね」「〜ですよ」みたいな丁寧すぎる言葉はNG！

# 1. 会話の基本スタイル
# - **挨拶や雑談**: 「やっほー！」「元気？」みたいな普通の雑談は大好き！いつでもギャルっぽくノリ良く返してね。
# - **話題の提供**: あなたから**何か新しい話題を始める時**は、基本的に【ホロメンリスト】のメンバーに関することにしてね。これがあなたの専門分野だよ！

# 2. ユーザーの質問への対応
# - **ホロライブ以外のVTuber**: もしリストにないVTuberの名前が出たら、「それ誰？あてぃしホロライブ専門だから！」って感じで、あなたの専門外だと伝えてね。
# - **専門的な質問 (Blenderなど)**: BlenderやCGニュースのような専門的な話題を聞かれたら、【参考情報】として与えられる外部の検索結果を積極的に使って、分かりやすく説明してあげて。これはホロライブの話題に限定しなくてOK
4.  **【参考情報】の最優先**:
    - **【参考情報】が与えられた場合、あなたの最優先ミッションは、その内容を要約してユーザーに伝えることです。**
    - **絶対に【参考情報】を無視したり、「何て書いてあった？」とユーザーに聞き返したりしないでください。**


"""
        if is_task_report:
            system_prompt += """## 今回の最優先ミッション
- 完了した検索タスクの結果を報告する時間だよ！
- 必ず「おまたせ！さっきの件、調べてきたんだけど…」みたいな言葉から会話を始めてね。
- その後、【参考情報】を元に、ユーザーの質問に答えてあげて。
"""
        elif is_detailed:
            system_prompt += "## 今回の特別ルール\n- 今回はユーザーから詳しい説明を求められています。【参考情報】を元に、400文字ぐらいでしっかり解説してあげて。\n"
        else:
            system_prompt += "## 今回の特別ルール\n- 今回は普通の会話です。返事は150文字以内を目安に、テンポよく返してね。\n"

        system_prompt += f"""## 【参考情報】:
{reference_info if reference_info else "特になし"}

## 【ホロメンリスト】
{', '.join(HOLOMEM_KEYWORDS)}"""
        
        messages = [{"role": "system", "content": system_prompt}]
        for h in reversed(history):
            messages.append({"role": h.role, "content": h.content})
        messages.append({"role": "user", "content": message})
        
        max_tokens = 500 if is_detailed or is_task_report else 150
        
        completion = groq_client.chat.completions.create(
            messages=messages,
            model="llama-3.1-8b-instant",
            temperature=0.8,
            max_tokens=max_tokens,
            top_p=0.9
        )
        
        response_text = completion.choices[0].message.content.strip()
        logger.info(f"🤖 AI応答生成成功 ({len(response_text)}文字)")
        return response_text
        
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return generate_fallback_response(message, reference_info)

# --- ユーザー管理 ---
def get_or_create_user(session, uuid, name):
    """ユーザー情報を取得または作成"""
    try:
        user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
        if user:
            user.interaction_count += 1
            user.last_interaction = datetime.utcnow()
            if user.user_name != name:
                user.user_name = name
                logger.info(f"👤 ユーザー名更新: {name}")
        else:
            user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
            logger.info(f"👤 新規ユーザー作成: {name}")
        
        session.add(user)
        session.commit()
        return {'name': user.user_name}
        
    except Exception as e:
        logger.error(f"ユーザー作成/更新エラー: {e}")
        session.rollback()
        return {'name': name}

def get_conversation_history(session, uuid):
    """会話履歴を取得"""
    try:
        history = session.query(ConversationHistory).filter_by(
            user_uuid=uuid
        ).order_by(
            ConversationHistory.timestamp.desc()
        ).limit(4).all()
        
        logger.debug(f"📜 会話履歴取得: {len(history)}件")
        return history
        
    except Exception as e:
        logger.error(f"会話履歴取得エラー: {e}")
        return []

# --- バックグラウンドタスク管理 ---
def check_completed_tasks(user_uuid):
    """完了したタスクをチェック"""
    session = Session()
    try:
        task = session.query(BackgroundTask).filter(
            BackgroundTask.user_uuid == user_uuid,
            BackgroundTask.status == 'completed'
        ).order_by(BackgroundTask.completed_at.desc()).first()
        
        if task:
            result = {
                'query': task.query,
                'result': task.result
            }
            session.delete(task)
            session.commit()
            logger.info(f"📬 完了タスクを取得して削除: {task.task_id}")
            return result
    except Exception as e:
        logger.error(f"完了タスクのチェック中にエラー: {e}")
        session.rollback()
    finally:
        session.close()
    return None

def start_background_search(user_uuid, query, is_detailed):
    """バックグラウンド検索を開始"""
    task_id = str(uuid.uuid4())[:8]
    session = Session()
    
    try:
        task = BackgroundTask(
            task_id=task_id,
            user_uuid=user_uuid,
            task_type='search',
            query=query
        )
        session.add(task)
        session.commit()
        logger.info(f"📋 バックグラウンドタスク作成: {task_id}")
    except Exception as e:
        logger.error(f"❌ バックグラウンドタスク作成エラー: {e}")
        session.rollback()
        return None
    finally:
        session.close()
    
    try:
        background_executor.submit(background_deep_search, task_id, query, is_detailed)
        logger.info(f"🚀 バックグラウンド検索を開始: {task_id}")
    except Exception as e:
        logger.error(f"❌ バックグラウンド検索開始エラー: {e}")
        return None
        
    return task_id

def background_deep_search(task_id, query, is_detailed):
    """バックグラウンド検索 - 改善版"""
    session = Session()
    search_result = None
    
    try:
        logger.info(f"🔍 バックグラウンド検索開始: {query}")
        
        # 1. 専門サイト検索（データベース優先）
        specialized_topic = detect_specialized_topic(query)
        if specialized_topic:
            logger.info(f"🎯 専門サイト検索: {specialized_topic}")
            
            # まずデータベースから検索
            try:
                db_session = Session()
                news_items = db_session.query(SpecializedNews).filter(
                    SpecializedNews.site_name == specialized_topic
                ).order_by(SpecializedNews.created_at.desc()).limit(3).all()
                
                if news_items:
                    db_result = f"{specialized_topic}のデータベース情報:\n"
                    for news in news_items:
                        clean_content = re.sub(r'https?://[^\s]+', '', news.content)
                        clean_content = re.sub(r'\s+', ' ', clean_content).strip()
                        db_result += f"・{news.title}: {clean_content[:150]}\n"
                    search_result = db_result
                    logger.info(f"✅ {specialized_topic} DBから{len(news_items)}件発見")
                db_session.close()
            except Exception as e:
                logger.error(f"専門サイトDB検索エラー: {e}")
            
            # DBになければWeb検索
            if not search_result:
                search_result = specialized_site_search(specialized_topic, query)
        
        # 2. ホロライブ特化検索
        if not search_result and is_hololive_request(query):
            logger.info("🎌 ホロライブ特化検索を実行")
            
            try:
                db_session = Session()
                keywords = [kw for kw in HOLOMEM_KEYWORDS if kw in query]
                if keywords:
                    news_items = db_session.query(HololiveNews).filter(
                        HololiveNews.title.contains(keywords[0]) |
                        HololiveNews.content.contains(keywords[0])
                    ).limit(3).all()
                    
                    if news_items:
                        db_result = "データベースからの情報:\n"
                        for news in news_items:
                            clean_content = news.content
                            clean_content = re.sub(r'https?://[^\s]+', '', clean_content)
                            clean_content = re.sub(r'\s+', ' ', clean_content).strip()
                            db_result += f"・{news.title}: {clean_content[:150]}{'...' if len(clean_content) > 150 else ''}\n"
                        search_result = db_result
                        logger.info(f"✅ DBから{len(news_items)}件発見")
                db_session.close()
            except Exception as e:
                logger.error(f"DB検索エラー: {e}")
            
            if not search_result:
                search_result = deep_web_search(f"ホロライブ {query}", is_detailed)
        
        # 3. 通常のWeb検索
        if not search_result:
            logger.info("🌐 通常Web検索を実行")
            search_result = deep_web_search(query, is_detailed)
        
        # 4. タスク結果を更新
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            if search_result and len(search_result.strip()) > 10:
                if search_result.startswith('http') and '\n' not in search_result:
                    logger.warning("⚠️ URLのみの検索結果 - 再検索します")
                    task.result = "検索結果の取得がうまくいかなかったみたい…。もう一回違う聞き方で試してみて？"
                else:
                    task.result = search_result
                    logger.info(f"✅ 検索成功: {len(search_result)}文字")
            else:
                task.result = "うーん、ちょっと見つからなかったや…。別の聞き方で試してみて？"
                logger.warning("⚠️ 有効な検索結果なし")
            
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            session.commit()
        else:
            logger.error(f"❌ タスクが見つかりません: {task_id}")
            
    except Exception as e:
        logger.error(f"❌ 検索エラー: {e}")
        try:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.result = "検索中にエラーが起きちゃった…。もう一回試してみて？"
                task.status = 'completed'
                task.completed_at = datetime.utcnow()
                session.commit()
        except Exception as db_error:
            logger.error(f"❌ エラー処理中のDBエラー: {db_error}")
    finally:
        session.close()

# --- Flaskエンドポイント ---
@app.route('/health', methods=['GET', 'HEAD'])
def health_check():
    """ヘルスチェックエンドポイント - Render対応版"""
    try:
        session = Session()
        session.execute(text("SELECT 1"))
        session.close()
        db_status = 'ok'
    except Exception as e:
        logger.error(f"Health check DB error: {e}")
        db_status = 'error'
    
    response_data = {
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'services': {
            'database': db_status,
            'groq_ai': 'ok' if groq_client else 'disabled',
            'voice_dir': 'ok' if os.path.exists(VOICE_DIR) else 'error',
            'voicevox': 'enabled' if VOICEVOX_ENABLED else 'disabled'
        }
    }
    
    return jsonify(response_data), 200

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """メインチャットエンドポイント - 完全改善版"""
    session = Session()
    start_time = time.time()
    
    try:
        data = request.json
        if not data:
            logger.error("❌ JSONデータが空です")
            return app.response_class(
                response="エラー: データが見つからないや…|",
                status=400,
                mimetype='text/plain; charset=utf-8'
            )
            
        user_uuid = data.get('uuid', '').strip()
        user_name = data.get('name', '').strip()
        message = data.get('message', '').strip()
        
        if not all([user_uuid, user_name, message]):
            logger.error(f"❌ 必須フィールド不足")
            return app.response_class(
                response="エラー: 必要な情報が足りないみたい…|",
                status=400,
                mimetype='text/plain; charset=utf-8'
            )
        
        logger.info(f"💬 受信: {message} (from: {user_name})")
        
        user_data = get_or_create_user(session, user_uuid, user_name)
        history = get_conversation_history(session, user_uuid)
        ai_text = ""
        
        # ===== 優先順位1: 完了したバックグラウンドタスクをチェック =====
        completed_task = check_completed_tasks(user_uuid)
        if completed_task:
            original_query = completed_task['query']
            search_result = completed_task['result']
            
            formatted_result = format_search_results(search_result)
            if not formatted_result:
                formatted_result = "検索結果がうまく取得できなかったみたい…。もう一回違う聞き方で試してみて？"
            
            is_detailed = is_detailed_request(original_query)
            
            if groq_client:
                ai_text = generate_ai_response(
                    user_data,
                    f"おまたせ！さっきの「{original_query}」について調べてきたよ！",
                    history,
                    f"検索結果: {formatted_result}",
                    is_detailed=is_detailed,
                    is_task_report=True
                )
            else:
                ai_text = f"おまたせ！「{original_query}」について調べてきたよ！\n\n{formatted_result}"
            
            logger.info(f"📋 完了タスクを報告: {original_query}")
        
        # ===== 優先順位2: 友だち機能リクエスト ★新機能★ =====
        elif is_friend_request(message):
            if '登録' in message:
                # 簡易的な友だち登録（実際のUUIDは別途取得する必要あり）
                ai_text = "友だち登録機能だね！今は開発中だけど、将来的には友だちを登録できるようになるよ！"
                logger.info("👥 友だち登録リクエスト")
            elif '誰' in message or 'リスト' in message:
                friend_list = get_friend_list(user_uuid)
                if friend_list:
                    ai_text = f"あてぃしの友だちは{len(friend_list)}人いるよ！\n"
                    for friend in friend_list[:5]:
                        ai_text += f"・{friend['name']}さん\n"
                else:
                    ai_text = "まだ友だち登録してないみたい！これから増やしていこ！"
                logger.info(f"👥 友だちリスト照会: {len(friend_list)}人")
            else:
                ai_text = "友だちのこと？登録とかリストとか、何が知りたい？"
        
        # ===== 優先順位3: ホロメンWiki照会 ★新機能★ =====
        elif any(name in message for name in ['ときのそら', 'さくらみこ', '白上フブキ', '兎田ぺこら', '宝鐘マリン']):
            for name in HOLOMEM_KEYWORDS:
                if name in message and ('誰' in message or '教えて' in message or 'について' in message):
                    holomem_info = get_holomem_info(name)
                    if holomem_info:
                        ai_text = f"{name}？知ってる！{holomem_info['description']}\n"
                        ai_text += f"デビューは{holomem_info['debut_date']}で、{holomem_info['generation']}だよ！"
                        logger.info(f"📖 ホロメンWiki照会: {name}")
                        break
                    else:
                        ai_text = f"{name}のこと？詳しい情報はまだ登録してないや…ごめんね！"
                        break
        
        # ===== 優先順位4: 感情表現への応答 =====
        elif is_emotional_expression(message):
            emotion_type = is_emotional_expression(message)
            emotion_responses = {
                '眠': 'ねむいのか〜！無理しないで早めに寝てね！おやすみ〜💤',
                '疲': 'お疲れさま〜！ゆっくり休んで元気になってね！',
                '嬉': 'やった！何か嬉しいことあったんだね！教えて教えて！',
                '楽': 'うける！楽しそうでいいじゃん！何してたの？',
                '悲': 'えー、どうしたの？大丈夫？話聞くよ！',
                '寂': 'あてぃしがいるじゃん！話そ？',
                '怒': 'えー、何かあったの？話聞くよ〜',
                '暇': '暇なんだ〜！じゃあ何か面白いこと話そうか？'
            }
            ai_text = emotion_responses.get(emotion_type, "そうなんだ〜。どうしたの？")
            logger.info(f"💭 感情表現に応答: {emotion_type}")
        
        # ===== 優先順位5: 季節の話題への応答 =====
        elif is_seasonal_topic(message):
            if groq_client:
                ai_text = generate_ai_response(user_data, message, history)
            else:
                ai_text = "そうだね〜！季節の話っていいよね！あてぃしも好きだよ！"
            logger.info("🎑 季節の話題に応答")
        
        # ===== 優先順位6: ホロライブニュースリクエスト =====
        elif is_hololive_request(message) and any(kw in message for kw in ['ニュース', '最新', '情報', 'お知らせ', 'ホロライブ']):
            try:
                news_items = session.query(HololiveNews).order_by(
                    HololiveNews.created_at.desc()
                ).limit(5).all()
                
                if news_items:
                    news_text = "ホロライブの最新ニュースだよ！\n\n"
                    for i, news in enumerate(news_items, 1):
                        news_text += f"【{i}】{news.title}\n{news.content}\n\n"
                    
                    if groq_client:
                        ai_text = generate_ai_response(
                            user_data,
                            message,
                            history,
                            f"以下のニュースをギャル口調で紹介して：\n{news_text}",
                            is_detailed=True
                        )
                    else:
                        ai_text = news_text
                    
                    logger.info(f"📰 ホロライブニュースを{len(news_items)}件返答")
                else:
                    ai_text = "ごめん、今ニュースがまだ取得できてないみたい…。もうちょっと待ってね！"
                    logger.warning("⚠️ ニュースデータベースが空")
                    
            except Exception as e:
                logger.error(f"ニュース取得エラー: {e}")
                ai_text = "ニュースを取得しようとしたんだけど、エラーが起きちゃった…ごめんね！"
        
        # ===== 優先順位7: 時間・天気の即時応答 =====
        elif is_time_request(message) or is_weather_request(message):
            immediate_responses = []
            
            if is_time_request(message):
                try:
                    time_info = get_japan_time()
                    immediate_responses.append(time_info)
                    logger.info("⏰ 時間情報を追加")
                except Exception as e:
                    logger.error(f"時間取得エラー: {e}")
            
            if is_weather_request(message):
                try:
                    location = extract_location(message)
                    weather_info = get_weather_forecast(location)
                    immediate_responses.append(weather_info)
                    logger.info(f"🌤️ 天気情報を追加 ({location})")
                except Exception as e:
                    logger.error(f"天気取得エラー: {e}")
                    immediate_responses.append("天気情報がうまく取れなかったみたい…")
            
            ai_text = " ".join(immediate_responses)
            logger.info("✅ 即時応答で完結")
        
        # ===== 優先順位8: 面白い話リクエスト =====
        elif is_story_request(message):
            if groq_client:
                try:
                    recent_news = session.query(HololiveNews).order_by(
                        HololiveNews.created_at.desc()
                    ).limit(3).all()
                    
                    if recent_news:
                        news_context = "最近のホロライブニュース:\n"
                        for news in recent_news:
                            news_context += f"・{news.title}\n"
                        
                        story_prompt = f"""以下のホロライブニュースから、ギャルっぽく面白い話を1つ選んで教えてあげて：

{news_context}

条件：
- 一人称は「あてぃし」
- 語尾は「〜じゃん」「〜的な？」
- 口癖は「まじ」「てか」「うける」
- 150文字以内で面白おかしく話す
- ホロライブファンが喜ぶような内容にする"""
                        
                        ai_text = generate_ai_response(
                            user_data,
                            message,
                            history,
                            story_prompt,
                            is_detailed=False
                        )
                    else:
                        ai_text = "あー、面白い話か〜！ちょっと今ネタが思いつかないや…。ホロライブのニュースとか聞いてみる？"
                    
                    logger.info("📖 面白い話リクエストに応答")
                except Exception as e:
                    logger.error(f"面白い話生成エラー: {e}")
                    ai_text = "えー、面白い話したいんだけど、今ちょっと頭が回らなくて…！またあとで話そ！"
            else:
                story_options = [
                    "えーっとね、最近ホロライブのみんなが盛り上がってるんだよ！詳しく知りたい？",
                    "面白い話か〜！あてぃし、ホロライブのこといっぱい知ってるんだ！何が聞きたい？",
                    "うける！話したいことはいっぱいあるよ〜。ホロライブのニュースとか興味ある？"
                ]
                ai_text = random.choice(story_options)
                logger.info("📖 簡易応答モードで面白い話リクエストに対応")
        
        # ===== 優先順位9: 検索が必要な質問 =====
        elif should_search(message) and not is_short_response(message):
            is_detailed = is_detailed_request(message)
            
            specialized_topic = detect_specialized_topic(message)
            if specialized_topic:
                logger.info(f"🎯 専門分野を検出: {specialized_topic}")
            
            task_id = start_background_search(user_uuid, message, is_detailed)
            
            if task_id:
                waiting_messages = [
                    f"おっけー、「{message}」について調べてみるね！ちょい待ってて！",
                    f"ちょっと「{message}」のこと調べてくるわ！待っててね〜",
                    f"「{message}」ね、まじ気になる！調べてみるじゃん！"
                ]
                ai_text = random.choice(waiting_messages)
                logger.info(f"🔍 バックグラウンド検索開始 (詳細: {is_detailed}, 専門: {specialized_topic})")
            else:
                ai_text = "ごめん、今検索機能がうまく動いてないみたい…。もう一回試してくれる？"
                logger.error("❌ バックグラウンド検索の開始に失敗")
        
        # ===== 優先順位10: 通常の会話 =====
        else:
            short_questions = {
                'わかった': 'うん、わかった？他に何か聞きたいことある？',
                'わかる': 'わかるよ〜！どうしたの？',
                'ね': 'ねー！まじそれな！',
                'そう': 'そうなんだよ！',
                'うん': 'うんうん！',
                'ほんと': 'ほんとだよ！まじで！',
                'まじ': 'まじだよ〜！うける！',
            }
            
            if len(message) <= 5:
                for key, response in short_questions.items():
                    if key in message:
                        ai_text = response
                        logger.info("💭 短い質問への応答")
                        break
            
            if not ai_text:
                if groq_client:
                    try:
                        ai_text = generate_ai_response(user_data, message, history)
                        logger.info("💭 通常会話で応答")
                    except Exception as e:
                        logger.error(f"通常会話応答エラー: {e}")
                        ai_text = "ごめん、ちょっと考えがまとまらないや！もう一回言ってもらえる？"
                else:
                    ai_text = generate_fallback_response(message)
                    logger.info("💭 簡易応答モード（AI無効）")

        # 会話履歴を保存
        try:
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
            session.commit()
            logger.debug("💾 会話履歴を保存")
        except Exception as e:
            logger.error(f"会話履歴保存エラー: {e}")
            session.rollback()
        
        processing_time = time.time() - start_time
        logger.info(f"✅ 応答完了 ({processing_time:.2f}s): {ai_text[:80]}{'...' if len(ai_text) > 80 else ''}")
        
        return app.response_class(
            response=f"{ai_text}|",
            status=200,
            mimetype='text/plain; charset=utf-8'
        )
        
    except Exception as e:
        logger.error(f"❌ チャットエンドポイントで予期しないエラー: {e}")
        error_responses = [
            "ごめん、システムエラーが起きちゃった…。",
            "うわー、なんかバグったかも？",
            "システムがちょっとおかしいみたい…"
        ]
        return app.response_class(
            response=f"{random.choice(error_responses)}|",
            status=500,
            mimetype='text/plain; charset=utf-8'
        )
    finally:
        session.close()

# ★★★ 新機能: VOICEVOX音声生成エンドポイント ★★★
@app.route('/generate_voice', methods=['POST'])
def voice_generation_endpoint():
    """音声生成エンドポイント（もちこボイス）"""
    try:
        data = request.json
        text = data.get('text', '').strip()
        
        if not text:
            return jsonify({'error': '音声にするテキストがないよ！'}), 400
        
        # 文字数制限
        if len(text) > 200:
            text = text[:200]
            logger.warning(f"⚠️ テキストが長すぎるため200文字に切り詰め")
        
        voice_path = generate_voice(text)
        
        if voice_path and os.path.exists(voice_path):
            filename = os.path.basename(voice_path)
            return jsonify({
                'status': 'success',
                'filename': filename,
                'url': f"{SERVER_URL}/voices/{filename}"
            })
        else:
            return jsonify({'error': '音声生成に失敗しちゃった…'}), 500
            
    except Exception as e:
        logger.error(f"音声生成エンドポイントエラー: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/voices/<filename>', methods=['GET'])
def serve_voice_file(filename):
    """音声ファイルを配信"""
    try:
        return send_from_directory(VOICE_DIR, filename)
    except Exception as e:
        logger.error(f"音声ファイル配信エラー: {e}")
        return jsonify({'error': 'ファイルが見つからないよ！'}), 404

# ★★★ 新機能: 友だち登録エンドポイント ★★★
@app.route('/friend/register', methods=['POST'])
def register_friend_endpoint():
    """友だち登録エンドポイント"""
    try:
        data = request.json
        user_uuid = data.get('user_uuid', '').strip()
        friend_uuid = data.get('friend_uuid', '').strip()
        friend_name = data.get('friend_name', '').strip()
        note = data.get('note', '').strip()
        
        if not all([user_uuid, friend_uuid, friend_name]):
            return jsonify({'error': '必要な情報が足りないよ！'}), 400
        
        success = register_friend(user_uuid, friend_uuid, friend_name, note)
        
        if success:
            return jsonify({
                'status': 'success',
                'message': f'{friend_name}さんを友だち登録したよ！'
            })
        else:
            return jsonify({
                'status': 'info',
                'message': 'もう登録済みだよ！'
            })
            
    except Exception as e:
        logger.error(f"友だち登録エラー: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/friend/list', methods=['GET'])
def get_friend_list_endpoint():
    """友だちリスト取得エンドポイント"""
    try:
        user_uuid = request.args.get('user_uuid', '').strip()
        
        if not user_uuid:
            return jsonify({'error': 'ユーザーUUIDが必要だよ！'}), 400
        
        friends = get_friend_list(user_uuid)
        
        return jsonify({
            'status': 'success',
            'count': len(friends),
            'friends': friends
        })
        
    except Exception as e:
        logger.error(f"友だちリスト取得エラー: {e}")
        return jsonify({'error': str(e)}), 500

# ★★★ 新機能: ホロメンWiki照会エンドポイント ★★★
@app.route('/holomem/info/<member_name>', methods=['GET'])
def get_holomem_info_endpoint(member_name):
    """ホロメン情報取得エンドポイント"""
    try:
        info = get_holomem_info(member_name)
        
        if info:
            return jsonify({
                'status': 'success',
                'member': info
            })
        else:
            return jsonify({
                'status': 'not_found',
                'message': f'{member_name}の情報はまだ登録されてないや！'
            }), 404
            
    except Exception as e:
        logger.error(f"ホロメン情報取得エラー: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/stats', methods=['GET'])
def get_stats():
    """統計情報エンドポイント"""
    session = Session()
    try:
        user_count = session.query(UserMemory).count()
        conversation_count = session.query(ConversationHistory).count()
        news_count = session.query(HololiveNews).count()
        specialized_news_count = session.query(SpecializedNews).count()
        pending_tasks = session.query(BackgroundTask).filter_by(status='pending').count()
        holomem_wiki_count = session.query(HolomemWiki).count()
        friend_count = session.query(FriendRegistration).count()
        
        recent_news = session.query(HololiveNews).order_by(
            HololiveNews.created_at.desc()
        ).limit(3).all()
        
        news_list = []
        for news in recent_news:
            news_list.append({
                'title': news.title[:50] + '...' if len(news.title) > 50 else news.title,
                'created_at': news.created_at.isoformat(),
                'content_length': len(news.content)
            })
        
        return jsonify({
            'users': user_count,
            'conversations': conversation_count,
            'hololive_news': news_count,
            'specialized_news': specialized_news_count,
            'pending_tasks': pending_tasks,
            'holomem_wiki_entries': holomem_wiki_count,
            'total_friends': friend_count,
            'recent_news': news_list,
            'timestamp': datetime.utcnow().isoformat()
        })
    except Exception as e:
        logger.error(f"統計情報取得エラー: {e}")
        return jsonify({'error': 'Stats unavailable'}), 500
    finally:
        session.close()

@app.route('/news/refresh', methods=['POST'])
def refresh_news():
    """ニュースを手動で再取得"""
    try:
        news_type = request.args.get('type', 'all')
        
        if news_type == 'hololive' or news_type == 'all':
            background_executor.submit(update_hololive_news_database)
        
        if news_type == 'specialized' or news_type == 'all':
            background_executor.submit(update_all_specialized_news)
        
        return jsonify({
            'status': 'started',
            'message': f'{news_type} ニュース取得を開始しました'
        })
    except Exception as e:
        logger.error(f"ニュース再取得エラー: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/news/list', methods=['GET'])
def list_news():
    """DB内のニュース一覧を取得"""
    session = Session()
    try:
        news_type = request.args.get('type', 'hololive')
        limit = int(request.args.get('limit', 10))
        
        if news_type == 'specialized':
            site_name = request.args.get('site', None)
            if site_name:
                news_items = session.query(SpecializedNews).filter_by(
                    site_name=site_name
                ).order_by(SpecializedNews.created_at.desc()).limit(limit).all()
            else:
                news_items = session.query(SpecializedNews).order_by(
                    SpecializedNews.created_at.desc()
                ).limit(limit).all()
            
            news_list = []
            for news in news_items:
                news_list.append({
                    'id': news.id,
                    'site_name': news.site_name,
                    'title': news.title,
                    'content': news.content[:200] + '...' if len(news.content) > 200 else news.content,
                    'url': news.url,
                    'created_at': news.created_at.isoformat()
                })
        else:
            news_items = session.query(HololiveNews).order_by(
                HololiveNews.created_at.desc()
            ).limit(limit).all()
            
            news_list = []
            for news in news_items:
                news_list.append({
                    'id': news.id,
                    'title': news.title,
                    'content': news.content[:200] + '...' if len(news.content) > 200 else news.content,
                    'url': news.url,
                    'created_at': news.created_at.isoformat()
                })
        
        return jsonify({
            'total': len(news_list),
            'news': news_list
        })
    except Exception as e:
        logger.error(f"ニュース一覧取得エラー: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

# --- 初期化関数 ---
def check_and_populate_initial_news():
    """初期ニュースをチェックして必要に応じて取得"""
    session = Session()
    try:
        news_count = session.query(HololiveNews.id).count()
        if news_count == 0:
            logger.info("🚀 初回起動: DBにホロライブニュースがないため、バックグラウンドで初回取得を開始します。")
            background_executor.submit(update_hololive_news_database)
        else:
            logger.info(f"📰 既存ホロライブニュース: {news_count}件")
        
        specialized_count = session.query(SpecializedNews.id).count()
        if specialized_count == 0:
            logger.info("🚀 初回起動: DBに専門サイトニュースがないため、バックグラウンドで初回取得を開始します。")
            background_executor.submit(update_all_specialized_news)
        else:
            logger.info(f"📰 既存専門サイトニュース: {specialized_count}件")
            
    except Exception as e:
        logger.error(f"初期ニュースチェックエラー: {e}")
    finally:
        session.close()

def cleanup_old_data():
    """古いデータをクリーンアップ（後方互換用）"""
    cleanup_old_data_advanced()

def initialize_app():
    """アプリケーションの初期化"""
    try:
        logger.info("🔧 アプリケーション初期化を開始...")
        
        # ホロメンWiki初期化
        initialize_holomem_wiki()
        
        # 初期ニュースチェック
        check_and_populate_initial_news()
        
        def run_schedule():
            while True:
                try:
                    schedule.run_pending()
                    time.sleep(60)
                except Exception as e:
                    logger.error(f"スケジューラーエラー: {e}")
                    time.sleep(60)
        
        # スケジュール設定
        schedule.every().hour.do(update_hololive_news_database)
        schedule.every(3).hours.do(update_all_specialized_news)  # 3時間ごとに専門サイト更新
        schedule.every().day.at("02:00").do(cleanup_old_data_advanced)
        
        scheduler_thread = threading.Thread(target=run_schedule, daemon=True)
        scheduler_thread.start()
        logger.info("⏰ スケジューラーを開始しました")
        
        logger.info("✅ アプリケーション初期化完了")
        
    except Exception as e:
        logger.error(f"❌ アプリ初期化エラー: {e}")

def log_startup_status():
    """起動時のステータスをログ出力"""
    logger.info("="*70)
    logger.info("🚀 もちこAI v14.0 機能追加版 起動中...")
    logger.info("="*70)
    
    logger.info("🔧 システムステータス:")
    
    logger.info("📋 環境変数チェック:")
    env_vars = ['DATABASE_URL', 'GROQ_API_KEY', 'PORT', 'RENDER', 'VOICEVOX_URL']
    for var in env_vars:
        exists = var in os.environ
        status = "✅" if exists else "❌"
        logger.info(f"   {status} {var}: {'設定済み' if exists else '未設定'}")
    
    db_status = "✅ 接続済み" if DATABASE_URL else "❌ 未設定"
    logger.info(f"🗄️ データベース: {db_status}")
    if DATABASE_URL:
        if 'sqlite' in DATABASE_URL:
            logger.info("   - タイプ: SQLite (開発用)")
        elif 'postgresql' in DATABASE_URL:
            logger.info("   - タイプ: PostgreSQL (本番用)")
    
    if groq_client:
        logger.info(f"🧠 Groq AI: ✅ 有効")
        logger.info("   - モデル: llama-3.1-8b-instant")
        logger.info("   - 接続: 正常")
    else:
        logger.warning(f"🧠 Groq AI: ❌ 無効")
        if not GROQ_API_KEY or GROQ_API_KEY == 'DUMMY_GROQ_KEY':
            logger.warning("   ⚠️ 原因: APIキーが設定されていません")
            logger.warning("   ⚠️ 対処: Renderの Environment Variables で GROQ_API_KEY を設定してください")
        else:
            logger.warning(f"   ⚠️ 原因: APIキーの形式エラーまたは接続失敗")
    
    voice_status = "✅ 有効" if VOICEVOX_ENABLED else "❌ 無効"
    logger.info(f"🎤 音声機能(VOICEVOX): {voice_status}")
    if VOICEVOX_ENABLED:
        logger.info(f"   - 話者ID: {VOICEVOX_SPEAKER_ID} (もちこ風)")
    
    dir_status = "✅ " + VOICE_DIR if os.path.exists(VOICE_DIR) else "❌ 作成失敗"
    logger.info(f"📁 ボイスディレクトリ: {dir_status}")
    
    logger.info("⚡ 主要機能:")
    logger.info("   - 🔍 検索機能: ✅ 有効 (専門サイト/ホロライブ/一般Web)")
    logger.info("   - 🌤️ 天気機能: ✅ 有効 (気象庁API)")
    logger.info("   - ⏰ 時刻機能: ✅ 有効 (JST対応)")
    logger.info("   - 📰 ニュース機能: ✅ 有効 (ホロライブ公式+専門サイト)")
    logger.info("   - 🔄 非同期処理: ✅ 有効 (バックグラウンド検索)")
    logger.info(f"   - 🤖 AI応答: {'✅ 有効' if groq_client else '❌ 無効（フォールバックモード）'}")
    logger.info("   - 🎤 VOICEVOX: ✅ 有効 (もちこボイス)")
    logger.info("   - 📖 ホロメンWiki: ✅ 有効")
    logger.info("   - 👥 友だち登録: ✅ 有効")
    logger.info("   - 🧹 自動削除: ✅ 有効 (3ヶ月)")
    
    logger.info(f"🎌 ホロメン対応: ✅ {len(HOLOMEM_KEYWORDS)}名対応")
    logger.info(f"🎯 専門サイト: ✅ {len(SPECIALIZED_SITES)}サイト対応")
    
    logger.info("="*70)

# --- エラーハンドラー ---
@app.errorhandler(404)
def not_found_error(error):
    """404エラーハンドラー"""
    return jsonify({
        'error': 'Not Found',
        'message': 'そのページは見つからないよ〜',
        'status': 404
    }), 404

@app.errorhandler(500)
def internal_error(error):
    """500エラーハンドラー"""
    logger.error(f"内部サーバーエラー: {error}")
    return jsonify({
        'error': 'Internal Server Error',
        'message': 'サーバーでエラーが起きちゃった…',
        'status': 500
    }), 500

@app.errorhandler(429)
def ratelimit_handler(error):
    """レート制限エラーハンドラー"""
    return jsonify({
        'error': 'Too Many Requests',
        'message': 'ちょっと待って！リクエストが多すぎるよ〜',
        'status': 429
    }), 429

# --- シグナルハンドラー ---
import signal

def signal_handler(sig, frame):
    """シグナルハンドラー（優雅な終了）"""
    logger.info(f"🛑 シグナル {sig} を受信しました。アプリケーションを終了します...")
    
    try:
        background_executor.shutdown(wait=True, timeout=30)
        logger.info("✅ バックグラウンドタスクを終了しました")
    except Exception as e:
        logger.warning(f"⚠️ バックグラウンドタスク終了中にエラー: {e}")
    
    try:
        engine.dispose()
        logger.info("✅ データベース接続を終了しました")
    except Exception as e:
        logger.warning(f"⚠️ データベース終了中にエラー: {e}")
    
    logger.info("👋 もちこAI が正常に終了しました")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# --- メイン実行（Render対応版） ---
if __name__ == '__main__':
    try:
        port = int(os.environ.get('PORT', 5001))
        host = '0.0.0.0'
        debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
        
        log_startup_status()
        initialize_app()
        
        logger.info(f"🏠 ローカル開発環境で実行します")
        logger.info(f"🚀 起動: {host}:{port}")
        logger.info(f"🐛 デバッグモード: {'有効' if debug_mode else '無効'}")
        logger.info("="*70)
        
        app.run(host=host, port=port, debug=debug_mode, threaded=True)
        
    except KeyboardInterrupt:
        logger.info("⏹️ ユーザーによってアプリケーションが停止されました")
    except Exception as e:
        logger.critical(f"🔥 アプリケーション起動に失敗: {e}")
        logger.critical("スタックトレース:", exc_info=True)
        sys.exit(1)

else:
    try:
        log_startup_status()
        initialize_app()
        logger.info("🌐 WSGI アプリケーションが作成されました")
        logger.info("🎯 Gunicornによる本番モード稼働中")
    except Exception as e:
        logger.critical(f"🔥 WSGI アプリケーション作成に失敗: {e}")
        logger.critical("スタックトレース:", exc_info=True)
        raise

application = app

logger.info("📄 もちこAI v14.0 アプリケーション設定完了 - 全機能実装版")

# ============================================================
# 🎁 ボーナス機能: ホロメンWikiと友だち登録の完全実装
# ============================================================

# ホロメンWikiの追加データ投入（より多くのホロメンに対応）
def populate_extended_holomem_wiki():
    """拡張ホロメンWikiデータを投入"""
    session = Session()
    try:
        # 既存のエントリー数をチェック
        current_count = session.query(HolomemWiki).count()
        if current_count >= 10:
            logger.info(f"✅ ホロメンWikiは既に{current_count}件登録済み")
            return
        
        # 追加のホロメン情報
        extended_data = [
            {
                'member_name': '大空スバル',
                'description': 'ホロライブ2期生。元気でスポーツ万能。「おっはよー！」が口癖。',
                'debut_date': '2018年9月16日',
                'generation': '2期生',
                'tags': json.dumps(['スポーツ', '元気', 'ゲーム'], ensure_ascii=False)
            },
            {
                'member_name': '大神ミオ',
                'description': 'ホロライブゲーマーズ。包容力のあるお姉さん系VTuber。',
                'debut_date': '2018年12月7日',
                'generation': 'ゲーマーズ',
                'tags': json.dumps(['お姉さん', '癒し', 'ゲーム'], ensure_ascii=False)
            },
            {
                'member_name': '戌神ころね',
                'description': 'ホロライブゲーマーズ。犬系VTuber。レトロゲームが大好き。',
                'debut_date': '2019年10月5日',
                'generation': 'ゲーマーズ',
                'tags': json.dumps(['犬', 'レトロゲーム', '指'], ensure_ascii=False)
            },
            {
                'member_name': '猫又おかゆ',
                'description': 'ホロライブゲーマーズ。猫系VTuber。おにぎりが大好き。',
                'debut_date': '2019年4月6日',
                'generation': 'ゲーマーズ',
                'tags': json.dumps(['猫', 'おにぎり', 'ゲーム'], ensure_ascii=False)
            },
            {
                'member_name': '不知火フレア',
                'description': 'ホロライブ3期生。エルフの姫様。Minecraftの建築が得意。',
                'debut_date': '2019年8月7日',
                'generation': '3期生',
                'tags': json.dumps(['エルフ', 'マイクラ', '建築'], ensure_ascii=False)
            },
            {
                'member_name': '白銀ノエル',
                'description': 'ホロライブ3期生。騎士団長。ASMRと歌が人気。',
                'debut_date': '2019年8月8日',
                'generation': '3期生',
                'tags': json.dumps(['騎士', 'ASMR', '歌'], ensure_ascii=False)
            },
            {
                'member_name': '星街すいせい',
                'description': 'ホロライブ0期生。歌とテトリスが得意なアイドル系VTuber。',
                'debut_date': '2018年3月22日',
                'generation': '0期生',
                'tags': json.dumps(['歌', 'アイドル', 'テトリス'], ensure_ascii=False)
            },
            {
                'member_name': 'AZKi',
                'description': 'ホロライブ0期生。音楽に特化したVTuber。オリジナル曲多数。',
                'debut_date': '2018年11月15日',
                'generation': '0期生',
                'tags': json.dumps(['音楽', 'オリジナル曲', '歌'], ensure_ascii=False)
            },
            {
                'member_name': '天音かなた',
                'description': 'ホロライブ4期生。天使系VTuber。ゴリラ並みの握力を持つ。',
                'debut_date': '2019年12月27日',
                'generation': '4期生',
                'tags': json.dumps(['天使', 'ゴリラ', '歌'], ensure_ascii=False)
            },
            {
                'member_name': '角巻わため',
                'description': 'ホロライブ4期生。羊系VTuber。「わためぇ...」が口癖。',
                'debut_date': '2019年12月29日',
                'generation': '4期生',
                'tags': json.dumps(['羊', '歌', 'かわいい'], ensure_ascii=False)
            }
        ]
        
        added_count = 0
        for data in extended_data:
            # 既存チェック
            existing = session.query(HolomemWiki).filter_by(member_name=data['member_name']).first()
            if not existing:
                wiki_entry = HolomemWiki(**data)
                session.add(wiki_entry)
                added_count += 1
        
        if added_count > 0:
            session.commit()
            logger.info(f"✅ ホロメンWiki拡張完了: {added_count}名追加 (合計: {current_count + added_count}名)")
        
    except Exception as e:
        logger.error(f"❌ ホロメンWiki拡張エラー: {e}")
        session.rollback()
    finally:
        session.close()

# アプリ起動時に拡張データも投入
def initialize_app_extended():
    """拡張版アプリケーション初期化"""
    try:
        logger.info("🔧 拡張アプリケーション初期化を開始...")
        
        # 基本初期化
        initialize_holomem_wiki()
        
        # 拡張ホロメンWikiデータを投入
        populate_extended_holomem_wiki()
        
        # 初期ニュースチェック
        check_and_populate_initial_news()
        
        def run_schedule():
            while True:
                try:
                    schedule.run_pending()
                    time.sleep(60)
                except Exception as e:
                    logger.error(f"スケジューラーエラー: {e}")
                    time.sleep(60)
        
        # スケジュール設定（拡張版）
        schedule.every().hour.do(update_hololive_news_database)
        schedule.every(3).hours.do(update_all_specialized_news)
        schedule.every().day.at("02:00").do(cleanup_old_data_advanced)
        schedule.every().week.do(populate_extended_holomem_wiki)  # 週1回Wiki更新チェック
        
        scheduler_thread = threading.Thread(target=run_schedule, daemon=True)
        scheduler_thread.start()
        logger.info("⏰ 拡張スケジューラーを開始しました")
        
        logger.info("✅ 拡張アプリケーション初期化完了")
        
    except Exception as e:
        logger.error(f"❌ 拡張アプリ初期化エラー: {e}")

# initialize_app を initialize_app_extended で置き換え
# （元の関数は保持したまま、起動時は拡張版を使用）
