"""importer_rushs.py -- faire ENTRER des rushs de telephone dans une video, sans les abimer.

    python tools/importer_rushs.py <slug> "C:\\chemin\\vers\\rush1.mov" "C:\\dossier\\de\\rushs"
    python tools/importer_rushs.py <slug>                 # rushs deja deposes dans _media/rushes/
    python tools/importer_rushs.py <slug> <chemins> --sonder   # diagnostic seul, rien n'est copie

Sortie : 0 import fait - 2 refus (aucun rush, fichier illisible, slug invalide), rien n'est copie.

Ce qu'il fait
-------------
1. cree `videos/<slug>/` (1_montage/, 2_habillage/, _media/rushes/) si besoin ;
2. SONDE chaque fichier AVANT de copier quoi que ce soit : un fichier sans image ou sans son,
   ou que ffprobe ne lit pas, fait REFUSER tout l'import (rien n'est a moitie fait) ;
3. COPIE les rushs (les originaux du telephone ne sont jamais deplaces ni modifies) ;
4. NORMALISE ceux qui en ont besoin, et garde alors l'original dans `_media/originaux/` :
   - **HDR** (iPhone en Dolby Vision / HLG, Android en HDR10) : converti en SDR BT.709. Sans
     ca, les couleurs sortent delavees ou grises une fois montees : ce n'est pas un
     etalonnage, c'est la condition pour que l'image ressemble a ce qu'on a filme ;
   - **cadence variable** (VFR, frequente sur telephone) : re-cadencee en images par seconde
     CONSTANTES. Sans ca, les coupes calculees au mot derivent par rapport a l'image ;
   La ROTATION (un portrait enregistre en paysage + une matrice de rotation) n'a pas besoin
   d'etre corrigee : ffmpeg l'applique au decodage. Elle est seulement signalee.
5. ecrit `1_montage/rushes.json` : ce qui a ete trouve et fait, rush par rush.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

for _flux in (sys.stdout, sys.stderr):
    if hasattr(_flux, "reconfigure"):
        _flux.reconfigure(errors="replace")

EXT_VIDEO = (".mov", ".mp4", ".m4v", ".mkv", ".3gp", ".avi", ".webm")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TRANSFERTS_HDR = ("smpte2084", "arib-std-b67")
CADENCES = (24, 25, 30, 50, 60)
#: Ecart relatif entre cadence nominale et cadence moyenne au-dela duquel on parle de VFR.
TOL_VFR = 0.01


def slug_valide(s: str) -> bool:
    return bool(SLUG_RE.match(s)) and len(s) <= 60


def nom_ascii(stem: str) -> str:
    """Un nom de fichier sans accent ni espace : certains outils de la chaine les supportent mal."""
    s = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")
    return s or "rush"


def _ratio(brut: str | None) -> float | None:
    if not brut or "/" not in brut:
        return None
    a, b = brut.split("/", 1)
    try:
        return float(a) / float(b) if float(b) else None
    except ValueError:
        return None


def sonder(f: Path) -> dict:
    """ffprobe d'un rush. `erreur` est renseignee si le fichier n'est pas un rush utilisable."""
    r = subprocess.run(
        [paths.ffprobe(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(f)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    info = {"fichier": str(f), "nom": f.name}
    if r.returncode != 0:
        info["erreur"] = "ffprobe ne lit pas ce fichier"
        return info
    d = json.loads(r.stdout or "{}")
    vs = [s for s in d.get("streams", []) if s.get("codec_type") == "video"
          and not (s.get("disposition") or {}).get("attached_pic")]
    aus = [s for s in d.get("streams", []) if s.get("codec_type") == "audio"]
    if not vs:
        info["erreur"] = "aucune piste video"
        return info
    if not aus:
        info["erreur"] = "aucune piste audio (impossible a transcrire)"
        return info
    v = vs[0]
    rot = 0
    for sd in v.get("side_data_list") or []:
        if sd.get("rotation") is not None:
            rot = int(float(sd["rotation"]))
    if not rot:
        rot = int(float((v.get("tags") or {}).get("rotate", 0) or 0))
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if abs(rot) % 180 == 90:
        w, h = h, w
    nominal = _ratio(v.get("r_frame_rate"))
    moyen = _ratio(v.get("avg_frame_rate"))
    vfr = bool(nominal and moyen and abs(nominal - moyen) / nominal > TOL_VFR)
    transfert = (v.get("color_transfer") or "").lower()
    primaires = (v.get("color_primaries") or "").lower()
    hdr = transfert in TRANSFERTS_HDR or (primaires == "bt2020" and "10" in (v.get("pix_fmt") or ""))
    info.update({
        "largeur": w, "hauteur": h, "rotation": rot,
        "fps_nominal": round(nominal, 3) if nominal else None,
        "fps_moyen": round(moyen, 3) if moyen else None,
        "vfr": vfr, "hdr": hdr, "transfert": transfert or None,
        "codec": v.get("codec_name"),
        "duree_s": round(float((d.get("format") or {}).get("duration") or 0), 3),
        "taille": f.stat().st_size,
    })
    return info


def cadence_cible(info: dict, force: int | None) -> int:
    """La cadence NOMINALE : sur un rush VFR la moyenne baisse avec les images perdues (un
    30 i/s qui en perd tombe a 23), et s'y caler ferait re-cadencer a 24. La nominale n'est
    ecartee que si elle est aberrante (certains conteneurs annoncent leur base de temps)."""
    if force:
        return force
    nominal = info.get("fps_nominal")
    ref = nominal if nominal and nominal <= 61 else (info.get("fps_moyen") or 30)
    return min(CADENCES, key=lambda c: abs(c - ref))


def zscale_dispo() -> bool:
    r = subprocess.run([paths.ffmpeg(), "-hide_banner", "-filters"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return " zscale " in r.stdout


def normaliser(src: Path, dst: Path, info: dict, fps: int) -> None:
    filtres = []
    if info["hdr"]:
        filtres.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                       "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")
    filtres.append("format=yuv420p")
    filtres.append("setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709")
    cmd = [paths.ffmpeg(), "-hide_banner", "-nostats", "-loglevel", "error", "-y",
           "-i", str(src), "-map", "0:v:0", "-map", "0:a:0",
           "-vf", ",".join(filtres), "-fps_mode", "cfr", "-r", str(fps),
           "-c:v", "libx264", "-crf", "17", "-preset", "medium",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           "-movflags", "+faststart", str(dst)]
    tmp = dst.with_name(dst.stem + ".part" + dst.suffix)
    cmd[-1] = str(tmp)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg a echoue sur {src.name} : {r.stderr.strip()[-400:]}")
    tmp.replace(dst)


def collecter(sources: list[str]) -> tuple[list[Path], list[str]]:
    fichiers, manquants = [], []
    for s in sources:
        p = Path(s.strip('"'))
        if p.is_dir():
            fichiers += sorted(x for x in p.iterdir()
                               if x.is_file() and x.suffix.lower() in EXT_VIDEO)
        elif p.is_file():
            fichiers.append(p)
        else:
            manquants.append(str(p))
    return fichiers, manquants


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug", help="nom de la video, en minuscules-avec-tirets (ex. recette-cookies)")
    ap.add_argument("sources", nargs="*", help="fichiers ou dossiers de rushs (chemins absolus)")
    ap.add_argument("--sonder", action="store_true", help="diagnostic seul, rien n'est copie")
    ap.add_argument("--fps", type=int, choices=CADENCES, default=None,
                    help="cadence de normalisation (defaut : la plus proche de la source)")
    a = ap.parse_args()

    if not slug_valide(a.slug):
        print(f"[REFUS] slug invalide : {a.slug!r}. Attendu : minuscules, chiffres et tirets "
              "(ex. recette-cookies).", file=sys.stderr)
        return 2

    vd = paths.VIDEOS / a.slug
    rushes = vd / "_media" / "rushes"

    if a.sources:
        fichiers, manquants = collecter(a.sources)
        if manquants:
            print("[REFUS] chemin(s) introuvable(s) :\n  " + "\n  ".join(manquants)
                  + "\nDonner le chemin ABSOLU (clic droit sur le fichier > Copier en tant que "
                  "chemin d'acces).", file=sys.stderr)
            return 2
        deja_la = False
    else:
        fichiers = sorted(x for x in rushes.iterdir()
                          if x.is_file() and x.suffix.lower() in EXT_VIDEO) if rushes.is_dir() else []
        deja_la = True

    if not fichiers:
        print(f"[REFUS] aucun rush trouve. Deux facons de les donner :\n"
              f"  1. un ou plusieurs chemins : python tools/importer_rushs.py {a.slug} \"C:\\...\\rush.mov\"\n"
              f"  2. deposer les fichiers dans {rushes} puis relancer sans chemin.\n"
              f"Extensions reconnues : {', '.join(EXT_VIDEO)}", file=sys.stderr)
        return 2

    infos = [sonder(f) for f in fichiers]
    mauvais = [i for i in infos if i.get("erreur")]
    if mauvais:
        print("[REFUS] fichier(s) inutilisable(s), rien n'a ete copie :", file=sys.stderr)
        for i in mauvais:
            print(f"  {i['nom']} : {i['erreur']}", file=sys.stderr)
        return 2
    noms = [nom_ascii(Path(i["nom"]).stem) for i in infos]
    doublons = sorted({n for n in noms if noms.count(n) > 1})
    if doublons and not deja_la:
        print(f"[REFUS] plusieurs rushs portent le meme nom : {', '.join(doublons)}. "
              "Les renommer avant l'import.", file=sys.stderr)
        return 2

    a_normaliser = [i for i in infos if i["hdr"] or i["vfr"]]
    if any(i["hdr"] for i in a_normaliser) and not a.sonder and not zscale_dispo():
        print("[REFUS] des rushs sont en HDR et ce ffmpeg n'a pas le filtre zscale.\n"
              "  Installer la version complete : winget install Gyan.FFmpeg", file=sys.stderr)
        return 2

    print(f"{len(infos)} rush(s) pour {a.slug} :")
    for i in infos:
        drap = [x for x, ok in (("HDR", i["hdr"]), ("VFR", i["vfr"]),
                                (f"rotation {i['rotation']}", bool(i["rotation"]))) if ok]
        print(f"  {i['nom']:<32} {i['largeur']}x{i['hauteur']}  {i['fps_moyen']} i/s  "
              f"{i['duree_s']:7.2f} s  {', '.join(drap) or 'standard'}")
    portrait = sum(1 for i in infos if i["hauteur"] > i["largeur"])
    if portrait < len(infos):
        print(f"[!] {len(infos) - portrait} rush(s) en PAYSAGE : un short est vertical (9:16).")
    petits = [i for i in infos if min(i["largeur"], i["hauteur"]) < 1080]
    if petits:
        print(f"[!] {len(petits)} rush(s) sous 1080 px de large : l'image sera molle.")
    if all(min(i["largeur"], i["hauteur"]) <= 1080 for i in infos):
        print("    Note : en 1080x1920 le punch-in (zoom) agrandit l'image. Filmer en 4K "
              "vertical le rend gratuit.")
    if a.sonder:
        print("\n--sonder : rien n'a ete copie.")
        return 0

    for sous in ("1_montage", "2_habillage", "_media/rushes"):
        (vd / sous).mkdir(parents=True, exist_ok=True)

    rapport = []
    for i, nom in zip(infos, noms):
        src = Path(i["fichier"])
        besoin = i["hdr"] or i["vfr"]
        if besoin:
            orig = paths.media(vd, "originaux", create=True) / (nom + src.suffix.lower())
            if not orig.exists() or orig.stat().st_size != src.stat().st_size:
                if deja_la:
                    shutil.move(str(src), str(orig))
                else:
                    shutil.copy2(src, orig)
            dst = rushes / f"{nom}.mp4"
            fps = cadence_cible(i, a.fps)
            raisons = " + ".join(x for x, ok in (("HDR -> SDR", i["hdr"]),
                                                   (f"VFR -> {fps} i/s", i["vfr"])) if ok)
            print(f"  normalisation {nom} ({raisons})...", flush=True)
            normaliser(orig, dst, i, fps)
            action = f"normalise ({raisons}), original dans _media/originaux/"
        else:
            dst = rushes / (nom + src.suffix.lower())
            if deja_la:
                if src != dst:
                    src.rename(dst)
                action = "deja en place"
            elif dst.exists() and dst.stat().st_size == src.stat().st_size:
                action = "deja importe"
            else:
                shutil.copy2(src, dst)
                action = "copie"
        rapport.append({**i, "rush": str(dst.relative_to(vd)).replace("\\", "/"), "action": action})

    sortie = vd / "1_montage" / "rushes.json"
    sortie.write_text(json.dumps({"video": a.slug, "rushes": rapport}, indent=2,
                                 ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nOK  {len(rapport)} rush(s) dans {rushes}")
    print(f"    rapport : {sortie}")
    print(f"    suite   : le skill /montage-rushs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
