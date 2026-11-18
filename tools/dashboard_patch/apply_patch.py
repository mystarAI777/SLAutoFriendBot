# -*- coding: utf-8 -*-
"""dashboard.py にモデル取得・Docker一覧を追加する（追加のみ・既存ロジックは変更しない）
使い方: dashboard.py と同じフォルダで apply_patch.bat を実行
"""
import os, sys, shutil

BASE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(BASE, "dashboard.py")
MARK = "# --- モデル取得（読み取り専用・追加機能）---"

PROBE = r'''# --- モデル取得（読み取り専用・追加機能）---
import urllib.request
from concurrent.futures import ThreadPoolExecutor

N8N_KEY = os.environ.get("N8N_API_KEY", "")
KINDS = (("ollama", ("ollama",)), ("llama", ("llama-server", "llama_server", "llama-")),
         ("comfyui", ("comfy",)), ("n8n", ("n8n",)), ("tts", ("irodori", "voicevox")),
         ("hermes", ("hermes",)))
MODEL_KEYS = ("ckpt_name", "unet_name", "lora_name", "vae_name", "clip_name", "clip_name1",
              "clip_name2", "control_net_name", "model_name")
MODEL_EXT = (".gguf", ".safetensors", ".ckpt", ".pt", ".pth", ".bin")
_DK = {"t": 0, "v": None}


def http_json(port, path, headers=None, timeout=1.5):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2000000).decode("utf-8", "replace"))


def kind_of(text):
    t = (text or "").lower()
    for k, keys in KINDS:
        if any(x in t for x in keys):
            return k
    return None


def base(p):
    return os.path.basename(str(p).replace("\\", "/"))


def cmd_models(cmd, flags):
    res, a = [], [str(x) for x in (cmd or [])]
    for i, x in enumerate(a):
        if flags and x in ("-m", "--model", "-hf", "--hf-repo") and i + 1 < len(a):
            res.append(a[i + 1])
        elif x.lower().endswith(MODEL_EXT):
            res.append(x)
    return [base(x) for x in dict.fromkeys(res)]


def mapped_models(pid):
    try:
        res = [base(m.path) for m in psutil.Process(pid).memory_maps() if m.path.lower().endswith(MODEL_EXT)]
        return list(dict.fromkeys(res))[:5]
    except Exception:
        return []


def probe_llama(port, proc):
    names = []
    try:
        p = http_json(port, "/props")
        n = base(p.get("model_path") or "")
        ctx = (p.get("default_generation_settings") or {}).get("n_ctx") or p.get("n_ctx")
        if n:
            names.append(n + ((" / ctx " + str(ctx)) if ctx else ""))
    except Exception:
        pass
    try:
        for m in (http_json(port, "/v1/models").get("data") or [])[:3]:
            mid = base(m.get("id") or "")
            if mid and not any(mid in x for x in names):
                names.append(mid)
    except Exception:
        pass
    pre = "モデル: "
    if not names:
        pre, names = "起動引数のモデル: ", cmd_models(proc.get("cmd"), True)
    if not names and proc.get("pid"):
        pre, names = "読込中のファイル: ", mapped_models(proc["pid"])
    if not names:
        return {"head": "モデル不明（ロード中の可能性）", "lines": []}
    return {"head": names[0], "lines": [pre + x for x in names]}


def probe_ollama(port, proc):
    ms = http_json(port, "/api/ps").get("models") or []
    lines = ["ロード中: %s（VRAM %.1fGB）" % (m.get("name") or m.get("model"), (m.get("size_vram") or 0) / 1073741824)
             for m in ms]
    head = ", ".join(str(m.get("name") or m.get("model")) for m in ms) or "待機（モデル未ロード）"
    return {"head": head, "lines": lines or ["ロード中のモデルなし（待機）"]}


def comfy_models(graph):
    res = []
    for n in (graph or {}).values():
        if not isinstance(n, dict):
            continue
        for k in MODEL_KEYS:
            v = (n.get("inputs") or {}).get(k)
            if isinstance(v, str) and v:
                res.append("%s: %s" % (k.replace("_name", ""), base(v)))
    return list(dict.fromkeys(res))[:10]


def probe_comfy(port, proc):
    q = http_json(port, "/queue")
    run, pend = q.get("queue_running") or [], q.get("queue_pending") or []
    lines = ["実行中 %d / 待機 %d" % (len(run), len(pend))]
    ms, label = [], ""
    if run:
        ms, label = comfy_models(run[0][2]), "実行中ジョブのモデル"
    else:
        for v in (safe(lambda: http_json(port, "/history?max_items=1"), {}) or {}).values():
            ms, label = comfy_models((v.get("prompt") or [0, 0, {}])[2]), "直近ジョブのモデル"
    lines += ["%s  %s" % (label, x) for x in ms]
    st = safe(lambda: http_json(port, "/system_stats"), {}) or {}
    ver = (st.get("system") or {}).get("comfyui_version")
    dev = (st.get("devices") or [{}])[0]
    if ver:
        lines.append("ComfyUI v%s" % ver)
    if dev.get("vram_total"):
        lines.append("VRAM空き %.1f / %.1f GB" % (dev.get("vram_free", 0) / 1073741824, dev["vram_total"] / 1073741824))
    head = lines[0] + ((" ・" + ms[0]) if ms else "")
    return {"head": head, "lines": lines}


def probe_n8n(port, proc):
    lines = []
    http_json(port, "/healthz")
    lines.append("n8n 稼働確認OK")
    head = "稼働中"
    if not N8N_KEY:
        lines.append("ワークフロー一覧は非表示（環境変数 N8N_API_KEY 未設定）")
        return {"head": head, "lines": lines}
    wfs = http_json(port, "/api/v1/workflows?active=true&limit=100", {"X-N8N-API-KEY": N8N_KEY}).get("data") or []
    head = "有効WF %d件" % len(wfs)
    lines.append("有効ワークフロー %d 件" % len(wfs))
    for w in wfs[:20]:
        sched, models = False, []
        for n in (w.get("nodes") or []):
            t = str(n.get("type") or "")
            sched = sched or "scheduleTrigger" in t or t.endswith(".cron")
            m = (n.get("parameters") or {}).get("model")
            if isinstance(m, dict):
                m = m.get("value") or m.get("cachedResultName")
            if isinstance(m, str) and m and ("langchain" in t or "openai" in t.lower() or "ollama" in t.lower()):
                models.append(m)
        lines.append("・%s%s%s" % (w.get("name"), "［定期実行］" if sched else "",
                                   ("　モデル: " + ", ".join(dict.fromkeys(models))) if models else ""))
    return {"head": head, "lines": lines}


def probe_tts(port, proc):
    for path, fmt in (("/version", "VOICEVOX %s"), ("/v1/models", None)):
        try:
            d = http_json(port, path)
            if fmt:
                return {"head": fmt % d, "lines": [fmt % d]}
            ids = [str(m.get("id")) for m in (d.get("data") or [])[:3]]
            if ids:
                return {"head": ids[0], "lines": ["モデル: " + x for x in ids]}
        except Exception:
            pass
    return None


PROBES = {"llama": probe_llama, "ollama": probe_ollama, "comfyui": probe_comfy,
          "n8n": probe_n8n, "tts": probe_tts}


def model_info(kind, port, proc):
    proc = proc or {}
    fn = PROBES.get(kind)
    r = safe(lambda: fn(port, proc), None) if fn and port else None
    if not r or not (r.get("head") or r.get("lines")):
        ms = []
        if kind in ("hermes", "tts") and (proc.get("name") or "").lower() not in PROTECTED:
            ms = cmd_models(proc.get("cmd"), False)
        r = {"head": ms[0] if ms else "", "lines": ["モデルファイル: " + x for x in ms]}
    return r


def docker_list():
    if time.time() - _DK["t"] < 10:
        return _DK["v"]
    exe = shutil.which("docker")
    res = None
    if exe:
        try:
            o = subprocess.run([exe, "ps", "--format", "{{json .}}"], capture_output=True, text=True,
                               timeout=4, creationflags=NOWIN).stdout.strip().splitlines()
            res = [{k: j.get(k) for k in ("Names", "Image", "Ports", "Status")}
                   for j in (json.loads(x) for x in o if x.strip())]
        except Exception:
            res = None
    _DK["t"], _DK["v"] = time.time(), res
    return res


def attach_info(items, out):
    ex = ThreadPoolExecutor(max_workers=8)
    jobs = []
    for d in items:
        d["kind"], d["info"], d["info_head"] = None, [], ""
        if not d["running"] or d.get("monitor_only"):
            continue
        pr = d.get("proc") or {}
        kind = kind_of(d["id"]) or kind_of(" ".join([pr.get("name", "")] + list(pr.get("cmd") or []) + [pr.get("cwd", "")]))
        jobs.append((d, kind, ex.submit(model_info, kind, d["port"], pr)))
    for o in out:
        o["kind"], o["info"], o["info_head"] = None, [], ""
        kind = kind_of(" ".join([o["name"]] + list(o["cmd"] or []) + [o["cwd"]]))
        if kind:
            jobs.append((o, kind, ex.submit(model_info, kind, (o["ports"] or [None])[0], o)))
    dkf = ex.submit(docker_list)
    for d, kind, f in jobs:
        r = safe(lambda: f.result(timeout=8), None) or {}
        d["kind"], d["info"], d["info_head"] = kind, r.get("lines", []), r.get("head", "")
    dk = safe(lambda: dkf.result(timeout=8), None)
    ex.shutdown(wait=False)
    return dk


'''

# (置換前, 置換後) 置換前は dashboard.py 内に必ず1回だけ存在すること
EDITS = [
    ("def status():\n", PROBE + "def status():\n"),
    ("    # 警告（止めはしない）\n", "    dk = attach_info(items, out)\n\n    # 警告（止めはしない）\n"),
    ('"gpu": gp, "warnings": warn}', '"gpu": gp, "warnings": warn, "docker": dk}'),
    ("#oth div{padding:3px 0}\n",
     "#oth div{padding:3px 0}\n"
     ".sv small.md{color:var(--fg);max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n"
     "#det div{padding:2px 0}#det b{color:var(--fg)}\n"),
    ('<details><summary id="osum">登録外</summary>',
     '<details open><summary id="dsum">稼働中の詳細（モデル・状態）</summary><div id="det"></div></details>\n'
     '<details><summary id="osum">登録外</summary>'),
    ("const lab=[$('span',{class:'dot'}),v.name,$('small',{},sub)];",
     "const lab=[$('span',{class:'dot'}),v.name,$('small',{},sub),...(v.info_head?[$('small',{class:'md'},v.info_head)]:[])];"),
    ("(v.exposed?'　⚠外部公開':'')+'　',",
     "(v.exposed?'　⚠外部公開':'')+(v.info_head?'　'+v.info_head:'')+'　',"),
    ("}\nasync function load(){",
     " const dt=document.getElementById('det');dt.replaceChildren();\n"
     " d.services.concat(d.others).filter(v=>v.info&&v.info.length).forEach(v=>dt.append($('div',{},$('b',{},v.name),...v.info.map(x=>$('div',{},'　'+x)))));\n"
     " (d.docker||[]).forEach(c=>dt.append($('div',{},'Docker: '+(c.Names||'')+'　'+(c.Image||'')+'　'+(c.Ports||'')+'　'+(c.Status||''))));\n"
     "}\nasync function load(){"),
]


def main():
    if not os.path.isfile(TARGET):
        print("dashboard.py が見つかりません。apply_patch.py を dashboard.py と同じフォルダに置いてください。")
        return 1
    with open(TARGET, "r", encoding="utf-8", newline="") as f:
        src = f.read()
    if MARK in src:
        print("適用済みです。変更しません。")
        return 0
    crlf = "\r\n" in src
    out = src
    for i, (old, new) in enumerate(EDITS, 1):
        if crlf:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        n = out.count(old)
        if n != 1:
            print("適用できません: 変更箇所 %d が %d 件見つかりました（想定は1件）。dashboard.py が想定と異なります。何も変更していません。" % (i, n))
            return 1
        out = out.replace(old, new)
    shutil.copyfile(TARGET, TARGET + ".bak")
    with open(TARGET, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    print("適用しました。バックアップ: dashboard.py.bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
