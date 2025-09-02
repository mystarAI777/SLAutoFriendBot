import os
import requests
import logging
import sys
import time
import threading
import json
import re
import sqlite3
import random
from datetime import datetime, timedelta
from typing import Union, Dict, Any, List
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Index, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 設定定数
VOICEVOX_MAX_TEXT_LENGTH = 50
VOICEVOX_FAST_TIMEOUT = 10
CONVERSATION_HISTORY_TURNS = 2
VOICE_DIR = '/tmp/voices'
SERVER_URL = "https://slautofriendbot.onrender.com"

# --- 秘密情報/環境変数 読み込み ---
def get_secret(name: str) -> Union[str, None]:
    """環境変数または秘密ファイルから設定値を取得"""
    env_value = os.environ.get(name)
    if env_value: 
        return env_value
    try:
        with open(f'/etc/secrets/{name}', 'r') as f:
            return f.read().strip()
    except Exception:
        return None

# 必要な設定値を取得
DATABASE_URL = get_secret('DATABASE_URL')
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')
WEATHER_API_KEY = get_secret('WEATHER_API_KEY')

# --- クライアント初期化 ---
groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groqクライアント初期化成功")
    except Exception as e:
        logger.error(f"❌ Groqクライアント初期化エラー: {e}")

# VOICEVOX設定
VOICEVOX_URLS = [
    'http://localhost:50021',
    'http://127.0.0.1:50021',
    'http://voicevox-engine:50021',
    'http://voicevox:50021'
]
WORKING_VOICEVOX_URL = VOICEVOX_URL_FROM_ENV or VOICEVOX_URLS[0]
VOICEVOX_ENABLED = True

# 必須設定チェック
if not all([DATABASE_URL, groq_client]):
    logger.critical("FATAL: 必須設定(DATABASE_URL or GROQ_API_KEY)が不足しています。")
    sys.exit(1)

# Flask & データベース初期化
app = Flask(__name__)
CORS(app)
engine = create_engine(DATABASE_URL)
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

# テーブル作成
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# --- ユーティリティ関数 ---
def clean_text(text: str) -> str:
    """HTMLタグを除去し、テキストをクリーンアップ"""
    if not text:
        return ""
    # HTMLタグ除去
    text = re.sub(r'<[^>]+>', '', text)
    # 複数の空白を単一に
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def get_japan_time() -> str:
    """現在の日本時間を取得"""
    from datetime import timezone, timedelta
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    weekdays = ['月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日']
    weekday = weekdays[now.weekday()]
    return f"今は{now.year}年{now.month}月{now.day}日({weekday})の{now.hour}時{now.minute}分だよ！"

# --- 判定関数 ---
def is_time_request(message: str) -> bool:
    """時刻に関する質問かどうか判定"""
    time_keywords = ['今何時', '時間', '時刻', 'いま何時', '現在時刻', '今の時間']
    return any(keyword in message for keyword in time_keywords)

def is_weather_request(message: str) -> bool:
    """天気に関する質問かどうか判定"""
    return any(keyword in message for keyword in ['天気', 'てんき', '気温', '降水確率', '晴れ', '雨', '曇り'])

def is_recommendation_request(message: str) -> bool:
    """おすすめに関する質問かどうか判定"""
    return any(keyword in message for keyword in ['おすすめ', 'オススメ', '人気', '流行', 'はやり', 'ランキング'])

# ホロライブメンバーリスト
HOLOMEM_KEYWORDS = [
    'ホロライブ', 'ホロメン', 'hololive',
    'ときのそら', 'ロボ子', 'さくらみこ', '星街すいせい', 'AZKi',
    '白上フブキ', '夏色まつり', '湊あくあ', '紫咲シオン', '百鬼あやめ',
    '大空スバル', '大神ミオ', '猫又おかゆ', '戌神ころね',
    '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン',
    '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ',
    'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは'
]

def is_hololive_request(message: str) -> bool:
    """ホロライブに関する質問かどうか判定"""
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)

def should_search(message: str) -> bool:
    """Web検索が必要かどうか判定"""
    search_patterns = [
        r'(?:とは|について|教えて|知りたい)',
        r'(?:最新|今日|ニュース)',
        r'(?:調べて|検索)'
    ]
    search_words = ['誰', '何', 'どこ', 'いつ', 'どうして', 'なぜ']
    
    return (any(re.search(pattern, message) for pattern in search_patterns) or
            any(word in message for word in search_words))

def is_short_response(message: str) -> bool:
    """短い相槌かどうか判定"""
    short_responses = ['うん', 'そう', 'はい', 'そっか', 'なるほど', 'へー', 'ふーん', 'おお', 'わかった']
    return len(message.strip()) <= 3 or message.strip() in short_responses

# --- 天気取得機能 ---
LOCATION_CODES = {
    "東京": "130000",
    "大阪": "270000", 
    "名古屋": "230000",
    "福岡": "400000",
    "札幌": "016000",
    "仙台": "040000",
    "広島": "340000"
}

def extract_location(message: str) -> str:
    """メッセージから地域を抽出"""
    for location in LOCATION_CODES.keys():
        if location in message:
            return location
    return "東京"  # デフォルト

def get_weather_forecast(location: str) -> Union[str, None]:
    """気象庁APIから天気予報を取得"""
    area_code = LOCATION_CODES.get(location)
    if not area_code:
        return None
        
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        weather_text = clean_text(data.get('text', ''))
        office = data.get('publishingOffice', '気象庁')
        
        return f"{location}の天気情報だよ！（{office}発表）\n「{weather_text}」\nまじ参考にしてね〜！"
    except Exception as e:
        logger.error(f"天気API取得エラー: {e}")
        return None

# --- 無料検索エンジン設定 ---
FREE_SEARCH_ENGINES = [
    {
        'name': 'DuckDuckGo',
        'url': 'https://duckduckgo.com/html/?q={}',
        'result_selector': '.result__title a',
        'snippet_selector': '.result__snippet'
    },
    {
        'name': 'StartPage',
        'url': 'https://www.startpage.com/sp/search?query={}',
        'result_selector': '.w-gl__result-title',
        'snippet_selector': '.w-gl__description'
    },
    {
        'name': 'Searx',
        'url': 'https://searx.be/search?q={}&format=json',
        'json_api': True
    }
]

# 複数のUser-Agentをローテーション
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15'
]

def get_random_user_agent():
    """ランダムなUser-Agentを取得"""
    return random.choice(USER_AGENTS)

# --- 無料検索実装 ---
def search_duckduckgo(query: str) -> List[Dict[str, str]]:
    """DuckDuckGo検索（HTML scraping）"""
    try:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {
            'User-Agent': get_random_user_agent(),
            'Accept-Language': 'ja,en;q=0.9'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        results = []
        
        for result_div in soup.find_all('div', class_='links_main')[:3]:
            try:
                title_element = result_div.find('a', class_='result__a')
                snippet_element = result_div.find('div', class_='result__snippet')
                
                if title_element and snippet_element:
                    title = clean_text(title_element.get_text())
                    snippet = clean_text(snippet_element.get_text())
                    url = title_element.get('href', '')
                    
                    if title and snippet:
                        results.append({
                            'title': title,
                            'snippet': snippet,
                            'url': url
                        })
            except:
                continue
                
        return results
        
    except Exception as e:
        logger.error(f"DuckDuckGo検索エラー: {e}")
        return []

def search_with_requests(query: str) -> List[Dict[str, str]]:
    """Requests + BeautifulSoupによる直接検索"""
    try:
        # 日本語の検索エンジンを優先
        search_urls = [
            f"https://search.yahoo.co.jp/search?p={quote_plus(query)}",
            f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP"
        ]
        
        for search_url in search_urls:
            try:
                headers = {
                    'User-Agent': get_random_user_agent(),
                    'Accept-Language': 'ja,en;q=0.9',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
                }
                
                response = requests.get(search_url, headers=headers, timeout=10)
                response.raise_for_status()
                
                soup = BeautifulSoup(response.content, 'html.parser')
                results = []
                
                # Yahoo検索結果の解析
                if 'yahoo.co.jp' in search_url:
                    for result in soup.find_all('div', class_='Algo')[:3]:
                        try:
                            title_elem = result.find('h3')
                            snippet_elem = result.find('div', class_='compText')
                            
                            if title_elem and snippet_elem:
                                title = clean_text(title_elem.get_text())
                                snippet = clean_text(snippet_elem.get_text())
                                
                                if title and snippet:
                                    results.append({
                                        'title': title,
                                        'snippet': snippet,
                                        'url': ''
                                    })
                        except:
                            continue
                
                # Bing検索結果の解析
                elif 'bing.com' in search_url:
                    for result in soup.find_all('li', class_='b_algo')[:3]:
                        try:
                            title_elem = result.find('h2')
                            snippet_elem = result.find('div', class_='b_caption')
                            
                            if title_elem and snippet_elem:
                                title = clean_text(title_elem.get_text())
                                snippet = clean_text(snippet_elem.get_text())
                                
                                if title and snippet:
                                    results.append({
                                        'title': title,
                                        'snippet': snippet,
                                        'url': ''
                                    })
                        except:
                            continue
                
                if results:
                    logger.info(f"✅ 検索成功 ({search_url}): {len(results)}件")
                    return results
                    
            except Exception as e:
                logger.error(f"検索エラー ({search_url}): {e}")
                continue
        
        return []
        
    except Exception as e:
        logger.error(f"全体検索エラー: {e}")
        return []

# --- 専門サイト検索機能 ---
SPECIALIZED_SITES = {
    'Blender': {
        'base_url': 'https://docs.blender.org/manual/ja/latest/',
        'keywords': ['Blender', 'ブレンダー', '3D', 'モデリング'],
        'search_paths': ['/modeling/', '/rendering/', '/animation/']
    },
    'CGニュース': {
        'base_url': 'https://modelinghappy.com/',
        'keywords': ['CG', '3DCG', 'モデリング', 'レンダリング'],
        'search_paths': ['/category/news/', '/category/tutorial/']
    },
    '脳科学・心理学': {
        'base_url': 'https://nazology.kusuguru.co.jp/',
        'keywords': ['脳科学', '心理学', '認知', '神経'],
        'search_paths': ['/brain/', '/psychology/']
    },
    'セカンドライフ': {
        'base_url': 'https://community.secondlife.com/news/',
        'keywords': ['セカンドライフ', 'Second Life', 'SL', 'バーチャル'],
        'search_paths': ['/news/', '/forums/']
    }
}

def detect_specialized_topic(message: str) -> Union[str, None]:
    """専門トピックを検出"""
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in message for keyword in config['keywords']):
            return topic
    return None

def specialized_site_search(topic: str, query: str) -> Union[str, None]:
    """専門サイト内検索を実行"""
    if topic not in SPECIALIZED_SITES:
        return None
        
    config = SPECIALIZED_SITES[topic]
    try:
        # サイト固有の検索を実行
        site_query = f"site:{config['base_url']} {query}"
        results = search_with_requests(site_query)
        
        if results:
            # 結果をまとめて返す
            formatted_results = []
            for result in results[:2]:
                formatted_results.append(f"「{result['title']}」{result['snippet']}")
            return '\n'.join(formatted_results)
        
        # フォールバック: サイト直接アクセス
        return scrape_site_content(config['base_url'], query)
        
    except Exception as e:
        logger.error(f"専門サイト検索エラー ({topic}): {e}")
        return None

def scrape_site_content(base_url: str, query: str) -> Union[str, None]:
    """サイト直接スクレイピング（フォールバック）"""
    try:
        headers = {
            'User-Agent': get_random_user_agent()
        }
        
        response = requests.get(base_url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # ページ内容を検索
        text_content = clean_text(soup.get_text())
        
        # queryに関連する部分を抽出
        if query.lower() in text_content.lower():
            sentences = text_content.split('。')
            relevant_sentences = [s for s in sentences if query in s][:3]
            
            if relevant_sentences:
                return '。'.join(relevant_sentences) + '。'
                
        return None
        
    except Exception as e:
        logger.error(f"サイト直接取得エラー: {e}")
        return None

# --- メイン検索関数 ---
def deep_web_search(query: str) -> Union[str, None]:
    """無料検索エンジンを使用したディープ検索"""
    logger.info(f"🔍 無料ディープサーチ開始: '{query}'")
    
    try:
        # 複数の無料検索エンジンを順番に試行
        search_functions = [
            lambda q: search_duckduckgo(q),
            lambda q: search_with_requests(q)
        ]
        
        for search_func in search_functions:
            try:
                results = search_func(query)
                
                if results:
                    # 結果をGroq AIで要約
                    summary_text = ""
                    for i, result in enumerate(results[:2], 1):
                        summary_text += f"[情報{i}] {result['title']}: {result['snippet']}\n"
                    
                    if summary_text:
                        summary_prompt = f"""
                        以下の検索結果を、質問「{query}」に答える形で簡潔にまとめてください。
                        重要なポイントを分かりやすく説明してください：

                        {summary_text}
                        """
                        
                        completion = groq_client.chat.completions.create(
                            messages=[{"role": "system", "content": summary_prompt}],
                            model="llama-3.1-8b-instant",  # 🔧 FIXED: Updated model name
                            temperature=0.2,
                            max_tokens=200
                        )
                        
                        return completion.choices[0].message.content.strip()
                
            except Exception as e:
                logger.error(f"個別検索エラー: {e}")
                continue
        
        return None
        
    except Exception as e:
        logger.error(f"ディープサーチエラー: {e}")
        return None

# --- ホロライブ情報管理 ---
def update_hololive_database():
    """ホロライブ情報をデータベースに更新（1時間毎実行）"""
    session = Session()
    try:
        # 過去3ヶ月以前のデータを削除
        three_months_ago = datetime.utcnow() - timedelta(days=90)
        session.query(HololiveNews).filter(
            HololiveNews.created_at < three_months_ago
        ).delete()
        
        # 新しい情報を検索・保存
        search_result = deep_web_search("ホロライブ 最新ニュース")
        if search_result:
            news_entry = HololiveNews(
                title="最新ホロライブ情報",
                content=search_result,
                url="",
                published_date=datetime.utcnow()
            )
            session.add(news_entry)
            
        session.commit()
        logger.info("✅ ホロライブ情報データベース更新完了")
        
    except Exception as e:
        logger.error(f"ホロライブDB更新エラー: {e}")
        session.rollback()
    finally:
        session.close()

def get_hololive_info_from_db() -> Union[str, None]:
    """データベースから最新のホロライブ情報を取得"""
    session = Session()
    try:
        latest_news = session.query(HololiveNews)\
            .order_by(HololiveNews.created_at.desc())\
            .first()
            
        if latest_news:
            return latest_news.content
        return None
        
    except Exception as e:
        logger.error(f"ホロライブDB取得エラー: {e}")
        return None
    finally:
        session.close()

def extract_recommendation_topic(message: str) -> Union[str, None]:
    """おすすめのトピックを抽出"""
    topics = {
        '映画': ['映画', 'ムービー'],
        '音楽': ['音楽', '曲', 'ミュージック', '歌'],
        'アニメ': ['アニメ', 'アニメーション'],
        '本': ['本', '書籍', '漫画', 'マンガ', '小説'],
        'ゲーム': ['ゲーム', 'ゲーミング'],
        'グルメ': ['グルメ', '食べ物', 'レストラン', '料理']
    }
    
    for topic, keywords in topics.items():
        if any(kw in message for kw in keywords):
            return topic
    return None

# --- AI応答生成 ---
def generate_ai_response(user_data: Dict[str, Any], message: str, history: List[Any]) -> str:
    """メインAI応答生成関数"""
    if not groq_client:
        return "あてぃし、今ちょっと調子悪いかも...またあとで話して！"
    
    search_info = ""
    search_query = ""
    search_failed = False
    is_fallback = False
    specialized_topic = None
    
    # 短い相槌の場合は検索をスキップ
    if is_short_response(message):
        logger.info("💬 短い相槌を検出、検索をスキップして会話継続")
        pass
    # 時刻要求の場合
    elif is_time_request(message):
        search_info = get_japan_time()
    # 天気要求の場合
    elif is_weather_request(message):
        location = extract_location(message)
        weather_info = get_weather_forecast(location)
        if weather_info:
            search_info = weather_info
        else:
            return "今日の天気？調べてみたけど、情報が見つからなかった...ごめんね！でも、気象庁のホームページには、各地の天気予報が載ってるよ〜まじ便利だから見てみて！"
    # 専門サイト検索の場合
    elif (specialized_topic := detect_specialized_topic(message)):
        logger.info(f"🔬 専門トピック検出: {specialized_topic}")
        search_info = specialized_site_search(specialized_topic, message)
    # ホロライブ関連の場合
    elif is_hololive_request(message):
        logger.info("🎤 ホロライブ関連の質問を検知")
        holo_info = get_hololive_info_from_db()
        if holo_info:
            search_info = holo_info
        else:
            search_query = f"{message} ホロライブ 最新情報"
    # おすすめ要求の場合
    elif is_recommendation_request(message):
        topic = extract_recommendation_topic(message)
        search_query = f"最新 {topic} 人気ランキング" if topic else "最近 話題のもの ランキング"
    # 一般的な検索が必要な場合
    elif should_search(message):
        search_query = message
    
    # Web検索実行
    if search_query and not search_info:
        search_info = deep_web_search(search_query)
        if not search_info:
            search_failed = True
    
    # 検索失敗時のフォールバック（ホロライブ情報で代替）
    if search_failed and not is_hololive_request(message):
        logger.info("💡 検索失敗のため、代替案としてホロライブの情報を検索します。")
        fallback_info = get_hololive_info_from_db()
        if not fallback_info:
            fallback_info = deep_web_search("ホロライブ 最新ニュース")
        if fallback_info:
            search_info = fallback_info
            is_fallback = True
