"""Astuko — de l'astuce repérée à la vidéo prête à publier."""
import base64
import json
import math
import os
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import minimax as mm
import montage
from prompts import SYSTEM, user_message

DATA = os.environ.get("DATA_DIR", "/tmp/astuces")
os.makedirs(DATA, exist_ok=True)
PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "4"))

# prix indicatifs (USD) — tarifs MiniMax pay-as-you-go
PRIX_ECO_PAR_SCENE = 0.19 + 0.0035
PRIX_PREMIUM_PAR_SECONDE = 0.08
PRIX_VOIX_PAR_CARACTERE = 100 / 1_000_000

app = FastAPI(title="Astuko")
JOBS = {}
RENDER_LOCK = threading.Lock()


def auth(code):
    if PASSWORD and (code or "").strip() != PASSWORD:
        raise HTTPException(401, "Code d'accès incorrect.")


def save_job(job):
    JOBS[job["id"]] = job
    with open(os.path.join(DATA, job["id"], "job.json"), "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)


def load_job(jid):
    if jid in JOBS:
        return JOBS[jid]
    p = os.path.join(DATA, jid, "job.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            JOBS[jid] = json.load(f)
        return JOBS[jid]
    raise HTTPException(404, "Vidéo introuvable (le serveur a peut-être redémarré).")


def estimation(scenes, qualite):
    chars = sum(len(s.get("voix", "")) for s in scenes)
    voix = chars * PRIX_VOIX_PAR_CARACTERE
    if qualite == "premium":
        secs = sum(max(5, math.ceil(len(s.get("voix", "").split()) / 2.6 + 0.6)) for s in scenes)
        video = secs * PRIX_PREMIUM_PAR_SECONDE
    else:
        video = len(scenes) * PRIX_ECO_PAR_SCENE
    return round(voix + video, 2)


# ------------------------------------------------------------------ pages
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/config")
def config():
    return {"code_requis": bool(PASSWORD), "cle_configuree": bool(os.environ.get("MINIMAX_API_KEY"))}


@app.get("/api/voices")
def voices(x_code: str = Header(default="")):
    auth(x_code)
    try:
        return {"voices": mm.list_voices()}
    except mm.MiniMaxError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ analyse
def _compress_for_analysis(src, dst):
    subprocess.run(["ffmpeg", "-y", "-i", src, "-t", "180", "-vf", "scale=-2:480,fps=10",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-c:a", "aac", "-b:a", "48k",
                    "-ac", "1", dst], capture_output=True, check=True)
    return dst


@app.post("/api/analyser")
async def analyser(texte: str = Form(""), video: UploadFile | None = File(None),
                   qualite: str = Form("eco"), x_code: str = Header(default="")):
    auth(x_code)
    if not texte.strip() and not video:
        raise HTTPException(400, "Décris l'astuce ou importe la vidéo d'origine.")
    tmpdir = os.path.join(DATA, "_analyse_" + uuid.uuid4().hex[:8])
    os.makedirs(tmpdir)
    try:
        content = [{"type": "text", "text": user_message(texte, note_video=bool(video))}]
        if video and video.filename:
            src = os.path.join(tmpdir, "src")
            with open(src, "wb") as f:
                shutil.copyfileobj(video.file, f)
            small = _compress_for_analysis(src, os.path.join(tmpdir, "small.mp4"))
            if os.path.getsize(small) > 40 * 1024 * 1024:
                raise HTTPException(400, "Vidéo trop longue : coupe-la à moins de 3 minutes.")
            b64 = base64.b64encode(open(small, "rb").read()).decode()
            content.append({"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}", "fps": 1}})
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]
        try:
            plan = mm.extract_json(mm.chat(msgs))
        except (ValueError, json.JSONDecodeError):
            plan = mm.extract_json(mm.chat(msgs))  # une seconde chance
        perso = plan.get("personnage", "")
        for s in plan.get("scenes", []):
            if perso and perso.lower()[:30] not in s.get("visuel", "").lower():
                s["visuel"] = f"{s.get('visuel', '')} Main character: {perso}."
        plan["estimation"] = estimation(plan.get("scenes", []), qualite)
        return plan
    except mm.MiniMaxError as e:
        raise HTTPException(400, str(e))
    except subprocess.CalledProcessError:
        raise HTTPException(400, "Impossible de lire cette vidéo. Essaie un fichier MP4.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/estimation")
async def estimer(payload: dict, x_code: str = Header(default="")):
    auth(x_code)
    return {"estimation": estimation(payload.get("scenes", []), payload.get("qualite", "eco"))}


# ------------------------------------------------------------------ génération
def _step(job, msg, pct=None):
    job["etape"] = msg
    if pct is not None:
        job["progression"] = int(pct)
    job["maj"] = time.time()
    save_job(job)


VISUAL_STYLE = (" Photorealistic, vertical 9:16 framing, natural warm light, cinematic, shallow depth of field, "
                "smooth slow camera movement, Black African people, realistic modern West African setting, "
                "no text, no logo, no watermark.")


def _make_clip(job, i, scene, voice_dur, wd):
    prompt = scene["visuel"].strip() + VISUAL_STYLE
    out = os.path.join(wd, f"clip_{i}.mp4")
    img_url = None
    try:
        if job["qualite"] == "premium":
            url = mm.video_premium(prompt, math.ceil(voice_dur + 0.6))
        else:
            img_url = mm.image(prompt)
            url = mm.video_eco(prompt, img_url)
        mm.download(url, out)
        return out, None
    except Exception as e:  # plan de secours : image animée
        try:
            if not img_url:
                img_url = mm.image(prompt)
            img = mm.download(img_url, os.path.join(wd, f"img_{i}.jpg"))
            montage.still_to_clip(img, out, voice_dur + 1)
            return out, f"Plan {i + 1} : vidéo refusée/échouée, remplacée par une image animée ({e})."
        except Exception as e2:
            raise mm.MiniMaxError(f"Plan {i + 1} impossible : {e2}")


def run_job(jid):
    job = load_job(jid)
    wd = os.path.join(DATA, jid)
    try:
        scenes = job["scenes"]
        n = len(scenes)
        _step(job, "Voix off en cours…", 3)
        voices, vdurs = [], []
        for i, s in enumerate(scenes):
            p = os.path.join(wd, f"voix_{i}.mp3")
            mm.tts(s["voix"], job["voice_id"], p, job.get("vitesse", 1.0))
            voices.append(p)
            vdurs.append(montage.duration(p))
            _step(job, f"Voix off : {i + 1}/{n}", 3 + 12 * (i + 1) / n)

        _step(job, f"Création des {n} plans vidéo par l'IA (compte 3 à 8 minutes)…", 16)
        clips = [None] * n
        done = 0
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
            futs = {ex.submit(_make_clip, job, i, scenes[i], vdurs[i], wd): i for i in range(n)}
            for f in as_completed(futs):
                i = futs[f]
                clip, warn = f.result()
                clips[i] = clip
                if warn:
                    job.setdefault("avertissements", []).append(warn)
                done += 1
                _step(job, f"Plans vidéo prêts : {done}/{n}", 16 + 64 * done / n)

        _step(job, "Montage : transitions, sous-titres, musique…", 82)
        with RENDER_LOCK:
            final = os.path.join(wd, "video_finale.mp4")
            music = job.get("musique")
            montage.assemble(clips, voices, scenes, final, wd, titre=job.get("accroche", ""), music=music)
        job["statut"] = "termine"
        job["duree"] = round(montage.duration(final), 1)
        _step(job, "Vidéo prête !", 100)
        for f in os.listdir(wd):  # on libère la place, on garde la vidéo finale
            if f.startswith(("seg_", "clip_", "img_")):
                os.remove(os.path.join(wd, f))
    except Exception as e:
        traceback.print_exc()
        job["statut"] = "erreur"
        job["erreur"] = str(e)
        _step(job, "Erreur", job.get("progression", 0))


@app.post("/api/generer")
async def generer(payload: str = Form(...), musique: UploadFile | None = File(None),
                  x_code: str = Header(default="")):
    auth(x_code)
    p = json.loads(payload)
    scenes = [s for s in p.get("scenes", []) if s.get("voix", "").strip() and s.get("visuel", "").strip()]
    if not scenes:
        raise HTTPException(400, "Aucune scène à générer.")
    if len(scenes) > 12:
        raise HTTPException(400, "12 scènes maximum par vidéo.")
    if not p.get("voice_id"):
        raise HTTPException(400, "Choisis une voix.")
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:5]
    wd = os.path.join(DATA, jid)
    os.makedirs(wd)
    job = {"id": jid, "titre": p.get("titre", "Astuce"), "accroche": p.get("accroche", ""),
           "scenes": scenes, "voice_id": p["voice_id"], "vitesse": float(p.get("vitesse", 1.0)),
           "qualite": p.get("qualite", "eco"), "description": p.get("description_publication", ""),
           "hashtags": p.get("hashtags", []), "statut": "en_cours", "progression": 0,
           "etape": "Démarrage…", "cree": time.time(), "estimation": estimation(scenes, p.get("qualite", "eco"))}
    if musique and musique.filename:
        mp = os.path.join(wd, "musique" + os.path.splitext(musique.filename)[1][:5])
        with open(mp, "wb") as f:
            shutil.copyfileobj(musique.file, f)
        job["musique"] = mp
    save_job(job)
    threading.Thread(target=run_job, args=(jid,), daemon=True).start()
    return {"id": jid}


@app.get("/api/jobs")
def jobs(x_code: str = Header(default="")):
    auth(x_code)
    out = []
    for d in sorted(os.listdir(DATA), reverse=True):
        if d.startswith("_"):
            continue
        try:
            j = load_job(d)
            out.append({k: j.get(k) for k in ("id", "titre", "statut", "progression", "etape", "cree")})
        except Exception:
            pass
    return {"jobs": out[:30]}


@app.get("/api/jobs/{jid}")
def job_status(jid: str, x_code: str = Header(default="")):
    auth(x_code)
    j = load_job(jid)
    return {k: v for k, v in j.items() if k not in ("musique",)}


@app.get("/api/jobs/{jid}/video")
def job_video(jid: str, code: str = ""):
    auth(code)
    p = os.path.join(DATA, os.path.basename(jid), "video_finale.mp4")
    if not os.path.exists(p):
        raise HTTPException(404, "Vidéo pas encore prête.")
    j = load_job(jid)
    nom = "".join(c if c.isalnum() else "-" for c in j.get("titre", "astuce"))[:50].strip("-") or "astuce"
    return FileResponse(p, media_type="video/mp4", filename=f"{nom}.mp4")


@app.get("/healthz")
def health():
    return JSONResponse({"ok": True})
