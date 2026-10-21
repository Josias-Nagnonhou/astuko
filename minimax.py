"""Client MiniMax : texte (script), voix, images et vidéos."""
import json
import os
import re
import time

import requests

API = os.environ.get("MINIMAX_API_BASE", "https://api.minimax.io")
LLM_MODEL = os.environ.get("LLM_MODEL", "MiniMax-M3.1-Flash")
TTS_MODEL = os.environ.get("TTS_MODEL", "speech-2.8-hd")


class MiniMaxError(Exception):
    pass


def _key():
    k = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not k:
        raise MiniMaxError("La clé API MiniMax n'est pas configurée sur le serveur.")
    return k


def _h():
    return {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}


def _check(r):
    """Lève une erreur lisible si la réponse MiniMax est un échec."""
    try:
        data = r.json()
    except Exception:
        raise MiniMaxError(f"Réponse illisible de MiniMax (HTTP {r.status_code}) : {r.text[:300]}")
    if r.status_code >= 400:
        msg = (data.get("error") or {}).get("message") or json.dumps(data)[:300]
        raise MiniMaxError(_traduire(msg, r.status_code))
    br = data.get("base_resp") or {}
    if br.get("status_code", 0) not in (0, None):
        raise MiniMaxError(_traduire(br.get("status_msg", ""), br.get("status_code")))
    return data


def _traduire(msg, code):
    m = str(msg).lower()
    if "balance" in m or str(code) in ("1008", "402"):
        return "Solde MiniMax insuffisant : recharge ton compte API."
    if "sensitive" in m or str(code) in ("1026", "1027", "422"):
        return "MiniMax a refusé ce contenu (jugé sensible)."
    if "rate limit" in m or str(code) in ("1002", "429"):
        return "Trop de demandes en même temps chez MiniMax, réessaie dans une minute."
    if "auth" in m or "login" in m or str(code) in ("1004", "2049", "401"):
        return "Clé API MiniMax invalide."
    return f"Erreur MiniMax ({code}) : {msg}"


# ---------------------------------------------------------------- texte
def chat(messages, max_tokens=8000):
    body = {
        "model": LLM_MODEL,
        "messages": messages,
        "max_completion_tokens": max_tokens,
    }
    if "Flash" in LLM_MODEL:
        body["reasoning_effort"] = "low"
    r = requests.post(f"{API}/v1/chat/completions", headers=_h(), json=body, timeout=300)
    data = _check(r)
    content = data["choices"][0]["message"].get("content") or ""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()


def extract_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise MiniMaxError("Le script généré n'est pas dans le bon format, réessaie.")
    return json.loads(text[start : end + 1])


# ---------------------------------------------------------------- voix
def list_voices():
    r = requests.post(f"{API}/v1/get_voice", headers=_h(), json={"voice_type": "all"}, timeout=60)
    data = _check(r)
    voices = []
    for v in data.get("voice_cloning") or []:
        voices.append({"id": v["voice_id"], "nom": f"Clonée — {v.get('description') or v['voice_id']}", "type": "clonee"})
    for v in data.get("voice_generation") or []:
        voices.append({"id": v["voice_id"], "nom": f"Créée — {v.get('description') or v['voice_id']}", "type": "creee"})
    for v in data.get("system_voice") or []:
        vid = v.get("voice_id", "")
        if vid.lower().startswith("french"):
            voices.append({"id": vid, "nom": f"MiniMax — {v.get('voice_name') or vid}", "type": "systeme"})
    return voices


def tts(text, voice_id, out_path, speed=1.0):
    body = {
        "model": TTS_MODEL,
        "text": text,
        "stream": False,
        "language_boost": "French",
        "output_format": "hex",
        "text_normalization_mode": "quality",
        "voice_setting": {"voice_id": voice_id, "speed": float(speed), "vol": 1, "pitch": 0},
        "audio_setting": {"sample_rate": 44100, "bitrate": 128000, "format": "mp3", "channel": 1},
    }
    last = None
    for _ in range(3):
        try:
            r = requests.post(f"{API}/v1/t2a_v2", headers=_h(), json=body, timeout=180)
            data = _check(r)
            audio = (data.get("data") or {}).get("audio")
            if not audio:
                raise MiniMaxError("Aucun audio reçu de MiniMax.")
            with open(out_path, "wb") as f:
                f.write(bytes.fromhex(audio))
            return out_path
        except (MiniMaxError, requests.RequestException) as e:
            last = e
            if "insuffisant" in str(e) or "invalide" in str(e):
                raise
            time.sleep(4)
    raise MiniMaxError(f"Voix off impossible : {last}")


# ---------------------------------------------------------------- images
def image(prompt, aspect="9:16"):
    body = {"model": "image-01", "prompt": prompt[:1500], "aspect_ratio": aspect,
            "response_format": "url", "n": 1, "prompt_optimizer": False}
    r = requests.post(f"{API}/v1/image_generation", headers=_h(), json=body, timeout=180)
    data = _check(r)
    urls = (data.get("data") or {}).get("image_urls") or []
    if not urls:
        raise MiniMaxError("MiniMax a refusé de générer cette image (contenu jugé sensible).")
    return urls[0]


# ---------------------------------------------------------------- vidéos
def video_eco_submit(prompt, image_url):
    """Image -> vidéo 6 s avec Hailuo 2.3 Fast (mode économique). Renvoie l'id de tâche."""
    body = {"model": "MiniMax-Hailuo-2.3-Fast", "prompt": prompt[:2000], "first_frame_image": image_url,
            "duration": 6, "resolution": "768P", "prompt_optimizer": True}
    r = requests.post(f"{API}/v1/video_generation", headers=_h(), json=body, timeout=120)
    return _check(r)["task_id"]


def video_eco_wait(task_id):
    deadline = time.time() + 20 * 60
    while time.time() < deadline:
        time.sleep(10)
        try:
            q = requests.get(f"{API}/v1/query/video_generation", headers=_h(), params={"task_id": task_id}, timeout=60)
            d = _check(q)
        except requests.RequestException:
            continue
        st = d.get("status")
        if st == "Success":
            f = requests.get(f"{API}/v1/files/retrieve", headers=_h(), params={"file_id": d["file_id"]}, timeout=60)
            return _check(f)["file"]["download_url"]
        if st == "Fail":
            raise MiniMaxError("La génération de ce plan a échoué chez MiniMax.")
    raise MiniMaxError("La génération de ce plan a pris trop de temps.")


def video_premium_submit(prompt, duration):
    """Texte -> vidéo verticale avec MiniMax H3 Max (mode premium). Renvoie l'id de tâche."""
    body = {"model": "MiniMax-H3-Max", "content": [{"type": "text", "text": prompt[:6000]}],
            "duration": int(max(5, min(15, duration))), "resolution": "768P", "ratio": "9:16"}
    r = requests.post(f"{API}/v2/video_generation", headers=_h(), json=body, timeout=120)
    return _check(r)["task_id"]


def video_premium_wait(task_id):
    deadline = time.time() + 25 * 60
    while time.time() < deadline:
        time.sleep(10)
        try:
            q = requests.get(f"{API}/v2/query/video_generation/{task_id}", headers=_h(), timeout=60)
            task = _check(q)["task"]
        except requests.RequestException:
            continue
        st = task.get("status")
        if st == "succeeded":
            return task["content"]["url"]
        if st in ("failed", "cancelled"):
            raise MiniMaxError("La génération de ce plan a échoué chez MiniMax.")
    raise MiniMaxError("La génération de ce plan a pris trop de temps.")


def download(url, path):
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    return path
