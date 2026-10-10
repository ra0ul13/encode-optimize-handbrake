#!/usr/bin/env python3

"""
crf_optimizer.py — Mesure précise des bitrates vidéo/audio d'une source Blu-ray
(dossier BDMV) ou d'un M2TS, puis calcule le CRF optimal x264 pour atteindre
un % cible du bitrate vidéo.

En mode disque, HandBrakeCLI encode via la playlist (--main-feature ou --title N),
ce qui préserve chapitres, langues des pistes audio / sous-titres, etc.

Dépendances : ffmpeg, ffprobe (sudo apt install ffmpeg), HandBrakeCLI

Usage :
  ./crf_optimizer.py /chemin/DISQUE                 (dossier contenant BDMV)
  ./crf_optimizer.py /chemin/DISQUE/BDMV/STREAM/00800.m2ts
  ./crf_optimizer.py /chemin/DISQUE --title 3       (force un titre HandBrake)
  ./crf_optimizer.py film.m2ts --no-disc            (ancien mode : m2ts seul)
  ./crf_optimizer.py /chemin/DISQUE --calibrate --segments 8 --encode handbrake
"""

import argparse
import json
import math
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from datetime import timedelta

# ─────────────────────────── Couleurs ANSI ────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
RED    = "\033[31m"
WHITE  = "\033[97m"

def c(color, text): return f"{color}{text}{RESET}"
def header(text):   print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}\n{BOLD}{WHITE}  {text}{RESET}\n{BOLD}{CYAN}{'─'*60}{RESET}")
def ok(text):       print(f"  {GREEN}✔{RESET}  {text}")
def info(text):     print(f"  {CYAN}→{RESET}  {text}")
def warn(text):     print(f"  {YELLOW}⚠{RESET}  {text}")
def err(text):      print(f"  {RED}✘{RESET}  {text}", file=sys.stderr)
def label(k, v):    print(f"  {DIM}{k:<30}{RESET}{BOLD}{v}{RESET}")

# ─────────────────────────── Vérifications ────────────────────────────

def _which(cmd: str) -> bool:
    return subprocess.run(["which", cmd], capture_output=True).returncode == 0

def check_dependencies():
    missing = [cmd for cmd in ("ffmpeg", "ffprobe") if not _which(cmd)]
    if missing:
        err(f"Dépendance(s) manquante(s) : {', '.join(missing)}")
        print(f"\n  Installez avec : {CYAN}sudo apt install ffmpeg{RESET}\n")
        sys.exit(1)

def require_handbrake():
    if not _which("HandBrakeCLI"):
        err("HandBrakeCLI introuvable (nécessaire pour le mode disque, la calibration "
            "et l'encodage HandBrake).")
        print(f"\n  Installez avec : {CYAN}sudo apt install handbrake-cli{RESET}\n")
        sys.exit(1)

# ─────────────────────────── Source Blu-ray ───────────────────────────

def _ci_child(d: Path, name: str):
    """Sous-dossier/fichier de d dont le nom correspond sans tenir compte de la casse."""
    if not d or not d.is_dir():
        return None
    for ch in d.iterdir():
        if ch.name.upper() == name.upper():
            return ch
    return None


def find_disc_root(p: Path):
    """
    Retourne la racine du disque (dossier contenant BDMV) si p est :
      - la racine elle-même, - le dossier BDMV, - un .m2ts dans BDMV/STREAM.
    Sinon None.
    """
    p = p.resolve()
    if p.is_dir():
        if _ci_child(p, "BDMV"):
            return p
        if p.name.upper() == "BDMV":
            return p.parent
        return None
    if p.parent.name.upper() == "STREAM" and p.parent.parent.name.upper() == "BDMV":
        return p.parent.parent.parent
    return None


def parse_mpls_clips(mpls: Path) -> list:
    """Liste ordonnée (sans doublons) des clips (ex. '00800') référencés par une playlist MPLS."""
    try:
        data = mpls.read_bytes()
    except OSError:
        return []
    if data[:4] != b"MPLS" or len(data) < 12:
        return []
    pl_start = int.from_bytes(data[8:12], "big")
    n_items  = int.from_bytes(data[pl_start + 6:pl_start + 8], "big")
    pos      = pl_start + 10
    clips    = []
    for _ in range(n_items):
        if pos + 7 > len(data):
            break
        length = int.from_bytes(data[pos:pos + 2], "big")
        name   = data[pos + 2:pos + 7].decode("ascii", "replace").upper()
        if name not in clips:
            clips.append(name)
        pos += 2 + length
    return clips


def hb_scan_titles(root: Path):
    """Scan HandBrakeCLI en JSON. Retourne (liste des titres, index du main feature)."""
    info("Scan du disque par HandBrakeCLI (peut prendre un moment)…")
    cmd = ["HandBrakeCLI", "-i", str(root), "-t", "0", "--scan", "--json"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = (res.stdout or "") + "\n" + (res.stderr or "")
    marker = "JSON Title Set:"
    i = out.find(marker)
    if i < 0:
        err("Impossible de lire le résultat du scan HandBrakeCLI.")
        print(f"{DIM}{out[-1500:]}{RESET}")
        sys.exit(1)
    j = out.find("{", i)
    data, _ = json.JSONDecoder().raw_decode(out, j)
    return data.get("TitleList", []), data.get("MainFeature", -1)


def _title_duration(t: dict) -> float:
    d = t.get("Duration", {}) or {}
    if d.get("Ticks"):
        return d["Ticks"] / 90000.0
    return d.get("Hours", 0) * 3600 + d.get("Minutes", 0) * 60 + d.get("Seconds", 0)


def resolve_source(inp: Path, forced_title, no_disc: bool) -> dict:
    """
    Détermine ce que HandBrake encode (disque + titre) et quel m2ts ffprobe analyse.
    """
    if inp.is_file() and inp.suffix.lower() == ".iso":
        err("Les ISO ne sont pas gérés directement (ffprobe ne peut pas lire les clips).")
        info(f"Montez-la d'abord : {CYAN}sudo mount -o loop,ro film.iso /mnt/bd{RESET}"
             f" puis passez /mnt/bd au script.")
        sys.exit(1)

    root = None if no_disc else find_disc_root(inp)

    if root is None:
        if inp.is_dir():
            err(f"{inp} n'est pas une structure Blu-ray (pas de dossier BDMV).")
            sys.exit(1)
        if not no_disc:
            warn("Fichier M2TS hors structure BDMV : chapitres et langues des pistes "
                 "ne pourront pas être récupérés.")
        return {
            "mode": "file", "root": None, "name": inp.stem,
            "hb_input": inp, "hb_title_args": [], "probe_path": inp,
            "playlist": None, "duration": None, "filesize": None,
        }

    require_handbrake()
    header("Source Blu-ray — sélection du titre")
    info(f"Racine du disque : {root}")

    bdmv       = _ci_child(root, "BDMV")
    pl_dir     = _ci_child(bdmv, "PLAYLIST")
    stream_dir = _ci_child(bdmv, "STREAM")

    titles, main_idx = hb_scan_titles(root)
    if not titles:
        err("HandBrakeCLI n'a trouvé aucun titre sur ce disque.")
        sys.exit(1)
    by_idx = {t.get("Index"): t for t in titles}

    mpls_cache = {}
    def clips_of(t):
        pl = t.get("Playlist", -1)
        if pl is None or pl < 0 or not pl_dir:
            return []
        if pl not in mpls_cache:
            f = _ci_child(pl_dir, f"{pl:05d}.mpls")
            mpls_cache[pl] = parse_mpls_clips(f) if f else []
        return mpls_cache[pl]

    use_main = False
    if forced_title is not None:
        if forced_title not in by_idx:
            err(f"Titre {forced_title} inexistant. Titres disponibles :")
            for t in titles:
                print(f"    {t.get('Index'):>3}  {format_time(_title_duration(t))}"
                      f"  playlist {t.get('Playlist', -1):05d}")
            sys.exit(1)
        chosen = forced_title
    elif inp.is_file():
        clip = inp.stem.upper()
        main_t = by_idx.get(main_idx)
        if main_t and clip in clips_of(main_t):
            use_main = True
            chosen = main_idx
        else:
            cands = [t for t in titles if clip in clips_of(t)]
            if cands:
                best = max(cands, key=_title_duration)
                chosen = best.get("Index")
                warn(f"Le clip {clip} n'appartient pas au titre principal — "
                     f"utilisation du titre {chosen} qui le contient.")
            else:
                warn(f"Aucune playlist ne référence {clip} — utilisation du titre principal.")
                use_main = True
                chosen = main_idx
    else:
        use_main = True
        chosen = main_idx

    if chosen not in by_idx:
        err("HandBrake n'a pas identifié de titre principal. Utilisez --title N.")
        sys.exit(1)

    title    = by_idx[chosen]
    playlist = title.get("Playlist", -1)
    clips    = clips_of(title)
    duration = _title_duration(title)

    # Fichiers m2ts du titre
    clip_files = []
    if stream_dir:
        for name in clips:
            for ext in (".m2ts", ".M2TS", ".mts", ".MTS"):
                f = stream_dir / (name + ext)
                if f.exists():
                    clip_files.append(f)
                    break
        if not clip_files:
            warn("Clips de la playlist introuvables — analyse du plus gros m2ts du disque.")
            clip_files = sorted(stream_dir.glob("*.[mM]2[tT][sS]"),
                                key=lambda f: f.stat().st_size, reverse=True)[:1]
    if not clip_files:
        err("Aucun fichier m2ts trouvé dans BDMV/STREAM.")
        sys.exit(1)

    probe = max(clip_files, key=lambda f: f.stat().st_size)

    label("Titre HandBrake", f"{chosen}" + ("  (--main-feature)" if use_main else "  (-t)"))
    label("Playlist", f"{playlist:05d}.mpls" if playlist is not None and playlist >= 0 else "?")
    label("Durée du titre", format_time(duration))
    label("Clips", ", ".join(clips) if clips else "?")
    label("Chapitres", str(len(title.get("ChapterList", []) or [])))
    for i, a in enumerate(title.get("AudioList", []) or [], 1):
        label(f"Audio {i}", a.get("Description") or a.get("Language", "?"))
    for i, s in enumerate(title.get("SubtitleList", []) or [], 1):
        label(f"Sous-titre {i}", f"{s.get('Language', '?')}  ({s.get('SourceName', '?')})")
    if len(clip_files) > 1:
        info(f"Playlist multi-clips : bitrates mesurés sur le plus gros clip ({probe.name}).")

    return {
        "mode": "disc", "root": root, "name": root.name,
        "hb_input": root,
        "hb_title_args": ["--main-feature"] if use_main else ["-t", str(chosen)],
        "probe_path": probe,
        "playlist": playlist,
        "duration": duration,
        "filesize": sum(f.stat().st_size for f in clip_files),
    }

# ─────────────────────────── Diagnostic packets ──────────────────────

def diagnose_packets(path: Path):
    """
    Affiche les 10 premiers packets de chaque stream pour comprendre
    le format exact retourné par ffprobe sur ce fichier.
    """
    header(f"Diagnostic packets ffprobe — {path.name}")
    for spec in ("v:0", "a:0"):
        print(f"\n  {BOLD}Stream {spec} — 10 premiers packets{RESET}")
        cmd = [
            "ffprobe", "-v", "quiet",
            "-select_streams", spec,
            "-show_packets",
            "-show_entries", "packet=pts,pts_time,dts,dts_time,size,duration_time,stream_index",
            "-print_format", "csv=p=0",
            str(path)
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True)
            for i, line in enumerate(proc.stdout):
                if i >= 10:
                    break
                print(f"    {DIM}{line.rstrip()}{RESET}")
            proc.stdout.close()
            proc.wait()
        except Exception as e:
            print(f"    {RED}Erreur : {e}{RESET}")
    print()
    sys.exit(0)


# ─────────────────────────── Probe métadonnées ────────────────────────

def probe_metadata(path: Path) -> dict:
    """Lecture rapide des métadonnées via ffprobe -show_format -show_streams."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err(f"ffprobe a échoué sur {path.name}")
        sys.exit(1)
    return json.loads(result.stdout)

# ─────────────────────────── Mesure bitrate par packets ───────────────

MEASURE_WINDOWS    = 3
MEASURE_WINDOW_SEC = 30   # secondes par fenêtre


def measure_stream_bitrate(path: Path, stream_spec: str,
                           duration: float, label_str: str) -> int:
    """
    Mesure le bitrate moyen d'un stream sur MEASURE_WINDOWS fenêtres.
    Les timestamps M2TS ne démarrent pas à 0 (offset PCR) : on travaille
    en temps relatif au premier pts_time.
    """
    print(f"    {DIM}lecture séquentielle des packets ({stream_spec})…{RESET}", flush=True)

    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", stream_spec,
        "-show_packets",
        "-show_entries", "packet=stream_index,pts,pts_time,dts,dts_time,duration_time,size",
        "-print_format", "csv=p=0",
        str(path)
    ]

    packets = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1 << 20)
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                pts_time = float(parts[2])
                size     = int(parts[6])
                packets.append((pts_time, size))
            except ValueError:
                continue
        proc.stdout.close()
        proc.wait()
    except Exception as e:
        warn(f"Erreur lecture packets {stream_spec} : {e}")
        return 0

    if not packets:
        warn(f"Aucun packet lisible pour {stream_spec}.")
        return 0

    t_offset = packets[0][0]
    t_total  = packets[-1][0] - t_offset

    if t_total <= 0:
        warn(f"Durée des packets nulle pour {stream_spec}.")
        return 0

    margin  = t_total * 0.05
    usable  = t_total - 2 * margin - MEASURE_WINDOW_SEC
    n       = MEASURE_WINDOWS

    if usable <= 0:
        rel_offsets = [max(0.0, (t_total - MEASURE_WINDOW_SEC) / 2)]
    else:
        rel_offsets = [margin + usable * i / (n - 1) for i in range(n)]

    windows = []
    for rel_off in rel_offsets:
        win = min(MEASURE_WINDOW_SEC, t_total - rel_off)
        if win >= 2:
            t0_abs = t_offset + rel_off
            t1_abs = t0_abs + win
            windows.append((rel_off, t0_abs, t1_abs, win))

    if not windows:
        return 0

    window_bytes = [0] * len(windows)
    t_min_abs = windows[0][1]
    t_max_abs = windows[-1][2]

    for pts_abs, size in packets:
        if pts_abs < t_min_abs or pts_abs >= t_max_abs:
            continue
        for wi, (_, t0, t1, _win) in enumerate(windows):
            if t0 <= pts_abs < t1:
                window_bytes[wi] += size
                break

    bitrates = []
    for i, ((rel_off, _t0, _t1, win_dur), nbytes) in enumerate(zip(windows, window_bytes), 1):
        if nbytes > 0 and win_dur > 0:
            br = int(nbytes * 8 / win_dur)
            bitrates.append(br)
            print(f"    {DIM}fenêtre {i}/{len(windows)}  @{format_time(t_offset + rel_off)}"
                  f"  →  {br / 1_000_000:.3f} Mb/s{RESET}")
        else:
            print(f"    {DIM}fenêtre {i}/{len(windows)}  @{format_time(t_offset + rel_off)}"
                  f"  →  (aucun paquet){RESET}")

    if not bitrates:
        return 0

    avg = int(sum(bitrates) / len(bitrates))
    std = int((sum((b - avg) ** 2 for b in bitrates) / len(bitrates)) ** 0.5)
    print(f"    {BOLD}{label_str} : {avg / 1_000_000:.3f} Mb/s"
          f"{RESET}  {DIM}(écart-type ±{std / 1_000_000:.3f} Mb/s){RESET}")
    return avg


def analyse_bitrates(path: Path, meta: dict) -> dict:
    """Phase 1 : mesure précise des bitrates vidéo et audio."""
    fmt           = meta["format"]
    streams       = meta["streams"]
    duration      = float(fmt.get("duration", 0))
    filesize      = int(fmt.get("size", 0))
    total_br      = int(fmt.get("bit_rate", 0))

    video         = next((s for s in streams if s["codec_type"] == "video"), None)
    audio_streams = [s for s in streams if s["codec_type"] == "audio"]

    if not video:
        err("Aucun flux vidéo détecté.")
        sys.exit(1)

    width   = int(video.get("width", 1920))
    height  = int(video.get("height", 1080))
    fps_raw = video.get("r_frame_rate", "25/1").split("/")
    fps     = round(int(fps_raw[0]) / int(fps_raw[1]), 3) if len(fps_raw) == 2 else 25.0
    vcodec  = video.get("codec_name", "unknown")

    video_br_meta = int(video.get("bit_rate", 0))
    audio_br_meta = [int(a.get("bit_rate", 0)) for a in audio_streams]
    audio_codecs  = [a.get("codec_name", "?") for a in audio_streams]

    need_measure_video = video_br_meta == 0
    need_measure_audio = [br == 0 for br in audio_br_meta]
    needs_any_measure  = need_measure_video or any(need_measure_audio)

    if not needs_any_measure:
        audio_br_list = audio_br_meta
        video_br      = video_br_meta
    else:
        header("Mesure des bitrates par analyse de packets")
        info(f"Méthode : {MEASURE_WINDOWS} fenêtres × {MEASURE_WINDOW_SEC} s "
             f"réparties sur la durée\n")

        if need_measure_video:
            print(f"  {BOLD}Flux vidéo (v:0  —  {vcodec}){RESET}")
            video_br = measure_stream_bitrate(path, "v:0", duration, "Bitrate vidéo moyen")
            print()
        else:
            video_br = video_br_meta

        audio_br_list = list(audio_br_meta)
        for idx, (codec, declared_br, missing) in enumerate(
                zip(audio_codecs, audio_br_meta, need_measure_audio)):
            if missing:
                print(f"  {BOLD}Piste audio {idx} (a:{idx}  —  {codec}){RESET}")
                br = measure_stream_bitrate(path, f"a:{idx}", duration,
                                            f"Bitrate a:{idx} moyen")
                audio_br_list[idx] = br
                print()

    audio_br_total = sum(audio_br_list)

    measured_total = video_br + audio_br_total
    if total_br > 0 and measured_total > 0:
        ratio = measured_total / total_br
        if ratio > 1.10:
            warn(f"Somme mesurée ({measured_total/1_000_000:.2f} Mb/s) > "
                 f"total conteneur ({total_br/1_000_000:.2f} Mb/s) — recalibration proportionnelle.")
            video_br       = int(total_br * video_br / measured_total)
            audio_br_list  = [int(total_br * a / measured_total) for a in audio_br_list]
            audio_br_total = sum(audio_br_list)
        elif ratio < 0.80:
            warn(f"Somme mesurée ({measured_total/1_000_000:.2f} Mb/s) anormalement basse "
                 f"vs total conteneur ({total_br/1_000_000:.2f} Mb/s) — "
                 f"les packets overhead/PES non comptés sont importants.")

    return {
        "duration":      duration,
        "filesize":      filesize,
        "total_br":      total_br,
        "video_br":      video_br,
        "audio_br":      audio_br_total,
        "audio_br_list": audio_br_list,
        "width":         width,
        "height":        height,
        "fps":           fps,
        "vcodec":        vcodec,
        "audio_codecs":  audio_codecs,
        "nb_audio":      len(audio_streams),
    }

# ─────────────────────────── Calcul CRF ───────────────────────────────

REF_BITRATES = {2160: 40_000_000, 1080: 10_000_000, 720: 5_000_000, 576: 2_000_000}
REF_CRF      = 18
PRESET_OFFSETS = {
    "veryslow": -1, "slower": -0.5, "slow": 0,
    "medium": 1, "fast": 2, "faster": 3,
    "veryfast": 4, "superfast": 5, "ultrafast": 6
}

def get_ref_bitrate(height: int) -> int:
    for h in sorted(REF_BITRATES.keys(), reverse=True):
        if height >= h:
            return REF_BITRATES[h]
    return REF_BITRATES[576]

def compute_crf(video_br: int, height: int, preset: str, target_pct: float) -> float:
    """Estimation analytique du CRF via la relation bitrate ≈ k × 2^(-CRF/6)."""
    target_br = video_br * (target_pct / 100.0)
    ref_br    = get_ref_bitrate(height)
    k         = ref_br / (2 ** (-REF_CRF / 6))
    raw_crf   = -6 * math.log2(target_br / k)
    offset    = PRESET_OFFSETS.get(preset, 1)
    return round(max(0.0, min(51.0, raw_crf + offset)), 1)

def quality_label(crf: float) -> str:
    if crf <= 16: return c(GREEN,  "Quasi-transparente (lossless-like)")
    if crf <= 21: return c(GREEN,  "Haute qualité")
    if crf <= 26: return c(YELLOW, "Bonne qualité")
    if crf <= 33: return c(YELLOW, "Qualité acceptable")
    return c(RED, "Qualité dégradée — vérifiez le résultat")

# ─────────────────────────── Barre de progression ─────────────────────

def format_time(seconds: float) -> str:
    return str(timedelta(seconds=int(max(0, seconds))))

def progress_bar(pct: float, width: int = 30) -> str:
    filled = int(width * pct / 100)
    bar    = "█" * filled + "░" * (width - filled)
    color  = GREEN if pct > 80 else YELLOW if pct > 40 else CYAN
    return f"{color}{bar}{RESET}"

def run_ffmpeg_with_progress(cmd: list, duration: float, label_prefix: str = "Encodage") -> bool:
    cmd_p   = cmd + ["-progress", "pipe:1", "-nostats"]
    process = subprocess.Popen(cmd_p, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1)
    current_time  = 0.0
    current_fps   = 0.0
    current_speed = 0.0
    current_size  = 0

    print()
    try:
        for line in process.stdout:
            line = line.strip()
            if line.startswith("out_time_ms="):
                val = line.split("=")[1].strip()
                if val.lstrip("-").isdigit():
                    current_time = max(0, int(val)) / 1_000_000
            elif line.startswith("fps="):
                try: current_fps = float(line.split("=")[1].strip())
                except ValueError: pass
            elif line.startswith("speed="):
                try: current_speed = float(line.split("=")[1].strip().replace("x", ""))
                except ValueError: pass
            elif line.startswith("total_size="):
                try: current_size = int(line.split("=")[1].strip())
                except ValueError: pass

            if duration > 0:
                pct     = min(100.0, (current_time / duration) * 100)
                eta     = ((duration - current_time) / current_speed) if current_speed > 0.01 else 0
                size_mb = current_size / (1024 * 1024)
                print(
                    f"\r  {label_prefix}  {progress_bar(pct)} "
                    f"{c(BOLD+WHITE, f'{pct:5.1f}%')}  "
                    f"{DIM}{format_time(current_time)}/{format_time(duration)}  "
                    f"{current_fps:.0f} fps  {current_speed:.2f}x  "
                    f"ETA {format_time(eta)}  {size_mb:.1f} Mo{RESET}",
                    end="", flush=True
                )
    except KeyboardInterrupt:
        process.terminate()
        print(f"\n\n  {YELLOW}Interrompu par l'utilisateur.{RESET}\n")
        return False

    process.wait()
    print()
    if process.returncode != 0:
        stderr_out = process.stderr.read() if process.stderr else ""
        err(f"FFmpeg s'est terminé avec le code {process.returncode}")
        if stderr_out:
            print(f"\n{DIM}{stderr_out[-1000:]}{RESET}")
        return False
    return True

# ─────────────────────────── Commandes d'encodage ─────────────────────

X264_OPTS = ("level=4.1:deblock=-3,-3:psy-rd=1.2,0.00:no-fast-pskip:no-dct-decimate"
             ":sar=1.1:no-mbtree:aq-mode=3:aq-strength=0.9:colormatrix=bt709")


def default_output(source: dict) -> Path:
    if source["mode"] == "disc":
        return source["root"].parent / f"{source['name']}_x264.mkv"
    return source["hb_input"].with_name(source["name"] + "_x264.mkv")


def build_handbrake_cmd(source: dict, output: Path, crf: float,
                        calibration: bool = False) -> list:
    """
    Commande HandBrakeCLI sous forme de liste (sûre avec les espaces dans les chemins).

    Mode disque : -i <racine du disque> + --main-feature (ou -t N) → HandBrake lit
    la playlist MPLS : chapitres, langues et descriptions des pistes sont conservés.

    calibration=True : pas d'audio ni de sous-titres ni de chapitres, pour que la
    taille du fichier test corresponde au seul flux vidéo.
    """
    cmd = ["HandBrakeCLI",
           "-i", str(source["hb_input"]),
           *source["hb_title_args"],
           "-o", str(output)]

    if calibration:
        cmd += ["-a", "none"]
    else:
        cmd += [
            "--markers",                       # chapitres de la playlist
            "--all-audio",
            "--aencoder", "copy",
            "--audio-copy-mask", "aac,ac3,dts,dtshd,truehd,eac3,mp2,mp3,opus,vorbis,flac,alac",
            "--audio-fallback", "flac16",
            "--all-subtitles",
        ]

    cmd += [
        "-e", "x264",
        "-q", f"{crf:.1f}",
        "-x", X264_OPTS,
        "-l", "--crop-mode-auto",
        "--encoder-tune", "film",
        "--encoder-profile", "high",
        "--encoder-preset", "veryslow",
        "--encoder-level", "4.1",
        "--cfr",
    ]
    return cmd


def build_ffmpeg_cmd(source: dict, output_path: Path, crf: float, preset: str) -> list:
    """FFmpeg n'accepte que des CRF entiers — on arrondit.
    En mode disque, lecture de la playlist via le protocole bluray: (libbluray)."""
    if source["mode"] == "disc" and source["playlist"] is not None and source["playlist"] >= 0:
        inp = ["-playlist", str(source["playlist"]), "-i", f"bluray:{source['root']}"]
    else:
        inp = ["-i", str(source["hb_input"])]
    return [
        "ffmpeg", "-y", *inp,
        "-map", "0", "-map", "-0:d?",
        "-c:v", "libx264",
        "-crf", str(round(crf)),
        "-preset", preset,
        "-c:a", "copy",
        "-c:s", "copy",
        str(output_path)
    ]


def print_handbrake_cmd(source: dict, output: Path, crf: float):
    cmd = build_handbrake_cmd(source, output, crf)
    print(f"\n  {DIM}Commande HandBrakeCLI :{RESET}")
    print(f"  {CYAN}{shlex.join(cmd)}{RESET}\n")

# ─────────────────────────── Calibration CRF ──────────────────────────

TOLERANCE_PCT    = 3.0
MAX_ITERATIONS   = 7
SEGMENT_SECS     = 30
DEFAULT_SEGMENTS = 5


def pick_segment_offsets(duration: float, n: int, seg_len: float) -> list:
    margin = duration * 0.05
    usable = duration - 2 * margin - seg_len
    if usable <= 0:
        return [max(0.0, (duration - seg_len) / 2)]
    if n == 1:
        return [margin + usable / 2]
    return [margin + usable * i / (n - 1) for i in range(n)]


def encode_one_segment_hb(source: dict, offset: float, seg_len: float,
                          crf: float, seg_idx: int, total_segs: int):
    """
    Encode un segment du même titre que l'encodage final, avec les mêmes
    paramètres vidéo, sans audio ni sous-titres. Les offsets sont relatifs
    au début du titre (playlist), pas au clip m2ts.
    """
    with tempfile.NamedTemporaryFile(suffix=".mkv", delete=False) as f:
        tmp_path = Path(f.name)

    cmd = build_handbrake_cmd(source, tmp_path, crf, calibration=True) + [
        "--start-at", f"duration:{offset:.2f}",
        "--stop-at",  f"duration:{seg_len:.2f}",
    ]

    prefix = f"  Seg {seg_idx}/{total_segs} @{format_time(offset)}  "
    print(f"\n{prefix}{DIM}(HandBrakeCLI CRF {crf:.1f}){RESET}")

    process = None
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1)
        last_pct = ""
        for line in process.stdout:
            line = line.rstrip()
            if "Encoding:" in line or "Muxing:" in line:
                m = re.search(r"([0-9.]+) %", line)
                pct = m.group(1) if m else "?"
                if pct != last_pct:
                    print(f"\r  {DIM}{prefix}{pct}%{RESET}   ", end="", flush=True)
                    last_pct = pct
        process.wait()
        print()
        success = process.returncode == 0
    except KeyboardInterrupt:
        if process:
            process.terminate()
        if tmp_path.exists(): tmp_path.unlink()
        print(f"\n  {YELLOW}Interrompu.{RESET}")
        return None
    except Exception as e:
        warn(f"Erreur HandBrakeCLI segment {seg_idx} : {e}")
        if tmp_path.exists(): tmp_path.unlink()
        return None

    result = None
    if success and tmp_path.exists() and tmp_path.stat().st_size > 0:
        meta    = probe_metadata(tmp_path)
        vstream = next((s for s in meta["streams"] if s["codec_type"] == "video"), None)
        real_br = int(vstream.get("bit_rate", 0)) if vstream else 0
        if real_br == 0:
            real_br = int(tmp_path.stat().st_size * 8 / seg_len)
        result = real_br

    if tmp_path.exists():
        tmp_path.unlink()
    return result


def measure_crf(source: dict, crf: float, src: dict, n_segments: int, iteration: int):
    """Encode n_segments segments avec HandBrakeCLI et retourne le bitrate vidéo moyen."""
    seg_len = SEGMENT_SECS
    offsets = pick_segment_offsets(src["duration"], n_segments, seg_len)
    actual  = len(offsets)

    print(f"\n  {BOLD}{CYAN}── Itération {iteration}  |  CRF {crf:.1f}  |  "
          f"{actual} segments × {seg_len} s  ({actual * seg_len} s){RESET}")
    print(f"  {DIM}Encodeur : HandBrakeCLI avec paramètres finaux{RESET}")

    bitrates = []
    for i, offset in enumerate(offsets, 1):
        br = encode_one_segment_hb(source, offset, seg_len, crf, i, actual)
        if br is None or br == 0:
            warn(f"Segment {i} — bitrate non mesurable, ignoré.")
            continue
        bitrates.append(br)
        br_pct = (br / src["video_br"] * 100) if src["video_br"] else 0
        print(f"    {DIM}Seg {i}/{actual}  @{format_time(offset)}{RESET}"
              f"  →  {br / 1_000_000:.3f} Mb/s  ({br_pct:.1f}%)")

    if not bitrates:
        return None

    avg_br = sum(bitrates) / len(bitrates)
    std_br = (sum((b - avg_br) ** 2 for b in bitrates) / len(bitrates)) ** 0.5
    print(
        f"\n    {BOLD}Moyenne : {avg_br / 1_000_000:.3f} Mb/s{RESET}"
        f"  {DIM}écart-type ±{std_br / 1_000_000:.3f} Mb/s"
        f"  min {min(bitrates) / 1_000_000:.3f}  max {max(bitrates) / 1_000_000:.3f}"
        f"  ({len(bitrates)}/{actual} seg.){RESET}"
    )
    return avg_br


def calibrate_crf(source: dict, initial_crf: float, src: dict,
                  target_pct: float, n_segments: int) -> float:
    """
    Recherche binaire en deux phases :
    Phase A — entiers (pas 1), Phase B — décimales (pas 0.1) autour du meilleur entier.
    """
    header("Calibration CRF — recherche binaire multi-segments")

    target_br = src["video_br"] * (target_pct / 100.0)
    info(f"Bitrate vidéo source  : {src['video_br'] / 1_000_000:.3f} Mb/s")
    info(f"Cible ({target_pct:.0f}%)           : {target_br / 1_000_000:.3f} Mb/s")
    info(f"CRF de départ         : {initial_crf}  (estimation analytique)")
    info(f"Encodeur calibration  : HandBrakeCLI (paramètres finaux identiques)")
    info(f"Segments par CRF      : {n_segments} × {SEGMENT_SECS} s"
         f"  ({n_segments * SEGMENT_SECS} s d'encodage test par itération)")
    info(f"Tolérance             : ±{TOLERANCE_PCT:.1f}%  |  max {MAX_ITERATIONS} itérations par phase\n")

    def _binary_search(crf_start: float, lo: float, hi: float,
                       step: float, phase_label: str):
        history_local = []
        crf      = crf_start
        best_crf = crf_start
        best_gap = float("inf")

        for _ in range(1, MAX_ITERATIONS + 1):
            crf_r = round(crf, 1)
            avg_br = measure_crf(source, crf_r, src, n_segments, len(history_local) + 1)
            if avg_br is None:
                warn(f"Tous les segments CRF {crf_r} ont échoué — arrêt.")
                break

            real_pct  = (avg_br / src["video_br"] * 100) if src["video_br"] else 0
            deviation = real_pct - target_pct
            gap       = abs(deviation)
            history_local.append((crf_r, avg_br))

            arrow = (f"{RED}▲ +{deviation:.2f}%{RESET}  → CRF ↑"
                     if deviation > 0 else
                     f"{CYAN}▼ {deviation:.2f}%{RESET}  → CRF ↓")
            print(f"\n  {BOLD}{phase_label}  CRF {crf_r:.1f}{RESET}  :  "
                  f"{avg_br / 1_000_000:.3f} Mb/s  "
                  f"({real_pct:.2f}%)  écart {arrow}")

            if gap < best_gap:
                best_gap = gap
                best_crf = crf_r

            if gap <= TOLERANCE_PCT:
                ok(f"Convergé — CRF {crf_r:.1f} dans la tolérance (écart {gap:.2f}%)")
                break

            if deviation > 0:
                lo = crf + step
            else:
                hi = crf - step

            lo = round(lo, 1)
            hi = round(hi, 1)

            if lo > hi:
                info("Plage épuisée.")
                break

            tested = {h[0] for h in history_local}
            next_crf = round(round((lo + hi) / 2 / step) * step, 1)
            if next_crf in tested:
                found = False
                for d in range(1, 20):
                    for candidate in (round(next_crf + d * step, 1),
                                      round(next_crf - d * step, 1)):
                        if lo <= candidate <= hi and candidate not in tested:
                            next_crf = candidate
                            found = True
                            break
                    if found:
                        break
                else:
                    info("Tous les CRF de la plage testés.")
                    break

            crf = next_crf
        else:
            warn(f"Max itérations ({MAX_ITERATIONS}) atteint pour {phase_label}.")

        return best_crf, history_local, best_gap

    print(f"\n  {BOLD}{CYAN}━━ Phase A — recherche entière (pas 1){RESET}\n")
    best_int, hist_a, gap_a = _binary_search(round(initial_crf), 0.0, 51.0, 1.0, "Phase A")

    print(f"\n  {BOLD}{CYAN}━━ Phase B — affinage décimal (pas 0.1) autour de CRF {best_int:.0f}{RESET}\n")
    lo_b = max(0.0, best_int - 1.0)
    hi_b = min(51.0, best_int + 1.0)
    best_dec, hist_b, gap_b = _binary_search(best_int, lo_b, hi_b, 0.1, "Phase B")

    all_history = hist_a + hist_b
    hist_b_crfs = {h[0] for h in hist_b}
    print()
    print(f"  {DIM}{'CRF':>6}  {'Bitrate moy.':>15}  {'% source':>9}  {'Écart cible':>12}  Phase{RESET}")
    print(f"  {DIM}{'─'*58}{RESET}")
    for h_crf, h_br in sorted(all_history, key=lambda x: x[0]):
        h_pct    = (h_br / src["video_br"] * 100) if src["video_br"] else 0
        h_dev    = h_pct - target_pct
        retained = h_crf == best_dec
        marker   = f"  {GREEN}← retenu{RESET}" if retained else ""
        phase    = "B" if h_crf in hist_b_crfs else "A"
        style    = BOLD if retained else DIM
        print(
            f"  {style}{h_crf:>6.1f}{RESET}  "
            f"{h_br / 1_000_000:>13.3f} Mb/s  "
            f"{h_pct:>8.2f}%  "
            f"{h_dev:>+11.2f}%  "
            f"{DIM}{phase}{RESET}"
            f"{marker}"
        )

    print()
    if gap_b > TOLERANCE_PCT:
        warn(f"Tolérance ±{TOLERANCE_PCT:.1f}% non atteinte.")
        info("Le contenu M2TS est fortement VBR — résultat indicatif.")
        info(f"CRF {best_dec:.1f} est le plus proche (écart {gap_b:.2f}%).")

    return best_dec

# ─────────────────────────── Encodage ─────────────────────────────────

def run_encode(tool: str, cmd: list, duration: float) -> bool:
    """Lance ffmpeg ou HandBrakeCLI et affiche la progression."""
    if tool == "ffmpeg":
        success = run_ffmpeg_with_progress(cmd, duration, "Encodage  ")
    else:
        print()
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1)
        try:
            for line in process.stdout:
                line = line.rstrip()
                if "Encoding:" in line or "Muxing:" in line:
                    print(f"\r  {CYAN}{line[:78]:<78}{RESET}", end="", flush=True)
                elif line.strip():
                    print(f"  {DIM}{line}{RESET}")
        except KeyboardInterrupt:
            process.terminate()
            print(f"\n\n  {YELLOW}Interrompu par l'utilisateur.{RESET}\n")
            return False
        process.wait()
        print()
        success = process.returncode == 0
        if not success:
            err(f"HandBrakeCLI s'est terminé avec le code {process.returncode}")

    if success:
        ok("Encodage terminé.")
    return success


def _launch_encode(args, source: dict, output: Path, ffmpeg_cmd: list,
                   crf: float, duration: float):
    """Lance directement l'outil choisi sans demander de confirmation."""
    tool = args.encode
    header(f"Encodage en cours ({tool})")
    if tool == "ffmpeg":
        info(f"CRF FFmpeg : {round(crf)}  (arrondi depuis {crf:.1f})")
        if source["mode"] == "disc":
            warn("FFmpeg (protocole bluray:) ne récupère ni les chapitres ni les langues "
                 "des pistes — préférez --encode handbrake.")
        cmd = ffmpeg_cmd
    else:
        info(f"CRF HandBrake : {crf:.1f}")
        cmd = build_handbrake_cmd(source, output, crf)
    cmd_display = shlex.join(cmd)
    print(f"  {DIM}{cmd_display[:120]}{'...' if len(cmd_display) > 120 else ''}{RESET}\n")

    start = time.time()
    run_encode(tool, cmd, duration)
    ok(f"Terminé en {format_time(time.time() - start)}")

# ─────────────────────────── Main ─────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Mesure les bitrates d'une source Blu-ray/M2TS et calcule le CRF optimal x264.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  %(prog)s /media/FILM                       (dossier contenant BDMV → --main-feature)
  %(prog)s /media/FILM/BDMV/STREAM/00800.m2ts
  %(prog)s /media/FILM --title 3 --target 60
  %(prog)s /media/FILM --calibrate --segments 8 --encode handbrake
  %(prog)s film.m2ts --no-disc
        """
    )
    parser.add_argument("input",            type=Path,
                        help="Dossier Blu-ray (contenant BDMV), dossier BDMV, ou fichier M2TS")
    parser.add_argument("--target", "-t",   type=float, default=60.0, metavar="PCT",
                        help="Pourcentage cible du bitrate vidéo (défaut : 60)")
    parser.add_argument("--preset", "-p",   type=str,   default="slow",
                        choices=list(PRESET_OFFSETS.keys()),
                        help="Preset x264 pour FFmpeg (défaut : slow)")
    parser.add_argument("--title", "-T",    type=int,   default=None, metavar="N",
                        help="Titre HandBrake à encoder (défaut : --main-feature)")
    parser.add_argument("--no-disc",        action="store_true",
                        help="Traiter le M2TS seul, sans lire la playlist du disque")
    parser.add_argument("--output", "-o",   type=Path,  default=None,
                        help="Fichier de sortie (défaut : <nom>_x264.mkv)")
    parser.add_argument("--calibrate",      action="store_true",
                        help="Calibration par recherche binaire multi-segments")
    parser.add_argument("--segments", "-s", type=int, default=DEFAULT_SEGMENTS, metavar="N",
                        help=f"Segments de calibration par CRF candidat (défaut : {DEFAULT_SEGMENTS})")
    parser.add_argument("--diagnose",       action="store_true",
                        help="Affiche les 10 premiers packets de v:0 et a:0 (debug)")
    parser.add_argument("--encode",         type=str, default=None,
                        choices=["ffmpeg", "handbrake"],
                        metavar="{ffmpeg,handbrake}",
                        help="Lance l'encodage après calcul/calibration")

    args = parser.parse_args()
    check_dependencies()

    if not args.input.exists():
        err(f"Source introuvable : {args.input}")
        sys.exit(1)
    if not 10 <= args.target <= 95:
        err("--target doit être entre 10 et 95.")
        sys.exit(1)

    source = resolve_source(args.input, args.title, args.no_disc)

    if args.diagnose:
        diagnose_packets(source["probe_path"])
    if args.calibrate or args.encode == "handbrake":
        require_handbrake()

    output = args.output or default_output(source)

    # ══════════════════════════════════════════════════════════════════
    # PHASE 1 — Analyse et mesure précise des bitrates
    # ══════════════════════════════════════════════════════════════════
    header(f"Phase 1 — Analyse de {source['probe_path'].name}")
    info("Lecture des métadonnées via ffprobe...")

    meta = probe_metadata(source["probe_path"])
    src  = analyse_bitrates(source["probe_path"], meta)

    # En mode disque : durée et taille = celles du titre (playlist), pas du clip
    if source["mode"] == "disc":
        if source["duration"]:
            src["duration"] = source["duration"]
        if source["filesize"]:
            src["filesize"] = source["filesize"]

    header("Résumé des bitrates mesurés")
    label("Résolution",          f"{src['width']}x{src['height']}  @  {src['fps']:.3f} fps")
    label("Codec source",        src["vcodec"])
    label("Durée",               format_time(src["duration"]))
    label("Taille source",       f"{src['filesize'] / (1024**3):.2f} Go")
    label("Bitrate total (conteneur)", f"{src['total_br'] / 1_000_000:.3f} Mb/s")
    label("Bitrate vidéo",       f"{src['video_br'] / 1_000_000:.3f} Mb/s")

    for idx, (codec, br) in enumerate(zip(src["audio_codecs"], src["audio_br_list"])):
        label(f"Bitrate audio piste {idx} ({codec})", f"{br / 1_000_000:.3f} Mb/s")

    label("Bitrate audio total", f"{src['audio_br'] / 1_000_000:.3f} Mb/s")

    measured_sum = src["video_br"] + src["audio_br"]
    overhead     = src["total_br"] - measured_sum
    label("Overhead conteneur",  f"{overhead / 1_000_000:.3f} Mb/s"
                                 f"  ({overhead / src['total_br'] * 100:.1f}%)"
                                 if src["total_br"] > 0 else "N/A")

    if src["video_br"] == 0:
        err("Impossible de déterminer le bitrate vidéo. Vérifiez le fichier.")
        sys.exit(1)

    # ══════════════════════════════════════════════════════════════════
    # PHASE 2 — Estimation analytique du CRF
    # ══════════════════════════════════════════════════════════════════
    header("Phase 2 — Estimation analytique du CRF")

    crf         = compute_crf(src["video_br"], src["height"], args.preset, args.target)
    target_br   = src["video_br"] * (args.target / 100)
    target_size = (target_br + src["audio_br"]) * src["duration"] / (8 * 1024**3)

    label("Cible bitrate vidéo", f"{args.target:.0f}% de {src['video_br']/1_000_000:.3f} Mb/s"
                                 f"  →  {target_br/1_000_000:.3f} Mb/s")
    label("Preset x264",         args.preset)
    label("CRF estimé",          c(BOLD + CYAN, f"{crf:.1f}"))
    label("Qualité estimée",     quality_label(crf))
    label("Taille estimée",      f"{target_size:.2f} Go")

    ffmpeg_cmd = build_ffmpeg_cmd(source, output, crf, args.preset)
    print(f"\n  {DIM}Commande FFmpeg estimée :{RESET}")
    print(f"  {CYAN}{shlex.join(ffmpeg_cmd)}{RESET}")
    print_handbrake_cmd(source, output, crf)

    if not args.calibrate:
        if args.encode:
            _launch_encode(args, source, output, ffmpeg_cmd, crf, src["duration"])
        return

    # ══════════════════════════════════════════════════════════════════
    # PHASE 3 — Calibration CRF par encodages tests multi-segments
    # ══════════════════════════════════════════════════════════════════
    crf = calibrate_crf(source, crf, src, args.target, args.segments)
    ffmpeg_cmd = build_ffmpeg_cmd(source, output, crf, args.preset)

    header(f"Phase 3 — CRF final recommandé : {crf}")
    label("CRF retenu",      c(BOLD + GREEN, f"{crf:.1f}"))
    label("Qualité estimée", quality_label(crf))
    print(f"\n  {DIM}Commande FFmpeg à lancer :{RESET}")
    print(f"  {CYAN}{shlex.join(ffmpeg_cmd)}{RESET}")
    print_handbrake_cmd(source, output, crf)
    if args.encode:
        _launch_encode(args, source, output, ffmpeg_cmd, crf, src["duration"])
    else:
        ok("Calibration terminée. Copiez l'une des commandes ci-dessus pour lancer l'encodage.")


if __name__ == "__main__":
    main()


