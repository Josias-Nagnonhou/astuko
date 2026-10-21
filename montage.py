"""Montage final avec ffmpeg : plans + voix + sous-titres animés + transitions + musique."""
import json
import os
import subprocess

W, H, FPS = 720, 1280, 30
XF = 0.35  # durée d'une transition
GAP = 0.25  # respiration entre deux phrases
TRANSITIONS = ["smoothleft", "slideup", "fadewhite", "smoothright", "circleopen", "slidedown", "fade"]
FONT = os.environ.get("SUB_FONT", "Montserrat")
THREADS = os.environ.get("FFMPEG_THREADS", "2")


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg : " + p.stderr[-800:])
    return p


def duration(path):
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path])
    return float(json.loads(p.stdout)["format"]["duration"])


def still_to_clip(image_path, out, seconds):
    """Plan de secours : image fixe avec zoom lent (effet Ken Burns)."""
    frames = int(seconds * FPS) + 1
    vf = (f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
          f"zoompan=z='min(zoom+0.0012,1.18)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS}")
    run(["ffmpeg", "-y", "-loop", "1", "-i", image_path, "-vf", vf, "-t", f"{seconds:.3f}",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-threads", THREADS, out])
    return out


def prepare_segment(src, out, length):
    """Recadre en 9:16, ajuste la durée (ralenti léger puis gel), couleurs un peu plus vives."""
    d = duration(src)
    factor = min(max(length / d, 1.0), 1.5)
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
          f"setpts={factor:.4f}*PTS,fps={FPS},tpad=stop_mode=clone:stop_duration={length:.3f},"
          f"trim=duration={length:.3f},eq=saturation=1.1:contrast=1.04")
    run(["ffmpeg", "-y", "-i", src, "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-pix_fmt", "yuv420p", "-threads", THREADS, out])
    return out


def _ass_time(t):
    t = max(0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(scenes, starts, voice_durs, path, titre=""):
    """Sous-titres façon TikTok : 3-4 mots à la fois, pop-in, + badge d'étape en haut."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{FONT},58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,3,2,50,50,360,1
Style: Badge,{FONT},38,&H00111111,&H00111111,&H0000D7FF,&H0000D7FF,-1,0,0,0,100,100,2,0,3,14,0,8,40,40,130,1
Style: Hook,{FONT},56,&H0000E1FF,&H0000E1FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,5,2,8,50,50,230,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for i, sc in enumerate(scenes):
        st, vd = starts[i], voice_durs[i]
        seg_end = starts[i + 1] if i + 1 < len(starts) else st + vd + 1.0
        badge = (sc.get("etiquette") or "").strip().upper()
        if badge:
            lines.append(f"Dialogue: 1,{_ass_time(st)},{_ass_time(seg_end)},Badge,,0,0,0,,"
                         f"{{\\fad(150,100)\\t(0,180,\\fscx110\\fscy110)\\t(180,300,\\fscx100\\fscy100)}}{_esc(badge)}")
        if i == 0 and titre:
            lines.append(f"Dialogue: 1,{_ass_time(st)},{_ass_time(seg_end)},Hook,,0,0,0,,"
                         f"{{\\fad(200,150)}}{_esc(titre.upper())}")
        words = (sc.get("voix") or "").split()
        if not words:
            continue
        chunks, cur = [], []
        for w in words:
            cur.append(w)
            if len(cur) >= 4 or w.endswith((".", ",", "!", "?", ":")) and len(cur) >= 2:
                chunks.append(cur)
                cur = []
        if cur:
            chunks.append(cur)
        total_chars = sum(len(" ".join(c)) for c in chunks) or 1
        t = st
        for c in chunks:
            txt = " ".join(c)
            dur = vd * len(txt) / total_chars
            fx = "{\\fscx82\\fscy82\\t(0,120,\\fscx100\\fscy100)}"
            lines.append(f"Dialogue: 0,{_ass_time(t)},{_ass_time(t + dur)},Sub,,0,0,0,,{fx}{_esc(txt.upper())}")
            t += dur
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")
    return path


def assemble(clips, voices, scenes, out, workdir, titre="", music=None):
    """clips[i] = vidéo brute du plan i, voices[i] = mp3 de la voix du plan i."""
    n = len(clips)
    vd = [duration(v) for v in voices]
    D = [v + GAP for v in vd]
    D[-1] = vd[-1] + 1.2  # on laisse finir la dernière image
    L = [D[i] + (XF if i < n - 1 else 0) for i in range(n)]
    starts, acc = [], 0.0
    for d in D:
        starts.append(acc)
        acc += d
    total = acc

    segs = []
    for i, c in enumerate(clips):
        seg = os.path.join(workdir, f"seg_{i}.mp4")
        prepare_segment(c, seg, L[i])
        segs.append(seg)

    ass = build_ass(scenes, starts, vd, os.path.join(workdir, "subs.ass"), titre)

    cmd = ["ffmpeg", "-y"]
    for s in segs:
        cmd += ["-i", s]
    for v in voices:
        cmd += ["-i", v]
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]

    fc = []
    # vidéo : chaîne de transitions
    if n == 1:
        fc.append("[0:v]null[vx]")
    else:
        prev = "[0:v]"
        for k in range(1, n):
            off = starts[k]
            outl = f"[x{k}]" if k < n - 1 else "[vx]"
            fc.append(f"{prev}[{k}:v]xfade=transition={TRANSITIONS[(k - 1) % len(TRANSITIONS)]}:duration={XF}:offset={off:.3f}{outl}")
            prev = outl
    fontsdir = "/usr/share/fonts"
    fc.append(f"[vx]ass={ass}:fontsdir={fontsdir},format=yuv420p[v]")
    # audio : voix calées sur chaque plan
    for i in range(n):
        fc.append(f"[{n + i}:a]aresample=44100,apad=whole_dur={D[i]:.3f},atrim=0:{D[i]:.3f}[a{i}]")
    fc.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1,loudnorm=I=-15:TP=-1.5[voix]")
    if music:
        fc.append(f"[{2 * n}:a]aresample=44100,volume=0.18,atrim=0:{total:.3f},afade=t=out:st={max(total - 1.5, 0):.3f}:d=1.5[mus]")
        fc.append("[voix]asplit=2[voix1][voixsc]")
        fc.append("[mus][voixsc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=300[musd]")
        fc.append("[voix1][musd]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        fc.append("[voix]anull[a]")

    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-threads", THREADS,
            "-t", f"{total:.3f}", out]
    run(cmd)
    return out
