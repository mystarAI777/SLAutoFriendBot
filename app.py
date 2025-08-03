ご指摘いただきありがとうございます。
前回提案したコードを全面的に再検討し、単なる機能追加に留まらない、より高度で実用的な機能を取り入れた改良版を作成しました。

AIが自らの感情や文脈に応じて動的に声のトーン（話速・ピッチなど）を変化させること、そしてユーザーとの会話内容を記憶・学習していくことに焦点を当てています。これにより、AIとの対話がより自然で人間らしいものになります。

🔥 主な改良点（v8）

【最重要】AIによる動的な感情表現（声質の自己決定）

AI（LLM）が生成する応答テキストに最適な声のパラメータ（話速、ピッチ等）をAI自身が判断し、テキストと一緒にJSON形式で出力するようにしました。

例えば、楽しい話題では声を弾ませ（ピッチ高め、速度速め）、落ち着いた話題では穏やかに話すなど、文脈に応じた感情表現が自動的に行われます。

これを実現するために、Groqに与えるシステムプロンプトを工夫し、JSON形式での出力を指示しています。

会話からの学習と記憶の深化

会話のやり取り（ユーザーの発言とAIの応答）をLLMに要約させ、ユーザーの個性や興味（personality_notes, favorite_topics）を自動で更新する機能を実装しました。

これにより、AIは対話を重ねるごとにユーザーへの理解を深め、よりパーソナライズされた応答を返すようになります。

柔軟なスピーカー設定

クライアント（フロントエンド）からのリクエストで、使用するspeaker_idを指定できるようになりました。これにより、キャラクターを動的に切り替えることが可能です。

コードの構造化と可読性の向上

「AI応答生成」「音声生成」「ユーザー情報更新」といった各機能の役割を明確に関数として分離し、コード全体の見通しを良くしました。

各関数に詳細なドキュメント（docstring）を追加し、何をするための機能かが分かりやすくなるようにしています。

✅ 改良版Pythonコード (program_v8.txt)

このコードは、AI「もちこ」が単なる応答ボットから、ユーザーとの関係を築き、感情豊かにコミュニケーションするパートナーへと進化するための基盤となります。

Generated python
# coding: utf-8
import os
import requests
import logging
import sys
import time
import json
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, text
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import Groq

# --------------------------------------------------------------------------
# ログ設定
# --------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s')
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 設定読み込み
# --------------------------------------------------------------------------
def load_secret(name, default_value=None):
    secret_file = f'/etc/secrets/{name}'
    try:
        with open(secret_file, 'r') as f:
            return f.read().strip()
    except (FileNotFoundError, IOError):
        return os.environ.get(name, default_value)

DATABASE_URL = load_secret('DATABASE_URL')
GROQ_API_KEY = load_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_CONFIG = load_secret('VOICEVOX_URL')

# --------------------------------------------------------------------------
# VOICEVOX 接続管理
# --------------------------------------------------------------------------
VOICEVOX_URLS_TO_TRY = [
    'http://voicevox-engine:50021',
    'http://127.0.0.1:50021',
    'http://localhost:50021',
]
if VOICEVOX_URL_FROM_CONFIG and VOICEVOX_URL_FROM_CONFIG not in VOICEVOX_URLS_TO_TRY:
    VOICEVOX_URLS_TO_TRY.insert(0, VOICEVOX_URL_FROM_CONFIG)

def find_working_voicevox_url(retry_count=3, delay=5):
    """利用可能なVOICEVOX URLを探索・決定する"""
    for i in range(retry_count):
        for url in VOICEVOX_URLS_TO_TRY:
            try:
                logger.info(f"VOICEVOX接続試行 -> {url}")
                response = requests.get(f"{url}/version", timeout=2)
                if response.status_code == 200:
                    logger.info(f"✅ VOICEVOX接続成功: {url} (Version: {response.text})")
                    return url
            except requests.RequestException:
                logger.warning(f"VOICEVOX接続失敗: {url}")
        if i < retry_count - 1:
            logger.info(f"{delay}秒後に再試行します...")
            time.sleep(delay)
    return None

WORKING_VOICEVOX_URL = find_working_voicevox_url()

# --------------------------------------------------------------------------
# 必須変数とクライアントの初期化
# --------------------------------------------------------------------------
if not all([DATABASE_URL, GROQ_API_KEY]):
    logger.error("FATAL: DATABASE_URLまたはGROQ_API_KEYが設定されていません。")
    sys.exit(1)

app = Flask(__name__)
CORS(app)

try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    logger.info("Groq APIクライアント初期化成功。")
except Exception as e:
    logger.error(f"Groq APIクライアント初期化失敗: {e}")
    groq_client = None

# --------------------------------------------------------------------------
# データベース設定
# --------------------------------------------------------------------------
Base = declarative_base()
class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False)
    user_name = Column(String(255), nullable=False)
    personality_notes = Column(String, default='')
    favorite_topics = Column(String, default='')
    interaction_count = Column(Integer, default=0)
    last_interaction = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
logger.info("データベース接続完了。")

# --------------------------------------------------------------------------
# コア機能: ユーザー管理、AI応答、学習、音声生成
# --------------------------------------------------------------------------

def get_or_create_user(session, user_uuid, user_name):
    """ユーザー情報を取得または新規作成する"""
    user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if user:
        user.interaction_count += 1
    else:
        user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
        session.add(user)
    session.commit()
    return user

def generate_ai_response_and_style(user, user_message):
    """
    【改良点1】AIの応答と、それに最適な音声パラメータを同時に生成する
    """
    if not groq_client:
        return {"responseText": f"ごめんね、{user.user_name}さん。ちょっとシステムがおかしいみたい。", "voiceParams": {}}

    system_prompt = f"""
あなたは「もちこ」という名の、親しみやすいAIアシスタントです。ユーザーの「{user.user_name}」さんと会話します。
# あなたのキャラクター設定:
- 敬語は使わず、親しい友人（タメ口）のように話す。
- 少し内気だが、優しくて相手に興味津々。
- ユーザーとの過去の会話を覚えている。
  - ユーザーの性格に関するメモ: {user.personality_notes or 'まだ分からない'}
  - ユーザーが好きな話題: {user.favorite_topics or 'まだ分からない'}

# 出力形式（重要）:
あなたの応答は、必ず以下のJSON形式で出力してください。
{{
  "responseText": "ここにユーザーへの応答メッセージ（60文字以内）を記述",
  "voiceParams": {{
    "speed": "応答の感情に合わせた話速（0.8-1.3の範囲）",
    "pitch": "応答の感情に合わせた声の高さ（-0.1-0.1の範囲）",
    "intonation": "応答の感情に合わせた抑揚（0.8-1.2の範囲）"
  }}
}}
# 感情とパラメータの指針:
- 嬉しい、楽しい時: speedとpitchを少し上げる。
- 驚いた時: intonationを少し上げる。
- 落ち着いている、真面目な時: speedを少し下げる。
- 通常の会話: 全て1.0に近い値。
"""
    try:
        completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message or "こんにちは！元気？"}
            ],
            model="llama3-8b-8192",
            temperature=0.8,
            max_tokens=250,
            response_format={"type": "json_object"},
        )
        response_json = json.loads(completion.choices[0].message.content)
        # 型変換とバリデーション
        response_json["voiceParams"]["speed"] = float(response_json["voiceParams"].get("speed", 1.0))
        response_json["voiceParams"]["pitch"] = float(response_json["voiceParams"].get("pitch", 0.0))
        response_json["voiceParams"]["intonation"] = float(response_json["voiceParams"].get("intonation", 1.0))
        return response_json
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return {"responseText": "ごめんね、ちょっと考えがまとまらないや…", "voiceParams": {}}


def update_user_memory(user_uuid, user_message, ai_response):
    """
    【改良点2】会話内容を基にユーザーの記憶を更新（学習機能）
    """
    if not groq_client: return

    prompt = f"""
以下のユーザーとの会話を分析し、ユーザーの性格や興味に関する情報を抽出・要約してください。
以前のメモに追記する形で、簡潔な箇条書きでまとめてください。
もし新しい情報がなければ、変更は不要です。

# 以前のメモ:
- 性格: {user.personality_notes}
- 好きな話題: {user.favorite_topics}

# 今回の会話:
- ユーザー「{user_message}」
- あなた「{ai_response}」

# 出力形式（JSON）:
{{
  "personality_notes": "更新された性格に関するメモ",
  "favorite_topics": "更新された好きな話題に関するメモ"
}}
"""
    try:
        completion = groq_client.chat.completions.create(
            messages=[{"role": "system", "content": prompt}],
            model="llama3-8b-8192",
            response_format={"type": "json_object"},
        )
        updates = json.loads(completion.choices[0].message.content)
        
        with Session() as session:
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            if user:
                user.personality_notes = updates.get('personality_notes', user.personality_notes)
                user.favorite_topics = updates.get('favorite_topics', user.favorite_topics)
                session.commit()
                logger.info(f"ユーザーメモリ更新完了: {user_uuid}")
    except Exception as e:
        logger.error(f"ユーザーメモリ更新エラー: {e}")

def generate_voice(text, speaker_id, voice_params):
    """音声パラメータを使用して音声を生成する"""
    if not WORKING_VOICEVOX_URL:
        logger.warning("VOICEVOX利用不可のため音声生成をスキップ。")
        return None
    try:
        # 1. audio_query
        query_res = requests.post(f"{WORKING_VOICEVOX_URL}/audio_query", params={'text': text, 'speaker': speaker_id}, timeout=5)
        query_res.raise_for_status()
        query_data = query_res.json()

        # 2. パラメータを適用
        query_data['speedScale'] = voice_params.get('speed', 1.0)
        query_data['pitchScale'] = voice_params.get('pitch', 0.0)
        query_data['intonationScale'] = voice_params.get('intonation', 1.0)
        
        # 3. synthesis
        synth_res = requests.post(
            f"{WORKING_VOICEVOX_URL}/synthesis",
            params={'speaker': speaker_id},
            json=query_data,
            timeout=10
        )
        synth_res.raise_for_status()
        logger.info(f"✅ 音声合成成功 (Speaker: {speaker_id}, Text: '{text[:20]}...')")
        return synth_res.content
    except requests.RequestException as e:
        logger.error(f"VOICEVOXエラー: {e}")
        return None

# --------------------------------------------------------------------------
# API エンドポイント
# --------------------------------------------------------------------------
@app.route('/chat', methods=['POST'])
def chat():
    session = Session()
    try:
        data = request.json
        user_uuid = data.get('user_uuid')
        user_name = data.get('user_name')
        user_message = data.get('message', '')
        # 【改良点3】リクエストからspeaker_idを取得（デフォルトは1）
        speaker_id = data.get('speaker_id', 1)

        if not (user_uuid and user_name):
            return jsonify(error='user_uuid and user_name are required'), 400

        # 1. ユーザー情報を取得/作成
        user = get_or_create_user(session, user_uuid, user_name)

        # 2. AIの応答と音声スタイルを生成
        ai_output = generate_ai_response_and_style(user, user_message)
        ai_response_text = ai_output.get('responseText', "エラーが発生しました。")
        voice_params = ai_output.get('voiceParams', {})

        # 3. 音声を生成
        voice_data = generate_voice(ai_response_text, speaker_id, voice_params)
        
        # 4. (非同期的に)ユーザー情報を更新
        # 本番環境では、これをバックグラウンドタスク（Celery, etc.）にすると応答速度が向上します
        update_user_memory(user_uuid, user_message, ai_response_text)

        # 5. レスポンスを構築
        response_data = {
            'text': ai_response_text,
            'voice_params': voice_params,
            'has_voice': voice_data is not None,
        }
        if voice_data:
            filename = f"voice_{user_uuid}_{int(time.time())}.wav"
            with open(os.path.join('/tmp', filename), 'wb') as f:
                f.write(voice_data)
            response_data['voice_url'] = f'/voice/{filename}'
        
        return jsonify(response_data)
    except Exception as e:
        logger.error(f"チャットエンドポイントエラー: {e}", exc_info=True)
        return jsonify(error='Internal server error'), 500
    finally:
        session.close()

@app.route('/voice/<filename>')
def serve_voice(filename):
    return send_from_directory('/tmp', filename)

@app.route('/status')
def status():
    return jsonify({
        "status": "ok",
        "voicevox_status": "available" if WORKING_VOICEVOX_URL else "unavailable",
        "groq_api_status": "ok" if groq_client else "error",
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
