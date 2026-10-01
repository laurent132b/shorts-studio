"""trous_gardes.py - les trous que `pass_split_internal` a decide de GARDER, et ce qu'il y a dedans.

    python tools/trous_gardes.py <slug>
    python tools/trous_gardes.py videos/<slug>/1_montage -o rapport.md

Pourquoi ce fichier existe
--------------------------
Une bonne part des remarques de relecture designe un trou INTERNE a une ligne - « un petit
blanc », « un long euuh », « ca fait genre je bug ». La question qui suit est toujours la
meme : *quels autres trous le montage garde-t-il, et lesquels sont du meme genre ?*

- `cut_plan_report.md` liste bien les refus (« TROU ASR NON MUET »), mais il donne le trou
  ANNONCE par l'ASR et la duree du silence - jamais ce que le trou CONTIENT. Or c'est la
  seule chose qui permette d'arbitrer : a duree egale, un trou rempli d'un souffle passe et
  un trou rempli d'un « euuuh » non.
- `blancs.py` regarde le MASTER et ne voit que ce qui tombe sous -45 dB. Un « euuuh » est a
  -30 dB : il est invisible pour lui. C'est le complement exact de cet outil.

⚠️ IL MESURE ET IL CLASSE. IL NE COUPE RIEN ET NE PROPOSE RIEN - comme `tics.py` et
   `blancs.py`. Ce qui suit dit pourquoi une decision automatique serait fausse.

Pourquoi aucun seuil ne tranche
-------------------------------
Le controle « TROU ASR NON MUET » de `pass_split_internal` existe pour ne pas couper dans
un mot dont l'ASR n'a pas span-e la forme parlee (un « 5.6 » prononce « cinq point six »).
Il est juste. Mais il repond a *« ce trou est-il muet ? »* quand la question utile est
*« ce qui le remplit est-il un MOT ? »* - et une voyelle d'hesitation non transcrite lui
repond « non muet », donc « ne pas couper ».

Les trois discriminants naturels echouent, chacun sur un contre-exemple mesure :
- **la duree du son non transcrit** : les trous juges genants et les autres se recouvrent
  entierement (230 ms genant, 1 025 ms acceptable) ;
- **la platitude** (une hesitation est monotone, un mot est module) : 0,9 dB d'ecart sur un
  trou genant, 1,2 dB sur un trou acceptable ;
- **la position syntaxique** : « votre __ dossier », determinant et nom, 750 ms de son, ne
  gene pas.

**Donc pas de seuil, et pas de correctif d'outil sur cette base.** Cet outil sert a rendre
l'arbitrage POSSIBLE en une lecture, pas a s'en passer.

🔴 UN TROU HABITE PAR UNE QUEUE TRAINEE SE COUPE
-------------------------------------------------
Devant un trou dont le son remplit presque tout, le reflexe est de conclure « il y a de la
parole ici, donc on ne coupe pas » - ce que `pass_split_internal` REFUSE de faire, a raison
sur ses propres termes. Mais quand ce son est la **QUEUE TRAINEE** du mot de gauche (sa
derniere syllabe qui s'etire), ce n'est pas un mot en train de se perdre, c'est une syllabe
qui dure. **Une queue se tronque ; un mot ne se tronque pas.** Le refus de la passe
dit « l'automate ne le fera pas », il ne dit pas « il ne faut pas le faire ».

👉 **LE GESTE QUI TRANCHE** : re-transcrire le CANDIDAT TRONQUE, pas le trou. On extrait le
mot avec la borne qu'on veut poser et on demande a l'ASR ce qu'il entend. L'enveloppe seule
peut laisser croire qu'une consonne finale est coupee alors qu'elle est entiere.

    ffmpeg -ss <debut du mot> -to <borne visee> -i <rush> extrait.wav
    python tools/transcribe.py extrait.wav --engine elevenlabs   # le mot survit-il ?

⚠️ **ET NE PAS LIRE LE CHAMP `text` DU TRANSCRIPT POUR CONCLURE.** Il rend `''` sur un
extrait court ; les mots vivent dans `words`. *Un zero plausible se croit : prouver
l'instrument avec un temoin dont on connait la reponse avant de lire son verdict.*

⛔ **CE QUI N'EST PAS DEDUCTIBLE** : que tout trou habite se coupe. Le meme son se coupe ou
se garde selon ce qu'il SEPARE : un trou a une frontiere de phrase se coupe, un trou au
milieu d'un groupe nominal se garde, a niveau et duree comparables.

⚠️ ET NE PAS LIRE CE TABLEAU COMME UNE LISTE DE DEFAUTS. Sur un tutoriel d'ecran, l'orateur
   se tait pendant qu'il clique, tape, attend : le profil `long` garde la respiration interne
   jusqu'a 700 ms EXPRES. Un trou liste ici est un CANDIDAT, et le defaut le plus probable de
   cet outil serait de faire couper tous les trous listes.

Comment lire les colonnes
-------------------------
| colonne | ce que c'est |
|---|---|
| `annonce` | le trou selon les horodatages ASR - ce que `pass_split_internal` compare a son seuil |
| `silence` | le plus long silence CONTIGU reellement mesure dedans (enveloppe RMS 20 ms) |
| `son` | le plus long run CONTIGU au-dessus de -45 dB dedans : la chose a identifier |
| `moy` / `ecart` | niveau moyen de ce son, et sa dispersion. Un souffle est bas et plat, une syllabe est forte et modulee - mais cf. ci-dessus, ca ne tranche pas |
| `verdict` | ce que `pass_split_internal` a fait, et il a raison sur ses propres termes |

Le seuil compare est celui du PROFIL, jamais une valeur en dur : `internal` vaut 0,50 s en
`short` (le defaut) et 0,70 s en `long` (`--profile long`).

Un `son` FORT dans un trou : mot omis, ou debordement des DEUX cotes ?
----------------------------------------------------------------------
**Quand `son` est FORT - proche du niveau de parole - ce son est-il un MOT que l'ASR a
omis ?** C'est possible : plusieurs centaines de ms de parole a -20 dB sans aucun jeton ASR,
et un `text_ratio` a 1,000 qui ne le voit pas (le controle et sa reference derivent tous deux
des transcripts, ils tombent ensemble).

**Mais le producteur le plus courant est benin : l'ASR deborde ses mots des DEUX cotes.** Il
sous-estime la fin du mot de gauche et l'attaque du mot de droite, donc son « trou » contient
de la parole aux deux bords sans qu'aucun mot ne manque.

Ce qui les separe est la FORME de l'enveloppe, pas un niveau - et ca se voit en une
commande, `enveloppe.py montre <slug> <rush>:<a>:<b> --pas 0.02` :

| | ce qu'on voit |
|---|---|
| **debordement** (benin) | DEUX plateaux de parole, un a chaque bord, separes par un creux CENTRAL |
| **mot omis** | UN plateau, au milieu, avec du silence de part et d'autre |

⚠️ **La forme oriente, elle ne conclut pas : seule la re-transcription ISOLEE tranche**, et
elle coute ~10 s. La faire des que le mot en jeu porte du sens. *La forme dit ou chercher, la
re-transcription dit quoi.*
"""

import argparse
import json
import sys
from pathlib import Path

# La console Windows est en cp1252 : sans ceci, `--help` plante sur les caracteres hors
# latin-1 de cette docstring.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
import paths                                    # noqa: E402
import tighten_cuts as tc                       # noqa: E402

MIN_SON = 0.05          # en dessous, il n'y a rien a identifier dans le trou


def plus_long_son(x, lo, hi):
    """Le plus long run CONTIGU au-dessus du plancher dans `[lo, hi]`, et sa dispersion.

    ⚠️ Symetrique exact de `tighten_cuts.plus_long_silence`, et pour la meme raison : deux
    sondes posees sur les BORDS de l'intervalle ne disent rien de ce qu'il y a entre eux. Il
    faut un balayage qui puisse echouer sur l'ENSEMBLE de l'intervalle.
    """
    best, deb = (lo, lo, 0.0), None
    t = lo
    while t < hi:
        if tc.voiced(x, t):
            if deb is None:
                deb = t
        elif deb is not None:
            if t - deb > best[2]:
                best = (round(deb, 3), round(t, 3), round(t - deb, 3))
            deb = None
        t = round(t + tc.STEP, 3)
    if deb is not None and hi - deb > best[2]:
        best = (round(deb, 3), round(hi, 3), round(hi - deb, 3))
    a, b, d = best
    vals, u = [], a
    while u < b:
        vals.append(tc.db_at(x, u))
        u = round(u + 0.02, 3)
    if not vals:
        return a, b, d, -120.0, 0.0
    moy = sum(vals) / len(vals)
    ecart = (sum((v - moy) ** 2 for v in vals) / len(vals)) ** 0.5
    return a, b, d, moy, ecart


#: L'ordre de PRIORITE d'`assemble.py` et de `tighten_cuts.py` - la meme constante, ecrite
#: dans les deux (« for suffix in (".transcript.json", ".words_v2.json", ".words.json") »).
#:
#: ⚠️ Un rush peut porter PLUSIEURS de ces fichiers, avec les memes mots et des horodatages
#: qui different ; `.transcript.json` est lu en premier par le pipeline, donc il gagne. Lire
#: un autre fichier ici ferait corriger un horodatage dans le fichier PERDANT : tout reussirait
#: et le SRT livre resterait faux. Ne jamais retoucher un `words.json` seul.
PRIORITE_MOTS = (".transcript.json", ".words_v2.json", ".words.json")


def charge_mots(montage, stem):
    """Les mots d'un rush, depuis le fichier que le PIPELINE lirait. `None` si aucun."""
    for suffixe in PRIORITE_MOTS:
        chemin = montage / "transcripts" / f"{stem}{suffixe}"
        if chemin.exists():
            d = json.loads(chemin.read_text(encoding="utf-8"))
            return d["words"] if isinstance(d, dict) else d
    return None


def resout_montage(cible, serie):
    """Un dossier `1_montage`, ou un slug resolu par paths.py."""
    p = Path(cible)
    if p.is_dir() and (p / "edl_v3_timeline.json").exists():
        return p
    v = paths.video_dir(cible, serie) if serie else paths.video_dir(cible)
    m = Path(v) / "1_montage"
    if not (m / "edl_v3_timeline.json").exists():
        sys.exit(f"pas de edl_v3_timeline.json sous {m} - l'EDL n'a pas ete planifie ?")
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cible", help="slug de la video, ou son dossier 1_montage")
    ap.add_argument("--serie", help="ignore (conserve pour compatibilite)")
    ap.add_argument("--profile", choices=sorted(tc.PROFILS),
                    help="profil de rythme (short|long) ; defaut short")
    ap.add_argument("--seuil", type=float,
                    help="force le seuil interne (s) au lieu de celui du profil")
    ap.add_argument("-o", "--out", help="ecrire le markdown ici plutot que sur la sortie")
    args = ap.parse_args()

    montage = resout_montage(args.cible, args.serie)
    tl = montage / "edl_v3_timeline.json"

    profil = args.profile or tc.profil_par_defaut(tl)
    seuil = args.seuil if args.seuil is not None else tc.PROFILS[profil]["internal"]

    segs = json.loads(tl.read_text(encoding="utf-8"))
    segs = segs["segments"] if isinstance(segs, dict) else segs
    ff = tc.resolve_ffmpeg()

    mots_de = {}
    lignes = []
    for s in segs:
        stem = s["stem"]
        if stem not in mots_de:
            mots = charge_mots(montage, stem)
            if mots is None:
                continue
            mots_de[stem] = mots
        w = [z for z in mots_de[stem]
             if z["start"] >= s["in_ext"] - 1e-3 and z["end"] <= s["out_ext"] + 1e-3]
        for a, b in zip(w, w[1:]):
            annonce = b["start"] - a["end"]
            if annonce <= seuil:
                continue
            # ⚠️ `source` est RELATIF a la racine du depot, pas au repertoire courant. Le
            # resoudre depuis le dossier de montage plutot que depuis le `cwd` rend l'outil
            # utilisable quel que soit l'endroit d'ou on le lance.
            src = Path(s["source"])
            if not src.is_absolute():
                src = montage.parent.parent.parent / src
            x = tc.load_audio(ff, str(src))
            _, _, vrai = tc.plus_long_silence(x, a["end"], b["start"])
            _, _, dson, moy, ecart = plus_long_son(x, a["end"], b["start"])
            lignes.append(dict(i=s["i"], part=s.get("part"), stem=stem,
                               t=round(a["end"], 3), annonce=annonce, silence=vrai,
                               son=dson, moy=moy, ecart=ecart,
                               avant=a["text"], apres=b["text"],
                               coupe=vrai > seuil))

    lignes.sort(key=lambda r: -r["son"])
    coupes = [r for r in lignes if r["coupe"]]
    gardes = [r for r in lignes if not r["coupe"]]

    out = [f"# Trous internes gardes - {montage.parent.name}", "",
           f"Profil **{profil}**, seuil interne **{seuil:.2f} s** "
           f"(`tighten_cuts.PROFILS['{profil}']['internal']`).", "",
           f"**{len(lignes)}** trou(s) annonce(s) au-dela du seuil : "
           f"**{len(coupes)}** coupe(s) par `pass_split_internal`, "
           f"**{len(gardes)}** GARDE(s) parce que le silence reel y est plus court "
           f"que le trou annonce.", "",
           "[!] Un trou liste ici est un **candidat**, pas un defaut : le profil `long` "
           "garde la respiration interne expres. Aucun des trois discriminants essayes "
           "ne separe les trous signales des autres - cf. l'en-tete de l'outil.", "",
           "| son | annonce | silence | moy dB | ecart | ligne | entre ... et |",
           "|---:|---:|---:|---:|---:|---|---|"]
    for r in gardes:
        if r["son"] < MIN_SON:
            continue
        part = "" if r["part"] is None else f".{r['part']}"
        out.append(f"| **{r['son'] * 1000:.0f} ms** | {r['annonce'] * 1000:.0f} | "
                   f"{r['silence'] * 1000:.0f} | {r['moy']:.1f} | {r['ecart']:.1f} | "
                   f"L{r['i']}{part} `{r['stem']}` {r['t']:.2f} | "
                   f"'{r['avant']}' ... '{r['apres']}' |")

    if coupes:
        out += ["", "## Deja coupes par l'outil (pour memoire)", ""]
        for r in coupes:
            out.append(f"- L{r['i']} `{r['stem']}` {r['t']:.2f} - "
                       f"{r['silence'] * 1000:.0f} ms de silence reelle entre "
                       f"'{r['avant']}' et '{r['apres']}'")

    texte = "\n".join(out) + "\n"
    if args.out:
        Path(args.out).write_text(texte, encoding="utf-8")
        print(f"{len(gardes)} trou(s) garde(s) -> {args.out}")
    else:
        print(texte)
    return 0


if __name__ == "__main__":
    sys.exit(main())
