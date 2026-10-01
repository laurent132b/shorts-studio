#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Reperer les tics de langage d'un montage, et dire LESQUELS se retirent proprement.

POURQUOI CET OUTIL EXISTE
=========================
Retirer un « euh » interne demande de SCINDER la ligne en deux fragments, avec deux
troncatures complementaires - une troncature ne retirant que des mots aux EXTREMITES d'une
fenetre. La technique est simple, mais rien ne la DECLENCHE si aucune etape du pipeline ne
compte les tics : le seul detecteur devient une relecture complete a l'oreille.

Cet outil est ce declencheur. Il compte, il localise, il CLASSE par cout de retrait, et il
propose la decoupe ; `--appliquer` l'execute. L'arbitrage editorial reste humain.

    python tools/tics.py "videos/<slug>/1_montage"
    python tools/tics.py "videos/<slug>/1_montage" --aussi voila,hein
    python tools/tics.py "videos/<slug>/1_montage" --min-mots 3 --seuil 0.12

Il lit `edl_v3_timeline.json` et les transcripts des rushs - donc il decrit le montage
REEL, pas les rushs bruts. Il ecrit `tics.md` et `tics.json` a cote. A lancer en pre-vol,
apres `assemble.py --plan-only`.

LES QUATRE CLASSES, et c'est tout l'interet
===========================================
    BORD       le tic est le PREMIER ou le DERNIER mot du segment. Il part par une simple
               troncature de l'entree existante : aucun raccord ajoute. Le moins cher.
    SCISSION   interne, isole par du silence, et les deux cotes restent discriminants.
               Le cas nominal. Cout : un raccord de plus.
    COURTE     interne et isole, mais un cote tombe sous la regle des trois mots. La
               decoupe est possible et le selecteur risque de mal reattribuer le fragment
               court. A regarder une par une.
    COLLE      interne, sans silence exploitable de part et d'autre. Couper la s'entend :
               la coupe tombe en pleine chaine parlee. **Laisse en place** dans la decoupe
               proposee, volontairement.

⚠️ UNE LIGNE A N TICS SE DECOUPE EN N+1 FRAGMENTS, PAS EN N DECOUPES A DEUX
==========================================================================
Traiter chaque tic isolement produirait des propositions qui **contiennent encore les autres
tics** de la meme ligne : appliquees telles quelles, elles laisseraient un tic sur deux, et le
rapport aurait l'air complet. Le rapport raisonne donc par LIGNE : il pose tous les points de
coupe d'un coup, et ne coupe QUE sur les tics retirables (les `COLLE` restent a l'interieur
d'un fragment).

⚠️ IL NE MESURE PAS L'AUDIO. Il travaille sur les mots de l'ASR, donc sur ce qui a ete
transcrit. Un « euh » avale ou fondu dans le mot suivant n'y sera pas - le compte est un
PLANCHER, jamais un total. Et l'inverse : `ben` et `voila` sont aussi de vrais mots, d'ou
le lexique par defaut prudent et l'option `--aussi`.
"""

import argparse
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

#: Le lexique par DEFAUT. Volontairement etroit : ces cinq-la ne sont jamais autre chose
#: qu'une hesitation en francais parle. « voila » et « hein » portent du sens (« voila »
#: conclut, « hein » interpelle) et ne sont donc PAS ici - `--aussi voila,hein` pour les
#: ajouter quand on a decide de les traiter.
TICS_DEFAUT = ["euh", "heu", "ben", "bah", "beh"]

#: Silence minimal d'un cote du tic pour que la coupe soit propre. Meme ordre de grandeur
#: que `tighten_cuts.MIN_DEAD_AIR` (0,08) avec de la marge : sous ~0,12 s la coupe tombe
#: dans la coarticulation et s'entend.
SEUIL_ISOLE = 0.12

#: Regle des trois mots : sous ce nombre, le fragment n'est plus discriminant et
#: `select_takes` peut le reattribuer a la mauvaise prise.
MIN_MOTS = 3

#: Borne du nombre de passes de `--appliquer`. La decoupe expose de nouveaux tics de bord,
#: donc elle se repete jusqu'a point fixe (en general 2 ou 3 passes). La borne n'est qu'un
#: garde-fou contre une boucle infinie, pas un reglage.
MAX_PASSES = 6

#: Marge de RESPIRATION laissee de chaque cote d'un tic retire, en secondes, plafonnee a la
#: moitie du silence reellement disponible de ce cote (donc jamais un bout du tic).
#:
#: ⚠️ SANS ELLE, RETIRER UN TIC REINTRODUIT UN DEFAUT : bornes posees pile aux mots, les
#: raccords de tic ne jouent que ~10 ms de silence contre ~140 ms pour un raccord ordinaire,
#: et les mots se collent. Un tic isole est PRECEDE et SUIVI de silence : le retirer sans en
#: rendre un peu supprime la respiration en meme temps que l'hesitation.
#:
#: Elle est volontairement GENEREUSE : `tighten_cuts` la ramenera a la cible du profil
#: (0,14 s pour une continuation en long-form). C'est la meme division du travail que
#: `pass_split_internal` - la decoupe pose large, le resserrage tranche. Poser juste ici
#: dupliquerait la logique de resserrage, et les deux divergeraient.
MARGE_TIC = 0.20

def _force_utf8_stdout():
    """La console Windows est en cp1252 et les messages de cet outil peuvent porter des
    caracteres hors latin-1 : sans ceci, l'outil fait son travail puis meurt sur un
    UnicodeEncodeError a l'affichage, avec un code de sortie non nul - ce qui casse la boucle
    de pre-vol qui l'appelle.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


ORDRE = ["BORD", "SCISSION", "COURTE", "COLLE"]
AIDE = {
    "BORD": "premier/dernier mot du segment -> simple TRONCATURE, aucun raccord ajoute",
    "SCISSION": "interne et isole, deux cotes solides -> scission en deux fragments",
    "COURTE": "interne et isole, mais un cote sous la regle des trois mots",
    "COLLE": "interne SANS silence -> couper la s'entend. LAISSE EN PLACE dans la decoupe",
}


def motif(mots):
    """Un tic peut porter sa ponctuation et s'allonger ('euhhh')."""
    alt = "|".join(re.escape(m) + "+" if m in ("euh", "heu") else re.escape(m) for m in mots)
    return re.compile(rf"^({alt})[\s,.!?…]*$", re.IGNORECASE)


def plier(s):
    return s.strip().lower().replace("’", "'")


def analyser(mdir, lexique, seuil, min_mots, classes_coupe=frozenset({"BORD", "SCISSION"})):
    """Rend (tics, lignes). `lignes` est un OrderedDict indexe par la POSITION du segment.

    ⚠️ LA CLE N'EST PAS `(i, stem)`. Ce couple est unique dans une timeline NEUVE, et cesse de
    l'etre des que cet outil a decoupe une ligne : un segment scinde donne deux entrees qui
    gardent le meme `i` et le meme `stem`. Or `--appliquer` boucle jusqu'au point fixe (cf.
    `main`), et `pass_split_internal` de `tighten_cuts` produit la meme duplication. Avec une
    cle `(i, stem)`, la passe 2 ecraserait silencieusement des lignes : le plan du DERNIER
    segment d'un groupe s'appliquerait a TOUS ses segments, multipliant les fragments et
    fabriquant des fenetres de duree <= 0 (zero image a l'encodage, sans erreur ffmpeg), et
    le rapport sous-compterait sans le dire.

    La cle est donc la POSITION du segment dans la timeline. Elle est exacte parce que
    `analyser` et `appliquer` lisent le MEME fichier, dans le MEME ordre, sans ecriture
    entre les deux - et ce couplage implicite est verifie plutot que suppose : chaque ligne
    porte un `repere`, que `appliquer` recoupe avant de toucher quoi que ce soit.
    """
    import tighten_cuts as tc

    tl_path = mdir / "edl_v3_timeline.json"
    if not tl_path.exists():
        sys.exit(f"timeline absente : {tl_path}\nLancer d'abord `assemble.py --plan-only`.")
    segs = json.loads(tl_path.read_text(encoding="utf-8"))
    if isinstance(segs, dict):
        segs = segs["segments"]
    tdir = tc.find_transcripts(tl_path)
    if not tdir:
        sys.exit(f"transcripts introuvables depuis {tl_path}")

    rx = motif(lexique)
    words_of, tics, lignes = {}, [], OrderedDict()
    for k_seg, s in enumerate(segs):
        stem = s["stem"]
        if stem not in words_of:
            words_of[stem] = tc.load_words(tdir, stem)
        dedans = [w for w in words_of[stem]
                  if w["start"] >= s["in_ext"] - 1e-3 and w["end"] <= s["out_ext"] + 1e-3]
        pos = [k for k, w in enumerate(dedans) if rx.match(plier(w["text"]))]
        if not pos:
            continue
        locaux = []
        for k in pos:
            w = dedans[k]
            av, ap = dedans[:k], dedans[k + 1:]
            gap_av = round(w["start"] - av[-1]["end"], 3) if av else None
            gap_ap = round(ap[0]["start"] - w["end"], 3) if ap else None
            isole = (gap_av is not None and gap_av >= seuil) or \
                    (gap_ap is not None and gap_ap >= seuil)
            if not av or not ap:
                classe = "BORD"
            elif not isole:
                classe = "COLLE"
            elif len(av) < min_mots or len(ap) < min_mots:
                classe = "COURTE"
            else:
                classe = "SCISSION"
            rec = {
                "classe": classe, "ligne": s["i"], "rush": stem,
                "tic": w["text"].strip(), "k": k,
                "t_master": round(s["_cum_start"] + (w["start"] - s["in_ext"]), 2),
                "t_rush": round(w["start"], 3),
                "duree": round(w["end"] - w["start"], 3),
                "gap_avant": gap_av, "gap_apres": gap_ap,
            }
            tics.append(rec)
            locaux.append(rec)

        # ⚠️ LA DECOUPE SE FAIT PAR LIGNE, tous points de coupe poses d'un coup, et
        # SEULEMENT sur les tics retirables. Un `COLLE` reste dans son fragment.
        coupes = [r["k"] for r in locaux if r["classe"] in classes_coupe]

        # ⚠️ LA DISCRIMINANCE SE VERIFIE SUR LES FRAGMENTS, PAS SUR L'AVANT/APRES DU TIC.
        # Un tic est classe SCISSION quand il reste au moins `min_mots` de CHAQUE cote -
        # mais ce compte porte sur toute la ligne, alors que la decoupe finale produit
        # N+1 fragments bien plus courts. Sur une ligne a plusieurs tics, deux SCISSION
        # valides isolement fabriqueraient un fragment d'UN MOT entre elles : trois jump cuts
        # en quelques secondes pour retirer deux « euh ». Un « euh » laisse en place coute
        # moins qu'un hoquet.
        #
        # On retient donc une coupe seulement si le fragment qu'elle FERME et celui
        # qu'elle OUVRE tiennent la regle. La borne droite se prend au prochain tic
        # candidat : minorant sur, puisqu'une coupe refusee ne fait qu'AGRANDIR ce
        # fragment. Un tic dont la coupe est refusee redevient COURTE - la classe dit
        # exactement ca - et n'est donc plus retire par defaut.
        # ⚠️ Un cote VIDE ne fabrique aucun fragment : c'est le cas d'un BORD (le tic est
        # le premier ou le dernier mot), qui est une simple troncature. Le confondre avec
        # un fragment court ferait rejeter TOUS les BORD.
        retenues, ouvert = [], 0
        for rang, k in enumerate(coupes):
            droite = (coupes[rang + 1] if rang + 1 < len(coupes) else len(dedans)) - (k + 1)
            gauche = k - ouvert
            if (gauche == 0 or gauche >= min_mots) and (droite == 0 or droite >= min_mots):
                retenues.append(k)
                ouvert = k + 1
        for r in locaux:
            if r["k"] in coupes and r["k"] not in retenues:
                r["classe"] = "COURTE"
        coupes = retenues

        frags, debut = [], 0
        for k in coupes + [len(dedans)]:
            morceau = dedans[debut:k]
            if morceau:
                # Les BORNES, en temps de rush. `premier`/`dernier` disent quel bord herite
                # du bord du segment d'origine plutot que du mot : on ne veut perdre ni la
                # marge d'attaque posee par `compute_cuts`, ni la queue que les planchers de
                # `tighten_cuts` viennent de defendre.
                # la marge se prend dans le silence qui entourait le tic, jamais dedans :
                # au plus la MOITIE de l'ecart, donc la borne reste hors du mot-tic.
                m_deb = m_fin = 0.0
                if debut > 0:
                    ecart = morceau[0]["start"] - dedans[debut - 1]["end"]
                    m_deb = min(MARGE_TIC, max(0.0, ecart) / 2)
                if k < len(dedans):
                    ecart = dedans[k]["start"] - morceau[-1]["end"]
                    m_fin = min(MARGE_TIC, max(0.0, ecart) / 2)
                frags.append({"texte": " ".join(x["text"] for x in morceau).strip(),
                              "mots": len(morceau),
                              "debut": round(morceau[0]["start"] - m_deb, 3),
                              "fin": round(morceau[-1]["end"] + m_fin, 3),
                              "premier": debut == 0,
                              "dernier": k == len(dedans)})
            debut = k + 1
        lignes[k_seg] = {
            "ligne": s["i"], "rush": stem, "seg": k_seg,
            # Le REPERE de la ligne : ce que `appliquer` doit RETROUVER a cette position
            # avant d'y toucher. Les positions bougent des qu'une timeline est modifiee :
            # un indice ne se fait pas confiance, il se recoupe.
            "repere": [s["i"], stem, s["in_ext"], s["out_ext"]],
            "t_master": round(s["_cum_start"], 2),
            "texte_origine": s["text"].strip(),
            "tics": locaux,
            "n_retires": len(coupes),
            "fragments": frags,
            "raccords_ajoutes": max(0, len(frags) - 1),
            "fragments_courts": [f for f in frags if f["mots"] < min_mots],
        }
    return tics, lignes


def tc_hms(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def _fabriquer(s, f, j):
    """Le segment issu d'un fragment. Un seul endroit qui pose des bornes, donc un seul a
    verifier.

    ⚠️ `tic_part` est POSE OU RETIRE, jamais herite. `dict(s)` recopie tout, donc un
    `tic_part` d'une passe precedente survivrait a une troncature de la passe suivante, et
    ferait chercher la cause d'un defaut au mauvais endroit.

    Les bornes fabriquees ici sont verifiees en sortie de `appliquer`, par la MEME fonction
    que `assemble.py` et `audit_edl.py` - recopier ici le predicat `out_ext <= in_ext` en
    ferait une seconde verite tenue d'etre egale a l'autre.
    """
    part = dict(s)
    part["in_ext"] = round(s["in_ext"] if f["premier"] else f["debut"], 3)
    part["out_ext"] = round(s["out_ext"] if f["dernier"] else f["fin"], 3)
    part["text"] = f["texte"]
    part.pop("punch", None)      # une decoupe invalide un punch calcule avant
    part.pop("tic_part", None)
    if j is not None:
        part["tic_part"] = j
    return part


def appliquer(mdir, lignes, classes, min_mots):
    """Decoupe la timeline aux tics retirables. Rend (n_lignes, n_raccords, refus).

    ⚠️ CE N'EST PAS UNE NOUVELLE MECANIQUE - c'est celle de `pass_split_internal` de
    `tighten_cuts`, appliquee a un autre motif. Les bornes se posent aux MOTS et le
    raccord qui en resulte est laisse a `tighten_cuts`, qui le ramenera a la cible de
    son profil. Une passe qui couperait ET resserrerait dupliquerait cette logique, et
    les deux divergeraient.

    POURQUOI UN OUTIL PLUTOT QU'UNE SCISSION A LA MAIN. La scission par entrees
    complementaires dans le fichier de selection est juste, mais son cout croit avec le
    format : sur un long-form elle demande des dizaines d'entrees ecrites a la main, et ne
    se fait donc pas. Deux differences de FOND avec la voie manuelle, et elles vont dans le
    bon sens : la decoupe ne repasse pas par `select_takes`, donc un fragment court ne peut
    pas tomber en TROU ; et elle est rejouable, donc elle ne se perime pas au remontage.

    ⚠️ CETTE PASSE N'EST PAS IDEMPOTENTE PAR CONSTRUCTION. Apres la decoupe un tic n'est plus
    dans aucun segment, mais la PASSE se rejoue sur ses propres fragments : c'est ce qui rend
    la cle par position (cf. `analyser`) et la verification des fenetres en sortie
    indispensables. *Une affirmation de verification qui porte a cote de l'endroit ou le
    defaut vit est pire qu'aucune : elle CLOT la question.*
    """
    tl_path = mdir / "edl_v3_timeline.json"
    segs = json.loads(tl_path.read_text(encoding="utf-8"))
    if isinstance(segs, dict):
        segs = segs["segments"]

    plan, refus = {}, []
    for k_seg, L in lignes.items():
        gardes = [t for t in L["tics"] if t["classe"] in classes]
        if not gardes:
            continue
        courts = [f for f in L["fragments"] if f["mots"] < min_mots]
        if courts and "COURTE" not in classes:
            # une COURTE non demandee peut quand meme naitre d'une SCISSION voisine :
            # on la signale plutot que de la couper en silence
            refus.append((L["ligne"], L["rush"], [f["texte"][:40] for f in courts]))
        plan[k_seg] = L

    out, n_rac, n_tronc = [], 0, 0
    for k_seg, s in enumerate(segs):
        L = plan.get(k_seg)
        if L is None:
            out.append(s)
            continue
        # ⚠️ LE REPERE. `analyser` et `appliquer` relisent le meme fichier dans le meme
        # ordre, donc les positions s'alignent - mais cet alignement est un couplage
        # IMPLICITE entre deux fonctions, et un couplage implicite qui se rompt est
        # exactement ce qui a produit le defaut d'origine. On le recoupe.
        vu = [s["i"], s["stem"], s["in_ext"], s["out_ext"]]
        if vu != L["repere"]:
            sys.exit(
                f"segment {k_seg} : la timeline a change entre l'analyse et la decoupe."
                + chr(10) + f"  attendu : {L['repere']}"
                + chr(10) + f"  trouve  : {vu}"
                + chr(10) + "  Appliquer un plan sur un segment qui n'est plus le sien"
                + " decouperait AILLEURS, en silence. Relancer l'outil."
            )
        frags = L["fragments"]
        if not frags:
            # la ligne entiere n'etait qu'un tic : rien a fabriquer, on la laisse telle quelle
            out.append(s)
            continue
        if len(frags) == 1:
            # ⚠️ UN SEUL FRAGMENT N'EST PAS « RIEN A FAIRE » - c'est le cas du BORD. Un tic
            # en premier ou dernier mot se retire par simple TRONCATURE : le plan ne porte
            # alors qu'un fragment, et une garde `len < 2` rendrait le segment intact alors
            # que l'outil annonce « BORD -> simple troncature ». Un plan sans coupe se
            # reconnait a `premier ET dernier`, jamais au NOMBRE de fragments.
            f = frags[0]
            if f["premier"] and f["dernier"]:
                out.append(s)
                continue
            out.append(_fabriquer(s, f, None))
            n_tronc += 1
            continue
        for j, f in enumerate(frags):
            out.append(_fabriquer(s, f, j))
        n_rac += len(frags) - 1

    # ⚠️ ON REFUSE D'ECRIRE CE QU'ON VIENT DE FABRIQUER FAUX. Un outil qui pose des bornes
    # doit refuser les siennes : `assemble.py` refuse aussi, mais lui arrive APRES - ici on
    # nomme le coupable a la source, avant que le fichier ne soit touche. La verification
    # est celle d'`audit_edl`, partagee et non recopiee.
    from audit_edl import exiger_fenetres_positives
    exiger_fenetres_positives(out, f"decoupe des tics de {tl_path}")

    tl_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    # ⚠️ `plan` porte desormais la LIGNE, pas ses fragments : compter `len(L)` compterait
    # ses CLES (toujours > 1), donc ce retour ne retomberait jamais a zero et la boucle a
    # point fixe de `main` tournerait jusqu'a MAX_PASSES en ne faisant plus rien.
    return (len([1 for L in plan.values() if len(L["fragments"]) > 1]) + n_tronc,
            n_rac, refus, n_tronc)


def main():
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("montage", help="le dossier videos/<slug>/1_montage")
    ap.add_argument("--aussi", default="",
                    help="mots a AJOUTER au lexique, separes par des virgules "
                         "(p.ex. voila,hein) - ils portent du sens, donc pas par defaut")
    ap.add_argument("--seuil", type=float, default=SEUIL_ISOLE,
                    help=f"silence minimal d'un cote pour couper proprement (defaut {SEUIL_ISOLE}s)")
    ap.add_argument("--min-mots", type=int, default=MIN_MOTS,
                    help=f"regle de discriminance des fragments (defaut {MIN_MOTS})")
    ap.add_argument("--appliquer", action="store_true",
                    help="DECOUPE la timeline aux tics retirables (par defaut l'outil ne "
                         "fait que classer). A lancer APRES assemble --plan-only et AVANT "
                         "tighten_cuts, qui resserrera les raccords ainsi crees.")
    ap.add_argument("--classes", default="BORD,SCISSION",
                    help="classes a retirer avec --appliquer (defaut BORD,SCISSION). "
                         "COLLE n'est jamais retirable : la coupe s'entendrait. COURTE "
                         "s'ajoute explicitement, un cote passant sous la discriminance.")
    args = ap.parse_args()

    mdir = Path(args.montage)
    if not mdir.is_dir():
        sys.exit(f"dossier introuvable : {mdir}")
    lexique = TICS_DEFAUT + [m.strip() for m in args.aussi.split(",") if m.strip()]

    classes = frozenset(c.strip().upper() for c in args.classes.split(",") if c.strip())
    if "COLLE" in classes:
        sys.exit("COLLE n'est pas retirable : le tic n'est precede d'aucun silence, donc "
                 "la coupe s'entend. C'est le seul refus de cet outil.")
    tics, lignes = analyser(mdir, lexique, args.seuil, args.min_mots, classes)
    if not tics:
        print(f"Aucun tic trouve (lexique : {', '.join(lexique)}).")
        print("[!] Le compte est un PLANCHER : un 'euh' fondu dans le mot suivant "
              "n'est pas transcrit, donc pas vu ici.")
        return 0

    par_classe = {c: [x for x in tics if x["classe"] == c] for c in ORDRE}
    total_s = sum(x["duree"] for x in tics)
    multi = [v for v in lignes.values() if len(v["tics"]) > 1]
    raccords = sum(v["raccords_ajoutes"] for v in lignes.values())
    courts = sum(len(v["fragments_courts"]) for v in lignes.values())

    print(f"lexique : {', '.join(lexique)}   |   seuil d'isolement {args.seuil}s   |   "
          f"discriminance {args.min_mots} mots\n")
    print(f"{len(tics)} tic(s) sur {len(lignes)} ligne(s), {total_s:.1f} s de tic pur\n")
    for c in ORDRE:
        print(f"  {c:<9} {len(par_classe[c]):>3}   "
              f"{sum(x['duree'] for x in par_classe[c]):5.1f} s   {AIDE[c]}")
    compte = {}
    for x in tics:
        m = plier(x["tic"]).rstrip(",.!?… ")
        compte[m] = compte.get(m, 0) + 1
    print("\n  par mot : " + " | ".join(f"{k} {v}" for k, v in
                                        sorted(compte.items(), key=lambda kv: -kv[1])))
    print(f"\n  {len(multi)} ligne(s) portent PLUSIEURS tics - elles se decoupent en une "
          "seule passe,\n  pas en decoupes successives (cf. l'en-tete de l'outil).")
    print(f"  cout de la decoupe complete : +{raccords} raccord(s)"
          + (f", dont {courts} fragment(s) sous {args.min_mots} mots a arbitrer"
             if courts else ""))

    if args.appliquer:
        # ⚠️ LA DECOUPE EXPOSE DE NOUVEAUX TICS DE BORD, DONC ELLE SE REPETE.
        # Retirer un tic interne cree deux bornes ; un tic jusque-la interne peut s'y
        # retrouver PREMIER ou DERNIER mot, donc devenir un BORD retirable. On boucle donc
        # ici, jusqu'a point fixe : laisser l'appelant relancer, c'est compter sur quelqu'un
        # qui y pense.
        n_lig = n_rac = n_tronc = 0
        refus, passes = [], 0
        while passes < MAX_PASSES:
            passes += 1
            l, r, ref, t = appliquer(mdir, lignes, classes, args.min_mots)
            n_lig += l; n_rac += r; n_tronc += t
            refus = ref or refus
            if l == 0:
                break
            tics, lignes = analyser(mdir, lexique, args.seuil, args.min_mots, classes)
        else:
            print(f"[!] {MAX_PASSES} passes sans point fixe - l'outil s'arrete la. "
                  "Ce n'est jamais arrive : verifier avant de lever la borne.")
        print(f"\n-- DECOUPE APPLIQUEE : {n_lig} ligne(s), +{n_rac} raccord(s), "
              f"{n_tronc} troncature(s) de bord, {passes} passe(s) jusqu'au point fixe "
              f"[classes {','.join(sorted(classes))}]")
        for i, stem, txt in refus:
            print(f"   [!] L{i} ({stem}) : fragment(s) sous {args.min_mots} mots -> {txt}")
        print("   -> relancer `tighten_cuts.py` pour resserrer les nouveaux raccords.")

    rap = mdir / "tics.md"
    with rap.open("w", encoding="utf-8") as f:
        f.write("# Tics de langage du montage\n\n")
        f.write(f"Lexique `{', '.join(lexique)}` | seuil d'isolement {args.seuil} s | "
                f"discriminance {args.min_mots} mots.\n\n")
        f.write(f"**{len(tics)} tics sur {len(lignes)} lignes, {total_s:.1f} s de tic pur.** "
                "Le compte est un PLANCHER : un tic avale n'est pas transcrit, donc pas vu.\n\n")
        f.write("| classe | n | duree | quoi en faire |\n|---|---:|---:|---|\n")
        for c in ORDRE:
            f.write(f"| `{c}` | {len(par_classe[c])} | "
                    f"{sum(x['duree'] for x in par_classe[c]):.1f} s | {AIDE[c]} |\n")
        f.write(f"\nDecoupe complete : **+{raccords} raccords**, "
                f"{len(multi)} ligne(s) a plusieurs tics")
        f.write(f", {courts} fragment(s) sous {args.min_mots} mots.\n" if courts else ".\n")
        f.write("\n> [!] Une ligne a N tics se decoupe en **N+1 fragments d'un coup**. Les "
                "fragments ci-dessous sont deja complets : ne pas les recouper tic par tic.\n")
        f.write("\n---\n")
        for v in lignes.values():
            etiquettes = " ".join(f"`{r['classe']}`" for r in v["tics"])
            f.write(f"\n## L{v['ligne']} | {v['rush']} | master {tc_hms(v['t_master'])} | "
                    f"{len(v['tics'])} tic(s) {etiquettes}\n\n")
            f.write(f"> {v['texte_origine']}\n\n")
            for r in v["tics"]:
                ga = "-" if r["gap_avant"] is None else f"{r['gap_avant'] * 1000:.0f} ms"
                gp = "-" if r["gap_apres"] is None else f"{r['gap_apres'] * 1000:.0f} ms"
                f.write(f"- '{r['tic']}' a {tc_hms(r['t_master'])} "
                        f"({r['duree'] * 1000:.0f} ms) | silence avant {ga}, apres {gp} "
                        f"| **{r['classe']}**\n")
            garde = [r for r in v["tics"] if r["classe"] == "COLLE"]
            if garde:
                f.write(f"\n{len(garde)} tic(s) `COLLE` **laisse(s) en place** : les couper "
                        "s'entendrait.\n")
            if v["n_retires"] == 0:
                f.write("\nRien a decouper sur cette ligne.\n")
                continue
            f.write(f"\ndecoupe en {len(v['fragments'])} fragment(s), "
                    f"+{v['raccords_ajoutes']} raccord(s) :\n\n```python\n")
            for fr in v["fragments"]:
                marque = "   # [!] " + f"{fr['mots']} mots" if fr["mots"] < args.min_mots else ""
                f.write(f'    "{fr["texte"]}",{marque}\n')
            f.write("```\n")

    (mdir / "tics.json").write_text(
        json.dumps({"lexique": lexique, "seuil": args.seuil, "min_mots": args.min_mots,
                    "tics": tics,
                    # ⚠️ `ligne:rush` ne designe PAS un segment : deux segments d'une
                    # meme ligne scindee la partagent. La position les separe.
                    "lignes": [dict(v, cle=f"{v['ligne']}:{v['rush']}#{v['seg']}")
                               for v in lignes.values()]},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nOK -> {rap}")
    print(f"OK -> {mdir / 'tics.json'}")
    # Ce rappel ne s'imprime que sans `--appliquer` : sinon la sortie affirmerait a la fois
    # « DECOUPE APPLIQUEE » et « rien n'est applique », et le reflexe serait de relancer pour
    # voir - donc de rejouer une decoupe qui n'est PAS idempotente.
    if not args.appliquer:
        print("\n[!] Rien n'est applique : cet outil CLASSE et PROPOSE, il ne coupe pas.")
        print("   Pour appliquer la decoupe : --appliquer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
