# -*- coding: utf-8 -*-
"""paths.py -- le seul endroit du depot qui sait ou sont les choses.

Arborescence d'une video
------------------------
    videos/<slug>/
        1_montage/     transcripts des rushs, EDL, rapports        (versionne)
        2_habillage/   composition Hyperframes, sous-titres          (versionne)
        _media/        rushes/ originaux/ masters/ renders/ captures/   (IGNORE par git)

`<slug>` est libre : `mon-premier-short`, `recette-cookies`... Pas de numero impose.

Ne jamais recoller un chemin a la main dans un script : demander a ce module.

    python tools/paths.py                 # racine, ffmpeg, liste des videos
    python tools/paths.py <slug>          # les dossiers d'une video
    python tools/paths.py --bin ffmpeg    # le binaire, pour un script shell
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
VIDEOS = ROOT / "videos"
MODELS = ROOT / "models"

MEDIA_ZONES = ("rushes", "originaux", "masters", "renders", "captures")

#: Conserve pour compatibilite avec les outils portes : il n'y a pas de series ici.
SERIES: tuple[str, ...] = ()


class AmbiguousVideo(Exception):
    """Gardee pour compatibilite : un slug est unique, elle n'est jamais levee."""


class UnknownVideo(Exception):
    """Aucune video ne repond a cette reference."""


def principal_root() -> Path:
    return ROOT


def is_linked_worktree() -> bool:
    return False


# --------------------------------------------------------------------------- videos

def list_videos(typ: str | None = None) -> list[Path]:
    """Les dossiers de `videos/`, tries. `typ` est ignore (pas de series)."""
    if not VIDEOS.is_dir():
        return []
    return [p for p in sorted(VIDEOS.iterdir())
            if p.is_dir() and not p.name.startswith((".", "_"))]


def video_dir(ref, typ: str | None = None) -> Path:
    """Le dossier d'une video, depuis son slug ou un chemin situe dedans."""
    ref = str(ref)
    p = Path(ref)
    if p.exists():
        try:
            return video_from_path(p)
        except UnknownVideo:
            pass
    exact = VIDEOS / ref
    if exact.is_dir():
        return exact
    connues = ", ".join(d.name for d in list_videos()) or "aucune"
    raise UnknownVideo(f"aucune video '{ref}' dans videos/. Connues : {connues}")


def video_from_path(p) -> Path:
    """Remonte au dossier video depuis n'importe quel chemin situe dedans.

    Le chemin LEXICAL est essaye avant le chemin resolu : `resolve()` traverse les liens
    et les jonctions Windows, et sortirait alors de `videos/`.
    """
    videos = (Path(os.path.abspath(VIDEOS)), VIDEOS.resolve())
    for base in (Path(os.path.abspath(p)), Path(p).resolve()):
        q = base.parent if base.is_file() else base
        for cand in (q, *q.parents):
            if cand.parent in videos:
                return cand
    raise UnknownVideo(f"{p} n'est pas dans videos/")


def montage_dir(ref, typ=None) -> Path:
    return video_dir(ref, typ) / "1_montage"


def habillage_dir(ref, typ=None) -> Path:
    return video_dir(ref, typ) / "2_habillage"


#: Conserve pour compatibilite : ici toute video passe par la chaine de montage.
MONTAGES_HORS_CHAINE: tuple[str, ...] = ()


def montage_fiche(ref, typ=None) -> str | None:
    return None


def media(ref, zone: str, typ=None, create: bool = False) -> Path:
    """Une zone de `_media/`. `create=True` cree le dossier au besoin."""
    if zone not in MEDIA_ZONES:
        raise ValueError(f"zone inconnue : {zone!r}. Attendu : {', '.join(MEDIA_ZONES)}")
    d = video_dir(ref, typ) / "_media" / zone
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def transcripts_dir(ref, typ=None) -> Path:
    """Transcripts DES RUSHS : la source de verite des timings."""
    return montage_dir(ref, typ) / "transcripts"


def transcripts_master_dir(ref, typ=None) -> Path:
    """Re-transcription DU MASTER : la source des sous-titres karaoke.

    Caler des sous-titres sur les timecodes des rushs produit un decalage silencieux.
    """
    return montage_dir(ref, typ) / "transcripts-master"


# --------------------------------------------------------------------------- binaires

def _resolve_binary(name: str) -> str:
    """ffmpeg / ffprobe : `FFMPEG_BIN`/`FFPROBE_BIN`, puis le PATH, puis WinGet."""
    env = os.environ.get(f"{name.upper()}_BIN")
    if env:
        if Path(env).exists() or shutil.which(env):
            return env
        sys.exit(f"{name.upper()}_BIN pointe vers un binaire introuvable : {env}")
    found = shutil.which(name)
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
    cands = sorted(local.glob(f"Gyan.FFmpeg*/ffmpeg-*/bin/{name}.exe"))
    if cands:
        return str(cands[-1])
    sys.exit(f"{name} introuvable. Installe-le : winget install Gyan.FFmpeg "
             f"(puis rouvre le terminal), ou definis {name.upper()}_BIN.")


def ffmpeg() -> str:
    return _resolve_binary("ffmpeg")


def ffprobe() -> str:
    return _resolve_binary("ffprobe")


# --------------------------------------------------------------------------- CLI

def _main(argv):
    if len(argv) > 2 and argv[1] == "--bin":
        if argv[2] not in ("ffmpeg", "ffprobe"):
            sys.exit(f"--bin attend ffmpeg ou ffprobe, pas {argv[2]!r}")
        print(_resolve_binary(argv[2]))
        return
    if len(argv) > 1 and argv[1] not in ("--help", "-h"):
        try:
            d = video_dir(argv[1])
        except UnknownVideo as e:
            sys.exit(str(e))
        print(f"video        {d.name}")
        print(f"  montage    {montage_dir(d)}")
        print(f"  habillage  {habillage_dir(d)}")
        for z in MEDIA_ZONES:
            m = media(d, z)
            etat = f"{len(list(m.iterdir())):>3} fichier(s)" if m.is_dir() else "ABSENT"
            print(f"  _media/{z:<9} {etat}")
        return
    print(f"racine   {ROOT}")
    print(f"ffmpeg   {ffmpeg()}")
    print(f"ffprobe  {ffprobe()}")
    vids = list_videos()
    print(f"\nvideos ({len(vids)}) : {', '.join(v.name for v in vids) or 'aucune'}")


if __name__ == "__main__":
    _main(sys.argv)
