"""tighten_cuts.py - affine les points de coupe d'un montage-rushs.

Se place ENTRE un premier `assemble.py` et un second `assemble.py --honor-cuts` :

    python tools/assemble.py  <input>/edl_v3.json --out <out>/<video>_montage.mp4
    python tools/tighten_cuts.py <input>/edl_v3_timeline.json
    python tools/assemble.py  <input>/edl_v3_timeline.json --honor-cuts --out <out>/<video>_montage.mp4

Tout est mesure sur l'enveloppe RMS 20 ms des rushs source, jamais sur les
timestamps de l'ASR - qui mentent de plusieurs facons distinctes :

1. PRE-ROLL D'ATTAQUE. `assemble.py` pose `in_ext = start` sans marge des que le
   silence precedent depasse MAX_GAP_EXTEND_S - donc sur presque toutes les lignes
   d'un jump cut. Or l'ASR date un mot sur sa partie VOISEE : la plosive d'attaque
   tombe avant (jusqu'a 150 ms perdus : "Dis-moi" -> "is-moi").

1bis. VIDE EN TETE, le biais exactement INVERSE du precedent. L'ASR peut aussi etre
   EN AVANCE : le timestamp du premier mot apres un silence PRECEDE la parole, et la
   coupe ouvre sur du vide. Les deux passes tirent le meme bord en sens opposes, donc
   l'ordre compte - cf. l'entete de `pass_dead_air`. Le segment 0 n'est examine par
   aucune autre passe (le resserrage ne voit que les raccords).

2. TRONCATURE ASR. L'ASR normalise certaines prononciations en forme ecrite et
   leur donne une fenetre trop courte : "slash design tiret sync" devient
   "/design-sync" et perd 1,1 s ; "etc." (et cetera) perd 0,45 s. On detecte le
   cas sans ambiguite : de l'audio VOISE, contigu a la parole du segment, qui
   n'est couvert par AUCUN mot du transcript. Une prise jetee, elle, est toujours
   separee par un vrai silence ET suivie de mots transcrits.
   ⚠️ SAUF DERRIERE UNE SCISSION DE `tics.py` : l'hesitation retiree est de l'audio voise,
   contigu et non transcrit, donc cette passe la RE-COLLE au fragment de gauche (quelques
   dizaines de ms d'attaque du "euh", qui s'entendent). Borner ce `out_ext` au creux
   mesure, APRES cette passe.

3. SILENCE MORT AUX RACCORDS. Le montage laisse 0,3-0,5 s entre deux phrases la
   ou 0,14 s suffit. Resserrage par TYPE de raccord (voir TARGETS) : uniforme, on
   ferait fondre la structure du script ; par type, on garde une respiration la ou
   elle porte du sens (changement de point, entree du CTA).

4. QUEUE NON PARLEE. Le resserrage borne la queue sur le dernier SON, pas sur la
   derniere PAROLE - et du son n'est pas de la parole. Une respiration, un
   frottement de vetement, le bruit d'un mouvement passent le plancher a -45 dB et
   sont conserves. Aucune mesure audio ne peut les distinguer ; le transcript, si.

5. SILENCE INTERNE A UN SEGMENT. Ce n'est pas un mensonge de l'ASR mais un fait de
   tournage : une prise contient des respirations, et le montage les garde. Toutes
   les passes ci-dessus travaillent sur les BORNES ; `pass_split_internal` coupe le
   segment en deux, et c'est le resserrage (3) qui ramene ensuite le nouveau raccord
   a sa cible.

Regle dure commune : on ne rogne QUE du silence mesure, jamais de la parole, et on
ne fait que REDUIRE un raccord existant (l'elargir rouvrirait sur une prise jetee).

UNE PASSE ACOUSTIQUE DOIT RESPECTER LA FRONTIERE DES MOTS
---------------------------------------------------------
Plusieurs passes tirent les memes bords, et quand deux passes se contredisent par
construction, la derniere gagne en silence. D'ou des planchers poses a la frontiere des
mots, ou la MESURE prime sur le seuil de voisement : deux sur le bord droit
(`_tail_floor`, pose par `pass_truncation`, et `_mot_fin`, qui protege les fricatives
sourdes finales), un sur le bord gauche (`pass_preroll` ne recule pas dans le mot
precedent). Pour juger un correctif, COMPTER le symptome sur l'ensemble ("combien de
coupes tombent dans un mot ?") plutot que reecouter un cas.

MOT INVENTE ET MOT AVANCE : DEUX MENSONGES QUE LES GARDE-FOUS LAISSENT PASSER
----------------------------------------------------------------------------
- L'ASR peut declarer un mot jamais prononce (le signal y est a -90/-100 dB, sous le
  plancher d'un micro debruite). `pass_truncation` etend alors `out_ext` vers ce mot, et
  laisse une longue queue morte. Signe : trier les extensions `MOT ENTIER` de
  `cut_plan_report.md` par taille ; un mot fantome est une ABERRATION de la distribution
  (dizaines de ms partout, et une valeur cinq fois plus grande), pas une valeur haute.
- L'ASR peut placer un mot plusieurs centaines de ms AVANT sa prononciation. Au-dela de
  `MAX_DEAD_AIR`, `dead_air_head` juge la mesure douteuse et ne coupe pas : le segment ouvre
  sur du silence numerique. Signe : `tools/blancs.py`, colonne `min` des plages MORTES ;
  sous -100 dB ce n'est plus du fond de piece, c'est le FICHIER qui est vide.
Dans les deux cas le remede est un override CIBLE sur la timeline, resolu par le TEXTE et
qui refuse d'ecrire si la timeline n'est plus dans l'etat attendu - pas un changement de
seuil ici. Pour un mot avance, recaler aussi son horodatage dans le transcript, sinon il
sort du SRT (`assemble.py` construit le SRT depuis les mots).

CE QUE LES PASSES D'ICI LAISSENT OU RAMENENT
--------------------------------------------
a) `pass_preroll` peut RAMENER un mot voisin, entier ou ampute : sa remontee traverse les
   creux de moins de `PLOSIVE_HOLE` (80 ms, pour ne pas couper dans l'occlusion d'une
   plosive), et une frontiere entre deux mots peut etre plus courte. Aucun rapport ne le
   voit ; re-transcrire l'extrait exact (`tools/transcribe.py`) le montre.
b) `pass_truncation` (`MOT ENTIER`) laisse la TRAINE du debordement ASR : elle ramene
   `out_ext` sur la fin DECLAREE du mot, que l'ASR date souvent plus tard que sa fin
   acoustique. Un blanc mort de raccord dont la duree ressemble a l'extension du segment de
   gauche vient de la. A distinguer du mot fantome : ici la distribution des extensions est
   homogene.
Le geste : un override cible sur la timeline, apres cette passe, avec la borne a 60 ms
apres la fin acoustique mesuree a l'enveloppe (le relachement d'une consonne finale dure
30-60 ms : seuil ACOUSTIQUE, valable quel que soit le format). Un override de borne doit
ramener AUSSI le palier de punch concerne quand le punch est deja pose : `assemble`
construit chaque sous-plan avec `s.get("out", out_ext)`, donc un dernier palier laisse en
arriere remet la matiere retiree, sans erreur.

LE TROU DE L'ASR DIVERGE DU SILENCE REEL DANS LES DEUX SENS
-----------------------------------------------------------
Le trou entre deux mots ASR peut SOUS-estimer le silence reel (l'ASR deborde ses mots) ou
le SUR-estimer (il n'y a aucun silence la ou l'ASR en annonce 200 ms). Sous-estime, une
recherche de silence conclut "rien a couper" ; sur-estime, on tronque le script, et
`pass_preroll` remonte ensuite dans la queue du mot precedent faute de creux : une syllabe
amputee se retrouve collee en tete de segment. Avant de retirer un mot par troncature ou
scission, mesurer le silence REEL a ses bords (`plus_long_silence`, ou
`tools/enveloppe.py montre <ref> <rush>:<a>:<b> --pas 0.01`), jamais le trou ASR.

Un creux peut aussi exister sans passer le plancher de -45 dB (fond a -43 dB entre deux
mots a -15 dB) : aucune passe automatique ne s'y arrete. La borne se pose alors a la main,
puis s'eprouve en re-transcrivant l'extrait isole.

`MIN_DEAD_AIR` est GLOBAL aux formats : ne pas le baisser sous 0,08 sans mesure sur tous.

Un `cut_plan_report.md` perime reste credible (chiffres complets et plausibles) : comparer
son mtime a celui du master avant de le lire comme l'etat du livrable.
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
import subprocess
import sys
import unicodedata
from pathlib import Path

import numpy as np

import paths

TOOLS_DIR = Path(__file__).resolve().parent

SR = 16000
# Au-dessus de ce niveau : de la parole. En dessous, PAS forcement du silence : le fond de
# piece d'un micro debruite est vers -76/-82 dB, et tout ce qui vit entre -76 et -45 est du
# souffle, une amorce de consonne, de la PRESENCE. Le seuil est juste pour son usage
# ("est-ce de la parole ?") et ne doit pas bouger : toutes les passes en dependent. Le
# souffle n'est pas un defaut a corriger, c'est ce qui rend une pause acceptable a
# l'oreille (voir `tools/blancs.py`).
FLOOR_DB = -45.0          # au-dessus : de la parole. En dessous : pas forcement du silence.
FRAME = 0.02
STEP = 0.01

MAX_PREROLL = 0.15        # on ne recule jamais plus (sinon on ramene la respiration)
MIN_PREROLL = 0.03        # en dessous, inaudible : on ne touche pas
#  Creux traverse en remontant vers l'attaque : une occlusion de plosive dure 20-60 ms,
#  un vrai blanc bien plus. Sans ca, la remontee s'arrete ENTRE la salve du /d/ et la
#  voyelle, et « Decouvre » s'entend « couvre ».
PLOSIVE_HOLE = 0.08
#  Ce qu'on garde APRES le dernier mot d'un segment. Le resserrage borne la queue sur le
#  dernier SON, pas sur la derniere PAROLE - et du son n'est pas de la parole : une
#  respiration, un frottement de vetement, le bruit d'un mouvement passent le plancher a
#  -45 dB et sont conserves. 0,15 s : le relachement d'une consonne finale dure 30-60 ms,
#  on est large ; 0,20 laissait voir le debut d'un geste qui culmine ~180 ms apres le mot.
#
#  ⚠️ SUR LE DERNIER SEGMENT, CETTE QUEUE EST LA FIN DE LA VIDEO. Le reglage est pense pour
#  un RACCORD, ou le segment suivant masque la queue ; sur le dernier il n'y a pas de
#  suivant, et une inspiration de fin de prise reste audible. La borner par un override
#  cible sur la timeline. Le plancher `_tail_floor` peut d'ailleurs l'empecher : rien ici
#  ne distingue un souffle mesure d'un mot non transcrit.
TAIL_AFTER_SPEECH = 0.15
#
# ⚠️ L'ECART ENTENDU A UN RACCORD N'EST PAS LA CIBLE DE `TARGETS` : il vaut cible + cette
#    queue + le pre-roll d'attaque de la ligne suivante (`MAX_PREROLL`, 0,15 aussi), soit
#    un plancher de 0,15 a 0,30 s d'ecart mot-a-mot. Sur un short, c'est souvent trop aux
#    FINS DE PHRASE. Descendre cette valeur fait couper dans le relachement de la consonne
#    finale ; la piste la plus etroite serait de la rendre dependante du TYPE de raccord,
#    comme `TARGETS`, et de ne toucher que le profil `short`.

# --- vide en tete : le biais INVERSE du pre-roll -------------------------------------
# L'ASR peut aussi etre EN AVANCE : le timestamp du premier mot apres un silence PRECEDE
# la parole reelle, et la coupe ouvre sur du vide. Pour le premier segment c'est
# STRUCTUREL : le resserrage parcourt `zip(edl, edl[1:])`, donc le segment 0 n'est jamais
# le `b` d'un raccord et sa tete n'est examinee par personne.
#
# Le vide est mesure avec `sound_start`, donc avec le plancher ABSOLU (FLOOR_DB) que tout
# le reste du fichier utilise. ⚠️ Deux passes qui tirent le MEME bord doivent partager la
# meme definition de "voise", sinon elles oscillent : un seuil relatif ici ferait avancer
# la coupe que `pass_preroll` ramenerait aussitot.
MIN_DEAD_AIR = 0.08       # en dessous, ce n'est pas audible comme un vide
MAX_DEAD_AIR = 0.60       # garde-fou : au-dela on suspecte une mesure ratee, on ne coupe pas
DEAD_AIR_MARGIN = 0.02    # on garde 20 ms avant l'attaque, comme le pre-roll

# --- silence INTERNE a un segment --------------------------------------------------
#  Silence INTERNE a un segment au-dela duquel on le coupe en deux : vers 0,66-0,70 s il
#  s'entend comme un blanc, vers 0,48 s et en dessous comme une respiration.
MAX_INTERNAL_GAP = 0.50

MAX_TRUNC_EXTEND = 1.60   # extension max sur une troncature ASR
# Silence tolere a l'interieur d'une zone voisee orpheline. Doit rester SOUS une
# vraie pause inter-phrase (~0.20 s) : au-dessus, la detection franchit le blanc
# et va mordre sur la phrase suivante (constate a 0.25 : le micro-souffle apres
# "etc." tombait a 20 ms). Les trous internes d'une meme utterance font < 0.10 s.
TRUNC_MAX_HOLE = 0.12
TRUNC_MIN_GAIN = 0.05     # en dessous, pas la peine

# Silence vise (s) selon le type de raccord
TARGETS = {
    "suite": 0.10,        # continuation intra-phrase : virgule, enumeration
    "phrase": 0.14,       # nouvelle phrase dans le meme bloc
    "section": 0.20,      # changement de partie, entree du CTA
}

# Un raccord est "section" si la ligne SUIVANTE ouvre sur un de ces marqueurs.
SECTION_MARKERS = (
    "tout d'abord", "premierement", "deuxiemement", "troisiemement",
    "quatriemement", "cinquiemement", "et enfin", "enfin,", "ensuite",
    "pour finir", "dernier point", "dis-moi", "abonne-toi", "commente",
    # pivots recurrents : "si tu veux" ouvre souvent un CTA, "et aujourd'hui" un
    # nouveau bloc, "et n'hesite pas" un CTA abonnement.
    "si tu veux", "et aujourd'hui", "aujourd'hui,", "et n'hesite pas",
)

SENTENCE_END = re.compile(r"[.!?…]\s*$")

# --- PROFILS DE FORMAT -------------------------------------------------------------
# LA LIGNE DE PARTAGE :
#   - un seuil ACOUSTIQUE ne depend pas du format. Le relachement d'une consonne
#     finale dure 30-60 ms sur un short comme sur un long, l'occlusion d'une plosive
#     20-60 ms. -> ces seuils restent GLOBAUX (MAX_PREROLL, PLOSIVE_HOLE, FLOOR_DB,
#     MIN/MAX_DEAD_AIR, TRUNC_*). Les profiler serait du bruit.
#   - un seuil EDITORIAL est un choix de RYTHME. Ce qui s'entend comme "un blanc"
#     plutot que "une respiration" depend de la duree de la video et de la posture
#     du spectateur. -> par profil.
PROFILS = {
    # short : le profil par defaut.
    #
    # ⚠️ SUR UN SHORT DONT LE SCRIPT EST DERIVE DES PRISES, `internal` 0,50 EST LE SEUIL
    #    QUI DECIDE DU RYTHME : la frontiere de ligne tombe sur une respiration et non sur
    #    une fin de phrase, donc ce qui serait un raccord (cible 0,10-0,20) devient un gap
    #    INTERNE juge a 0,50. Des pauses de 0,46-0,50 s passent alors sans etre examinees.
    #    Le geste est `--max-internal-gap 0.20`, pas une retouche du profil : le garde-fou
    #    de silence mesure de `pass_split_internal` fait le tri et refuse de couper dans un
    #    mot. Les syllabes trainees et les "euh" non transcrits n'y sont pas atteints :
    #    `tools/trous_gardes.py` les liste avec leur contenu.
    "short": {"tail": TAIL_AFTER_SPEECH, "internal": MAX_INTERNAL_GAP, "targets": dict(TARGETS)},
    # long : trois ecarts, chacun pour une raison distincte.
    #  `tail` 0,30 - une chute de phrase de long-form traine plus qu'une punchline, et 0,15
    #    la coupait ; les enchainements y demandent plus d'espace.
    #  `internal` 0,70 - sur un long derive des prises, une ligne est un GROUPE DE SOUFFLE :
    #    la respiration interne est legitime et plus longue. Couper a 0,50 puis resserrer a
    #    0,10 la detruit.
    #  `targets` - un long se regarde en arriere, un short penche en avant.
    "long":  {"tail": 0.30, "internal": 0.70,
              "targets": {"suite": 0.14, "phrase": 0.20, "section": 0.30}},
}

#: Le profil par defaut : ce depot monte des shorts. `--profile long` pour une video longue.
PROFIL_DEFAUT = "short"


def profil_par_defaut(tl_path):
    """Le profil de rythme quand `--profile` n'est pas donne."""
    return PROFIL_DEFAUT


def raison_sans_profil(tl_path):
    """Gardee pour compatibilite : tout chemin a un profil ici."""
    return None


_audio_cache = {}


# --------------------------------------------------------------------------- audio

def resolve_ffmpeg():
    """Delegue a tools/paths.py - cf. son entete pour l'ordre de resolution."""
    return paths.ffmpeg()


def load_audio(ff, path):
    if path not in _audio_cache:
        raw = subprocess.run(
            [ff, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
            capture_output=True).stdout
        if not raw:
            sys.exit(f"audio illisible : {path}")
        _audio_cache[path] = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    return _audio_cache[path]


def db_at(x, t):
    n = int(SR * FRAME)
    k = int(t * SR)
    if k < 0 or k + n > len(x):
        return -120.0
    r = float(np.sqrt(np.mean(x[k:k + n] ** 2)))
    return 20 * np.log10(r) if r > 1e-6 else -120.0


def voiced(x, t):
    return db_at(x, t) >= FLOOR_DB


def sound_start(x, lo, hi):
    t = lo
    while t < hi - FRAME:
        if voiced(x, t):
            return round(t, 3)
        t += STEP
    return round(hi, 3)


def sound_end(x, hi, lo):
    t = hi
    while t > lo + FRAME:
        if voiced(x, t - FRAME):
            return round(t, 3)
        t -= STEP
    return round(lo, 3)


def dead_air_head(x, in_ext, out_ext):
    """Duree de vide en tete de `[in_ext, out_ext]`, ou `None` si rien de mesurable.

    Delegue a `sound_start`, donc au meme plancher que `voiced` : c'est ce qui garantit que
    `pass_preroll` ne defasse pas ce que cette passe vient de faire (cf. le bloc de constantes).
    """
    onset = sound_start(x, in_ext, out_ext)
    vide = round(max(0.0, onset - in_ext), 3)
    if vide >= out_ext - in_ext - FRAME:
        return None                       # segment entierement sous le plancher : on ne touche pas
    if vide > MAX_DEAD_AIR:
        return None                       # trop pour etre credible : mesure douteuse
    return vide


def true_attack(x, planned):
    """Debut de la zone parlee qui precede `planned`, occlusions de plosive traversees.

    ⚠️ Une PLOSIVE n'est pas un son continu : elle fait salve, puis OCCLUSION, puis voyelle.
    Chercher « le dernier creux avant la parole » atterrit donc pile entre la salve et la
    voyelle, et la coupe mange l'attaque - precisement ce que la passe existe pour eviter.

    Exemple type, "Decouvre" : salve du /d/ tres forte, occlusion sous le plancher, voyelle
    ~20 ms plus loin. Chercher le dernier creux rendait une borne entre les deux, et on
    entendait "couvre".

    On remonte donc depuis `planned` en traversant les creux COURTS, et on ne s'arrete
    qu'au premier silence assez long pour etre un vrai blanc (`PLOSIVE_HOLE`). La borne
    `lo` limite deja la remontee, et `pass_preroll` plafonne le deplacement reel a
    MAX_PREROLL : on ne peut donc pas ramener la queue de la phrase d'avant.
    """
    lo = max(0.0, planned - MAX_PREROLL - 0.10)
    times, quiet = [], []
    t = lo
    while t <= planned + 0.05:
        times.append(round(t, 3))
        quiet.append(not voiced(x, t))
        t += STEP
    attack, run = None, 0
    for k in range(len(times) - 1, -1, -1):
        if quiet[k]:
            run += 1
            if run * STEP >= PLOSIVE_HOLE:
                break
        else:
            run = 0
            attack = times[k]
    return attack


# ------------------------------------------------------------------- transcripts

def find_transcripts(edl_path):
    d = Path(edl_path).resolve().parent
    for cand in (d / "transcripts", d.parent / "transcripts"):
        if cand.is_dir():
            return cand
    return None


def load_words(tdir, stem):
    for suffix in (".transcript.json", ".words_v2.json", ".words.json"):
        p = tdir / f"{stem}{suffix}"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data["words"] if isinstance(data, dict) else data
    return []


def next_word_start(words, after):
    later = [w["start"] for w in words if w["start"] >= after - 1e-3]
    return min(later) if later else None


# ------------------------------------------------------------------------ passes

def plus_long_silence(x, lo, hi):
    """Le plus long silence CONTIGU dans `[lo, hi]` : rend `(debut, fin, duree)`.

    ⚠️ **NE PAS remplacer par deux sondes sur les BORDS de l'intervalle.** `sound_start(x, lo,
    hi)` rend `lo` des que la fenetre de 20 ms posee EN `lo` est voisee - et elle l'est presque
    toujours, parce que l'audio du mot precedent deborde sa fin ASR. Deux sondes rendent alors
    0 ms la ou le plus long silence contigu fait plus d'une demi-seconde. *Une mesure prise sur
    des bords choisis ne dit rien de ce qu'il y a entre eux* : il faut un balayage qui puisse
    echouer sur l'ENSEMBLE de l'intervalle.
    """
    debut = None
    meilleur = (lo, lo, 0.0)
    t = lo
    while t < hi:
        if not voiced(x, t):
            if debut is None:
                debut = t
        elif debut is not None:
            if t - debut > meilleur[2]:
                meilleur = (round(debut, 3), round(t, 3), round(t - debut, 3))
            debut = None
        t = round(t + STEP, 3)
    if debut is not None and hi - debut > meilleur[2]:
        meilleur = (round(debut, 3), round(hi, 3), round(hi - debut, 3))
    return meilleur


def pass_split_internal(edl, words_of, seuil, log, ff=None):
    """Coupe un segment en DEUX quand il porte un silence trop long EN SON MILIEU.

    ⚠️ Le trou annonce par l'ASR est VERIFIE a l'enveloppe avant de couper. Le span d'un token
    NORMALISE par l'ASR ne couvre pas sa forme PARLEE : "5.6", "v2.1", "24/7", "100 %" sont
    ecrits sous une forme rendue, et un span peut s'arreter avant la derniere syllabe ("six"
    de "cinq point six"), annoncant un trou de 630 ms la ou le silence reel en fait 210.
    Couper sur l'ASR couperait dans le mot, et le plancher `_mot_fin` verrouillerait ensuite
    la mauvaise borne. On mesure donc le plus long silence CONTIGU dans le trou, et on ne
    coupe que s'il depasse encore le seuil - sur ses bornes acoustiques, pas sur celles de
    l'ASR. Un trou reellement muet donne le meme resultat qu'un decoupage sur l'ASR.

    Le decoupage seul ne raccourcit rien : il fabrique un raccord la ou il n'y en avait
    pas. C'est **voulu** - la boucle de resserrage qui suit traite ce nouveau raccord
    comme les autres et le ramene a sa cible (0,10 s pour une continuation). Une passe qui
    couperait ET resserrerait dupliquerait cette logique, et les deux divergeraient.

    Le prix a payer : un raccord interne est un JUMP CUT dans la meme prise. Sur un plan
    parle, la tete bouge peu pendant une respiration, donc il passe - et il passe d'autant
    mieux qu'un changement de cadre tombe dessus, ce que `punch.py` fait naturellement.
    """
    out, n = [], 0
    for e in edl:
        w = [x for x in words_of.get(e["stem"], [])
             if x["start"] >= e["in_ext"] - 1e-3 and x["end"] <= e["out_ext"] + 1e-3]
        coupes = []
        for a, b in zip(w, w[1:]):
            annonce = b["start"] - a["end"]
            if annonce <= seuil:
                continue
            fin, debut = a["end"], b["start"]
            if ff is not None:
                x = load_audio(ff, e["source"])
                s0, s1, vrai = plus_long_silence(x, fin, debut)
                if vrai <= seuil:
                    log.append(
                        f"L{e['i']:<4} TROU ASR NON MUET  {annonce * 1000:.0f} ms annonces entre "
                        f"'{a['text']}' et '{b['text']}', mais le plus long silence CONTIGU "
                        f"fait {vrai * 1000:.0f} ms -> AUCUNE coupe (le span ASR de "
                        f"'{a['text']}' ne couvre pas sa forme parlee)"
                    )
                    continue
                # On coupe sur les bornes du silence MESURE, jamais sur celles de l'ASR.
                fin, debut = s0, s1
            coupes.append((fin, debut, a["text"], b["text"]))
        if not coupes:
            out.append(e)
            continue
        pts = [e["in_ext"]]
        for fin, debut, _, _ in coupes:
            pts += [fin, debut]
        pts.append(e["out_ext"])
        for k in range(0, len(pts) - 1, 2):
            part = dict(e)
            part["in_ext"] = round(pts[k], 3)
            part["out_ext"] = round(pts[k + 1], 3)
            part["part"] = k // 2
            part.pop("punch", None)      # un decoupage invalide un punch calcule avant
            out.append(part)
        for fin, debut, ma, mb in coupes:
            log.append(f"L{e['i']:<4} SILENCE INTERNE  {(debut - fin) * 1000:.0f} ms entre "
                       f"'{ma}' et '{mb}' -> coupe, le raccord sera resserre")
        n += len(coupes)
    return out, n


def pass_speech_tail(e, words, log, tail_after=TAIL_AFTER_SPEECH):
    """Borne la queue d'un segment sur le dernier MOT, pas sur le dernier son.

    Ne fait que REDUIRE, comme toutes les autres passes. Le cas vise est le bruit
    non-parle en fin de prise : respiration, vetement, mouvement - audible pour le
    detecteur, sans interet pour le montage, et parfois franchement genant a l'image.
    """
    # ⚠️ LE CRITERE EST LE CHEVAUCHEMENT, PAS L'INCLUSION. Un mot qui deborde le bord droit
    # (`start < out_ext < end`) est de la parole AU bord : il doit ENTRER dans `w`. Filtre
    # sur `end <= out_ext`, il en sortirait, la limite se calculerait sur le mot PRECEDENT,
    # et la coupe tomberait AU MILIEU du dernier mot ("avec Cla-"). Le mot qui chevauche rend
    # `limite` superieure a `out_ext`, donc la passe ne fait rien : quand il y a de la parole
    # au bord, il n'y a pas de queue non parlee a retirer.
    w = [x for x in words if x["start"] >= e["in_ext"] - 1e-3 and x["start"] < e["out_ext"] - 1e-3]
    if not w:
        return 0
    limite = round(w[-1]["end"] + tail_after, 3)
    # ⚠️ LE PLANCHER DE `pass_truncation`. Cette passe se borne sur `w[-1]["end"]`, un
    # horodatage ASR ; `pass_truncation` tourne AVANT elle et son metier est de prouver cet
    # horodatage FAUX (de l'audio voise au-dela). Sans plancher, les deux passes se
    # contredisent par construction et la derniere gagne : la parole mesuree serait rejetee.
    # Une MESURE prime sur un horodatage. Le plancher est donc unilateral.
    plancher = e.get("_tail_floor")
    if plancher is not None and limite < plancher - 1e-3:
        log.append(f"L{e['i']:<4} QUEUE gardee      plancher {plancher:.3f} "
                   f"(la troncature avait mesure de la parole ; {(plancher-limite)*1000:.0f} ms preserves)")
        limite = plancher
    if e["out_ext"] <= limite + 1e-3:
        return 0
    trop = round(e["out_ext"] - limite, 3)
    log.append(f"L{e['i']:<4} QUEUE NON PARLEE  out {e['out_ext']:.3f} -> {limite:.3f}  "
               f"({trop * 1000:.0f} ms de son apres '{w[-1]['text']}')")
    e["out_ext"] = limite
    return 1


def pass_dead_air(x, e, log):
    """Avance `in_ext` jusqu'a l'attaque quand l'ASR a date le premier mot EN AVANCE.

    ⚠️ Doit tourner AVANT `pass_preroll`, et l'ordre n'est pas arbitraire. Les deux passes
    tirent le meme bord dans des sens opposes : lancee apres, celle-ci verrait le recul de
    150 ms que le pre-roll vient de poser, le prendrait pour du vide et l'annulerait.

    Dans cet ordre elles ne peuvent pas se battre : une fois la coupe posee 20 ms avant
    l'attaque, `true_attack` la retrouve sur place et le gain du pre-roll tombe sous
    MIN_PREROLL, donc il ne fait rien. Et quand le vide est trop court pour declencher ici,
    le pre-roll ne l'aggrave pas : `true_attack` ne renvoie une valeur que s'il trouve de
    l'audio VOISE avant la coupe, ce qui est faux par definition quand elle ouvre sur du vide.
    """
    vide = dead_air_head(x, e["in_ext"], e["out_ext"])
    if vide is None or vide < MIN_DEAD_AIR:
        return 0
    new_in = round(e["in_ext"] + vide - DEAD_AIR_MARGIN, 3)
    if new_in <= e["in_ext"] or new_in >= e["out_ext"]:
        return 0
    log.append(f"L{e['i']:<3} VIDE EN TETE  in {e['in_ext']:.3f} -> {new_in:.3f}  "
               f"({vide*1000:.0f} ms de vide retires, ASR en avance)")
    e["in_ext"] = new_in
    return 1


def pass_preroll(x, e, log, words=()):
    at = true_attack(x, e["in_ext"])
    if at is None:
        return 0
    new_in = round(max(at - 0.02, e["in_ext"] - MAX_PREROLL), 3)
    # ⚠️ PLANCHER GAUCHE - le MIROIR de `_mot_fin`. Cette passe recule jusqu'a 150 ms pour
    # retrouver l'attaque que l'ASR a datee trop tard. Mais elle cherche un SON, et si un mot
    # precede immediatement le debut du segment, c'est CE mot qu'elle trouve - un mot qu'on
    # avait justement exclu (typiquement le "euh" que `tics.py` vient de retirer). Toute
    # troncature qui demarre juste apres un mot est exposee.
    avant = [w["end"] for w in words if w["end"] <= e["in_ext"] + 1e-3]
    if avant:
        borne = round(max(avant), 3)
        if new_in < borne:
            new_in = borne
    gain = e["in_ext"] - new_in
    if gain < MIN_PREROLL:
        return 0
    log.append(f"L{e['i']:<3} attaque  in {e['in_ext']:.3f} -> {new_in:.3f}  (+{gain*1000:.0f} ms recuperes)")
    e["in_ext"] = new_in
    return 1


def pass_truncation(x, e, words, log, next_e=None):
    """Etend la fin si de l'audio voise contigu n'est couvert par aucun mot."""
    end = e["out_ext"]
    if not voiced(x, end):
        return 0
    limit = end + MAX_TRUNC_EXTEND
    nxt = next_word_start(words, end + 0.02)
    if nxt is not None:
        limit = min(limit, nxt - 0.05)
    # ne jamais mordre sur le segment suivant s'il vient du meme rush :
    # les deux trims sont concatenes, un chevauchement rejouerait l'audio.
    if next_e is not None and next_e["source"] == e["source"] and next_e["in_ext"] > end:
        limit = min(limit, next_e["in_ext"] - 0.02)
    if limit <= end:
        return 0                      # un mot transcrit suit de pres : prise jetee, pas troncature

    t, last_voiced, hole = end, end, 0.0
    while t < limit:
        if voiced(x, t):
            last_voiced, hole = t, 0.0
        else:
            hole += STEP
            if hole > TRUNC_MAX_HOLE:
                break
        t += STEP
    new_out = round(min(last_voiced + 0.10, limit), 3)
    if new_out - end < TRUNC_MIN_GAIN:
        return 0
    log.append(f"L{e['i']:<3} TRONCATURE ASR  out {end:.3f} -> {new_out:.3f}  "
               f"(+{(new_out-end)*1000:.0f} ms de parole non transcrite recuperes)")
    e["out_ext"] = new_out
    # PLANCHER pour `pass_speech_tail` : on vient de MESURER de la parole au-dela de
    # l'horodatage ASR. Aucune passe ulterieure n'a le droit de la rejeter sur la foi de
    # cet horodatage, que celle-ci vient precisement de prouver faux. Cf. son en-tete.
    e["_tail_floor"] = new_out
    return 1


def deaccent(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def classify(prev_e, next_e):
    if not SENTENCE_END.search(prev_e["text"].strip()):
        return "suite"
    # sans accents : le transcript ecrit "Deuxiemement" ou "Deuxiemement" selon l'ASR
    head = deaccent(next_e["text"].strip().lower())
    if any(head.startswith(deaccent(m)) for m in SECTION_MARKERS):
        return "section"
    return "phrase"


# -------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("timeline", help="edl_v3_timeline.json produit par assemble.py")
    ap.add_argument("--dry-run", action="store_true", help="affiche sans ecrire")
    ap.add_argument("--no-preroll", action="store_true")
    ap.add_argument("--no-dead-air", action="store_true",
                    help="Desactive le retrait du vide en tete (ASR en avance)")
    ap.add_argument("--no-truncation-fix", action="store_true")
    ap.add_argument("--no-speech-tail", action="store_true",
                    help="Desactive le bornage de la queue sur le dernier MOT "
                         f"(defaut : {TAIL_AFTER_SPEECH}s apres lui)")
    ap.add_argument("--no-tighten", action="store_true")
    ap.add_argument("--no-split-internal", action="store_true",
                    help="Desactive le decoupage des silences INTERNES a un segment")
    ap.add_argument("--profile", choices=sorted(PROFILS), default=None,
                    help="Profil de RYTHME (short|long). Defaut : short. "
                         "Cf. le bloc PROFILS.")
    ap.add_argument("--max-internal-gap", type=float, default=None,
                    help="Silence interne au-dela duquel on coupe le segment en deux. "
                         "Par defaut, la valeur du profil.")
    ap.add_argument("--targets", default=None,
                    help="JSON pour surcharger les cibles du profil")
    ap.add_argument("--report", default=None, help="Default: <timeline_dir>/cut_plan_report.md")
    args = ap.parse_args()

    prof_nom = args.profile or profil_par_defaut(args.timeline)
    prof = PROFILS[prof_nom]

    targets = dict(prof["targets"])
    if args.targets:
        targets.update(json.loads(args.targets))
    max_internal = args.max_internal_gap if args.max_internal_gap is not None else prof["internal"]
    tail_after = prof["tail"]

    ff = resolve_ffmpeg()
    tl_path = Path(args.timeline)
    edl = json.loads(tl_path.read_text(encoding="utf-8"))
    tdir = find_transcripts(tl_path)
    words_of = {}
    if tdir:
        for e in edl:
            words_of.setdefault(e["stem"], load_words(tdir, e["stem"]))

    log = []
    n_pre = n_trunc = n_dead = n_split = n_tail = n_mot = 0
    # Le decoupage des silences INTERNES vient en premier : il cree de nouveaux segments,
    # donc de nouvelles bornes, que toutes les passes suivantes doivent voir comme les
    # autres - pre-roll compris, sinon la reprise apres la respiration ouvre sur du vide.
    if not args.no_split_internal:
        edl, n_split = pass_split_internal(edl, words_of, max_internal, log, ff=ff)
    # vide en tete AVANT le pre-roll : les deux tirent le meme bord en sens opposes,
    # cf. l'entete de `pass_dead_air`.
    if not args.no_dead_air:
        for e in edl:
            n_dead += pass_dead_air(load_audio(ff, e["source"]), e, log)
    # pre-roll ensuite sur TOUTE la liste : la detection de troncature a besoin
    # du in_ext definitif du segment suivant pour ne pas deborder dessus.
    if not args.no_preroll:
        for e in edl:
            n_pre += pass_preroll(load_audio(ff, e["source"]), e, log,
                                   words_of.get(e["stem"], []))
    if not args.no_truncation_fix:
        for k, e in enumerate(edl):
            nxt = edl[k + 1] if k + 1 < len(edl) else None
            n_trunc += pass_truncation(load_audio(ff, e["source"]), e,
                                       words_of.get(e["stem"], []), log, nxt)

    for e in edl:
        x = load_audio(ff, e["source"])
        e["_head"] = round(sound_start(x, e["in_ext"], e["out_ext"]) - e["in_ext"], 3)
        e["_end"] = sound_end(x, e["out_ext"], e["in_ext"])
        # ⚠️ LA FIN DU DERNIER MOT, qui servira de PLANCHER au resserrage ci-dessous.
        # `_end` est le dernier SON, mesure par seuil de voisement - et ce seuil SOUS-MESURE
        # la fin des fricatives sourdes ("memes", "fois", "prix" : le "s" passe dessous).
        # `_end` tombe alors AVANT la fin du mot, et le resserrage couperait DANS le mot.
        # L'horodatage ASR, lui, tient le mot entier : on s'en sert comme borne basse. Meme
        # raisonnement que `_tail_floor`. ACOUSTIQUE, donc valable pour tous les formats.
        mots = [w for w in words_of.get(e["stem"], [])
                if w["start"] >= e["in_ext"] - 1e-3 and w["start"] < e["out_ext"] - 1e-3]
        e["_mot_fin"] = round(mots[-1]["end"], 3) if mots else None

    rows = []
    before = after = 0.0
    for a, b in zip(edl, edl[1:]):
        tail = round(a["out_ext"] - a["_end"], 3)
        gap = round(tail + b["_head"], 3)
        kind = classify(a, b)
        target = targets[kind]
        new_gap = gap
        if not args.no_tighten:
            new_tail = min(tail, max(0.0, round(target - b["_head"], 3)))
            a["out_ext"] = round(a["_end"] + new_tail, 3)
            # le plancher : jamais en-deca de la fin du dernier MOT (cf. `_mot_fin` ci-dessus)
            if a.get("_mot_fin") is not None and a["out_ext"] < a["_mot_fin"] - 1e-3:
                n_mot += 1
                log.append(f"L{a['i']:<4} MOT ENTIER        out {a['out_ext']:.3f} -> "
                           f"{a['_mot_fin']:.3f}  ({(a['_mot_fin'] - a['out_ext']) * 1000:.0f} ms "
                           f"de mot que le seuil de voisement n'entendait pas)")
                a["out_ext"] = a["_mot_fin"]
            new_tail = round(a["out_ext"] - a["_end"], 3)
            new_gap = round(new_tail + b["_head"], 3)
        before += gap
        after += new_gap
        rows.append((a["i"], b["i"], kind, gap, new_gap, b["text"].strip()[:38]))

    # ⚠️ La queue non parlee se borne APRES le resserrage, pas avant. Avant, `out_ext` vaut
    # encore la marge de `compute_cuts` (0,15 s au plus) et il n'y a rien a couper : les
    # 350 ms n'apparaissent que parce que la boucle ci-dessus borne la queue sur le dernier
    # SON (`_end`), qui inclut le bruit du mouvement. Cette passe est donc son correctif,
    # et elle doit la suivre.
    if not args.no_speech_tail:
        for e in edl:
            n_tail += pass_speech_tail(e, words_of.get(e["stem"], []), log, tail_after)

    # ⚠️ PASSE ANTI-CHEVAUCHEMENT - le defaut le plus audible. Quand `out_ext` du segment de
    # gauche depasse `in_ext` du suivant et que les deux viennent du MEME rush, l'assemblage
    # joue deux fois le meme fragment d'audio : on entend "pa-par", "de de", "for-force".
    # Rare quand les lignes sont des phrases espacees dans le rush, systematique quand une
    # ligne est un groupe de souffle et que les fenetres voisines se touchent.
    #
    # La correction est sans perte : ramener `out_ext` a `in_ext` du suivant fait entendre
    # l'audio exactement une fois, et la continuite est preservee puisque le segment
    # suivant reprend precisement la. On ne rogne QUE du dedoublement, jamais de la parole.
    n_chev = 0
    for a, b in zip(edl, edl[1:]):
        if a.get("stem") != b.get("stem"):
            continue
        double = round(a["out_ext"] - b["in_ext"], 3)
        if double > 0.001:
            a["out_ext"] = round(b["in_ext"], 3)
            log.append(f"L{a['i']:<4} CHEVAUCHEMENT  out {a['out_ext'] + double:.3f} -> "
                       f"{a['out_ext']:.3f}  ({double * 1000:.0f} ms rejoues en double avec "
                       f"L{b['i']}, meme rush)")
            n_chev += 1

    for e in edl:
        e.pop("_head", None)
        e.pop("_end", None)

    print(f"ffmpeg : {ff}")
    print(f"transcripts : {tdir if tdir else 'introuvables (detection de troncature desactivee)'}\n")
    print(f"PROFIL : {prof_nom}"
          f"{'' if args.profile else ' (defaut)'}"
          f"  |  queue {tail_after}s | interne {max_internal}s | "
          f"cibles {targets['suite']}/{targets['phrase']}/{targets['section']}\n")
    for line in log:
        print(line)
    n_floor = sum(1 for l in log if "QUEUE gardee" in l)
    print(f"\n-- silences internes coupes : {n_split} | queues non parlees : {n_tail} | "
          f"queues gardees par le plancher : {n_floor} | "
          f"mots gardes entiers : {n_mot} | "
          f"vide en tete : {n_dead} ligne(s) | "
          f"pre-roll : {n_pre} | troncatures ASR corrigees : {n_trunc} | "
          f"chevauchements ecretes : {n_chev}\n")
    print(f"{'raccord':>9} {'type':>8} {'avant':>7} {'apres':>7} {'gain':>8}   suite")
    print("-" * 84)
    for i, j, kind, g0, g1, txt in rows:
        gain = f"-{(g0-g1)*1000:.0f} ms" if g1 < g0 - 1e-6 else "-"
        print(f"{i:>4}->{j:<3} {kind:>8} {g0:7.3f} {g1:7.3f} {gain:>8}   {txt}...")
    print(f"\nsilence aux raccords : {before:.2f} s -> {after:.2f} s   (gain {before-after:.2f} s)")

    if args.dry_run:
        print("\n--dry-run : rien n'a ete ecrit.")
        return

    tl_path.write_text(json.dumps(edl, ensure_ascii=False, indent=1), encoding="utf-8")
    rep = Path(args.report) if args.report else tl_path.parent / "cut_plan_report.md"
    with rep.open("w", encoding="utf-8") as f:
        f.write("# Plan de coupe - tighten_cuts.py\n\n")
        f.write(f"Silences internes coupes : {n_split} | vide en tete retire : {n_dead} "
                f"ligne(s) | pre-roll d'attaque : {n_pre} "
                f"| troncatures ASR corrigees : {n_trunc}\n\n")
        if log:
            f.write("## Corrections de bord\n\n")
            for line in log:
                f.write(f"- {line}\n")
            f.write("\n")
        f.write("## Raccords\n\n| raccord | type | avant | apres | suite |\n|---|---|---:|---:|---|\n")
        for i, j, kind, g0, g1, txt in rows:
            f.write(f"| {i} -> {j} | {kind} | {g0:.3f} | {g1:.3f} | {txt}... |\n")
        f.write(f"\n**silence aux raccords : {before:.2f} s -> {after:.2f} s "
                f"(gain {before-after:.2f} s)**\n")
    print(f"\nOK -> {tl_path}")
    print(f"OK -> {rep}")
    print("\nReassembler avec :\n"
          f"  python tools/assemble.py \"{tl_path}\" --honor-cuts --out <out>/<video>_montage.mp4")


if __name__ == "__main__":
    main()
