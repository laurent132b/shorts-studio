"""
tools/punch.py - rythmer un montage par des changements de CADRE (punch-in).

Alterne le cadrage entre un etat large (100 %) et un etat serre (~115 %) d'un segment
d'EDL a l'autre, pour donner le rythme de plan attendu sur un short vertical. Ecrit une
cle `punch` par segment dans `edl_v3_timeline.json` ; `assemble.py --honor-punch` la lit
et l'applique dans le meme graphe ffmpeg que le concat.

    python tools/punch.py plan videos/<V>/1_montage/h1/edl_v3_timeline.json
    python tools/punch.py plan <timeline.json> --scale-in 1.15 --anchor 540,700
    python tools/punch.py stills <master.mp4> --at 8.5 --scales 1.0,1.10,1.15,1.20

## Pourquoi le decoupage se cale sur l'EDL et pas sur un metronome

Les bonnes pratiques Reels/TikTok visent un changement de plan toutes les 2-3 s, et la
duree mediane d'un segment d'EDL de short tombe deja dans cet ordre de grandeur. Un
metronome independant poserait des bascules au milieu d'un mot ; se caler sur les segments
donne "un cut = un cadrage", gratuitement puisque chaque segment a deja son propre `trim`.

La regle d'alternance ne bascule donc pas a chaque segment : a chaque frontiere elle garde
celle qui tombe le plus pres de `--target-plan-s` (defaut 2,5 s), sans jamais descendre
sous `--min-plan-s`. Un segment tres court ne fait pas un plan, il s'absorbe.

## ATTENTION - Le punch est un AGRANDISSEMENT, il n'est pas gratuit en nettete

Sur des rushs en 1080x1920 il n'y a AUCUNE marge de recadrage : un punch a 115 % recadre
940x1670 puis re-etire en 1080x1920. C'est un vrai upscale, sur une source deja
compressee. D'ou la sous-commande `stills` : arbitrer l'amplitude en REGARDANT une
planche, avant d'encoder quoi que ce soit. Un tournage en 4K vertical rendrait le punch
gratuit (jusqu'a ~200 %) ; cela se decide a la prise de vue, pas ici.

## Le referentiel des bornes - RUSH, et pas master

Les `in`/`out` des pas de `punch` sont en temps RUSH : ils couvrent exactement
`[in_ext, out_ext]` du segment. Tout consommateur autre qu'`assemble.py` doit convertir -
`t_master = _cum_start + (pas.in - in_ext)` - et REFUSER si les pas ne couvrent plus ce
segment. Sur une source CONTINUE (un seul rush) les deux referentiels donnent le meme
nombre, donc l'erreur y est invisible.

## ATTENTION - Un punch DEPLACE le visage

Le punch deplace le visage : il se decide AVANT l'habillage, qui se pose sur le master
punche. Toute mesure de position du visage (zones sures, plancher des sous-titres) se
refait donc sur le master PUNCHE, en prenant l'UNION des boites visage sur tous les etats
de punch (le pire cas), pas sur un seul.

## L'ancrage

Un recadrage centre sur le cadre fait DERIVER le visage a chaque bascule : il grossit vers
le centre geometrique, qui n'est pas la ou sont les yeux. `--anchor x,y` epingle un point
de la source pour qu'il reste au meme pixel en sortie. Viser la LIGNE DES YEUX : c'est le
point que l'oeil suit, et le garder fixe rend la bascule lisible comme une intention.

Sans `--anchor`, le recadrage est centre et l'outil le dit. Les reperes du visage se
mesurent une fois par tournage avec `punch.py reperes` (detection YuNet, `tools/visage.py`).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import paths  # noqa: E402
#  Le meme echappement que le brouillon etiquete d'assemble.py : `fontfile` doit etre entre
#  apostrophes ET voir ses `:` echappes, sans quoi l'analyse du graphe echoue sur un message
#  qui ne montre pas la position fautive. Une 2e copie de cette regle divergerait.
from assemble import _esc_filter_value  # noqa: E402


# La console Windows est en cp1252 : sans ce reglage, un caractere inencodable dans un
# message fait mourir l'outil sur un UnicodeEncodeError APRES son travail, avec un code de
# sortie non nul qui casse la boucle appelante.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
#  Milieu de la fenetre 2-3 s visee par les bonnes pratiques Reels/TikTok.
TARGET_PLAN_S = 2.5
#  Plancher dur : jamais de plan plus court, quoi qu'en dise la cible.
MIN_PLAN_S = 1.2
#  Fenetre dans laquelle on prefere une VRAIE coupe a une coupe interne. Trop large, tous
#  les plans se calent sur les segments et on retombe sur les 4-6 s ; trop etroite, on
#  coupe dans un segment alors qu'une vraie coupe etait a deux images.
SNAP_S = 0.6
#  Fenetre ELARGIE, utilisee UNIQUEMENT quand la bascule ideale est collee a un blanc : une
#  vraie coupe a peine au-dela de SNAP_S vaut mieux qu'une coupe interne pres d'un silence,
#  qui donne l'impression d'un mot rapatrie d'une autre prise. Elargir pour ce cas SEUL
#  laisse la cible de rythme intacte partout ailleurs.
SNAP_BLANC_S = 1.2
#  Duree a partir de laquelle un blanc fabrique l'illusion d'un raccord. Sous ~0,12 s une
#  coupe ne s'entend pas, d'ou 0,15 s.
MIN_BLANC_S = 0.15
#  Distance sous laquelle une bascule est COLLEE a ce blanc, donc lue avec lui.
#
#  Ce n'est PAS "la bascule tombe DANS le blanc" : une bascule posee 40 ms apres l'attaque
#  du mot qui suit un blanc est du bon cote du son, mais ces 40 ms de cadre serre ne se
#  voient pas. Le spectateur voit un plan qui ne contient que le dernier mot d'une phrase,
#  precede d'un silence : un plan d'insert. Une mesure prise sur un instant choisi ne dit
#  rien de ce qu'il y a autour de lui, d'ou une distance plutot qu'un test ponctuel.
PROXIMITE_BLANC_S = 0.25
SCALE_IN = 1.15
#  L'etat LARGE n'est pas forcement le cadre plein. Quand le cadrage de tournage laisse voir
#  ce qu'on ne veut pas montrer (les jambes, un bureau), les DEUX etats sont zoomes (par ex.
#  125 % / 135 %) et le recadrage sert autant a nettoyer le cadre qu'a rythmer.
#  1.0 = plein cadre.
SCALE_BASE = 1.0
#  En dessous, une derniere sequence est fusionnee dans la precedente au lieu de rester
#  un plan croupion : une bascule a 0,6 s de la fin se lit comme un accident, pas un rythme.
TAIL_MERGE_RATIO = 0.5


def even(v: float) -> int:
    """Arrondi a l'entier PAIR le plus proche.

    yuv420p sous-echantillonne la chrominance d'un facteur 2 : une largeur, une hauteur ou
    un decalage impair fait echouer l'encodage ou decale le plan de chroma d'un demi-pixel.
    Le cout est une derive d'aspect sous le dixieme de pour-cent (940/1670 contre 1080/1920,
    soit 0,07 %), tres en dessous du pixel sur 1080 - invisible, et le prix a payer.
    """
    return int(round(v / 2.0)) * 2


def crop_for(scale: float, w_src: int, h_src: int, anchor=None, anchor_out=None):
    """Boite de recadrage qui pose le point `anchor` de la source sur `anchor_out` a l'ecran.

    Un point (ax, ay) de la source atterrit en sortie a :

        out = (src - crop) * s        =>        crop = ax - ox / s

    Sans `anchor_out`, la cible vaut l'ancre elle-meme : le point ne bouge pas, ce qui
    revient a `crop = ax * (1 - w / W)`. On derive avec `w / W` plutot qu'avec `1 / scale`
    pour rester coherent avec la largeur APRES arrondi pair, sans quoi l'ancrage derive de
    la moitie de l'arrondi.

    ⚠️ **Pourquoi une cible SEPAREE de l'ancre.** Garder un point immobile ne suffit pas des
    que les deux etats sont zoomes : si chaque etat cale sa fenetre au meme endroit de la
    source, la ligne des yeux se deplace d'un etat a l'autre et le visage saute a chaque
    bascule. Avec une cible de sortie commune, chaque etat calcule le `crop_y` qui remet les
    yeux au meme endroit.

    ⚠️ **Ce qui est rendu n'est pas forcement ce qui est demande - lire `clamp`.** La boite
    doit tenir dans la source : `x` et `y` sont donc bornes. Un rabotage veut dire que la
    cible demandee est INATTEIGNABLE a cette echelle, et la boite rendue ne respecte plus
    `anchor_out`. `clamp` porte le nombre de pixels rabotes par axe ; l'appelant doit refuser
    plutot que d'encoder une boite qui ne veut pas dire ce qu'il croit.

    Un `crop_y` negatif rabote a 0 a l'air plausible, mais il colle la fenetre au bord HAUT
    de la source et depense tout le budget de recadrage a couper le BAS (tete au milieu du
    cadre). Un `crop` negatif n'est pas un arrondi : c'est la signature d'une erreur de signe
    ou d'unite en amont, et le rabotage detruirait cette information.
    """
    if scale <= 1.0 + 1e-9:
        return None
    w = min(w_src, even(w_src / scale))
    h = min(h_src, even(w * h_src / w_src))  # derive de w : garde l'aspect de la source
    reel = w_src / w
    ax = anchor[0] if anchor else w_src / 2.0
    ay = anchor[1] if anchor else h_src / 2.0
    ox = anchor_out[0] if anchor_out else ax
    oy = anchor_out[1] if anchor_out else ay
    x_veut = even(ax - ox / reel)
    y_veut = even(ay - oy / reel)
    x = max(0, min(x_veut, w_src - w))
    y = max(0, min(y_veut, h_src - h))
    return {
        "scale": round(w_src / w, 4),   # l'echelle REELLE, apres arrondi pair
        "crop": [w, h, x, y],
        "out": [w_src, h_src],
        "clamp": [x - x_veut, y - y_veut],      # 0,0 = la cible est atteinte
        "veut": [x_veut, y_veut],
    }


def probe_size(path):
    """Geometrie AFFICHEE d'un rush, rotation d'affichage comprise.

    ⚠️ `stream=width,height` rend la geometrie CODEE, pas celle qu'on voit. Un `.mov` de
    tournage vertical est tres souvent code en 1920x1080 avec une **matrice d'affichage** de
    -90 deg : ffprobe annonce alors du paysage pour un rush qui se lit en portrait (cas
    courant des rushs iPhone).

    Sans correction, `crop_for` calculerait une boite sur 1920x1080 alors que ffmpeg decode
    du 1080x1920 : le `crop` tomberait hors du cadre, ou tiendrait par hasard et recadrerait
    n'importe ou. `assemble.py`, lui, n'a rien a corriger : ffmpeg applique la rotation AVANT
    le graphe de filtres. La chaine marche donc, seul le CALCUL serait faux.
    """
    out = subprocess.run(
        [paths.ffprobe(), "-v", "error", "-select_streams", "v:0",
         "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True,
    ).stdout
    try:
        st = json.loads(out)["streams"][0]
        w, h = int(st["width"]), int(st["height"])
    except (ValueError, KeyError, IndexError):
        raise SystemExit(f"ffprobe n'a pas rendu de geometrie pour {path}")
    rot = 0
    for sd in st.get("side_data_list") or []:
        if sd.get("rotation") is not None:
            rot = int(float(sd["rotation"]))
            break
    else:
        try:                                   # conteneurs anciens : un tag, pas de side data
            rot = int(st.get("tags", {}).get("rotate", 0))
        except (TypeError, ValueError):
            rot = 0
    if abs(rot) % 180 == 90:
        w, h = h, w
    return w, h


def seg_duration(e):
    """Duree effective du segment, bornes de coupe comprises si tighten_cuts est passe."""
    a = e.get("in_ext", e.get("start"))
    b = e.get("out_ext", e.get("end"))
    return max(0.0, float(b) - float(a))


def _coupe_proche(vraies_coupes, ideal, cur, total, min_plan_s, fenetre):
    """La VRAIE coupe la plus proche de `ideal` dans `fenetre`, ou None."""
    m = None
    for c in vraies_coupes:
        if c - cur < min_plan_s or total - c < min_plan_s or abs(c - ideal) > fenetre:
            continue
        if m is None or abs(c - ideal) < abs(m - ideal):
            m = c
    return m


def _blancs_autour(x, t, portee, mini):
    """Les plages NON VOISEES de >= `mini` s situees a moins de `portee` de `t`.

    Rend une liste de `(distance, duree, dedans)`.

    ⚠️ Le balayage va PLUS LOIN que `portee` (de `mini` + 0,20 s) a dessein : un blanc coupe
    par le bord de la fenetre mesurerait une duree trop courte et passerait sous le seuil.
    Le blanc le plus long serait donc precisement celui qu'on raterait.
    """
    from tighten_cuts import STEP, voiced  # noqa: E402

    lo = max(0.0, t - portee - mini - 0.20)
    hi = t + portee + mini + 0.20
    plages, debut, u = [], None, lo
    while u < hi:
        if not voiced(x, u):
            if debut is None:
                debut = u
        elif debut is not None:
            if u - debut >= mini:
                plages.append((debut, u))
            debut = None
        u = round(u + STEP, 3)
    if debut is not None and hi - debut >= mini:
        plages.append((debut, hi))

    out = []
    for a, b in plages:
        dist = 0.0 if a <= t <= b else min(abs(t - a), abs(t - b))
        if dist <= portee:
            out.append((round(dist, 3), round(b - a, 3), a <= t <= b))
    return out


def _sonde_blancs(edl):
    """Rend `blanc_proche(instant_master)` -> `(distance, duree, dedans)` ou `None`.

    ⚠️ Le seuil de voisement vient de `tighten_cuts.voiced` et n'est **pas** redefini ici :
    c'est le meme plancher que toutes les passes de coupe. En poser un second, propre au
    punch, ferait deux verites acoustiques qui pourraient se contredire.

    ⚠️ La mesure se fait sur le RUSH, en temps SOURCE, parce que c'est en temps source que la
    bascule est exprimee (`trim`). Les cumuls `_cum_*` du plan derivent du master encode (la
    quantification a la frame s'accumule de raccord en raccord, plusieurs dizaines de ms au
    bout d'une vingtaine) : un detecteur cale dessus lirait un autre instant que celui que
    ffmpeg decoupe reellement.
    """
    from tighten_cuts import load_audio  # noqa: E402

    ff = paths.ffmpeg()
    bornes, t = [], 0.0
    for e in edl:
        d = seg_duration(e)
        bornes.append((t, t + d, e))
        t += d

    def blanc_proche(m):
        for a, b, e in bornes:
            if a - 1e-6 <= m < b + 1e-6:
                src_t = float(e.get("in_ext", e["start"])) + (m - a)
                cands = _blancs_autour(load_audio(ff, e["source"]), src_t,
                                       PROXIMITE_BLANC_S, MIN_BLANC_S)
                return min(cands) if cands else None
        return None

    return blanc_proche


def _loin_des_blancs(ideal, blanc_proche, cur, total, min_plan_s, portee=0.80, pas=0.02):
    """L'instant le plus proche de `ideal` qui ne soit colle a AUCUN blanc.

    ⚠️ L'aval est prefere a l'amont : en aval on s'eloigne du blanc en entrant dans la phrase
    qui suit, donc la bascule tombe au milieu d'un debit continu, le seul endroit ou un
    changement de cadre se lit comme un punch et non comme un raccord. En amont on retombe
    dans la detente de la phrase qui finit, c'est-a-dire au moment le plus faible du signal.
    """
    d = 0.0
    while d <= portee:
        for t in ((ideal + d, ideal - d) if d else (ideal,)):
            if t - cur < min_plan_s or total - t < min_plan_s:
                continue
            if blanc_proche(t) is None:
                return round(t, 3)
        d = round(d + pas, 3)
    return None


def plan_shots(edl, target_plan_s=TARGET_PLAN_S, min_plan_s=MIN_PLAN_S,
               snap_s=SNAP_S, start_punched=False,
               blanc_proche=None, snap_blanc_s=SNAP_BLANC_S, deplacer=False):
    """Decoupe le montage en PLANS de ~`target_plan_s`, en preferant les vraies coupes.

    ⚠️ Basculer uniquement aux frontieres de segment NE SUFFIT PAS : les segments longs
    (plus de 3,2 s, jusqu'a 6 s d'un seul tenant) sont minoritaires en nombre mais occupent
    souvent plus de la moitie du temps a l'ecran. Une regle qui ne coupe qu'aux frontieres
    laisse donc une grande part de la video hors de la fenetre 2-3 s.

    D'ou deux sortes de frontieres, et une preference entre elles :

    - **la vraie coupe** (frontiere de segment) : la bascule de cadre y rend VISIBLE une
      coupe qui l'etait peu. C'est la meilleure, on lui donne la priorite ;
    - **la coupe interne** (au milieu d'un segment continu) : un « jump-in » franc. L'audio
      n'est pas coupe la, donc il n'y a aucun artefact sonore - `assemble.py` n'y met
      d'ailleurs pas de micro-fondu, qui s'entendrait comme un creux.

    L'algorithme avance donc de `target_plan_s` en `target_plan_s` et, a chaque pas, se
    CALE sur une vraie coupe si elle tombe a moins de `snap_s` de l'ideal ; sinon il coupe
    dans le segment. Aucun plan ne descend sous `min_plan_s`.

    Renvoie (steps_par_segment, frontieres, duree_totale). Chaque step porte ses bornes en
    temps SOURCE (ce que `trim` attend) et son etat.
    """
    durs = [seg_duration(e) for e in edl]
    starts, t = [], 0.0
    for d in durs:
        starts.append(t)
        t += d
    total = t
    vraies_coupes = starts[1:]

    bounds, cur, journal = [], 0.0, []
    while total - cur > target_plan_s + min_plan_s * TAIL_MERGE_RATIO:
        ideal = cur + target_plan_s
        snap = _coupe_proche(vraies_coupes, ideal, cur, total, min_plan_s, snap_s)
        b, reelle = (snap, True) if snap is not None else (ideal, False)

        #  ⚠️ Une bascule de cadre COLLEE a un blanc se lit comme un RACCORD, pas comme un
        #  punch (cf. PROXIMITE_BLANC_S). La DETECTION est toujours active ; le DEPLACEMENT
        #  est opt-in, pour ne pas changer le rythme d'un montage deja arbitre qu'on
        #  replanifierait. Rien ne refuse : c'est une question de rythme, pas de validite.
        pres = blanc_proche(ideal) if (not reelle and blanc_proche is not None) else None
        if pres is not None:
            dist, duree, dedans = pres
            pose, motif = ideal, None
            if deplacer:
                large = _coupe_proche(vraies_coupes, ideal, cur, total,
                                      min_plan_s, snap_blanc_s)
                if large is not None:
                    pose, motif = large, "vraie coupe, fenetre elargie"
                else:
                    loin = _loin_des_blancs(ideal, blanc_proche, cur, total, min_plan_s)
                    if loin is not None:
                        pose, motif = loin, "au large du blanc"
            journal.append({"ideal": round(ideal, 3), "pose": round(pose, 3),
                            "motif": motif, "deplacee": motif is not None,
                            "dist": dist, "duree": duree, "dedans": dedans})
            if motif is not None:
                b, reelle = pose, pose in vraies_coupes

        if b <= cur + 1e-6 or total - b < min_plan_s:
            break
        bounds.append((round(b, 3), reelle))
        cur = b

    edges = [0.0] + [b for b, _ in bounds] + [total]

    def etat(m):
        """Parite du plan qui contient l'instant master `m`."""
        k = 0
        while k + 2 < len(edges) and edges[k + 1] <= m:
            k += 1
        return (k % 2 == 1) != bool(start_punched)

    steps_par_segment = []
    for i, e in enumerate(edl):
        a, b = starts[i], starts[i] + durs[i]
        pts = [a] + [x for x, _ in bounds if a + 1e-6 < x < b - 1e-6] + [b]
        steps = []
        for k in range(len(pts) - 1):
            m0, m1 = pts[k], pts[k + 1]
            steps.append({
                "in": round(e["in_ext"] + (m0 - a), 3),
                "out": round(e["in_ext"] + (m1 - a), 3),
                "master": [round(m0, 3), round(m1, 3)],
                "punched": etat(m0 + 1e-6),
            })
        steps_par_segment.append(steps)
    return steps_par_segment, bounds, total, journal


def shots_from(steps_par_segment):
    """Aplatit en PLANS : (debut_master, duree, etat, segments_traverses, coupe_reelle)."""
    plats = [s for steps in steps_par_segment for s in steps]
    shots, i = [], 0
    while i < len(plats):
        j = i
        while j + 1 < len(plats) and plats[j + 1]["punched"] == plats[i]["punched"] \
                and abs(plats[j + 1]["master"][0] - plats[j]["master"][1]) < 1e-6:
            j += 1
        deb = plats[i]["master"][0]
        fin = plats[j]["master"][1]
        shots.append((deb, fin - deb, plats[i]["punched"], j - i + 1))
        i = j + 1
    return shots


def verifie_ancrage(etats, anchor, anchor_out, h_src, forcer):
    """OU tombe l'ancre dans le cadre de SORTIE, et refuse une boite rabotee.

    ⚠️ **Verifier que la ligne des yeux ne BOUGE PAS entre les etats ne dit rien de l'endroit
    OU elle tombe.** Des yeux a 988 px dans les deux etats sont stables, mais 988 / 1920 =
    51 % : la tete est au milieu du cadre. D'ou l'affichage en POURCENTAGE du cadre, avec la
    fenetre attendue d'un plan parlant vertical.
    """
    cible = 33.0, 42.0                     # % de la hauteur du cadre pour la ligne des yeux
    oy = (anchor_out or anchor or (0, h_src / 2.0))[1]
    print(f"[punch] ancre de sortie y={oy:.0f} sur {h_src}  ->  {oy / h_src * 100:.0f} % du cadre"
          f"   (plan parlant vertical : {cible[0]:.0f}-{cible[1]:.0f} %)")
    if anchor and not (cible[0] <= oy / h_src * 100 <= cible[1]):
        print(f"        ATTENTION hors de la fenetre {cible[0]:.0f}-{cible[1]:.0f} % : "
              f"tete trop {'basse' if oy / h_src * 100 > cible[1] else 'haute'} dans le cadre.")
    rabotes = []
    for nom, demandee, box in etats:
        if box is None:
            continue
        dx, dy = box["clamp"]
        w, h, x, y = box["crop"]
        print(f"        {nom:6} {demandee:.2f}x -> {w}x{h}+{x}+{y}"
              f"   fenetre source y {y}..{y + h}")
        if dx or dy:
            rabotes.append(f"  {nom} {demandee:.2f}x : voulait +{box['veut'][0]}+{box['veut'][1]}, "
                           f"rabote de ({dx:+d},{dy:+d}) -> +{x}+{y}")
    if not rabotes:
        return
    msg = ("La boite ne tient pas dans la source : l'ancrage demande est INATTEIGNABLE a cette "
           "echelle.\n" + "\n".join(rabotes) + "\n\n"
           "La boite rabotee ne respecte plus --anchor-out : l'encoder donne un cadrage qui n'est\n"
           "pas celui demande, sans qu'aucune etape suivante ne s'en plaigne.\n"
           "Sortir par le HAUT : augmenter --scale-in / --scale-base, ou viser une ancre de\n"
           "sortie plus basse. --forcer-rabotage passe outre, en connaissance de cause.")
    if forcer:
        print(f"\nATTENTION {msg}\n")
        return
    raise SystemExit(f"\n{msg}")


def cmd_plan(args):
    tl_path = Path(args.timeline)
    edl = json.loads(tl_path.read_text(encoding="utf-8"))
    edl.sort(key=lambda e: e["i"])
    if not edl:
        raise SystemExit("EDL vide")

    w_src, h_src = (args.size if args.size else probe_size(edl[0]["source"]))
    anchor = args.anchor
    if anchor is None:
        print(f"[punch] aucun --anchor : recadrage CENTRE sur le cadre ({w_src // 2},{h_src // 2}).")
        print("        Le visage derivera a chaque bascule s'il n'est pas au centre.")
        print("        Mesurer la ligne des yeux et la repasser en --anchor x,y.")

    box = crop_for(args.scale_in, w_src, h_src, anchor, args.anchor_out)
    base = crop_for(args.scale_base, w_src, h_src, anchor, args.anchor_out)
    if box is None:
        raise SystemExit(f"--scale-in {args.scale_in} <= 1.0 : il n'y a rien a puncher")
    if base is not None and args.scale_base >= args.scale_in:
        raise SystemExit(f"--scale-base {args.scale_base} >= --scale-in {args.scale_in} : "
                         "l'etat large doit etre PLUS large que l'etat serre")
    verifie_ancrage(
        [("serre", args.scale_in, box), ("large", args.scale_base, base)],
        anchor, args.anchor_out, h_src, args.forcer_rabotage)

    blanc_proche = None if args.sans_detecteur else _sonde_blancs(edl)
    steps_par_segment, bounds, total, journal = plan_shots(
        edl, target_plan_s=args.target_plan_s, min_plan_s=args.min_plan_s,
        snap_s=args.snap_s, start_punched=args.start_punched,
        blanc_proche=blanc_proche, snap_blanc_s=args.snap_blanc_s,
        deplacer=args.eviter_blancs)

    for e, steps in zip(edl, steps_par_segment):
        e["punch"] = {
            "out": [w_src, h_src],
            "steps": [{"in": s["in"], "out": s["out"],
                       "scale": (box if s["punched"] else base or {"scale": 1.0})["scale"],
                       "crop": (box["crop"] if s["punched"]
                                else (base["crop"] if base else None))}
                      for s in steps],
        }

    w, h, x, y = box["crop"]
    shots = shots_from(steps_par_segment)
    n_reelles = sum(1 for _, reelle in bounds if reelle)
    print(f"\nsource {w_src}x{h_src}  |  serre {w}x{h}+{x}+{y}  "
          f"(echelle reelle {box['scale']}x, demandee {args.scale_in}x)")
    print(f"ancrage {'centre' if anchor is None else f'{anchor[0]:.0f},{anchor[1]:.0f}'}"
          f"  |  plan cible {args.target_plan_s}s (plancher {args.min_plan_s}s, "
          f"calage {args.snap_s}s)  |  depart {'SERRE' if args.start_punched else 'LARGE'}\n")
    print("   debut | duree | etat  | segments")
    print("  -------|-------|-------|---------")
    for deb, d, st, n in shots:
        print(f"  {deb:6.2f}s | {d:5.2f}s | {'SERRE' if st else 'large':5} | {n}")

    durees = [d for _, d, _, _ in shots]
    dans = sum(1 for d in durees if 2.0 <= d <= 3.2)
    print(f"\n{len(shots)} plans sur {total:.1f}s  |  moy {total / max(1, len(shots)):.2f}s, "
          f"min {min(durees):.2f}s, max {max(durees):.2f}s")
    print(f"{dans}/{len(shots)} plans dans la fenetre 2,0-3,2 s")
    print(f"{n_reelles}/{len(bounds)} bascules posees sur une VRAIE coupe, "
          f"{len(bounds) - n_reelles} en coupe interne (jump-in)")

    #  ⚠️ Ce rapport est TOUJOURS imprime : le defaut qu'il decrit ne se voit sur aucun autre
    #  controle (les durees de plan sont dans la cible, aucune QA ne regarde le son SOUS une
    #  bascule de cadre) et ne s'entend qu'a la relecture du master fini, apres l'encodage.
    if blanc_proche is None:
        print("\n[blancs] detecteur DESACTIVE (--sans-detecteur) : une bascule peut etre "
              "collee a un silence sans que rien ne le dise.")
    elif not journal:
        print(f"\n[blancs] aucune bascule interne collee a un blanc "
              f"(blanc >= {MIN_BLANC_S}s a moins de {PROXIMITE_BLANC_S}s).")
    else:
        deplacees = [j for j in journal if j["deplacee"]]
        restantes = [j for j in journal if not j["deplacee"]]
        print(f"\n[blancs] {len(journal)} bascule(s) interne(s) COLLEE(S) a un blanc. Un "
              "changement de cadre au bord d'un silence ne se lit pas comme un punch mais "
              "comme un RACCORD ; si le blanc precede la fin d'une phrase, son dernier mot "
              "parait rapatrie d'une autre prise :")
        for j in journal:
            ou = "DEDANS" if j["dedans"] else f"a {j['dist'] * 1000:.0f} ms"
            suite = (f"-> {j['pose']:6.2f}s  ({j['motif']})" if j["deplacee"]
                     else "LAISSEE EN PLACE")
            print(f"  {j['ideal']:6.2f}s  blanc de {j['duree'] * 1000:4.0f} ms, "
                  f"{ou:>9}   {suite}")
        if deplacees:
            print(f"  {len(deplacees)} deplacee(s) par --eviter-blancs.")
        if restantes:
            print(f"  [!] {len(restantes)} NON deplacee(s). `--eviter-blancs` les recale sur "
                  "une vraie coupe (fenetre elargie), sinon au large du blanc.")
            print("      Rien n'est refuse ici volontairement : le rythme d'un montage deja "
                  "arbitre ne se change pas sans qu'on le demande.")

    if args.dry_run:
        print(f"\n--dry-run : {tl_path} N'A PAS ete modifie.")
        return
    tl_path.write_text(json.dumps(edl, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nOK -> {tl_path} (cle `punch` posee)")
    print(f"  python tools/assemble.py \"{tl_path}\" --honor-cuts --honor-punch")
    print("  ATTENTION - remesurer visage/menton sur le master PUNCHE avant le plan d'habillage")


def cmd_stills(args):
    """Planche A/B des amplitudes sur UNE frame : l'arbitrage, sans encoder un seul plan."""
    master = Path(args.master)
    if not master.exists():
        raise SystemExit(f"master introuvable : {master}")
    w_src, h_src = (args.size if args.size else probe_size(master))
    scales = [float(s) for s in args.scales.split(",")]

    try:
        v = paths.video_from_path(master)
        out_dir = paths.media(v, "captures", create=True) / "mods"
    except paths.UnknownVideo:
        out_dir = master.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    font = next((f for f in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf")
                 if Path(f).exists()), None)
    written = []
    for t in [float(x) for x in args.at.split(",")]:
        parts, labels = [], []
        for k, s in enumerate(scales):
            box = crop_for(s, w_src, h_src, args.anchor, args.anchor_out)
            chain = [] if box is None else [
                "crop={}:{}:{}:{}".format(*box["crop"]),
                f"scale={w_src}:{h_src}:flags=lanczos",
            ]
            chain.append(f"scale={args.tile_w}:-2")
            if font:
                #  `expansion=none` est OBLIGATOIRE des que le texte contient un `%`.
                #  drawtext fait par defaut une expansion facon strftime : « 115 % » y
                #  devient « Stray % near '' », le filtre est ignore, et l'etiquette
                #  disparait SANS erreur ni code de retour - la planche parait juste
                #  normale, avec quatre vignettes qu'on ne peut plus nommer. Echapper en
                #  `\%` ne suffit pas, c'est l'expansion qu'il faut couper.
                pct = int(round((box["scale"] if box else 1.0) * 100))
                chain.append(
                    f"drawtext=fontfile='{_esc_filter_value(font)}'"
                    f":text='{pct} %':expansion=none"
                    ":fontcolor=white:fontsize=34:x=16:y=16"
                    ":box=1:boxcolor=black@0.6:boxborderw=10"
                )
            parts.append(f"[0:v]{','.join(chain)}[t{k}]")
            labels.append(f"[t{k}]")
        parts.append(f"{''.join(labels)}hstack=inputs={len(scales)}[out]")
        out_png = out_dir / f"punch_ab_t{str(t).replace('.', 'p')}.png"
        cmd = [paths.ffmpeg(), "-y", "-ss", f"{t:.3f}", "-i", str(master),
               "-filter_complex", ";".join(parts), "-map", "[out]",
               "-frames:v", "1", str(out_png)]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if res.returncode != 0:
            print(res.stderr[-2500:])
            raise SystemExit(f"ffmpeg a echoue (code {res.returncode})")
        written.append(out_png)
        print(f"OK -> {out_png}")

    print(f"\n{len(written)} planche(s), amplitudes {', '.join(f'{s:g}x' for s in scales)}.")
    print("Regarder la NETTETE sur le plan le plus serre : les rushs sont en 1080x1920,")
    print("donc tout punch est un agrandissement. C'est le seul arbitrage qui compte ici.")


def cmd_reperes(args):
    """OU est le visage dans la SOURCE, et ou il tomberait pour un cadrage donne.

    C'est l'entree de `plan` : `--anchor` / `--anchor-out` se choisissent depuis des reperes
    MESURES, jamais depuis les coordonnees converties d'un autre logiciel de montage.

    ⚠️ **Ne PAS retenir la plus grosse boite.** YuNet peut trouver un faux visage sur des
    MAINS croisees, plus gros que le vrai visage et pourtant a un score bien plus bas. On
    trie par SCORE, et `--tout` montre les autres detections pour qu'une mesure douteuse se
    voie au lieu de se propager.
    """
    try:
        import cv2
    except ImportError:
        raise SystemExit("opencv manque : pip install opencv-python-headless")
    sys.path.insert(0, str(TOOLS_DIR))
    from visage import modele_yunet            # une seule resolution du poids dans le depot

    modele = modele_yunet()
    if not modele.exists():
        raise SystemExit(f"Poids YuNet absent : {modele}\n"
                         "Le telecharger : python tools/visage.py telecharger")
    sources = []
    for m in args.sources:
        p = Path(m)
        sources += sorted(p.glob("*.mov")) + sorted(p.glob("*.mp4")) if p.is_dir() else [p]
    if not sources:
        raise SystemExit("aucune source")

    tmp = Path(args.out) if args.out else TOOLS_DIR.parent / ".punch-reperes"
    tmp.mkdir(parents=True, exist_ok=True)
    mesures = []
    print(f"{'source':<26}{'score':>6}{'front':>7}{'yeux':>7}{'menton':>8}{'cx':>7}")
    for src in sources:
        w_src, h_src = probe_size(src)
        for t in args.at:
            png = tmp / f"{src.stem}_{str(t).replace('.', 'p')}.png"
            cmd = [paths.ffmpeg(), "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src),
                   "-frames:v", "1", str(png)]
            if subprocess.run(cmd, capture_output=True).returncode != 0 or not png.exists():
                print(f"{src.name:<26}  (pas de frame a {t:g}s)")
                continue
            img = cv2.imread(str(png))
            h, w = img.shape[:2]
            det = cv2.FaceDetectorYN.create(str(modele), "", (w, h), 0.6, 0.3, 5000)
            det.setInputSize((w, h))
            _, faces = det.detect(img)
            if faces is None or not len(faces):
                print(f"{src.name:<26}  aucun visage")
                continue
            for k, f in enumerate(sorted(faces, key=lambda r: -r[14])):
                x, y, bw, bh = f[:4]
                yeux = (f[5] + f[7]) / 2
                cx = (f[4] + f[6]) / 2
                if k == 0:
                    mesures.append((y, yeux, y + bh, cx))
                elif not args.tout:
                    continue
                etiq = f"{src.stem}@{t:g}s" if k == 0 else ""
                suff = "" if k == 0 else "   (secondaire - verifier)"
                print(f"{etiq:<26}{f[14]:6.2f}{y:7.0f}{yeux:7.0f}{y + bh:8.0f}{cx:7.0f}{suff}")

    if not mesures:
        raise SystemExit("aucune mesure exploitable")
    fronts, yeux, mentons, cxs = zip(*mesures)
    med = sorted(yeux)[len(yeux) // 2]
    print(f"\n{len(mesures)} mesure(s), source {w_src}x{h_src}")
    print(f"  front   {min(fronts):.0f}..{max(fronts):.0f}     "
          f"yeux  {min(yeux):.0f}..{max(yeux):.0f}  (mediane {med:.0f})")
    print(f"  menton  {min(mentons):.0f}..{max(mentons):.0f}     "
          f"centre x  {min(cxs):.0f}..{max(cxs):.0f}")

    if not args.borne_basse:
        print("\n--borne-basse <y> pour deduire le zoom minimum (ce qu'on veut exclure du bas).")
        return
    marge = args.borne_basse - med
    if marge <= 0:
        raise SystemExit(f"--borne-basse {args.borne_basse:.0f} est au-dessus des yeux "
                         f"({med:.0f}) : rien a cadrer.")
    print(f"\nzoom minimum pour garder le bas de fenetre sous {args.borne_basse:.0f} px, "
          f"yeux calees sur la mediane {med:.0f} :")
    print(f"  {'yeux a':>8}{'zoom mini':>11}  {'crop':<18}{'bas':>6}{'plage reelle':>15}")
    for pct in (47, 44, 42, 40, 38, 36, 33):
        oy = pct / 100 * h_src
        #  bas = med + (h_src - oy) / s  <=  borne   =>   s >= (h_src - oy) / (borne - med)
        s = (h_src - oy) / marge
        box = crop_for(max(s, 1.0001), w_src, h_src, (w_src / 2, med), (w_src / 2, oy))
        w, h, x, y = box["crop"]
        reel = w_src / w
        lo, hi = (min(yeux) - y) * reel / h_src * 100, (max(yeux) - y) * reel / h_src * 100
        print(f"  {pct:6.0f} %{s * 100:10.0f} %  {f'{w}x{h}+{x}+{y}':<18}{y + h:6.0f}"
              f"{f'{lo:.0f}-{hi:.0f} %':>15}")
    print("\nATTENTION la colonne \"plage reelle\" est ce que le sujet fait VRAIMENT sur ces rushs :")
    print("   un seul recadrage ne fige pas la ligne des yeux, il la centre. C'est la plage")
    print("   qui doit tenir dans 33-42 %, pas seulement la mediane.")
    print("Le zoom se paie en nettete : arbitrer en REGARDANT (`punch.py stills`).")


def parse_anchor(s):
    try:
        x, y = s.split(",")
        return (float(x), float(y))
    except Exception:
        raise argparse.ArgumentTypeError("attendu : x,y (ex. 540,700)")


def parse_size(s):
    try:
        w, h = s.lower().split("x")
        return (int(w), int(h))
    except Exception:
        raise argparse.ArgumentTypeError("attendu : LARGEURxHAUTEUR (ex. 1080x1920)")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("plan", help="pose la cle `punch` dans un edl_v3_timeline.json")
    pl.add_argument("timeline", help="Chemin d'un edl_v3_timeline.json (APRES tighten_cuts.py)")
    pl.add_argument("--scale-in", type=float, default=SCALE_IN,
                    help=f"Echelle de l'etat serre (defaut {SCALE_IN})")
    pl.add_argument("--scale-base", type=float, default=SCALE_BASE,
                    help=f"Echelle de l'etat LARGE (defaut {SCALE_BASE} = plein cadre). "
                         "Au-dessus de 1.0, les deux etats sont zoomes : utile quand le "
                         "cadrage de tournage laisse voir ce qu'on ne veut pas montrer.")
    pl.add_argument("--target-plan-s", type=float, default=TARGET_PLAN_S,
                    help=f"Duree VISEE d'un plan (defaut {TARGET_PLAN_S}s, milieu de la "
                         "fenetre 2-3 s). On bascule a la frontiere de segment la plus proche.")
    pl.add_argument("--min-plan-s", type=float, default=MIN_PLAN_S,
                    help=f"Plancher dur : jamais de plan plus court (defaut {MIN_PLAN_S}s)")
    pl.add_argument("--snap-s", type=float, default=SNAP_S,
                    help=f"Fenetre de calage sur une VRAIE coupe (defaut {SNAP_S}s). Au-dela, "
                         "la bascule se fait en coupe interne (jump-in) pour tenir la cible.")
    pl.add_argument("--eviter-blancs", action="store_true",
                    help="DEPLACER une bascule interne collee a un blanc : vers une vraie "
                         "coupe (fenetre --snap-blanc-s), sinon au large du blanc. La "
                         "DETECTION est toujours active ; seul le deplacement est optionnel, "
                         "pour ne pas changer le rythme d'un montage deja arbitre qu'on "
                         "replanifierait. Cf. PROXIMITE_BLANC_S.")
    pl.add_argument("--snap-blanc-s", type=float, default=SNAP_BLANC_S,
                    help=f"Fenetre de calage ELARGIE, pour ce cas seul (defaut {SNAP_BLANC_S}s)")
    pl.add_argument("--sans-detecteur", action="store_true",
                    help="Ne pas decoder l'audio des rushs. La detection des bascules collees "
                         "a un blanc est alors muette - a n'utiliser que si l'audio est absent.")
    pl.add_argument("--anchor", type=parse_anchor, default=None,
                    help="Point de la source a suivre, \"x,y\". Viser la ligne des yeux. "
                         "Defaut : centre du cadre (le visage derive).")
    pl.add_argument("--anchor-out", type=parse_anchor, default=None,
                    help="Ou ce point doit atterrir a l'ecran, \"x,y\". Defaut : au meme "
                         "endroit (le point ne bouge pas). A poser des que les DEUX etats "
                         "sont zoomes : c'est ce qui permet de caler le cadre en haut pour "
                         "couper le bas sans faire sauter le visage d'un plan a l'autre.")
    pl.add_argument("--start-punched", action="store_true",
                    help="Ouvrir sur l'etat SERRE au lieu du large")
    pl.add_argument("--forcer-rabotage", action="store_true",
                    help="Accepter une boite rabotee au bord de la source. Par defaut c'est "
                         "un REFUS : la boite ne respecte alors plus --anchor-out, et le "
                         "cadrage obtenu n'est pas celui demande sans qu'aucune etape "
                         "suivante ne s'en plaigne.")
    pl.add_argument("--size", type=parse_size, default=None,
                    help="Forcer la geometrie source au lieu de sonder le 1er rush")
    pl.add_argument("--dry-run", action="store_true",
                    help="Afficher le plan sans ecrire dans le timeline")
    pl.set_defaults(func=cmd_plan)

    st = sub.add_parser("stills", help="planche A/B des amplitudes sur une frame de master")
    st.add_argument("master", help="Un master deja assemble (non punche)")
    st.add_argument("--at", default="8.0",
                    help="Instants en secondes, separes par des virgules (defaut 8.0). "
                         "Choisir un plan SERRE, ou l'agrandissement se voit le plus.")
    st.add_argument("--scales", default="1.0,1.10,1.15,1.20",
                    help="Amplitudes comparees (defaut 1.0,1.10,1.15,1.20)")
    st.add_argument("--anchor", type=parse_anchor, default=None)
    st.add_argument("--anchor-out", type=parse_anchor, default=None)
    st.add_argument("--size", type=parse_size, default=None)
    st.add_argument("--tile-w", type=int, default=540,
                    help="Largeur d'une vignette dans la planche (defaut 540)")
    st.set_defaults(func=cmd_stills)

    rp = sub.add_parser("reperes",
                        help="mesurer le visage dans la source, et en deduire le zoom minimum")
    rp.add_argument("sources", nargs="+",
                    help="Des rushs, ou un dossier de rushs. Mesurer sur les RUSHS : un master "
                         "deja punche donnerait les reperes du cadrage qu'on veut changer.")
    rp.add_argument("--at", type=lambda s: [float(v) for v in s.split(",")], default=[2.0],
                    help="Instants en secondes, separes par des virgules (defaut 2.0)")
    rp.add_argument("--borne-basse", type=float, default=None,
                    help="Le y source de ce qu'on veut EXCLURE par le bas (jambes, bureau...). "
                         "Donne la table des zooms minimum par hauteur d'oeil visee.")
    rp.add_argument("--tout", action="store_true",
                    help="Montrer aussi les detections secondaires (mains croisees, encart "
                         "webcam...) au lieu du seul meilleur score")
    rp.add_argument("--out", default=None, help="Ou deposer les frames extraites")
    rp.set_defaults(func=cmd_reperes)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
