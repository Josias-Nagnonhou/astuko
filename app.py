"""Astuko — de l'astuce repérée à la vidéo prête à publier."""
import base64
import json
import math
import os
import re
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


SAVE_LOCK = threading.Lock()


def save_job(job):
    JOBS[job["id"]] = job
    with SAVE_LOCK:
        p = os.path.join(DATA, job["id"], "job.json")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)


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


def _resolve_facebook(url):
    """Les liens « partager » de Facebook redirigent vers la page de connexion : on retrouve la vraie adresse."""
    import html as _html
    import re
    import requests

    if not re.search(r"(facebook\.com/share|fb\.watch|m\.facebook\.com|facebook\.com/story\.php)", url):
        return url
    for ua in ("facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
               "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"):
        try:
            r = requests.get(url, headers={"User-Agent": ua, "Accept-Language": "fr-FR,fr;q=0.9"},
                             allow_redirects=True, timeout=20)
        except requests.RequestException:
            continue
        final = r.url
        m = re.search(r'property="og:url"\s+content="([^"]+)"', r.text) or \
            re.search(r'content="([^"]+)"\s+property="og:url"', r.text)
        if m:
            cand = _html.unescape(m.group(1))
            if "login" not in cand:
                return cand
        if "login" not in final and final != url:
            return final
        m = re.search(r"[?&]next=([^&]+)", final)
        if m:
            from urllib.parse import unquote
            nxt = unquote(m.group(1))
            if "login" not in nxt:
                return nxt
    return url


def _download_link(url, folder):
    """Récupère la vidéo d'un lien TikTok / Facebook / YouTube / Instagram (pour l'analyse seulement)."""
    import yt_dlp

    url = _resolve_facebook(url)

    opts = {
        "outtmpl": os.path.join(folder, "lien.%(ext)s"),
        "format": "b[height<=720][ext=mp4]/b[height<=720]/bv*[height<=720]+ba/b",
        "merge_output_format": "mp4",
        "max_filesize": 300 * 1024 * 1024,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
        "match_filter": yt_dlp.utils.match_filter_func("duration < 900"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            desc = " ".join(x for x in [info.get("title") or "", info.get("description") or ""] if x)[:1500]
    except Exception as e:
        m = str(e).lower()
        if "sign in" in m or "bot" in m or "login" in m or "private" in m:
            raise HTTPException(400, "Cette plateforme bloque la lecture de ce lien (vidéo privée ou protection anti-robot). "
                                     "Télécharge la vidéo et importe le fichier à la place.")
        raise HTTPException(400, "Impossible de lire ce lien. Vérifie qu'il est public, ou importe le fichier vidéo.")
    files = [f for f in os.listdir(folder) if f.startswith("lien.")]
    if not files:
        raise HTTPException(400, "La vidéo de ce lien est trop longue ou introuvable. Importe le fichier à la place.")
    return os.path.join(folder, files[0]), desc


@app.post("/api/analyser")
def analyser(texte: str = Form(""), lien: str = Form(""), video: UploadFile | None = File(None),
             qualite: str = Form("eco"), x_code: str = Header(default="")):
    auth(x_code)
    lien = lien.strip()
    has_file = bool(video and video.filename)
    if not texte.strip() and not has_file and not lien:
        raise HTTPException(400, "Colle le lien de la vidéo, importe-la, ou décris l'astuce.")
    if lien and not lien.startswith(("http://", "https://")):
        raise HTTPException(400, "Le lien doit commencer par https://")
    tmpdir = os.path.join(DATA, "_analyse_" + uuid.uuid4().hex[:8])
    os.makedirs(tmpdir)
    try:
        src = None
        if has_file:
            src = os.path.join(tmpdir, "src")
            with open(src, "wb") as f:
                shutil.copyfileobj(video.file, f)
        elif lien:
            src, desc = _download_link(lien, tmpdir)
            if desc:
                texte = (texte + "\n\nTitre/description d'origine (contexte seulement) : " + desc).strip()
        content = [{"type": "text", "text": user_message(texte, note_video=bool(src))}]
        if src:
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
    """Crée le plan i. Reprend une tâche déjà lancée chez MiniMax après un redémarrage (pas de double paiement)."""
    out = os.path.join(wd, f"clip_{i}.mp4")
    if os.path.exists(out) and os.path.getsize(out) > 1000:
        return out, None
    prompt = scene["visuel"].strip() + VISUAL_STYLE
    t = job.setdefault("taches", {}).setdefault(str(i), {})
    try:
        if job["qualite"] == "premium":
            if not t.get("task_id"):
                t["task_id"] = mm.video_premium_submit(prompt, math.ceil(voice_dur + 0.6))
                save_job(job)
            url = mm.video_premium_wait(t["task_id"])
        else:
            if not t.get("img"):
                t["img"] = mm.image(prompt)
                save_job(job)
            img_path = os.path.join(wd, f"image_{i}.jpg")
            if not os.path.exists(img_path):
                try:
                    mm.download(t["img"], img_path)
                except Exception:
                    pass
            if not t.get("task_id"):
                t["task_id"] = mm.video_eco_submit(prompt, t["img"])
                save_job(job)
            url = mm.video_eco_wait(t["task_id"])
        mm.download(url, out + ".part")
        os.replace(out + ".part", out)
        return out, None
    except Exception as e:  # plan de secours : image animée
        try:
            img_url = t.get("img") or mm.image(prompt)
            img = mm.download(img_url, os.path.join(wd, f"img_{i}.jpg"))
            montage.still_to_clip(img, out, voice_dur + 1)
            return out, f"Plan {i + 1} : vidéo refusée ou échouée, remplacée par une image animée ({e})."
        except Exception as e2:
            raise mm.MiniMaxError(f"Plan {i + 1} impossible : {e2}")


RUNNING = set()


def run_job(jid):
    if jid in RUNNING:
        return
    RUNNING.add(jid)
    job = load_job(jid)
    wd = os.path.join(DATA, jid)
    try:
        scenes = job["scenes"]
        n = len(scenes)
        _step(job, "Voix off en cours…", max(job.get("progression", 0), 3))
        voices, vdurs = [], []
        for i, s in enumerate(scenes):
            p = os.path.join(wd, f"voix_{i}.mp3")
            if not (os.path.exists(p) and os.path.getsize(p) > 500):
                mm.tts(s["voix"], job["voice_id"], p + ".part", job.get("vitesse", 1.0))
                os.replace(p + ".part", p)
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
            montage.assemble(clips, voices, scenes, final + ".tmp.mp4", wd, titre=job.get("accroche", ""), music=music)
            os.replace(final + ".tmp.mp4", final)
        job["statut"] = "termine"
        job["duree"] = round(montage.duration(final), 1)
        _step(job, "Vidéo prête !", 100)
        for f in os.listdir(wd):  # on libère la place, on garde la vidéo finale
            if f.startswith(("seg_", "video_sans_son")):
                os.remove(os.path.join(wd, f))
    except Exception as e:
        traceback.print_exc()
        job["statut"] = "erreur"
        job["erreur"] = str(e)
        _step(job, "Erreur", job.get("progression", 0))
    finally:
        RUNNING.discard(jid)


@app.on_event("startup")
def resume_jobs():
    """Après un redémarrage du serveur, on relance les vidéos qui étaient en cours."""
    for d in os.listdir(DATA):
        if d.startswith("_"):
            shutil.rmtree(os.path.join(DATA, d), ignore_errors=True)
            continue
        try:
            j = load_job(d)
        except Exception:
            continue
        if j.get("statut") == "en_cours":
            j["reprises"] = j.get("reprises", 0) + 1
            if j["reprises"] > 3:
                j["statut"] = "erreur"
                j["erreur"] = "Le serveur a redémarré plusieurs fois pendant cette vidéo. Relance-la ou préviens Claude."
                save_job(j)
                continue
            j["etape"] = "Reprise après redémarrage du serveur…"
            save_job(j)
            threading.Thread(target=run_job, args=(d,), daemon=True).start()


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
    out = {k: v for k, v in j.items() if k not in ("musique", "taches")}
    out["fichiers"] = _fichiers(jid)
    return out


FICHIER_RE = re.compile(r"^(clip_\d+\.mp4|image_\d+\.jpg|img_\d+\.jpg|voix_\d+\.mp3|subs\.ass|video_finale\.mp4)$")


def _fichiers(jid):
    wd = os.path.join(DATA, os.path.basename(jid))
    if not os.path.isdir(wd):
        return []
    res = []
    for f in sorted(os.listdir(wd), key=lambda x: (re.sub(r"\d+", "", x), int((re.findall(r"\d+", x) or [0])[0]))):
        if FICHIER_RE.match(f) and os.path.getsize(os.path.join(wd, f)) > 0:
            num = re.findall(r"\d+", f)
            kind = "plan" if f.startswith("clip_") else "image" if "image_" in f or f.startswith("img_") else \
                "voix" if f.startswith("voix_") else "autre"
            res.append({"nom": f, "type": kind, "plan": int(num[0]) + 1 if num and kind != "autre" else None})
    return res


@app.get("/api/jobs/{jid}/fichier/{nom}")
def job_fichier(jid: str, nom: str, code: str = ""):
    auth(code)
    if not FICHIER_RE.match(nom):
        raise HTTPException(404, "Fichier inconnu.")
    p = os.path.join(DATA, os.path.basename(jid), nom)
    if not os.path.exists(p):
        raise HTTPException(404, "Fichier introuvable.")
    types = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".mp3": "audio/mpeg", ".ass": "text/plain"}
    return FileResponse(p, media_type=types[os.path.splitext(nom)[1]], filename=f"{jid}-{nom}")


@app.get("/api/jobs/{jid}/zip")
def job_zip(jid: str, code: str = ""):
    auth(code)
    jid = os.path.basename(jid)
    wd = os.path.join(DATA, jid)
    files = _fichiers(jid)
    if not files:
        raise HTTPException(404, "Aucun fichier pour cette vidéo.")
    zp = os.path.join(wd, "tout.zip")
    import zipfile
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(os.path.join(wd, f["nom"]), f["nom"])
        j = load_job(jid)
        script = "\n\n".join(f"Plan {i + 1} — {s.get('etiquette', '')}\n{s.get('voix', '')}" for i, s in enumerate(j.get("scenes", [])))
        z.writestr("script.txt", script + "\n\n---\n" + (j.get("description") or "") + "\n" + " ".join(j.get("hashtags") or []))
    return FileResponse(zp, media_type="application/zip", filename=f"astuko-{jid}.zip")


@app.post("/api/jobs/{jid}/relancer")
def job_relancer(jid: str, x_code: str = Header(default="")):
    """Relance une vidéo en erreur : réutilise les voix et plans déjà générés (rien n'est repayé)."""
    auth(x_code)
    j = load_job(jid)
    if j["id"] in RUNNING:
        return {"ok": True}
    j["statut"] = "en_cours"
    j["erreur"] = None
    j["reprises"] = 0
    _step(j, "Relance : on réutilise ce qui est déjà prêt…", j.get("progression", 0))
    threading.Thread(target=run_job, args=(j["id"],), daemon=True).start()
    return {"ok": True}


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
