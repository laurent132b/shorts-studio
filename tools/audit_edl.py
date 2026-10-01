"""Les controles d'un EDL que AUCUN rapport ne porte.

`rapport_selection.md` et `qa_montage.json` disent beaucoup de choses justes, mais il y a
des defauts qu'ils ne voient pas, et qui ne se manifestent par aucune erreur : le montage
s'assemble, la duree est juste, le SRT est coherent.

1. PROVENANCE. `select_takes` cherche la MEILLEURE fenetre dans TOUS les rushs, pas
   seulement dans celui que la selection designe. Une ligne courte et peu discriminante
   (« d'inscription », « workflow. ») se fait donc servir par un autre rush - qui passe alors
   pour consomme jusqu'a ce point, et emporte ses propres lignes en trou, plus loin, sans que
   rien ne relie la cause a l'effet.

   ⚠️ Une SUPPRESSION ailleurs dans le script peut changer la provenance ICI : retirer un
   passage libere un rush, et des lignes d'un autre rush se font aussitot servir par lui.
   Donc ce controle se relance apres CHAQUE lot de coupes, pas une fois.

2. ORDRE. Sur un screencast, la suite des rushs doit rester croissante : un retour en
   arriere fait rejouer une image deja vue sous un propos qui a avance.

   ⚠️ CE CONTROLE REGARDE LE RUSH, PAS LE TEMPS - donc il peut rendre ECHEC sur un cas
   parfaitement sain, et le message (« rush_002 revient, deja vu a L0 ») ne dit pas lequel
   des deux on a. Une accroche et une promesse tournees dans une seule prise reviennent au
   meme FICHIER sans revenir dans le temps. Le geste, avant de conclure : comparer les
   `in_ext` des lignes en cause DANS le rush. S'ils croissent, c'est un retour au fichier et
   non dans le temps. Le risque vise est reel sur un screencast (l'ecran a change entre-temps)
   et quasi nul sur un plan face camera, ou revenir au meme rush garde le meme cadrage. Un
   controle qui refuse trop est moins cher qu'un controle qui laisse passer, mais il faut
   savoir le lire.

3. CHEVAUCHEMENT. Deux fenetres voisines du meme rush qui se recouvrent rejouent un fragment
   d'audio (« pa-par », « for-force »). `tighten_cuts` les ecrete et `qa_montage` les mesure
   (OVERLAPPING_CUT) ; c'est repris ici pour avoir les controles d'un seul coup.

   ⚠️ Le test porte sur `out_ext`/`in_ext`, PAS sur `start`/`end`. Les seconds sont les
   bornes des MOTS datees par l'ASR, les premieres les bornes de COUPE - seules honorees par
   ffmpeg. `assemble` pose une marge autour des mots et `tighten_cuts` ecrete `out_ext` sans
   toucher aux mots : mesurer `start`/`end` donnerait de FAUX chevauchements. Corollaire : le
   controle 3 ne veut rien dire sur un `edl_v3.json` non planifie, et le dit au lieu de
   repondre « 0 ».

4. FENETRES DE COUPE. Une fenetre INVERSEE (`out_ext` < `in_ext`) rend ZERO IMAGE, et ffmpeg
   ne dit rien : ni erreur, ni avertissement, code de sortie 0. Le master est ampute de cette
   parole, et il est FAUX ET CONSISTANT - les mots d'une fenetre inversee n'entrent pas non
   plus dans le SRT, donc le son et le texte s'accordent parfaitement. Cf. la docstring de
   `fenetres_inversees`.

   Ce controle est aussi pose EN REFUS dans `assemble.py` et `tics.py --appliquer` : ici il
   faut le lancer, la il est sur le chemin de PRODUCTION. La verification est partagee
   (`exiger_fenetres_positives`), pas dupliquee - deux copies d'un meme invariant sont deux
   verites tenues d'etre egales, et elles divergent.

    python tools/audit_edl.py videos/<slug>/1_montage
    python tools/audit_edl.py videos/<slug>/1_montage --edl <un autre edl>.json

Sortie : 0 si les controles passent, 1 sinon.
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path


# La console Windows est en cp1252 et les messages de cet outil peuvent porter des caracteres
# hors latin-1. Sans ceci, l'outil fait son travail puis meurt sur un UnicodeEncodeError a
# l'affichage, avec un code de sortie non nul.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
OVERLAP_S = 0.030
COURT_MOTS = 3

#: En dessous, un segment ne porte plus assez de parole pour valoir un raccord : c'est
#: presque toujours un mot TRONQUE (un « lot. » de 385 ms rendu en 90 ms).
#: ⚠️ NON BLOQUANT, et volontairement : c'est un SIGNAL, pas une regle. Un seuil chiffre dans
#: un outil partage peut etre juste pour un format et faux pour un autre ; un avertissement
#: ne peut pas degrader un montage, un refus si.
PLANCHER_UTILE_S = 0.15


def fenetres_inversees(segs, plancher=PLANCHER_UTILE_S):
    """Les fenetres de coupe qui ne peuvent rendre AUCUNE image, et celles qui en rendent
    a peine. Rend deux listes de tuples `(position, segment, duree)`.

    ⚠️ CE QUE CA PROTEGE. Un segment dont `out_ext` est INFERIEUR a `in_ext` devient chez
    `assemble.py` un `trim=start=171.120:end=170.790`, et **ffmpeg rend zero image, sans
    erreur, sans avertissement, code de sortie 0**. La parole du segment disparait du master.

    Et le master est FAUX ET CONSISTANT, donc invisible a la relecture : `assemble.py` filtre
    les mots du SRT sur `new_end > new_start`, donc aucun mot d'une fenetre inversee n'entre
    dans les sous-titres. La video n'a pas l'air cassee, elle a l'air montee serre. Il faut
    comparer au RUSH pour voir le trou : ce controle doit donc etre MECANIQUE.

    Troisieme degat, le moins visible : `build_master_srt` fait `cum += seg_dur` avec un
    `seg_dur` NEGATIF, donc tous les `_cum_start` posterieurs sont faux - et c'est ce que
    lisent `punch.py`, `blancs.py`, `qa_montage.py` et tout ancrage sur le master.

    ⚠️ CE REFUS EST DE LA CATEGORIE SURE. `out_ext <= in_ext` n'est pas un reglage de
    calibration : il ne depend d'aucun format, et **ne peut refuser que ce qui est deja
    casse**. Il ne peut donc pas degrader un montage sain.
    """
    inversees, courtes = [], []
    for k, s in enumerate(segs):
        a, b = s.get("in_ext"), s.get("out_ext")
        if a is None or b is None:
            continue
        d = round(float(b) - float(a), 4)
        if d <= 0:
            inversees.append((k, s, d))
        elif d < plancher:
            courtes.append((k, s, d))
    return inversees, courtes


def _decrire(k, s, d):
    return (f"   segment {k:<4} L{s.get('i')} {s.get('stem')}  "
            f"in {float(s['in_ext']):.3f} -> out {float(s['out_ext']):.3f}  "
            f"= {d * 1000:+.0f} ms"
            + chr(10)
            + f"        '{str(s.get('text', ''))[:78]}'")


def exiger_fenetres_positives(segs, source, plancher=PLANCHER_UTILE_S):
    """Le meme controle, en REFUS - pour les appelants qui sont sur le chemin de PRODUCTION.

    Un avertissement suffit a `audit_edl` : on le lance pour le lire, c'est son metier. Il
    ne suffit PAS a `assemble.py`, qui ENCODE. *Le controle doit porter sur le chemin de
    PRODUCTION, pas sur une reconstitution de ce chemin.*

    Rend la liste des fenetres courtes (non bloquantes) ; s'arrete sur une inversee.
    """
    inversees, courtes = fenetres_inversees(segs, plancher)
    for k, s, d in courtes:
        print(f"[fenetres] /!\\ segment {k} (L{s.get('i')} {s.get('stem')}) ne dure que "
              f"{d * 1000:.0f} ms - presque toujours un mot TRONQUE : "
              f"'{str(s.get('text', ''))[:60]}'")
    if inversees:
        lignes = [
            "",
            f"FENETRE(S) INVERSEE(S) - {len(inversees)} segment(s) de duree nulle ou negative.",
            f"  source : {source}",
            "",
        ]
        lignes += [_decrire(k, s, d) for k, s, d in inversees]
        lignes += [
            "",
            "  ffmpeg rendrait ZERO IMAGE sur ces segments, SANS erreur : le master serait",
            "  ampute de leur parole, et le SRT serait coherent avec l'image (les mots d'une",
            "  fenetre inversee n'y entrent pas) : le master serait faux ET coherent, donc",
            "  invisible a la relecture.",
            "",
            "  Ce refus n'a pas d'echappatoire : il ne refuse que ce qui est DEJA casse.",
            "  Reparer la borne (`in_ext` / `out_ext`) du segment, puis relancer.",
            "",
        ]
        sys.exit(chr(10).join(lignes))
    return courtes


def charge_selection(montage):
    """`SELECTION` de `build_script.py`, a plat : un stem attendu par ligne de script.

    Le fichier est importe par chemin, pas par nom : il vit dans le dossier de la
    video, pas sur le `sys.path`, et chaque video a le sien.
    """
    bs_path = montage / "build_script.py"
    if not bs_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(f"build_script_{montage.parent.name}", bs_path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(montage))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(montage))
    return [e[0] for e in mod.SELECTION if e[0] != "--"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("montage", type=Path, help="dossier 1_montage de la video")
    ap.add_argument("--edl", default="edl_v3_timeline.json",
                    help="EDL a auditer, relatif au dossier de montage (defaut : la timeline)")
    args = ap.parse_args()

    montage = args.montage.resolve()
    edl_path = montage / args.edl
    if not edl_path.exists():
        print(f"introuvable : {edl_path}")
        return 2

    data = json.loads(edl_path.read_text(encoding="utf-8"))
    segs = data["segments"] if isinstance(data, dict) else data
    attendu = charge_selection(montage)

    print(f"{edl_path.name} : {len(segs)} segments")
    echec = False

    # 1. provenance
    print("\n1. PROVENANCE")
    if attendu is None:
        print("   NON APPLICABLE - pas de build_script.py, rien qui dise quel rush est attendu.")
    elif not segs or max(s["i"] for s in segs) >= len(attendu):
        # ⚠️ L'appariement se fait par `s["i"]` (l'index de LIGNE), pas par position :
        # `tighten_cuts` et `tics.py` SCINDENT une ligne en plusieurs segments, donc il peut
        # y avoir plus de segments que de lignes. Comparer `len(segs)` a `len(attendu)`
        # abandonnerait le controle sur tout montage scinde ; il suffit que chaque index
        # tombe dans les bornes.
        haut = max(s["i"] for s in segs) if segs else -1
        print(f"   [!!] ligne {haut} citee par l'EDL pour {len(attendu)} lignes de "
              "SELECTION : l'appariement serait faux, controle abandonne.")
        print("   Relancer build_script.py puis select_takes.py avant d'auditer.")
        echec = True
    else:
        ecarts = [(s["i"], attendu[s["i"]], s["stem"], s["text"])
                  for s in segs if s["stem"] != attendu[s["i"]]]
        print(f"   {len(ecarts)} ecart(s)")
        for i, veut, a, txt in ecarts:
            print(f"   L{i:<4} attendu {veut}  obtenu {a}   '{txt[:70]}'")
        echec = echec or bool(ecarts)

    # 2. ordre des rushs
    print("\n2. ORDRE")
    vus, ordre, retours = {}, [], []
    for s in segs:
        if not ordre or ordre[-1] != s["stem"]:
            if s["stem"] in vus:
                retours.append((s["i"], s["stem"], vus[s["stem"]]))
            ordre.append(s["stem"])
            vus[s["stem"]] = s["i"]
    print(f"   suite : {' '.join(o.replace('rush_', '') for o in ordre)}")
    print(f"   {len(retours)} retour(s) en arriere")
    for i, stem, prem in retours:
        print(f"   L{i:<4} {stem} revient (deja vu a L{prem})")
    echec = echec or bool(retours)

    # 3. chevauchements, sur les bornes de COUPE
    print(f"\n3. CHEVAUCHEMENTS (> {OVERLAP_S * 1000:.0f} ms)")
    if any(s.get("in_ext") is None for s in segs):
        print("   NON APPLICABLE - pas de bornes de coupe dans ce fichier.")
        print("   Ce controle ne vaut que sur une timeline planifiee par assemble.py.")
    else:
        chev = [(b["i"], a["stem"], a["out_ext"] - b["in_ext"])
                for a, b in zip(segs, segs[1:])
                if a["stem"] == b["stem"] and a["out_ext"] - b["in_ext"] > OVERLAP_S]
        print(f"   {len(chev)} chevauchement(s)")
        for i, stem, d in chev:
            print(f"   L{i:<4} {stem}  rejoue {d:.3f} s")
        echec = echec or bool(chev)

    # 4. les lignes courtes - pas un echec, le facteur de RISQUE du controle 1
    print(f"\n4. LIGNES COURTES (<= {COURT_MOTS} mots) - non bloquant, c'est ce qui se fait voler")
    for s in segs:
        n = len(s["text"].split())
        if n > COURT_MOTS:
            continue
        vole = attendu is not None and len(segs) == len(attendu) and s["stem"] != attendu[s["i"]]
        print(f"   L{s['i']:<4} {n} mot(s)  {s['stem']}  '{s['text']}'"
              f"{'  [!!] MAUVAIS RUSH' if vole else ''}")

    # 5. les fenetres de coupe - le seul controle qui porte sur ce que ffmpeg RENDRA
    #
    # ⚠️ Il est numerote 5 alors que l'en-tete du module le decrit en 4e : le 4 de
    # l'affichage (« LIGNES COURTES ») a toujours ete non bloquant, et renumeroter
    # perimerait les notes existantes qui citent les controles par leur numero.
    print(f"{chr(10)}5. FENETRES DE COUPE")
    if any(s.get("in_ext") is None for s in segs):
        print("   NON APPLICABLE - pas de bornes de coupe dans ce fichier.")
        print("   Ce controle ne vaut que sur une timeline planifiee par assemble.py.")
    else:
        inversees, courtes = fenetres_inversees(segs)
        print(f"   {len(inversees)} fenetre(s) inversee(s), "
              f"{len(courtes)} sous {PLANCHER_UTILE_S * 1000:.0f} ms")
        for k, s, d in inversees:
            print(f"   L{s['i']:<4} {s['stem']}  in {s['in_ext']:.3f} -> "
                  f"out {s['out_ext']:.3f} = {d * 1000:+.0f} ms  "
                  f"-> ZERO IMAGE, sans erreur ffmpeg")
            print(f"          '{str(s.get('text', ''))[:70]}'")
        for k, s, d in courtes:
            print(f"   L{s['i']:<4} {s['stem']}  {d * 1000:.0f} ms seulement "
                  f"(mot tronque ?) '{str(s.get('text', ''))[:50]}'  - non bloquant")
        echec = echec or bool(inversees)

    print(f"\n=> {'ECHEC' if echec else 'les quatre controles passent'}")
    return 1 if echec else 0


if __name__ == "__main__":
    raise SystemExit(main())
