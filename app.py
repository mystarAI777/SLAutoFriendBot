import os
import requests
import uuid
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

VOICEVOX_URL = "http://127.0.0.1:50021"
AUDIO_DIR = "static/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

@app.route('/healthz')
def health_check():
    return "OK", 200

@app.route('/synthesize', methods=['POST'])
def synthesize_speech():
    data = request.get_json()
    text = data.get('text', 'こんにちは、世界')
    speaker_id = 9 # もちこさん固定

    try:
        res_query = requests.post(
            f"{VOICEVOX_URL}/audio_query",
            params={'text': text, 'speaker': speaker_id},
            timeout=10
        )
        res_query.raise_for_status()
        audio_query = res_query.json()

        res_synth = requests.post(
            f"{VOICEVOX_URL}/synthesis",
            params={'speaker': speaker_id},
            json=audio_query,
            timeout=10
        )
        res_synth.raise_for_status()

        filename = f"{uuid.uuid4()}.wav"
        filepath = os.path.join(AUDIO_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(res_synth.content)

        base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
        audio_url = f"{base_url.rstrip('/')}/audio/{filename}"
        
        # テキストも一緒に返すように変更
        return jsonify({"audio_url": audio_url, "message": text})

    except requests.exceptions.RequestException as e:
        print(f"VOICEVOXとの通信エラー: {e}")
        return jsonify({"error": "VOICEVOX engine is not responding"}), 503
    except Exception as e:
        print(f"サーバーエラー: {e}")
        return jsonify({"error": "An internal error occurred"}), 500

@app.route('/audio/<filename>')
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)
