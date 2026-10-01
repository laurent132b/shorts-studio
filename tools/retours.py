"""Les retours sur un rendu, poses A LA FRAME dans la page de review.

    python tools/retours.py <slug>                  # les retours du dernier rendu commente
    python tools/retours.py <slug> --rendu <fichier.mp4>
    python tools/retours.py <slug> --traite         # toutes les notes ont ete appliquees
    python tools/retours.py <slug> --rouvrir        # une note est arrivee apres coup
    python tools/retours.py <slug> --ouvrir         # lance le serveur et ouvre la page
    python tools/retours.py --tous                  # toutes les videos qui ont des retours ouverts

Sortie : 0 aucun retour ouvert - 1 des retours attendent - 2 on ne sait pas.

Le principe
-----------
On arrete le lecteur sur la frame, on ecrit une note, elle est enregistree dans le PROJET :

    videos/<slug>/2_habillage/retours/<rendu sans extension>.json

Un fichier PAR RENDU : des notes sur un master n'ont pas de sens sur la version suivante, les
instants ont bouge. Le fichier garde la TAILLE et la DATE du rendu au moment des notes : un
rendu re-ecrit sous le meme nom est signale.

Pour chaque note, l'outil dit le MOT prononce a cet instant (lu dans
`1_montage/transcripts-master/*.words.json`, a defaut dans un `.srt` de `2_habillage/`) et la
phrase autour. C'est la session Claude qui lit ce fichier, applique, puis marque `--traite`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

for _flux in (sys.stdout, sys.stderr):
    if hasattr(_flux, "reconfigure"):
        _flux.reconfigure(errors="replace")

#: Le serveur de review. Plusieurs ports possibles : un autre outil de la machine peut
#: occuper le premier, et le serveur dit qui il est (`identite`).
PORT_DEFAUT = 8790
PORTS = range(8790, 8800)
APP = "shorts-studio"
EXT_RENDUS = (".mp4", ".mov", ".webm")
NOTE_MAX = 2000
FENETRE_MOTS = 6


# --------------------------------------------------------------------------- stockage

def dossier_retours(video_dir: Path) -> Path:
    return video_dir / "2_habillage" / "retours"


def fichier_pour(video_dir: Path, rendu: str) -> Path:
    tige = Path(str(rendu)).name
    for ext in EXT_RENDUS:
        if tige.lower().endswith(ext):
            tige = tige[: -len(ext)]
    tige = re.sub(r"[^A-Za-z0-9._-]+", "-", tige) or "rendu"
    return dossier_retours(video_dir) / f"{tige}.json"


def _lire_json(p: Path) -> dict | None:
    try:
        if not p.is_file() or p.stat().st_size > 2 * 1024 * 1024:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ecrire_json(p: Path, obj: dict) -> None:
    """Ecriture atomique : un navigateur qui recharge pendant l'ecriture lit l'ancien fichier
    ou le nouveau, jamais un fichier a moitie ecrit."""
    p.parent.mkdir(parents=True, exist_ok=True)
    s = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    json.loads(s)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(s, encoding="utf-8", newline="\n")
    os.replace(tmp, p)


def _maintenant() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _horodater(propres: list[dict], avant: list[dict]) -> list[dict]:
    """Horodate les notes NEUVES cote serveur (une seule horloge), garde celles deja ecrites."""
    connus = {(round(float(n.get("t", 0)), 3), str(n.get("txt", ""))): n.get("ajoute")
              for n in avant if isinstance(n, dict)}
    horodate = _maintenant()
    for n in propres:
        n["ajoute"] = connus.get((n["t"], n["txt"])) or horodate
    return propres


def _nettoyer_notes(notes, avec_ajoute: bool = False) -> list[dict]:
    propres = []
    for n in notes if isinstance(notes, list) else []:
        if not isinstance(n, dict) or not str(n.get("txt", "")).strip():
            continue
        try:
            t = max(0.0, float(n.get("t", 0)))
        except (TypeError, ValueError):
            continue
        item = {"t": round(t, 3), "txt": str(n["txt"]).strip()[:NOTE_MAX]}
        if avec_ajoute and n.get("ajoute"):
            item["ajoute"] = str(n["ajoute"])
        propres.append(item)
    propres.sort(key=lambda x: x["t"])
    return propres


def lire_retours(video_dir: Path) -> list[dict]:
    """Tous les lots de retours d'une video, le plus recent d'abord."""
    d = dossier_retours(video_dir)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        obj = _lire_json(p)
        if not obj or not isinstance(obj.get("notes"), list):
            continue
        obj = dict(obj)
        obj["_fichier"] = str(p.relative_to(video_dir)).replace("\\", "/")
        obj["notes"] = _nettoyer_notes(obj["notes"], avec_ajoute=True)
        obj.setdefault("rendu", p.stem)
        obj.setdefault("traite", False)
        out.append(obj)
    out.sort(key=lambda o: o.get("mis_a_jour", ""), reverse=True)
    return out


def ecrire_retours(video_dir: Path, rendu: str, notes, meta: dict | None = None) -> dict:
    """Ecrit le lot d'un rendu. Une note ajoutee a un lot deja TRAITE le rouvre."""
    p = fichier_pour(video_dir, rendu)
    avant = _lire_json(p) or {}
    propres = _nettoyer_notes(notes)
    change = _nettoyer_notes(avant.get("notes", [])) != propres
    notes_avant = avant.get("notes", []) if isinstance(avant.get("notes"), list) else []
    propres = _horodater(propres, notes_avant)
    corps = {
        "_lu_par": "python tools/retours.py <slug>  (ecrit par tools/review/serveur.py)",
        "video": video_dir.name,
        "rendu": Path(str(rendu)).name,
        "rendu_taille": (meta or {}).get("rendu_taille", avant.get("rendu_taille")),
        "rendu_mtime": (meta or {}).get("rendu_mtime", avant.get("rendu_mtime")),
        "fps": (meta or {}).get("fps", avant.get("fps")),
        "duree_s": (meta or {}).get("duree_s", avant.get("duree_s")),
        "cree": avant.get("cree") or _maintenant(),
        "mis_a_jour": _maintenant(),
        "traite": False if change else bool(avant.get("traite", False)),
        "traite_le": None if change else avant.get("traite_le"),
        "notes": propres,
    }
    _ecrire_json(p, corps)
    return corps


def marquer_traite(video_dir: Path, rendu: str, traite: bool) -> dict | None:
    p = fichier_pour(video_dir, rendu)
    obj = _lire_json(p)
    if obj is None:
        return None
    obj["traite"] = bool(traite)
    obj["traite_le"] = _maintenant() if traite else None
    obj["mis_a_jour"] = _maintenant()
    _ecrire_json(p, obj)
    return obj


def resume(video_dir: Path) -> dict:
    ouverts = total = 0
    for f in lire_retours(video_dir):
        total += len(f["notes"])
        if not f.get("traite"):
            ouverts += len(f["notes"])
    return {"ouverts": ouverts, "total": total}


def rendus(video_dir: Path) -> list[dict]:
    """Les fichiers relisibles de `_media/masters/`, le plus recent d'abord."""
    d = video_dir / "_media" / "masters"
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        if not p.is_file() or p.suffix.lower() not in EXT_RENDUS:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"nom": p.name, "taille": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def etat_rendu(video_dir: Path, lot: dict) -> str | None:
    """Le rendu a-t-il change depuis les notes ? Rend une phrase, ou None."""
    f = video_dir / "_media" / "masters" / str(lot.get("rendu", ""))
    if not f.is_file():
        return "le rendu commente n'est plus dans _media/masters/"
    st = f.stat()
    if lot.get("rendu_taille") and lot["rendu_taille"] != st.st_size:
        return ("le rendu a ete RE-ECRIT depuis les notes (taille differente) : "
                "les instants peuvent avoir bouge")
    return None


# --------------------------------------------------------------------------- les mots

_SRT_T = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def _srt_sec(m) -> float:
    h, mn, s, ms = m.groups()
    return int(h) * 3600 + int(mn) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _mots_depuis_srt(p: Path) -> list[dict]:
    mots = []
    for bloc in re.split(r"\n\s*\n", p.read_text(encoding="utf-8", errors="replace")):
        lignes = [x for x in bloc.strip().splitlines() if x.strip()]
        if len(lignes) < 2:
            continue
        tete = " ".join(lignes[:2])
        if "-->" not in tete:
            continue
        gauche, droite = tete.split("-->", 1)
        ma, mb = _SRT_T.search(gauche), _SRT_T.search(droite)
        if not ma or not mb:
            continue
        texte = " ".join(lignes[2:] if _SRT_T.search(lignes[0]) is None else lignes[1:])
        texte = re.sub(r"<[^>]+>|\{[^}]+\}", "", texte).strip()
        if texte:
            mots.append({"start": _srt_sec(ma), "end": _srt_sec(mb), "text": texte})
    return mots


def _choisir(candidats: list[Path], rendu: str) -> tuple[Path | None, str]:
    if not candidats:
        return None, "aucun"
    if len(candidats) == 1:
        return candidats[0], candidats[0].name
    plus_recent = max(candidats, key=lambda c: c.stat().st_mtime)
    return plus_recent, f"{plus_recent.name} (le plus recent de {len(candidats)})"


def source_mots(video_dir: Path, rendu: str) -> tuple[list[dict], str]:
    tm = video_dir / "1_montage" / "transcripts-master"
    if tm.is_dir():
        p, prov = _choisir(sorted(tm.glob("*.words.json")), rendu)
        if p is not None:
            d = _lire_json(p)
            mots = d.get("words") if isinstance(d, dict) else d
            if isinstance(mots, list) and mots and isinstance(mots[0], dict):
                return ([m for m in mots if m.get("start") is not None and m.get("text")],
                        f"mots du master : 1_montage/transcripts-master/{prov}")
    p, prov = _choisir(sorted((video_dir / "2_habillage").glob("*.srt")), rendu)
    if p is not None:
        return _mots_depuis_srt(p), f"cues du SRT : 2_habillage/{prov}"
    return [], "aucune source de mots (transcrire le master : voir le skill montage-rushs)"


def mot_a(mots: list[dict], t: float) -> tuple[dict | None, float, str]:
    if not mots:
        return None, 0.0, ""
    i = None
    for k, m in enumerate(mots):
        if float(m["start"]) <= t <= float(m.get("end", m["start"])):
            i = k
            break
    if i is None:
        i = min(range(len(mots)), key=lambda k: abs(float(mots[k]["start"]) - t))
    m = mots[i]
    a, b = max(0, i - FENETRE_MOTS), min(len(mots), i + FENETRE_MOTS + 1)
    bouts = [f"[{str(mots[k]['text']).strip()}]" if k == i else str(mots[k]["text"]).strip()
             for k in range(a, b)]
    return m, round(t - float(m["start"]), 3), " ".join(bouts)


# --------------------------------------------------------------------------- serveur

def lien(video_dir: Path, rendu: str | None, port: int = PORT_DEFAUT) -> str:
    from urllib.parse import quote
    u = f"http://127.0.0.1:{port}/?video={quote(video_dir.name)}"
    if rendu:
        u += f"&rendu={quote(Path(rendu).name)}"
    return u


def serveur_repond(port: int = PORT_DEFAUT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def identite(port: int) -> str | None:
    """Qui ecoute sur ce port : "nous" (le serveur de CE depot), "autre", ou None (libre).

    Un port qui repond ne suffit pas : un autre outil de la machine peut l'occuper, et un
    lien vers lui montrerait d'autres videos, de facon credible. Le serveur de ce depot
    publie la racine qu'il sert sur `/api/identite`, et on la compare a la notre.
    """
    if not serveur_repond(port):
        return None
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/identite", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:                            # noqa: BLE001
        return "autre"
    meme = os.path.normcase(os.path.abspath(str(d.get("racine", "")))) == \
        os.path.normcase(os.path.abspath(str(paths.ROOT)))
    return "nous" if d.get("app") == APP and meme else "autre"


def port_actif() -> int | None:
    """Le port ou tourne deja le serveur de ce depot, s'il tourne."""
    for p in PORTS:
        if identite(p) == "nous":
            return p
    return None


def demarrer_serveur(port: int | None = None, attente_s: float = 8.0) -> int | None:
    """Rend le port du serveur de review de CE depot, en le lancant au besoin.

    Sans `port`, essaie 8790 a 8799 : reutilise notre serveur s'il tourne deja, saute un
    port occupe par un autre programme, lance le serveur sur le premier port libre.
    Detache et sans fenetre, il survit a la session. None si rien n'a pu etre lance.
    """
    script = paths.ROOT / "tools" / "review" / "serveur.py"
    if not script.is_file():
        return None
    for p in ([port] if port else list(PORTS)):
        qui = identite(p)
        if qui == "nous":
            return p
        if qui == "autre":
            continue
        try:
            drapeaux = 0x08000000 | 0x00000200 if os.name == "nt" else 0
            subprocess.Popen(                        # noqa: S603
                [sys.executable, "-u", str(script)], cwd=str(paths.ROOT),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=drapeaux, close_fds=True, start_new_session=(os.name != "nt"),
                env={**os.environ, "REVIEW_PORT": str(p)})
        except OSError:
            return None
        fin = time.time() + attente_s
        while time.time() < fin:
            if identite(p) == "nous":
                return p
            time.sleep(0.25)
        return None
    return None


def ouvrir_review(video_dir: Path, rendu: str, port: int | None = None) -> str:
    """Lance le serveur et ouvre la page sur ce rendu. Ne leve JAMAIS : rend une phrase.

    Appelee par `assemble.py` des qu'un master est ecrit, pour que la review s'ouvre sans
    qu'on la demande. `REVIEW_MUET=1` l'empeche d'ouvrir le navigateur (tests).
    """
    if port is None and os.environ.get("REVIEW_PORT"):
        port = int(os.environ["REVIEW_PORT"])
    try:
        port = demarrer_serveur(port)
        if port is None:
            return ("[review] le serveur n'a pas pu etre lance (ports 8790-8799 occupes ?). "
                    "A la main : python tools/review/serveur.py")
        u = lien(video_dir, rendu, port)
        if os.environ.get("REVIEW_MUET"):
            return f"[review] {u}  (REVIEW_MUET : page non ouverte)"
        import webbrowser
        ok = False
        try:
            ok = bool(webbrowser.open(u))
        except Exception:                        # noqa: BLE001
            ok = False
        return f"[review] {u}\n[review] " + ("page ouverte dans le navigateur." if ok else
                                             "ouvrir le lien a la main.")
    except Exception as e:                       # noqa: BLE001
        return f"[review] ouverture impossible ({e.__class__.__name__}) : {lien(video_dir, rendu, port)}"


# --------------------------------------------------------------------------- affichage

def fmt_tc(t: float, fps) -> str:
    mn, s = divmod(max(0.0, float(t)), 60)
    frame = f"  f{int(round(t * fps)):04d}" if fps else ""
    return f"{int(mn):02d}:{s:06.3f}{frame}"


def choisir_lot(lots: list[dict], rendu: str | None) -> dict | None:
    if rendu:
        cible = fichier_pour(Path("."), rendu).stem
        for lot in lots:
            if fichier_pour(Path("."), lot.get("rendu", "")).stem == cible:
                return lot
        return None
    ouverts = [x for x in lots if not x.get("traite") and x["notes"]]
    return (ouverts or lots or [None])[0]


def imprimer_lot(video_dir: Path, lot: dict) -> None:
    rendu = str(lot.get("rendu", "?"))
    fps = lot.get("fps")
    print(f"video     {video_dir.name}")
    print(f"rendu     _media/masters/{rendu}" + (f"  ({fps} fps)" if fps else ""))
    print(f"fichier   {lot.get('_fichier')}")
    print(f"etat      {'TRAITE le ' + str(lot.get('traite_le')) if lot.get('traite') else 'OUVERT'}"
          f"  -  {len(lot['notes'])} retour(s), mis a jour {lot.get('mis_a_jour')}")
    ecart = etat_rendu(video_dir, lot)
    if ecart:
        print(f"[!] {ecart}", file=sys.stderr)
    mots, prov_m = source_mots(video_dir, rendu)
    print(f"mots      {prov_m}\n")
    for i, n in enumerate(lot["notes"], 1):
        t = float(n["t"])
        print(f"{i:>2}. {fmt_tc(t, fps)}  {n['txt']}")
        m, ecart_m, phrase = mot_a(mots, t)
        if m is not None:
            dans = 0 <= ecart_m <= float(m.get("end", m["start"])) - float(m["start"])
            ou = "sur" if dans else f"{ecart_m:+.2f} s du debut de"
            print(f"      dit       {ou} << {str(m['text']).strip()} >>  :  {phrase}")
        else:
            print("      dit       ?")
    print("\nUne note = une demande (une phrase avec un 'et' en porte souvent deux).")
    print("Cocher chacune, puis :")
    print(f"    python tools/retours.py {video_dir.name} --rendu {rendu} --traite")


# --------------------------------------------------------------------------- commandes

def cmd_video(a) -> int:
    try:
        vd = paths.video_dir(a.ref)
    except paths.UnknownVideo as e:
        print(f"[ON NE SAIT PAS] {e}", file=sys.stderr)
        return 2

    if a.ouvrir or a.lien:
        rs = rendus(vd)
        cible = a.rendu or (rs[0]["nom"] if rs else None)
        if cible is None:
            print("[ON NE SAIT PAS] aucun rendu dans _media/masters/ : rien a relire.",
                  file=sys.stderr)
            return 2
        if a.lien:
            print(lien(vd, cible, a.port or port_actif() or PORT_DEFAUT))
        else:
            print(ouvrir_review(vd, cible, a.port))
        return 0

    if a.traite or a.rouvrir:
        lot = choisir_lot(lire_retours(vd), a.rendu)
        if lot is None:
            print("[ON NE SAIT PAS] aucun lot de retours dans 2_habillage/retours/.",
                  file=sys.stderr)
            return 2
        obj = marquer_traite(vd, lot["rendu"], bool(a.traite))
        print(f"{'TRAITE' if a.traite else 'ROUVERT'}  {obj['rendu']}  ({len(obj['notes'])} retour(s))")
        return 0

    lots = lire_retours(vd)
    if not lots:
        print(f"video     {vd.name}\naucun retour. Rendus relisibles :")
        for r in rendus(vd)[:6]:
            print(f"    {datetime.fromtimestamp(r['mtime']).strftime('%Y-%m-%d %H:%M')}  {r['nom']}")
        print(f"    -> python tools/retours.py {vd.name} --ouvrir")
        return 0
    lot = choisir_lot(lots, a.rendu)
    if lot is None:
        print(f"[ON NE SAIT PAS] aucun lot pour {a.rendu}. Lots connus : "
              + ", ".join(str(x.get("rendu")) for x in lots), file=sys.stderr)
        return 2
    autres = [x for x in lots if x is not lot and not x.get("traite") and x["notes"]]
    if lot.get("traite") and not autres and a.rendu is None:
        print(f"video     {vd.name}")
        print(f"RIEN D'OUVERT : le dernier lot ({lot.get('rendu')}, {len(lot['notes'])} note(s)) "
              f"a ete traite le {lot.get('traite_le')}.")
        print(f"    detail : python tools/retours.py {vd.name} --rendu {lot.get('rendu')}")
        return 0
    imprimer_lot(vd, lot)
    if autres:
        print(f"[!] {len(autres)} autre(s) lot(s) OUVERT(S) : "
              + ", ".join(str(x.get("rendu")) for x in autres), file=sys.stderr)
    return 1 if (not lot.get("traite") and lot["notes"]) or autres else 0


def cmd_arreter() -> int:
    """Arrete le serveur de review de CE depot (il tourne en fond apres --ouvrir)."""
    import urllib.request
    p = port_actif()
    if p is None:
        print("Aucun serveur de review de ce depot ne tourne.")
        return 0
    req = urllib.request.Request(f"http://127.0.0.1:{p}/api/arreter", data=b"{}",
                                 headers={"content-type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:                       # noqa: BLE001
        print(f"[ON NE SAIT PAS] arret impossible sur le port {p} : {e}", file=sys.stderr)
        return 2
    print(f"Serveur de review arrete (port {p}).")
    return 0


def cmd_tous() -> int:
    ouverts = 0
    for vd in paths.list_videos():
        for lot in lire_retours(vd):
            if lot.get("traite") or not lot["notes"]:
                continue
            ouverts += 1
            print(f"{vd.name:<40} {len(lot['notes']):>3} retour(s)  {lot.get('rendu')}")
    if not ouverts:
        print("Aucun retour ouvert.")
        return 0
    print(f"\n{ouverts} lot(s) ouvert(s).")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref", nargs="?", help="slug de la video (le nom de son dossier dans videos/)")
    ap.add_argument("--rendu", default=None, help="nom du fichier relu dans _media/masters/")
    ap.add_argument("--port", type=int, default=None,
                    help="force un port (defaut : le premier libre entre 8790 et 8799)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--traite", action="store_true", help="chaque note a ete appliquee ou arbitree")
    g.add_argument("--rouvrir", action="store_true")
    g.add_argument("--ouvrir", action="store_true", help="lance le serveur et ouvre la page")
    g.add_argument("--lien", action="store_true", help="imprime l'adresse de la page")
    g.add_argument("--tous", action="store_true", help="les lots ouverts de toutes les videos")
    g.add_argument("--arreter", action="store_true", help="arrete le serveur de review")
    a = ap.parse_args()
    if a.tous:
        return cmd_tous()
    if a.arreter:
        return cmd_arreter()
    if not a.ref:
        ap.error("donner le slug d'une video, ou --tous")
    return cmd_video(a)


if __name__ == "__main__":
    sys.exit(main())
