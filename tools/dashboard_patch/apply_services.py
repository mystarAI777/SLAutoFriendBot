# -*- coding: utf-8 -*-
"""services.json にノート(Obsidian)で判明した構成をID単位でマージする
・verified が true の既存項目は変更しない／既存の登録は削除しない／新規IDは追加
"""
import json, os, shutil, sys

BASE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(BASE, "services.json")
CMD = lambda bat: ["cmd", "/c", bat]

ENTRIES = [
    {"id": "comfyui-8188", "cwd": "H:\\", "cmd": CMD("H:\\start_comfyui_hermes.bat")},
    {"id": "comfyui-8190", "cwd": "F:\\ComfyUI_H3", "cmd": CMD("F:\\ComfyUI_H3\\start_comfyui_h3_hermes.bat")},
    {"id": "comfyui-8191", "name": "ComfyUI 8191 (MM / MiniMax-H3-Image)", "group": "comfyui", "port": 8191,
     "cwd": "F:\\ComfyUI_MM", "cmd": CMD("F:\\ComfyUI_MM\\start_comfyui_mm_hermes.bat"),
     "match": ["comfy", "main.py"], "heavy": False, "verified": False, "note": "指定時のみ使用"},
    {"id": "llama-8094", "cwd": "H:\\llama.cpp", "cmd": CMD("H:\\llama.cpp\\start_llama_qwythos.bat"),
     "note": "10分無通信でスリープ(--sleep-idle-seconds 600)。停止しても自動復帰ジョブで戻る場合あり"},
    {"id": "llama-8093", "name": "llama-server 8093 (qwen3.6-35b-a3b)", "group": "llm", "port": 8093,
     "cwd": "", "cmd": "", "match": ["llama-server"], "heavy": True, "verified": False,
     "note": "hermes_use_qwen36.bat 経由。起動後に「登録」"},
    {"id": "llama-8097", "name": "llama-server 8097 (gemma4-12b-qat)", "group": "llm", "port": 8097,
     "cwd": "", "cmd": "", "match": ["llama-server"], "heavy": False, "verified": False,
     "note": "hermes_use_gemma12b.bat 経由。起動後に「登録」"},
    {"id": "llama-8098", "name": "llama-server 8098 (要約用)", "group": "llm", "port": 8098,
     "cwd": "", "cmd": "", "match": ["llama-server"], "heavy": False, "verified": False,
     "note": "夜間要約用。起動後に「登録」"},
    {"id": "llama-8095", "name": "llama-server 8095 (Bonsai-8B・CPUのみ)", "group": "llm", "port": 8095,
     "cwd": "", "cmd": "", "match": ["llama-server"], "heavy": False, "verified": False,
     "note": "依頼文の英語プロンプト変換用。起動後に「登録」"},
    {"id": "freetoken-1919", "name": "FreeToken 1919 (WSL2 / Qwen3.6-35B NVFP4)", "group": "llm", "port": 1919,
     "monitor_only": True, "cmd": "", "heavy": True, "note": "WSL2内。停止は stop_freetoken.bat。監視のみ"},
    {"id": "ollama-11434", "monitor_only": True, "name": "Ollama 11434 (廃止済み)",
     "note": "10/07廃止。稼働していれば異常(トレイ常駐アプリの再起動など)"},
]


def main():
    if not os.path.isfile(TARGET):
        print("services.json が見つかりません。このファイルを services.json と同じフォルダに置いてください。")
        return 1
    with open(TARGET, "r", encoding="utf-8-sig") as f:
        conf = json.load(f)
    byid = {s["id"]: s for s in conf}
    added, updated = [], []
    for e in ENTRIES:
        cur = byid.get(e["id"])
        if cur is None:
            conf.append(dict(e)); added.append(e["id"])
        elif not cur.get("verified"):
            cur.update({k: v for k, v in e.items() if k != "id"}); updated.append(e["id"])
    shutil.copyfile(TARGET, TARGET + ".bak")
    with open(TARGET, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    print("services.json を更新しました。バックアップ: services.json.bak")
    print("追加:", ", ".join(added) or "なし")
    print("更新:", ", ".join(updated) or "なし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
