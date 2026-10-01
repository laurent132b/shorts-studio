"""vers_hyperframes.py -- preparer le dossier d'habillage HyperFrames d'une video montee.

    python tools/vers_hyperframes.py <slug>                    # le master du montage
    python tools/vers_hyperframes.py <slug> --master <fichier.mp4>
    python tools/vers_hyperframes.py <slug> --engine whisper   # sans cle ElevenLabs

Sortie : 0 dossier pret - 2 refus (pas de master, transcription impossible).

Ce qu'il ecrit dans `videos/<slug>/2_habillage/` :

    public/input-video.mp4   le master RE-ENCODE avec une image cle par seconde (ignore par git)
    metadata.json            largeur, hauteur, duree, images par seconde (format ffprobe)
    transcript.json          les mots du MASTER, tableau plat [{text, start, end}, ...]

Le re-encodage n'est pas un luxe : le moteur de rendu se positionne image par image, et sur
une video dont les images cles sont espacees (cas normal d'un master H.264) il FIGE l'image
sous les overlays. Une image cle par seconde rend chaque instant atteignable.

Le transcript vient de la re-transcription DU MASTER (`1_montage/transcripts-master/`),
jamais des rushs : apres les coupes, les instants des rushs ne correspondent plus a rien.
S'il n'existe pas encore, l'outil le fabrique avec `transcribe.py` (ElevenLabs si une cle
existe). C'est ce transcript, et non `hyperframes transcribe` (Whisper), que l'habillage doit
utiliser : il est plus juste et il a deja ete relu au montage.
"""
from __future__ import annotations

import argparse
import json

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

for _flux in (sys.stdout, sys.stderr):
    if hasattr(_flux, "reconfigure"):
        _flux.reconfigure(errors="replace")

TOOLS = Path(__file__).resolve().parent


def trouver_master(vd: Path, donne: str | None) -> Path | None:
    if donne:
        p = Path(donne)
        return p if p.is_file() else None
    masters = vd / "_media" / "masters"
    canon = masters / f"{vd.name}_montage.mp4"
    if canon.is_file():
        return canon
    cands = sorted((p for p in masters.glob("*.mp4") if "_habille" not in p.stem),
                   key=lambda p: p.stat().st_mtime, reverse=True) if masters.is_dir() else []
    return cands[0] if cands else None


def mots_du_master(vd: Path, master: Path, engine: str) -> list[dict]:
    tm = vd / "1_montage" / "transcripts-master"
    cible = tm / f"{master.stem}.words.json"
    perime = cible.is_file() and cible.stat().st_mtime < master.stat().st_mtime
    if not cible.is_file() or perime:
        if perime:
            print("[habillage] le master est plus recent que son transcript : re-transcription.")
        r = subprocess.run([sys.executable, str(TOOLS / "transcribe.py"), str(master),
                            "--out", str(tm), "--engine", engine])
        if r.returncode != 0 or not cible.is_file():
            sys.exit(2)
    d = json.loads(cible.read_text(encoding="utf-8"))
    mots = d.get("words") if isinstance(d, dict) else d
    return [m for m in mots if isinstance(m, dict) and str(m.get("text", "")).strip()
            and m.get("start") is not None and m.get("end") is not None]


def sonder(master: Path) -> dict:
    r = subprocess.run([paths.ffprobe(), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate",
                        "-show_entries", "format=duration", "-of", "json", str(master)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit(f"[REFUS] ffprobe ne lit pas {master}")
    return json.loads(r.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug")
    ap.add_argument("--master", default=None, help="defaut : _media/masters/<slug>_montage.mp4")
    ap.add_argument("--engine", choices=["auto", "whisper", "elevenlabs"], default="auto",
                    help="moteur de transcription si le transcript du master manque")
    a = ap.parse_args()

    try:
        vd = paths.video_dir(a.slug)
    except paths.UnknownVideo as e:
        print(f"[REFUS] {e}", file=sys.stderr)
        return 2
    master = trouver_master(vd, a.master)
    if master is None:
        print(f"[REFUS] aucun master dans {vd / '_media' / 'masters'} : faire le montage "
              "d'abord (skill /montage-rushs).", file=sys.stderr)
        return 2

    mots = mots_du_master(vd, master, a.engine)
    if not mots:
        print("[REFUS] le transcript du master est vide.", file=sys.stderr)
        return 2
    meta = sonder(master)
    s = (meta.get("streams") or [{}])[0]
    duree = float((meta.get("format") or {}).get("duration") or 0)

    hab = vd / "2_habillage"
    (hab / "public").mkdir(parents=True, exist_ok=True)
    src = hab / "public" / "input-video.mp4"
    fps = 30
    brut = str(s.get("r_frame_rate") or "30/1")
    if "/" in brut and float(brut.split("/")[1] or 0):
        fps = max(1, round(float(brut.split("/")[0]) / float(brut.split("/")[1])))
    if not src.is_file() or src.stat().st_mtime < master.stat().st_mtime:
        print(f"[habillage] re-encodage du master (une image cle toutes les {fps} images)...",
              flush=True)
        tmp = src.with_name("input-video.part.mp4")
        r = subprocess.run([paths.ffmpeg(), "-hide_banner", "-nostats", "-loglevel", "error",
                            "-y", "-i", str(master), "-c:v", "libx264", "-crf", "18",
                            "-g", str(fps), "-keyint_min", str(fps), "-pix_fmt", "yuv420p",
                            "-movflags", "+faststart", "-c:a", "aac", "-b:a", "192k", str(tmp)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            print(f"[REFUS] ffmpeg : {r.stderr.strip()[-400:]}", file=sys.stderr)
            return 2
        tmp.replace(src)
    plat = [{"text": str(m["text"]).strip(),
             "start": round(float(m["start"]), 3),
             "end": round(min(float(m["end"]), duree) if duree else float(m["end"]), 3)}
            for m in mots]
    (hab / "transcript.json").write_text(json.dumps(plat, ensure_ascii=False, indent=1) + "\n",
                                         encoding="utf-8")
    (hab / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    print(f"OK  {hab}")
    print(f"    public/input-video.mp4  {s.get('width')}x{s.get('height')}, {duree:.2f} s, "
          f"{fps} i/s (depuis {master.name})")
    print(f"    transcript.json         {len(plat)} mots du master")
    print("    metadata.json")
    if s.get("width") and s.get("height") and int(s["width"]) > int(s["height"]):
        print("[!] master en PAYSAGE : un short est vertical (1080x1920).")
    print("    suite : le skill /habillage, etape 3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
