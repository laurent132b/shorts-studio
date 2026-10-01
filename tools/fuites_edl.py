# -*- coding: utf-8 -*-
"""tools/fuites_edl.py - un MOT DE TROP est-il entre dans le montage ?

Ce que ca attrape, et qu'aucun autre controle du depot ne voit
--------------------------------------------------------------
`select_takes.py` choisit une FENETRE DE MOTS contigus dans un rush. Rien ne garantit que
cette fenetre s'arrete exactement sur la ligne du script : elle peut embarquer un mot de
plus a une extremite. Le montage s'assemble, la duree est juste, le SRT est coherent, et
`qa_montage.py` ne dit rien, parce que tous ces controles font confiance a la fenetre.

Le cas le plus couteux est le marqueur de PRISE : "on garde la derniere" / "on refait" dit
a voix haute apres une prise. Si la fenetre de la ligne precedente s'etend d'un mot, le
montage sort avec un "On" colle a la fin de la phrase. Le `text_ratio` ne peut pas le
dire : un mot de trop sur une ligne le laisse vers 0,96, en pleine plage ou la
normalisation ASR en produit aussi sur des lignes completes. Il faut comparer le texte du
script aux mots reellement couverts par la fenetre.

Les trois pieges de la comparaison
----------------------------------
1. **Comparer les TEXTES ne marche pas**, il faut comparer des SEQUENCES DE TOKENS. Le
   script est ecrit a la main, l'ASR normalise : accents, apostrophes, ponctuation et casse
   different sans qu'un mot manque.

2. **UN MOT DE L'ASR PEUT PORTER PLUSIEURS TOKENS.** "Royaume-Uni," rend
   `["royaume", "uni"]`, "d'etendre" rend `["d", "etendre"]`. Une comparaison mot-a-mot
   qui ne garde que le PREMIER token de chaque mot declare des fuites partout, et un
   controle qui crie a tort cesse d'etre lu. On aplatit donc les tokens des deux cotes, et
   on retrouve la sequence du script comme sous-sequence CONTIGUE de celle de la fenetre.

3. **UN TOKEN QUI EST DE LA PONCTUATION POUR LA NORMALISATION ET UN MOT DANS LA BOUCHE.**
   `%` se prononce "pour cent", mais la normalisation l'efface du script ET de la fenetre :
   une fenetre arretee juste apres "20" se retrouve parfaitement, `text_ratio` vaut 1,000,
   et le montage ampute le chiffre-cle de la phrase. Un controle et sa reference qui
   derivent d'une seule normalisation ne se controlent plus mutuellement.

   Le seul temoin possible compare les BORNES du token aux bornes de la FENETRE. C'est le
   SCRIPT qui dit si le symbole a ete prononce : si la ligne de `final_transcript.json`
   ECRIT un symbole de `SYMBOLES_PRONONCES` (`%`, `$`, `+`, `=`, ...), on cherche dans le
   rush un token de ce texte exact, VOISIN de la fenetre (a moins d'une seconde d'un bord)
   et dont le milieu tombe DEHORS : c'est un `SYMBOLE OMIS`, compte comme une fuite.

   Ce qu'il ne voit pas : un symbole que le script N'ECRIT PAS ("vingt pour cent" redige en
   lettres alors que l'ASR a rendu "20 %") est un ECART DE LEXIQUE (cas 1). Et un token
   symbole a plus d'une seconde de la fenetre est une autre prise, pas une troncature.

Ce qu'il rend
-------------
Pour chaque ligne, les mots de la fenetre situes AVANT ou APRES la sequence du script, avec
leur timecode source, donc directement la borne a poser. La reparation ne se fait pas en
retouchant `words.json` (`select_takes` et `assemble` re-clampent la duree des mots, la
retouche est ignoree en silence) mais par un override cible et rejouable sur `start`/`end`
de l'EDL, AVANT `assemble.py --plan-only`.

    python tools/fuites_edl.py "videos/<slug>/1_montage"
    python tools/fuites_edl.py "videos/<slug>/1_montage" --edl edl_v3_timeline.json

ATTENTION a lancer sur `edl_v3.json` (le defaut), c'est-a-dire AVANT la planification : c'est la
que la borne se corrige a moindre frais. Sur `edl_v3_timeline.json` il marche aussi, mais
`in_ext`/`out_ext` ont deja bouge et la fuite peut y avoir ete rognee par `tighten_cuts`
sans que la cause soit reglee.

Ce qu'il signale, et quoi en faire
----------------------------------
| ce que ca signale | ce que c'est | quoi en faire |
|---|---|---|
| `FUITE` (tete ou queue) | un vrai mot de trop dans la fenetre | poser la borne, c'est le cas qui compte |
| `ECART DE LEXIQUE` | le script et la parole different sur un mot, aucun ne manque ("4" ecrit / "quatre" dit) | rien, ce n'est pas une troncature |
| sequence introuvable | mot(s) manquant en bout, ou mot mal transcrit | regarder ; c'est parfois la perte du DERNIER MOT |

Sur une video longue montee par groupes de souffle, les fenetres voisines se TOUCHENT par
construction et il signale davantage. Il vaut le plus sur un short, ou une ligne est une
phrase autonome.

**Il POINTE, il ne DECIDE pas** (meme parti pris que `blancs.py` et `tics.py`) : savoir si
un mot manque au sens est editorial.

SON ENTREE EST L'EDL, PAS UNE TIMELINE PLANIFIEE
------------------------------------------------
`--edl` attend `edl_v3.json`, dont les bornes sont `start`/`end`. Sur
`edl_v3_timeline.json`, l'outil rend un FAUX POSITIF sur toute ligne que
`pass_split_internal` a SCINDEE ; le signe a reconnaitre est un message qui se contredit
("le dernier mot utile finit a X (la fenetre va jusqu'a X)", deux valeurs egales). Donc :
  - **auditer l'EDL**, et lire une sortie sur timeline comme indicative ;
  - une borne posee sur `end` n'est PAS la borne coupee : `tighten_cuts` recalcule
    `out_ext` apres. Cet outil ne peut donc pas prouver qu'un override a tenu.

L'AUDIT DE L'EDL NE SUFFIT PAS
------------------------------
La fenetre de MOTS est `[start, end]` ; ce que ffmpeg encode est `[in_ext, out_ext]`, que
`compute_cuts` elargit d'une MARGE de chaque cote. La coupe est donc plus large que ce que
cet outil regarde, et le mot voisin peut tomber dans la marge alors qu'il rend "aucune
fuite". Les horodatages ne suffisent pas a l'exclure : l'ASR date un mot sur sa partie
VOISEE, jamais sur la salve d'une plosive, qui peut deborder de quelques ms dans la coupe
et faire entendre l'attaque du mot suivant.

La seule methode qui tranche : re-transcrire l'extrait EXACT que ffmpeg encodera.

    ffmpeg -v error -ss <in_ext> -to <out_ext> -i <rush> -vn -c:a aac extrait.m4a
    python tools/transcribe.py extrait.m4a --engine elevenlabs --profile karaoke --language fr

Cet outil trie donc les lignes a regarder, il ne clot pas la question. Sur toute ligne dont
le mot voisin est un marqueur de prise ou un faux depart, prouver la coupe par
re-transcription (~10 s). Le correctif se pose alors sur `out_ext` de la TIMELINE, pas sur
`end` de l'EDL.

Codes de sortie : `0` aucune fuite, `1` au moins une, `2` erreur d'entree.
"""

# Sans ceci, `--help` plante sur une console Windows cp1252 des que la docstring porte un
# caractere hors latin-1.
import sys as _sys

for _f in (_sys.stdout, _sys.stderr):
    try:
        _f.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path


# Les symboles qui s'ECRIVENT en un caractere et se PRONONCENT en un ou plusieurs mots. La
# normalisation de `tokens()` les efface des deux cotes, donc seul un controle de BORNES peut
# voir qu'un tel token est reste dehors (piege 3 de la docstring). La liste s'etend a la
# main, jamais par une regex de ponctuation.
SYMBOLES_PRONONCES = "%€$£°+="
VOISINAGE_SYMBOLE_S = 1.0


def tokens(texte):
    """Sequence de tokens comparables : minuscules, sans accents, sans ponctuation.

    Meme normalisation que `select_takes.norm`, deliberement : ce controle doit juger la
    fenetre avec les memes yeux que l'outil qui l'a choisie.
    """
    t = texte.lower()
    t = "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")
    return [x for x in re.sub(r"[^a-z0-9 ]", " ", t).split() if x]


def charge_mots(dossier_transcripts, stem, cache):
    """Les mots d'un rush. `transcript.json` d'abord : c'est celui que lisent les outils."""
    if stem in cache:
        return cache[stem]
    for suffixe, cle in ((".transcript.json", "words"), (".words.json", None)):
        f = dossier_transcripts / f"{stem}{suffixe}"
        if f.exists():
            d = json.loads(f.read_text(encoding="utf-8"))
            cache[stem] = d[cle] if cle and isinstance(d, dict) else d
            return cache[stem]
    cache[stem] = []
    return cache[stem]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("montage", type=Path, help="dossier 1_montage de la video")
    ap.add_argument("--edl", default="edl_v3.json",
                    help="EDL a auditer (defaut edl_v3.json, AVANT planification)")
    ap.add_argument("--script", default="final_transcript.json")
    ap.add_argument("--transcripts", default="transcripts")
    ap.add_argument("--tolerance", type=float, default=0.02,
                    help="marge en s pour decider qu'un mot est DANS la fenetre (defaut 0,02)")
    args = ap.parse_args()

    montage = args.montage.resolve()
    f_edl = montage / args.edl
    f_script = montage / args.script
    d_tr = montage / args.transcripts
    for f in (f_edl, f_script):
        if not f.exists():
            print(f"introuvable : {f}", file=sys.stderr)
            return 2
    if not d_tr.is_dir():
        print(f"introuvable : {d_tr}", file=sys.stderr)
        return 2

    edl = json.loads(f_edl.read_text(encoding="utf-8"))
    segs = edl["segments"] if isinstance(edl, dict) else edl
    script = {d["i"]: d["text"] for d in json.loads(f_script.read_text(encoding="utf-8"))}
    cache = {}

    print(f"{f_edl.name} : {len(segs)} segment(s)  |  {f_script.name} : {len(script)} ligne(s)\n")

    # ⚠️ ON GROUPE LES SEGMENTS PAR `i` AVANT DE COMPARER, et c'est indispensable.
    #
    # Une ligne de script peut etre montee en PLUSIEURS segments : `pass_split_internal` de
    # `tighten_cuts.py` scinde un segment pour retirer un silence interne, et un scindage
    # manuel fait la meme chose pour retirer une hesitation au MILIEU d'une ligne (deux
    # entrees complementaires sur la meme ancre).
    #
    # Comparer CHAQUE segment a la ligne ENTIERE ferait donc "manquer" des mots a chacune des
    # moities, par construction, et la ligne apparaitrait une fois par moitie. Une PHRASE,
    # ce sont les segments consecutifs de meme `i`, jamais un segment de timeline.
    groupes = []
    for s in segs:
        if groupes and groupes[-1][0] == s["i"]:
            groupes[-1][1].append(s)
        else:
            groupes.append((s["i"], [s]))

    fuites = 0
    introuvables = 0
    symboles_omis = 0
    for i, membres in groupes:
        if i not in script:
            print(f"  L{i:<3} pas de ligne de script pour ce segment - ignore")
            continue
        stems = {Path(s.get("source", "")).stem for s in membres}
        if any(not charge_mots(d_tr, st, cache) for st in stems):
            print(f"  L{i:<3} aucun transcript pour {'/'.join(sorted(stems))} - non mesurable")
            continue

        # Les mots de la FENETRE, bornes sur les temps de MOT (start/end), pas sur les
        # bornes de coupe : la fuite est un choix de select_takes, pas de tighten_cuts.
        #
        # ⚠️ UN MOT APPARTIENT A LA FENETRE SI SON MILIEU Y TOMBE, pas s'il y est ENTIEREMENT
        # contenu : la borne `end` de l'EDL tombe souvent DANS le dernier mot (convention de
        # `select_takes`), et exiger `m["end"] <= fin` signalerait le dernier mot de presque
        # toutes les lignes comme une troncature.
        fen = []
        for s in membres:
            mots = charge_mots(d_tr, Path(s.get("source", "")).stem, cache)
            deb, fin = s["start"], s["end"]
            fen += [m for m in mots
                    if deb - args.tolerance <= (m["start"] + m["end"]) / 2 <= fin + args.tolerance]
        if not fen:
            continue
        deb = min(s["start"] for s in membres)
        fin = max(s["end"] for s in membres)
        stem = "+".join(sorted(stems))

        # -- Piege 3 : un SYMBOLE que le script ECRIT, et qu'aucune fenetre n'inclut. On ne
        # passe pas par `tokens()`, qui l'efface : on compare les BORNES du token du rush a
        # celles de la fenetre. Un symbole a plus de VOISINAGE_SYMBOLE_S d'un bord est une
        # autre prise, pas une troncature : on ne le signale pas.
        attendus_sym = [c for c in script[i] if c in SYMBOLES_PRONONCES]
        if attendus_sym:
            dedans = {id(m) for m in fen}
            for s in membres:
                mots = charge_mots(d_tr, Path(s.get("source", "")).stem, cache)
                for m in mots:
                    if m["text"].strip() not in attendus_sym or id(m) in dedans:
                        continue
                    apres = s["end"] < m["start"] <= s["end"] + VOISINAGE_SYMBOLE_S
                    avant = s["start"] - VOISINAGE_SYMBOLE_S <= m["end"] < s["start"]
                    if not (apres or avant):
                        continue
                    symboles_omis += 1
                    ou = (f"HORS de la fenetre, qui finit a {s['end']:.3f}" if apres
                          else f"HORS de la fenetre, qui commence a {s['start']:.3f}")
                    geste = (f"etendre `end` a {m['end']:.3f}" if apres
                             else f"ramener `start` a {m['start']:.3f}")
                    print(f"  L{i:<3} SYMBOLE OMIS  \"{m['text'].strip()}\""
                          f" ({Path(s.get('source', '')).stem} {m['start']:.3f} -> {m['end']:.3f})"
                          f" est {ou}")
                    print(f"        -> le script l'ecrit, donc il a ete prononce : {geste}"
                          " (override sur l'EDL, avant --plan-only)")

        # -- On APLATIT les tokens, en gardant qui appartient a quel mot (piege 2).
        toks, proprio = [], []
        for k, m in enumerate(fen):
            for t in tokens(m["text"]):
                toks.append(t)
                proprio.append(k)
        attendu = tokens(script[i])
        if not attendu:
            continue

        # La sequence du script doit etre une sous-sequence CONTIGUE de celle de la fenetre.
        pos = -1
        for d in range(len(toks) - len(attendu) + 1):
            if toks[d:d + len(attendu)] == attendu:
                pos = d
                break
        if pos < 0:
            introuvables += 1
            # On distingue la classe la plus FREQUENTE des non-defauts : le script et la
            # parole different sur un MOT, sans qu'aucun mot manque : "4" ecrit / "quatre"
            # dit, "des problemes" ecrit / "les problemes" dit.
            # Un ecart de lexique n'est PAS une troncature, et les confondre fait chercher
            # une borne de coupe la ou il n'y a qu'une divergence d'ecriture.
            k = next((k for k in range(min(len(attendu), len(toks)))
                      if attendu[k] != toks[k]), None)
            substitution = (k is not None
                            and attendu[k + 1:] == toks[k + 1:]
                            and len(attendu) == len(toks))
            if substitution:
                print(f"  L{i:<3} ECART DE LEXIQUE  script \"{attendu[k]}\" / dit "
                      f"\"{toks[k]}\"  - aucun mot ne manque")
            else:
                print(f"  L{i:<3} ?? la sequence du script ne se retrouve PAS dans la fenetre")
                print(f"        script  : {' '.join(attendu)}")
                print(f"        fenetre : {' '.join(toks)}")
                print("        -> mot(s) manquant en bout, ou mot mal transcrit :"
                      " a regarder, pas a ignorer")
            continue

        tete = sorted(set(proprio[:pos]))
        queue = sorted(set(proprio[pos + len(attendu):]))
        if not tete and not queue:
            continue

        fuites += 1
        print(f"  L{i:<3} FUITE  ({stem}, fenetre {deb:.3f} -> {fin:.3f})")
        if tete:
            print(f"        TETE  {[fen[k]['text'] for k in tete]}"
                  f"  -> le 1er mot utile commence a {fen[proprio[pos]]['start']:.3f}")
        if queue:
            dernier = fen[proprio[pos + len(attendu) - 1]]
            print(f"        QUEUE {[fen[k]['text'] for k in queue]}"
                  f"  -> le dernier mot utile finit a {dernier['end']:.3f}"
                  f" (la fenetre va jusqu'a {fin:.3f})")

    print()
    if fuites or introuvables or symboles_omis:
        print(f"[!!] {fuites} fuite(s), {introuvables} sequence(s) introuvable(s),"
              f" {symboles_omis} symbole(s) omis.")
        print("     Corriger par un override CIBLE sur start/end de l'EDL, AVANT")
        print("     `assemble.py --plan-only`. Ne PAS retoucher words.json : select_takes et")
        print("     assemble re-clampent la duree des mots, la retouche est ignoree en silence.")
        return 1
    print("[OK] aucune fuite : chaque fenetre couvre exactement sa ligne de script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
