#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/cues_phrases.py - la 5e regle de sous-titrage, celle que la PONCTUATION ne voit pas.

    python tools/cues_phrases.py <slug>                        # controle seul
    python tools/cues_phrases.py <slug> --appliquer            # recoupe le SRT en place
    python tools/cues_phrases.py videos/<slug>/1_montage <srt> --appliquer

POURQUOI CET OUTIL EXISTE
=========================
`rechunk_karaoke.py` applique quatre des cinq regles de segmentation des sous-titres :
jamais de mot coupe, jamais de mot-outil en fin de bloc, pas de mot court seul, <= 28
caracteres.

Il ne peut pas appliquer la cinquieme - **jamais deux phrases dans le meme bloc** - parce
qu'il ne voit que `words.json`, ou une frontiere de phrase ne se lit que dans la
PONCTUATION. Or l'ASR ne ponctue pas toujours : quand les prises s'enchainent sans marquer
la fin, c'est le DERNIER mot d'une ligne de script qui perd son point, exactement celui
dont on aurait besoin. Une cue comme "critique Dis-nous" se lit alors comme un
enchainement absurde, et aucune regle textuelle ne peut le voir.

LE DETECTEUR JUSTE EST L'EDL, PAS LA PONCTUATION
================================================
Un segment de `edl_v3_timeline.json` EST une ligne de script, donc une phrase. Une cue qui
chevauche deux segments melange deux phrases, ponctuation ou pas. C'est une frontiere
**structurelle** et non typographique : elle survit a une ASR muette.

ATTENTION au remontage : ce controle depend de la timeline. Toute retouche du montage impose
de le relancer, sinon les cues sont recoupees sur des frontieres qui ont bouge.

ATTENTION `--appliquer` RECONSTRUIT le texte des cues depuis le words.json du MASTER (le seul
de `transcripts-master/`), pas depuis le SRT qu'on lui donne. Une orthographe corrigee
ailleurs est donc annulee au premier recoupage : corriger dans le words.json du master, puis
`rechunk_karaoke`.

ATTENTION RECOUPER PEUT CASSER UNE AUTRE REGLE. Scinder "eleve Si tu utilises Opus" laisse
"eleve" (5 lettres) seul sur son bloc, ce que la regle interdit (un mot de >= 8 lettres seul
est tolere, un mot court non). D'ou le reequilibrage : on emprunte le dernier mot du bloc
PRECEDENT, qui est dans la MEME phrase ("d'effort n'etait pas assez" + "eleve" devient
"d'effort n'etait pas" + "assez eleve").

DEFAUT CONNU : quand le fragment court OUVRE une phrase, son bloc precedent est de l'autre
cote de la frontiere qu'on vient de poser, et l'emprunt la retraverse : `--appliquer` defait
alors une partie de sa propre coupe, en silence. Donc **relancer le controle SANS
`--appliquer` apres chaque `--appliquer`**. Le corriger demanderait d'emprunter au bloc
SUIVANT dans ce cas, ce qui change le decoupage de toute video qui relance l'outil.

Autre limite : ce fichier ne regroupe pas les segments de meme `i`. Deux moities d'une meme
ligne scindee par `pass_split_internal` y comptent pour DEUX phrases, ce qui peut produire un
faux positif.
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

# 28 caracteres : la largeur d'une ligne de sous-titre lisible sur un short 9:16. Doit
# rester egal au `--maxline` de `rechunk_karaoke.py`.
MAX_CAR = 28
MIN_SEUL = 8      # un mot seul sur un bloc doit faire au moins ca


def _force_utf8_stdout():
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def secs(x: str) -> float:
    h, m, rest = x.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def tc(t: float) -> str:
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


def lire_srt(p: Path):
    out = []
    for b in p.read_text(encoding="utf-8").strip().split("\n\n"):
        li = b.split("\n")
        if len(li) < 3:
            continue
        a, z = li[1].split(" --> ")
        out.append({"t0": secs(a), "t1": secs(z), "texte": " ".join(li[2:]).strip()})
    return out


def ecrire_srt(p: Path, cues):
    p.write_text("\n\n".join(
        f"{i}\n{tc(c['t0'])} --> {tc(c['t1'])}\n{c['texte']}"
        for i, c in enumerate(cues, start=1)) + "\n", encoding="utf-8")


def frontieres(timeline: Path):
    """Les fins de segment, hors la derniere : ce sont les frontieres de PHRASE du montage."""
    tl = json.loads(timeline.read_text(encoding="utf-8"))
    segs = tl["segments"] if isinstance(tl, dict) else tl
    if not segs or "_cum_end" not in segs[0]:
        sys.exit("cette timeline n'est pas PLANIFIEE (pas de `_cum_end`) : lancer d'abord\n"
                 "  assemble.py <edl> --honor-cuts --plan-only\n"
                 "Sans les cumuls, une frontiere de phrase en temps MASTER est indeterminable.")
    return sorted({round(s["_cum_end"], 3) for s in segs[:-1]}), segs


def recouper(cues, bornes, mots):
    sortie, coupes = [], []
    for c in cues:
        dedans = [b for b in bornes if c["t0"] + 0.02 < b < c["t1"] - 0.02]
        if not dedans:
            sortie.append(c)
            continue
        b = dedans[0]
        dans = [w for w in mots if w["start"] >= c["t0"] - 0.03 and w["end"] <= c["t1"] + 0.03]
        g = [w for w in dans if w["start"] < b]
        d = [w for w in dans if w["start"] >= b]
        if not g or not d:
            sortie.append(c)      # la frontiere ne separe aucun mot : rien a faire
            continue
        sortie.append({"t0": c["t0"], "t1": round(g[-1]["end"], 3),
                       "texte": " ".join(w["text"] for w in g)})
        sortie.append({"t0": round(d[0]["start"], 3), "t1": c["t1"],
                       "texte": " ".join(w["text"] for w in d)})
        coupes.append((c["texte"], sortie[-2]["texte"], sortie[-1]["texte"]))
    return sortie, coupes


def reequilibrer(cues, mots):
    """Un fragment reduit a UN MOT COURT emprunte au bloc PRECEDENT (meme phrase)."""
    faits = []
    for k in range(1, len(cues)):
        cur, prev = cues[k], cues[k - 1]
        mc = cur["texte"].split()
        if len(mc) != 1 or len(mc[0].strip(".,!?;:")) >= MIN_SEUL:
            continue
        mp = prev["texte"].split()
        if len(mp) < 3:
            continue                       # ne pas appauvrir le voisin sous 2 mots
        emprunte = mp[-1]
        if len(emprunte) + 1 + len(cur["texte"]) > MAX_CAR:
            continue
        w = [x for x in mots if x["start"] >= prev["t0"] - 0.03 and x["end"] <= prev["t1"] + 0.03]
        if len(w) < 2:
            continue
        prev["texte"] = " ".join(mp[:-1])
        prev["t1"] = round(w[-2]["end"], 3)
        cur["texte"] = f"{emprunte} {cur['texte']}"
        cur["t0"] = round(w[-1]["start"], 3)
        faits.append((emprunte, prev["texte"], cur["texte"]))
    return faits


def main() -> int:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cible", help="slug de la video (videos/<slug>/) ou dossier 1_montage")
    ap.add_argument("srt", nargs="?", default=None,
                    help="le SRT karaoke ; par defaut le seul *karaoke*.srt du 2_habillage")
    ap.add_argument("--serie", default=None, help="ignore (conserve pour compatibilite)")
    ap.add_argument("--appliquer", action="store_true",
                    help="recouper le SRT EN PLACE (sinon, controle seul)")
    a = ap.parse_args()

    p = Path(a.cible)
    if (p / "edl_v3_timeline.json").exists():
        mdir = p
        hab = p.parent / "2_habillage"
    else:
        mdir = paths.montage_dir(a.cible, a.serie)
        hab = paths.habillage_dir(a.cible, a.serie)

    if a.srt:
        srt = Path(a.srt)
    else:
        cands = sorted(hab.glob("*karaoke*.srt"))
        if len(cands) != 1:
            sys.exit(f"{len(cands)} SRT karaoke dans {hab} - donner le chemin. "
                     + " | ".join(c.name for c in cands))
        srt = cands[0]

    bornes, segs = frontieres(mdir / "edl_v3_timeline.json")
    tm = mdir / "transcripts-master"
    mj = sorted(tm.glob("*.words.json"))
    if len(mj) != 1:
        sys.exit(f"{len(mj)} words.json de master dans {tm} - il en faut exactement un.")
    mots = json.loads(mj[0].read_text(encoding="utf-8"))
    cues = lire_srt(srt)

    print(f"srt       {srt.name} : {len(cues)} cues")
    print(f"timeline  {len(segs)} segments, donc {len(bornes)} frontieres de phrase")

    enjambent = []
    for c in cues:
        croise = {i for i, s in enumerate(segs)
                  if s["_cum_start"] < c["t1"] - 0.02 and s["_cum_end"] > c["t0"] + 0.02}
        if len(croise) > 1:
            enjambent.append((c, sorted(croise)))

    if not enjambent:
        print("\n[OK] aucune cue n'enjambe une frontiere de phrase.")
        return 0

    print(f"\n[!!] {len(enjambent)} cue(s) MELANGENT DEUX PHRASES :")
    for c, ls in enjambent:
        print(f"   {c['t0']:6.2f}->{c['t1']:6.2f}  L{ls}  \"{c['texte']}\"")

    if not a.appliquer:
        print("\nRien n'est ecrit. `--appliquer` pour recouper.")
        print("Puis refaire les sous-titres incrustes a partir du SRT corrige.")
        return 1

    sortie, coupes = recouper(cues, bornes, mots)
    for avant, g, d in coupes:
        print(f"   recoupe      \"{avant}\"  ->  \"{g}\" + \"{d}\"")
    for emp, g, d in reequilibrer(sortie, mots):
        print(f"   reequilibre  \"{emp}\" descend  ->  \"{g}\" + \"{d}\"")

    trop = [c for c in sortie if len(c["texte"]) > MAX_CAR]
    if trop:
        sys.exit(f"{len(trop)} cue(s) > {MAX_CAR} caracteres apres recoupe, RIEN n'est ecrit : "
                 + " | ".join(c["texte"] for c in trop))

    ecrire_srt(srt, sortie)
    print(f"\nOK -> {srt.name} : {len(cues)} -> {len(sortie)} cues")
    print("ATTENTION toute copie de ce SRT deja utilisee ailleurs n'est PAS mise a jour :")
    print("   refaire les sous-titres incrustes a partir de ce fichier.")
    print("Relancer ce controle SANS --appliquer pour verifier qu'il converge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
