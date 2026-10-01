"""blancs.py - les blancs qu'on ENTEND dans un master, et ce qui les remplit.

Ce que les autres outils ne couvrent pas
----------------------------------------
- `qa_montage.py` lance `silencedetect` sur le master a `d=1.0 s` : il ne voit pas un blanc
  de 150 a 600 ms. Son autre controle (`INTERNAL_PAUSE`, seuil 0,55 s) travaille sur les
  horodatages ASR.
- `tighten_cuts.py` mesure les rushs SOURCE, avant montage. Il ne relit jamais son resultat.
- `trous_gardes.py` liste les trous internes gardes, dans les rushs.
Ici on regarde 0,10 a 0,80 s, sur le MASTER.

⚠️ **Cet outil ne coupe rien et ne propose rien.** Il mesure et il classe, comme `tics.py`.

Ce qui decide qu'un blanc s'entend : ce qui le REMPLIT, pas sa duree
--------------------------------------------------------------------
Un silence habite par un souffle s'entend comme une respiration ; un silence au plancher
s'entend comme une panne. A duree egale, l'un passe et l'autre non. Deux consequences :

1. **Le souffle n'est pas un defaut, c'est la solution.** `FLOOR_DB = -45` laisse passer une
   respiration ; couper les souffles fabriquerait des blancs morts.
2. **Poser un lit de fond de piece sous les raccords ne sert a rien.** Le fond de piece d'un
   micro debruite est vers -76/-82 dB : un lit pris dans le rush serait inaudible. Ce qui
   manque a un raccord mort est un SOUFFLE, et ca ne se fabrique pas.

`FLOOR_DB = -45` n'est donc PAS le bruit de piece : tout ce qui vit entre -76 et -45 est du
souffle, de la salive, une amorce de consonne. Sa valeur reste juste pour son usage
("est-ce de la parole ?").

⚠️ MORT/HABITE ne repond qu'a « couper ici fabriquerait-il un blanc mort ? », jamais a
« cette pause est-elle trop longue ? ». Les deux sont orthogonales : la respiration est ce qui
rend un blanc habite ET ce qui le rend long. D'ou le signalement des habitees INTERNES longues
en pied de rapport : il ne tranche rien, il rappelle qu'il reste une question de rythme.

Un seuil ABSOLU, et pourquoi pas un plancher relatif
----------------------------------------------------
Le classement MORT/HABITE se fait sur le **minimum absolu** de la plage, contre
`SEUIL_MORT_DB = -80 dB`. Les deux estimateurs relatifs naturels sont pires :

1. **Le centile bas du MASTER est circulaire** : c'est le blanc lui-meme qui fournit les
   trames les plus basses. Retirer le plus profond fait remonter le plancher, des plages
   limites basculent vers MORT sans avoir change, et le compte augmente alors que le
   montage s'ameliore. *Un estimateur de bruit de fond calcule sur le signal qu'on nettoie
   se degrade a mesure qu'on le nettoie.*
2. **Le centile bas des RUSHS tombe sur le zero numerique** (-120 dB : amorce
   d'enregistrement, gate), et tout se retrouve « habite ».

Le seuil de -80 dB separe des blancs juges genants (minimum -87,9 et -89,6 dB) de blancs
juges acceptables (-76,9 et -53,2 dB), dont un deux fois plus long qu'un genant : la duree ne
separe rien, le niveau oui, avec 11 dB de marge au plus serre. Il suppose une seule chaine de
prise de son : l'outil MESURE le decalage de la parole entre le master et ses rushs et avertit
au-dela de 3 dB (master re-nivele, autre micro). Avec si peu de points, ce seuil TRIE et ne
decide pas.

Bornes assumees
---------------
`MIN_PLAGE = 0,10 s` : sous 100 ms une coupe ne s'entend pas. Aucune decision ne depend d'un
classement : les chiffres de chaque plage (duree, minimum, moyenne) sont affiches, et deux
plages voisines de la limite se lisent dessus.

    python tools/blancs.py <slug>                       # resout le master de la video
    python tools/blancs.py <master.mp4> --edl <timeline.json>
    python tools/blancs.py <slug> --min 0.15 --json

A lancer APRES l'encodage du master, avant de faire relire. C'est le pendant de `tics.py` en
pre-vol.

Pieges
------
- ⚠️ **« 0 blanc mort » sur un COMPOSITE est une fausse assurance** : la musique remplit tout
  au-dessus du seuil, alors que les blancs s'entendent encore. Mesurer sur le master voix.
- **CapCut affiche un timecode 30 fps sur un fichier 60 fps.** Ce qui tranche est le total
  affiche compare a la duree reelle du fichier ; lire un timecode dans la mauvaise base decale
  de plusieurs centaines de ms et designe le mauvais silence.
- **Quel master** : le plus RECEMMENT ecrit, avec tous les candidats affiches et leurs durees,
  et un avertissement quand elles divergent. Le nom le plus nu peut designer un master d'avant
  resserrage, jamais livre.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import paths  # noqa: E402
import tighten_cuts as tc  # noqa: E402  (une seule implementation de l'enveloppe RMS)

#: Duree minimale d'une plage pour etre listee. Sous 100 ms, une coupe ne s'entend pas.
MIN_PLAGE = 0.10

#: Sous ce MINIMUM absolu, une plage est MORTE. Deux estimateurs relatifs sont pires (cf.
#: l'entete). Separe les points etiquetes avec 11 dB de marge au plus serre. Suppose une seule
#: chaine de prise de son : l'outil mesure et signale un ecart de niveau plutot que de le
#: supposer.
SEUIL_MORT_DB = -80.0

#: Centile de reference pour le controle de niveau (parole du master vs parole de ses rushs).
PLANCHER_Q = 0.002

#: Tolerance pour dire qu'une plage tombe SUR un raccord plutot qu'a l'interieur d'un segment.
MARGE_RACCORD = 0.06

#: Duree a partir de laquelle une plage HABITEE et INTERNE est SIGNALEE comme peut-etre trop
#: longue. Purement informatif - aucune decision ne se branche dessus, et c'est voulu : le
#: rythme ne se tranche pas au seuil. Ne s'affiche pas sur un long-form, ou une pause interne
#: longue est le plus souvent VOULUE.
HABITEE_LONGUE_S = 0.300


def enveloppe(x, duree, pas):
    t, out = 0.0, []
    while t < duree - tc.FRAME:
        out.append((round(t, 3), tc.db_at(x, t)))
        t = round(t + pas, 3)
    return out


def plancher_du_fichier(niveaux, bornes=None):
    """Le centile `PLANCHER_Q` des niveaux, **dans le montage seulement**.

    `bornes` restreint la mesure a l'intervalle couvert par les segments, pour que la tete et
    la queue d'encodage ne pesent pas sur le plancher d'un fichier court.

    Une plage a -120 dB « hors des segments connus » ne prouve pas que la queue contamine le
    centile : le -120 vient de l'echantillonnage a `STEP` (10 ms) d'une fenetre de `FRAME`
    (20 ms) numeriquement muette, ce que le centile, calcule au pas de `FRAME`, ne voit pas.
    *Une anomalie voisine ne prouve pas le mecanisme qu'on lui prete.*
    """
    if bornes is not None:
        m0, m1 = bornes
        vals = sorted(n for t, n in niveaux if m0 - 1e-6 <= t <= m1 + 1e-6)
    else:
        vals = sorted(n for _, n in niveaux)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(len(vals) * PLANCHER_Q))]


_CACHE_RUSH = {}


def plancher_des_sources(segs, ff):
    """Le centile `PLANCHER_Q` des rushs sources. Rend `(plancher, mediane_parole, n_rushs)`.

    Invariant d'une passe a l'autre : c'est tout l'interet, cf. l'entete. On prend les rushs
    ENTIERS et non les plages utilisees - ce sont justement les parties ECARTEES qui portent le
    silence dont on a besoin.
    """
    sources = sorted({s["source"] for s in segs if s.get("source")})
    if not sources:
        return None, None, 0
    trames = []
    for src in sources:
        if src not in _CACHE_RUSH:
            try:
                x = tc.load_audio(ff, src)
            except Exception:
                continue
            duree = len(x) / tc.SR
            vals, t = [], 0.0
            while t < duree - tc.FRAME:
                vals.append(tc.db_at(x, t))
                t = round(t + tc.FRAME, 3)
            _CACHE_RUSH[src] = vals
        trames.extend(_CACHE_RUSH[src])
    if not trames:
        return None, None, 0
    trames.sort()
    plancher = trames[min(len(trames) - 1, int(len(trames) * PLANCHER_Q))]
    parle = [v for v in trames if v >= tc.FLOOR_DB]
    mediane = parle[len(parle) // 2] if parle else None
    return plancher, mediane, len(sources)


def mediane_parole(niveaux):
    parle = sorted(n for _, n in niveaux if n >= tc.FLOOR_DB)
    return parle[len(parle) // 2] if parle else None


def plages_calmes(x, duree, mini):
    """Les plages contigues sous `FLOOR_DB` d'au moins `mini` secondes.

    Balayage complet, comme `plus_long_silence` de `tighten_cuts` - et pour la meme raison,
    ecrite la : une mesure prise sur des bords choisis ne dit rien de ce qu'il y a entre eux.
    """
    out, debut, t = [], None, 0.0
    while t < duree - tc.FRAME:
        if tc.db_at(x, t) < tc.FLOOR_DB:
            if debut is None:
                debut = t
        elif debut is not None:
            if t - debut >= mini:
                out.append((debut, round(t, 3)))
            debut = None
        t = round(t + tc.STEP, 3)
    if debut is not None and duree - debut >= mini:
        out.append((debut, round(duree, 3)))
    return out


def charger_timeline(ch):
    """Rend `(segments, raccords)` ou chaque segment porte ses bornes MASTER.

    `_cum_start` est pose par `assemble.py`. Sans lui on ne peut pas remonter au rush, et on
    le dit plutot que de deviner.
    """
    edl = json.loads(Path(ch).read_text(encoding="utf-8"))
    if isinstance(edl, dict):
        edl = edl.get("segments", edl)
    segs = []
    for s in edl:
        c0 = s.get("_cum_start")
        if c0 is None:
            continue
        segs.append({
            "i": s.get("i"), "part": s.get("part"), "stem": s.get("stem"),
            # ⚠️ `source` est indispensable : c'est lui qui donne le plancher de bruit
            # (cf. l'entete). Sans lui `plancher_des_sources` replie toujours sur le master.
            "source": s.get("source"),
            "m0": round(c0, 3), "m1": round(c0 + (s["out_ext"] - s["in_ext"]), 3),
            "in_ext": s["in_ext"], "out_ext": s["out_ext"],
        })
    segs.sort(key=lambda s: s["m0"])
    raccords = [s["m1"] for s in segs[:-1]]
    return segs, raccords


def origine(a, b, segs, raccords):
    """Ou est cette plage : sur un raccord, ou a l'interieur d'un segment ?"""
    for j in raccords:
        if a - MARGE_RACCORD <= j <= b + MARGE_RACCORD:
            gauche = next((s for s in segs if abs(s["m1"] - j) < 1e-6), None)
            droite = next((s for s in segs if abs(s["m0"] - j) < 1e-6), None)
            txt = "RACCORD"
            if gauche and droite:
                txt = f"RACCORD L{gauche['i']}->L{droite['i']}"
            det = ""
            if gauche and droite:
                det = (f"{gauche['stem']} sort a {gauche['out_ext']:.3f} / "
                       f"{droite['stem']} entre a {droite['in_ext']:.3f}")
            return txt, det, None
    for s in segs:
        if s["m0"] - 1e-6 <= a and b <= s["m1"] + 1e-6:
            r0 = round(s["in_ext"] + (a - s["m0"]), 3)
            r1 = round(s["in_ext"] + (b - s["m0"]), 3)
            part = "" if s["part"] is None else f".{s['part']}"
            return f"INTERNE L{s['i']}{part}", f"{s['stem']} {r0:.3f} -> {r1:.3f}", (s["stem"], r0, r1)
    return "?", "hors des segments connus", None


def duree_de(ch):
    out = subprocess.run([paths.ffprobe(), "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(ch)],
                         capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def choisir_master(cands):
    """Le master le plus RECEMMENT ecrit, en montrant les autres.

    ⚠️ **Ni « le nom le plus nu », ni un refus.**

    - preferer le nom nu (ecarter `tight`, `graded`) peut prendre le master d'AVANT
      `tighten_cuts`, jamais livre : l'outil mesurerait alors un fichier qui n'est pas celui
      qu'on fait relire, en affichant le bon nom de dossier ;
    - refuser des qu'il y a deux candidats rendrait la forme courte inutilisable, alors que
      deux masters ne different souvent QUE par l'etalonnage, qui ne touche pas l'audio.

    D'ou : le plus recent, **annonce**, avec les autres et leurs durees. Si les durees
    divergent, le choix n'est plus innocent, et on le dit.
    """
    infos = [(c, duree_de(c), c.stat().st_mtime) for c in cands]
    infos.sort(key=lambda t: -t[2])
    master = infos[0][0]
    if len(infos) > 1:
        durees = {round(d, 1) for _, d, _ in infos if d is not None}
        print(f"masters   {len(infos)} candidats, je prends le plus recent :")
        for c, d, _ in infos:
            drapeau = "  <-- retenu" if c == master else ""
            print(f"          {c.name}  {'?' if d is None else f'{d:.3f} s'}{drapeau}")
        if len(durees) > 1:
            print("[!]        leurs DUREES divergent : le choix change la mesure. Donne le chemin")
            print("          explicitement si ce n'est pas celui-la que tu fais relire.")
    return master


def trouver_edl(mdir, stem):
    """L'EDL d'un master. Rend None plutot que de deviner.

    ⚠️ Des variantes peuvent vivre dans des sous-dossiers (`1_montage/h1/`, `h6/`...), chacun
    avec son `edl_v3_timeline.json`. Sans ce detour l'outil tournerait SANS remonter aux rushs,
    avec un simple avertissement : il marcherait a moitie.

    On apparie le sous-dossier au nom du master (`<slug>_h1_montage` -> `h1`). Deux
    correspondances : on refuse, plutot que de prendre la premiere par ordre alphabetique.
    """
    direct = mdir / "edl_v3_timeline.json"
    if direct.exists():
        return str(direct)
    jetons = set(stem.lower().split("_"))
    cands = [d for d in sorted(mdir.glob("*/edl_v3_timeline.json")) if d.parent.name.lower() in jetons]
    if len(cands) == 1:
        return str(cands[0])
    if len(cands) > 1:
        print(f"[!]  {len(cands)} EDL possibles pour {stem} - donne --edl explicitement :",
              file=sys.stderr)
        for c in cands:
            print(f"     {c}", file=sys.stderr)
    return None


def serie_de(master):
    """Un fragment du nom du dossier video, ou None. Garde pour compatibilite : un dossier
    `videos/<slug>/` ne porte pas de serie, donc ceci ne vaut quelque chose que sur un slug
    a trois segments ou plus separes par `_`.
    """
    try:
        v = paths.video_from_path(Path(master))
    except Exception:
        return None
    bouts = v.name.split("_")
    return bouts[1] if len(bouts) > 2 else None


def mesurer(master, edl_path, mini):
    ff = paths.ffmpeg()
    x = tc.load_audio(ff, str(master))
    duree = len(x) / tc.SR
    niveaux = enveloppe(x, duree, tc.FRAME)

    segs, raccords = ([], [])
    if edl_path and Path(edl_path).exists():
        segs, raccords = charger_timeline(edl_path)
    bornes = (segs[0]["m0"], segs[-1]["m1"]) if segs else None
    # Le plancher vient des SOURCES quand on les connait (cf. l'entete : un plancher mesure sur
    # le signal qu'on nettoie est circulaire). Repli sur le master, annonce, sinon.
    plancher, med_rush, n_rushs = plancher_des_sources(segs, ff) if segs else (None, None, 0)
    origine_plancher, decalage = "rushs", None
    if plancher is None:
        plancher = plancher_du_fichier(niveaux, bornes)
        origine_plancher = "master (aucune source lisible)"
    else:
        med_master = mediane_parole(niveaux)
        if med_master is not None and med_rush is not None:
            decalage = round(med_master - med_rush, 1)

    lignes, hors = [], []
    for a0, b0 in plages_calmes(x, duree, mini):
        a, b = a0, b0
        if bornes:
            # ⚠️ On DECOUPE, on n'exclut pas. Une plage peut commencer dans le dernier segment
            # et finir dans la queue d'encodage : exclue, elle disparaitrait ; gardee entiere,
            # son minimum tomberait a -120 dB (silence numerique de l'encodeur) et elle
            # sortirait en tete du classement des blancs sans en etre un.
            a, b = max(a, bornes[0]), min(b, bornes[1])
            if b0 > bornes[1] + 1e-6 or a0 < bornes[0] - 1e-6:
                hors.append({"debut": a0, "fin": b0, "duree": round(b0 - a0, 3),
                             "garde": [round(a, 3), round(b, 3)]})
            if b - a < mini:
                continue
        vals, t = [], a
        while t < b:
            vals.append(tc.db_at(x, t))
            t = round(t + tc.STEP, 3)
        mn, moy = min(vals), sum(vals) / len(vals)
        hauteur = mn - SEUIL_MORT_DB
        ou, det, rush = ("(pas d'EDL)", "", None)
        if segs:
            ou, det, rush = origine(a, b, segs, raccords)
        lignes.append({
            "debut": a, "fin": b, "duree": round(b - a, 3),
            "min_db": round(mn, 1), "moy_db": round(moy, 1),
            "hauteur_db": round(hauteur, 1),
            "nature": "MORT" if mn < SEUIL_MORT_DB else "HABITE",
            "ou": ou, "source": det,
            "rush": None if not rush else {"stem": rush[0], "debut": rush[1], "fin": rush[2]},
        })
    return {
        "master": str(master), "duree_s": round(duree, 3),
        "plancher_rushs_db": None if plancher is None else round(plancher, 1),
        "seuil_parole_db": tc.FLOOR_DB,
        "seuil_mort_db": SEUIL_MORT_DB, "min_plage_s": mini,
        "serie": serie_de(master),
        "plancher_origine": origine_plancher, "plancher_rushs": n_rushs,
        "decalage_parole_db": decalage,
        "segments_connus": len(segs), "plages": lignes,
        "montage_de_a": None if not bornes else [bornes[0], bornes[1]],
        "hors_montage": hors,
    }


def rendre(r):
    print(f"master    {r['master']}")
    print(f"duree     {r['duree_s']:.3f} s")
    print(f"mort      minimum < {r['seuil_mort_db']:.0f} dB  (seuil ABSOLU ; deux estimateurs "
          f"relatifs ont echoue, cf. l'entete)")
    d = r["decalage_parole_db"]
    if d is None:
        print("[!]        niveau NON verifie (rushs illisibles) : le seuil absolu suppose la "
              "chaine de prise de son habituelle.")
    elif abs(d) > 3.0:
        print(f"[!]        la parole du master est {d:+.1f} dB par rapport a ses rushs : ce "
              f"master a ete RE-NIVELE,")
        print("          donc le seuil absolu est decale d'autant. Corriger avant de conclure.")
    else:
        print(f"niveau    parole du master {d:+.1f} dB vs ses {r['plancher_rushs']} rushs - "
              f"meme echelle, seuil applicable")
    print(f"parole    >= {r['seuil_parole_db']:.0f} dB (FLOOR_DB de tighten_cuts)")
    if not r["segments_connus"]:
        print("[!]  aucun EDL exploitable : les plages ne sont PAS remontees aux rushs,")
        print("    et le plancher est mesure sur TOUT le fichier, queue d'encodage comprise.")
    for h in r["hors_montage"]:
        g0, g1 = h["garde"]
        reste = "rien ne reste dans le montage" if g1 - g0 < r["min_plage_s"] else                 f"part gardee {g0:.3f}->{g1:.3f}"
        print(f"hors      {h['debut']:.3f}->{h['fin']:.3f}  {h['duree'] * 1000:.0f} ms deborde "
              f"la tete/queue d'encodage - {reste}")
    print()
    morts = [p for p in r["plages"] if p["nature"] == "MORT"]
    habites = [p for p in r["plages"] if p["nature"] == "HABITE"]
    print(f"{len(r['plages'])} plage(s) >= {r['min_plage_s'] * 1000:.0f} ms sous "
          f"{r['seuil_parole_db']:.0f} dB - {len(morts)} MORTE(s), {len(habites)} HABITEE(s)")
    if not morts and r["plages"]:
        # ⚠️ Le piege qui rend « 0 MORTE » dangereux : sur un COMPOSITE (musique, SFX), la
        # musique remplit tout au-dessus du seuil, alors que les blancs s'entendent encore.
        # Elle masque la mesure, pas l'oreille.
        print()
        print("[!]  AUCUNE plage morte - verifier sur quel fichier. Sur un COMPOSITE (musique,")
        print("    SFX) la musique remplit tout au-dessus du seuil : un master voix peut rendre")
        print("    plusieurs plages mortes quand son composite en rend 0, et les blancs restent")
        print("    audibles. Mesurer sur `<slug>_montage.mp4`, jamais sur le composite.")
    print()
    for titre, lot in (("MORTES - le son tombe sous le seuil : c'est ce qui s'entend comme un blanc",
                        morts),
                       ("HABITEES - un souffle, une amorce : s'entend comme une respiration",
                        habites)):
        if not lot:
            continue
        print(f"### {titre}")
        print(f"  {'master':>17}  {'duree':>7}  {'min':>7}  {'moy':>7}  {'vs seuil':>9}  ou")
        for p in sorted(lot, key=lambda q: -q["duree"]):
            print(f"  {p['debut']:7.3f}->{p['fin']:7.3f}  {p['duree'] * 1000:5.0f} ms  "
                  f"{p['min_db']:6.1f}  {p['moy_db']:6.1f}  {p['hauteur_db']:+8.1f}  "
                  f"{p['ou']}")
            if p["source"]:
                print(f"  {'':17}  {'':>7}  {'':>7}  {'':>7}  {'':>9}  -> {p['source']}")
        print()
    if r["serie"] == "Long":
        print("[!]  LONG-FORM : une partie de ces blancs MORTS peut etre VOULUE (silences")
        print("    pendant une manipulation a l'ecran, une attente). Les ecouter avant de")
        print("    les couper : un blanc mort n'est pas forcement un defaut.")
        print("    Ce rapport TRIE, il ne decide pas.")
    elif r["serie"] == "Ad":
        print("[!]  AD : la tolerance aux blancs depend de la densite de la musique posee")
        print("    par-dessus ; le seuil d'un short ne s'y transpose pas tel quel.")
    # ⚠️ HABITEE ne veut pas dire COURTE : le classement repond a « couper fabriquerait-il un
    # blanc mort ? », jamais a « est-ce trop long ? ». On signale les habitees internes
    # longues, sans rien trancher.
    longues = [q for q in habites
               if q["duree"] >= HABITEE_LONGUE_S and str(q.get("ou", "")).startswith("INTERNE")]
    if longues and r["serie"] != "Long":
        print(f"[!]  {len(longues)} habitee(s) INTERNE(s) de {HABITEE_LONGUE_S * 1000:.0f} ms ou "
              "plus. Ce ne sont PAS des blancs morts, et")
        print("    couper y fabriquerait du vide - mais HABITEE NE VEUT PAS DIRE COURTE : le")
        print("    classement acoustique ne dit pas si la pause est trop longue :")
        for q in sorted(longues, key=lambda z: -z["duree"]):
            print(f"      {q['debut']:7.3f}->{q['fin']:7.3f}  {q['duree'] * 1000:5.0f} ms  "
                  f"{q['ou']}  (min {q['min_db']:.1f} dB)")
        print("    A ECOUTER. Seuil calibre sur UN point : il TRIE, il ne decide pas.")
    print(f"[!]  Le seuil de {r['seuil_mort_db']:.0f} dB est calibre sur 4 points etiquetes "
          f"a l'oreille. Il TRIE,")
    print("    il ne decide pas : deux plages voisines de la limite se lisent sur leurs")
    print("    chiffres. Et un blanc MORT sur un raccord n'a pas de souffle a recuperer : ce")
    print("    qui reste a arbitrer est sa DUREE, donc l'oreille.")


def _force_utf8_stdout():
    """La console Windows est en cp1252 et le rapport de cet outil peut porter des caracteres
    hors latin-1 : sans ceci, l'outil mesure correctement puis meurt sur un UnicodeEncodeError
    a l'affichage, avec un code de sortie non nul.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cible", help="slug de la video, ou chemin d'un master")
    ap.add_argument("--serie", default=None, help="ignore (conserve pour compatibilite)")
    ap.add_argument("--edl", default=None,
                    help="Defaut : <video>/1_montage/edl_v3_timeline.json")
    ap.add_argument("--min", type=float, default=MIN_PLAGE,
                    help=f"duree minimale d'une plage listee (defaut {MIN_PLAGE})")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    p = Path(args.cible)
    if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".wav") and p.exists():
        master, video = p, None
        try:
            video = paths.video_from_path(p)
        except Exception:
            video = None
    else:
        video = paths.video_dir(args.cible, args.serie)
        choix = sorted((paths.media(video, "masters")).glob("*montage*.mp4"))
        if not choix:
            sys.exit(f"Aucun master de montage dans {paths.media(video, 'masters')}")
        master = choisir_master(choix)

    edl = args.edl
    if edl is None and video is not None:
        edl = trouver_edl(paths.montage_dir(video), master.stem)

    r = mesurer(master, edl, args.min)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        rendre(r)


if __name__ == "__main__":
    main()
