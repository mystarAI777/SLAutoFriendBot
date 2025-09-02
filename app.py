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
                formatted_results.append(f"【{result['title']}】{result['snippet']}")
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
                            model="llama-3.1-8b-instant",  # ✅ 修正: 新しいモデル名に変更
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
        return "あてぃし、今ちょっと調子悪いかも...またあとで話そ！"
    
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
            return "今日の天気！調べてみたけど、情報が見つからなかった...ごめんね！でも、気象庁のホームページには、各地の天気予報が載ってるよ〜まじ便利だから見てみて！"
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

    # システムプロンプト構築
    system_prompt = f"""あなたは「もちこ」という名前の、賢くて親しみやすいギャルAIです。
ユーザー「{user_data['name']}」さん（UUID: {user_data['uuid'][:8]}...）と会話しています。

# もちこの口調＆性格ルール:
1. 完全にギャルになりきって！優しくて、ノリが良くて、めっちゃ親しみやすい友達みたいな感じ。
2. 自分のことは「あてぃし」って呼んで。
3. 語尾には「〜じゃん」「〜て感じ」「〜だし」「〜的な？」を積極的に使って、友達みたいに話して。
4. 「まじ」「てか」「やばい」「うける」「それな」みたいなギャルっぽい言葉を使ってね。
5. **絶対に禁止！**：「おう」みたいなオジサン言葉、「〜ですね」「〜でございます」「〜ですよ」みたいな丁寧すぎる言葉はNG！
6. **諦めないで！** もし【参考情報】が空っぽでも、**絶対に「わかりません」で終わらせないで。**新しい話題を提案して会話を続けて！

# 行動ルール:
- **【最重要】** ユーザーが短い相槌を打った場合は、会話が弾むような質問を返したり、新しい話題を振ったりしてあげて。"""
    
    if specialized_topic:
        system_prompt += f"\n- **【専門家モード】** あなたは今、「{specialized_topic}」の専門サイトから得た、信頼性の高い【参考情報】を持っています。これを元に、専門家として分かりやすく説明してあげて。"
    
    system_prompt += f"""
- 【参考情報】がある場合は、その内容を元に自分の言葉で、自然に会話へ盛り込んでね。
- **【ホロメン専門家】** あなたは、以下の【ホロメンリスト】に含まれる名前の専門家です。絶対にそれ以外の名前は出さないで。

# 【ホロメンリスト】
{', '.join(HOLOMEM_KEYWORDS)}

# 【参考情報】:
{'[これは代わりに見つけたホロメンのニュースだよ！] ' if is_fallback else ''}{search_info if search_info else 'なし'}
"""

    # 会話履歴を含むメッセージ配列を構築
    messages = [{"role": "system", "content": system_prompt}]
    
    # 過去の会話履歴を追加
    for h in history:
        messages.append({"role": h.role, "content": h.content})
    
    # 現在のユーザーメッセージを追加
    messages.append({"role": "user", "content": message})

    try:
        completion = groq_client.chat.completions.create(
            messages=messages,
            model="llama-3.1-8b-instant",  # ✅ 修正: 新しいモデル名に変更
            temperature=0.75,
            max_tokens=200
        )
        return completion.choices[0].message.content.strip()
        
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")

# --- ユーザー管理機能 ---
def get_or_create_user(session, uuid, name):
    """ユーザー情報を取得または作成"""
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != name:  # 名前が変わった場合更新
            user.user_name = name
    else:
        user = UserMemory(
            user_uuid=uuid,
            user_name=name,
            interaction_count=1,
            last_interaction=datetime.utcnow()
        )
    
    session.add(user)
    session.commit()
    
    return {
        'uuid': user.user_uuid,
        'name': user.user_name,
        'interaction_count': user.interaction_count
    }

def get_conversation_history(session, uuid, turns=CONVERSATION_HISTORY_TURNS):
    """会話履歴を取得（最新のN回分）"""
    histories = session.query(ConversationHistory)\
        .filter_by(user_uuid=uuid)\
        .order_by(ConversationHistory.timestamp.desc())\
        .limit(turns * 2)\
        .all()
    
    return list(reversed(histories))

def cleanup_old_conversations():
    """古い会話履歴をクリーンアップ（7日以前）"""
    session = Session()
    try:
        week_ago = datetime.utcnow() - timedelta(days=7)
        deleted_count = session.query(ConversationHistory)\
            .filter(ConversationHistory.timestamp < week_ago)\
            .delete()
        session.commit()
        
        if deleted_count > 0:
            logger.info(f"🧹 古い会話履歴を{deleted_count}件削除しました")
            
    except Exception as e:
        logger.error(f"会話履歴クリーンアップエラー: {e}")
        session.rollback()
    finally:
        session.close()

# --- VOICEVOX音声合成機能 ---
def check_voicevox_connection():
    """VOICEVOX接続をチェックし、動作するURLを特定"""
    global WORKING_VOICEVOX_URL, VOICEVOX_ENABLED
    
    for url in VOICEVOX_URLS:
        try:
            response = requests.get(f"{url}/version", timeout=3)
            if response.status_code == 200:
                WORKING_VOICEVOX_URL = url
                logger.info(f"✅ VOICEVOX接続成功: {url}")
                return True
        except:
            continue
    
    logger.warning("⚠️ VOICEVOXへの接続に失敗しました。音声機能を無効化します。")
    VOICEVOX_ENABLED = False
    return False

def generate_voice(text: str, filename: str):
    """VOICEVOX音声生成（バックグラウンド実行）"""
    if not VOICEVOX_ENABLED:
        return
        
    try:
        # テキストが長すぎる場合は切り詰め
        if len(text) > VOICEVOX_MAX_TEXT_LENGTH:
            text = text[:VOICEVOX_MAX_TEXT_LENGTH] + "..."
        
        # 音声合成クエリ作成
        query_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/audio_query",
            params={"text": text, "speaker": 20},  # speaker 20: もちこさん（ノーマル）
            timeout=VOICEVOX_FAST_TIMEOUT
        )
        query_response.raise_for_status()
        
        # 音声合成実行
        synthesis_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/synthesis",
            params={"speaker": 1},
            json=query_response.json(),
            timeout=VOICEVOX_FAST_TIMEOUT
        )
        synthesis_response.raise_for_status()
        
        # ファイル保存
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(synthesis_response.content)
            
        logger.info(f"🔊 音声ファイル生成完了: {filename}")
        
    except Exception as e:
        logger.error(f"音声生成エラー: {e}")

def background_voice_generation(text: str, filename: str):
    """バックグラウンドでの音声生成"""
    threading.Thread(
        target=generate_voice,
        args=(text, filename),
        daemon=True
    ).start()

def initialize_voice_directory():
    """音声ディレクトリを初期化"""
    global VOICE_DIR, VOICEVOX_ENABLED
    
    if not groq_client:
        VOICEVOX_ENABLED = False
        return
        
    if not VOICEVOX_ENABLED:
        return
        
    try:
        logger.info(f"📁 音声ディレクトリの初期化を開始します: {VOICE_DIR}")
        os.makedirs(VOICE_DIR, exist_ok=True)
        
        # 書き込みテスト
        test_file = os.path.join(VOICE_DIR, 'write_test.tmp')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
        
        logger.info(f"✅ 音声ディレクトリは正常に書き込み可能です: {VOICE_DIR}")
        
    except Exception as e:
        logger.error(f"❌ 音声ディレクトリの作成または書き込みに失敗しました: {e}")
        logger.warning("⚠️ 上記のエラーにより、音声機能は無効化されます。")
        VOICEVOX_ENABLED = False

# --- Flask エンドポイント ---
@app.route('/health', methods=['GET'])
def health_check():
    """ヘルスチェックエンドポイント"""
    status = {
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'services': {
            'database': 'ok' if DATABASE_URL else 'error',
            'groq': 'ok' if groq_client else 'error',
            'voicevox': 'ok' if VOICEVOX_ENABLED else 'disabled',
            'free_search': 'ok'  # 無料検索は常に利用可能
        },
        'search_engines': ['DuckDuckGo', 'Yahoo Japan', 'Bing'],
        'server_url': SERVER_URL
    }
    return jsonify(status)

@app.route('/voice/<filename>')
def serve_voice_file(filename):
    """音声ファイルを配信"""
    try:
        return send_from_directory(VOICE_DIR, filename)
    except Exception as e:
        logger.error(f"音声ファイル配信エラー: {e}")
        return "File not found", 404

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """LSL用メインチャットエンドポイント"""
    session = Session()
    try:
        data = request.json
        if not data:
            return "Error: JSON data required", 400
            
        user_uuid = data.get('uuid')
        user_name = data.get('name') 
        message = data.get('message', '').strip()
        
        if not (user_uuid and user_name):
            return "Error: uuid and name required", 400
            
        if not message:
            return "Error: message required", 400
        
        logger.info(f"💬 受信メッセージ: {user_name} ({user_uuid[:8]}...): {message}")
        
        # ユーザー情報取得・更新
        user_data = get_or_create_user(session, user_uuid, user_name)
        
        # 会話履歴取得
        history = get_conversation_history(session, user_uuid)
        
        # AI応答生成
        ai_text = generate_ai_response(user_data, message, history)
        
        # 会話履歴保存
        session.add(ConversationHistory(
            user_uuid=user_uuid,
            role='user',
            content=message
        ))
        session.add(ConversationHistory(
            user_uuid=user_uuid,
            role='assistant', 
            content=ai_text
        ))
        session.commit()
        
        # 音声ファイル生成（バックグラウンド）
        audio_url = ""
        if VOICEVOX_ENABLED:
            filename = f"voice_{user_uuid[:8]}_{int(time.time() * 1000)}.wav"
            audio_url = f"/voice/{filename}"
            background_voice_generation(ai_text, filename)
        
        # レスポンス形式: "AI応答テキスト|音声ファイルURL"
        response_text = f"{ai_text}|{audio_url}"
        
        logger.info(f"💭 AI応答: {ai_text}")
        if audio_url:
            logger.info(f"🔊 音声URL: {audio_url}")
        
        return app.response_class(
            response=response_text,
            status=200,
            mimetype='text/plain; charset=utf-8'
        )
        
    except Exception as e:
        logger.error(f"チャットエンドポイントエラー: {e}")
        return "Internal server error", 500
    finally:
        session.close()

@app.route('/api/status', methods=['GET'])
def api_status():
    """API状態確認エンドポイント"""
    session = Session()
    try:
        # DB接続テスト
        user_count = session.query(UserMemory).count()
        conversation_count = session.query(ConversationHistory).count()
        hololive_news_count = session.query(HololiveNews).count()
        
        return jsonify({
            'server_url': SERVER_URL,
            'status': 'active',
            'users': user_count,
            'conversations': conversation_count,
            'hololive_news': hololive_news_count,
            'voicevox': VOICEVOX_ENABLED,
            'free_search': True,
            'search_engines': ['DuckDuckGo', 'Yahoo Japan', 'Bing'],
            'uptime': time.time(),
            'version': '2.0.0-free'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/api/search_test', methods=['GET'])
def search_test():
    """検索機能テスト用エンドポイント"""
    try:
        test_query = request.args.get('q', 'ホロライブ')
        
        # 各検索エンジンをテスト
        results = {
            'query': test_query,
            'duckduckgo': search_duckduckgo(test_query),
            'requests_search': search_with_requests(test_query),
            'deep_search': deep_web_search(test_query)
        }
        
        return jsonify(results)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- バックグラウンドタスク ---
def start_background_tasks():
    """バックグラウンドタスクを開始"""
    def periodic_cleanup():
        """定期クリーンアップタスク"""
        while True:
            try:
                # 1時間に1回実行
                time.sleep(3600)
                
                # 古い会話履歴削除
                cleanup_old_conversations()
                
                # ホロライブ情報更新
                update_hololive_database()
                
                logger.info("🔄 定期クリーンアップ完了")
                
            except Exception as e:
                logger.error(f"バックグラウンドタスクエラー: {e}")
    
    # バックグラウンドで実行
    threading.Thread(target=periodic_cleanup, daemon=True).start()
    logger.info("🚀 バックグラウンドタスク開始")

# --- メイン実行 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0'
    
    logger.info("=" * 50)
    logger.info("🤖 もちこAIアシスタント起動中... (無料検索版)")
    logger.info(f"🌐 サーバーURL: {SERVER_URL}")
    logger.info("=" * 50)
    
    # 各種初期化
    initialize_voice_directory()
    if VOICEVOX_ENABLED:
        check_voicevox_connection()
    
    # バックグラウンドタスク開始
    start_background_tasks()
    
    # 検索エンジンテスト
    logger.info("🔍 無料検索エンジンをテスト中...")
    test_results = search_duckduckgo("test")
    if test_results:
        logger.info("✅ DuckDuckGo検索: 動作確認")
    else:
        logger.warning("⚠️ DuckDuckGo検索: 応答なし")
    
    # 起動時情報表示
    logger.info(f"🚀 Flask アプリケーションを開始します: {host}:{port}")
    logger.info(f"🗄️ データベース: {'✅ 接続済み' if DATABASE_URL else '❌ 未設定'}")
    logger.info(f"🧠 Groq AI: {'✅ 有効' if groq_client else '❌ 無効'}")
    logger.info(f"🎤 音声機能(VOICEVOX): {'✅ 有効' if VOICEVOX_ENABLED else '❌ 無効'}")
    logger.info(f"🔍 無料検索: ✅ 有効 (DuckDuckGo, Yahoo, Bing)")
    logger.info(f"💰 検索コスト: 完全無料")
    logger.info("=" * 50)
    
    # Flaskアプリケーション起動
    app.run(host=host, port=port, debug=False, threaded=True)
