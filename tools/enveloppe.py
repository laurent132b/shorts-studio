"""enveloppe.py - REGARDER l'enveloppe RMS 20 ms, la ou on va poser une coupe.

    python tools/enveloppe.py montre <slug> rush_016:555.3:556.2
    python tools/enveloppe.py montre <slug> rush_009:311.9:312.2 --pas 0.01
    python tools/enveloppe.py tetes  <slug>

`tighten_cuts.py` mesure tout sur l'enveloppe RMS 20 ms des rushs source, jamais sur les
horodatages de l'ASR. Cet outil permet de la REGARDER a l'endroit ou une coupe doit tomber,
au lieu de la re-mesurer a la main.

Ce qu'il ne remplace PAS
------------------------
| | question | et pourquoi ce n'est pas celle-ci |
|---|---|---|
| `blancs.py` | ou sont les blancs d'un MASTER, et lesquels sont morts | il ne voit que sous -45 dB, et il travaille sur le master |
| `trous_gardes.py` | quels trous internes le montage garde, et ce qu'il y a dedans | il rend un tableau, pas une forme d'onde |
| celui-ci | **a quelle milliseconde exactement le son s'eteint et reprend** | - |

⚠️ IL MESURE ET IL AFFICHE. Il ne coupe rien, n'ecrit dans aucun EDL, ne propose aucune
   borne. Poser la coupe reste un override sur la timeline, resolu par le TEXTE.

CE QU'IL FAUT SAVOIR LIRE
-------------------------
1. **UN CREUX N'EXISTE PAS TOUJOURS.** Entre deux mots enchaines, l'enveloppe peut rester
   plate vers -25 dB sur 300 ms : il n'y a aucun creux. La coupe tombe alors sur la fin ASR
   du mot et compte sur le micro-fondu de 15 ms d'`assemble.py`. Le savoir AVANT de promettre
   une coupe propre change ce qu'on annonce.

2. **UN « euuuh » SE VOIT ET N'APPARAIT DANS AUCUN RAPPORT.** Il vit vers -30 dB, donc
   au-dessus du plancher de parole : ni `blancs.py` ni `qa_montage` ne le voient, et l'ASR ne
   le transcrit pas toujours. A l'enveloppe c'est un **plateau long et plat** entre deux mots.

3. **LE PLANCHER DU FICHIER N'EST PAS LE PLANCHER DU MICRO.** Un micro debruite plafonne vers
   -76/-82 dB. Sous **-100 dB**, ce n'est plus du fond de piece : c'est le FICHIER qui est
   vide. C'est la signature d'un mot que l'ASR a invente, ou d'un mot date trop tot.

4. **LE PAS DE 20 ms PEUT MENTIR SUR UNE PLOSIVE.** Une plosive fait salve, puis OCCLUSION,
   puis voyelle : a 20 ms le creux d'occlusion peut passer pour un blanc. `--pas 0.01`
   avant de couper devant une consonne - sinon « Decouvre » s'entend « couvre » (cf.
   `tighten_cuts.true_attack`).

Le mode `tetes` : instance ou CLASSE ?
--------------------------------------
Quand un defaut se trouve, la seule question qui decide du remede est *est-ce l'instance qui
derape, ou la passe ?* Apprendre un seuil a un outil partage sur la foi d'une seule video
degrade les autres formats. `tetes` balaye le vide en tete des N segments d'un montage et dit
combien depassent les garde-fous : une poignee sur des centaines, la classe est saine et
l'override sur la timeline est le bon niveau.
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

#: En dessous, ce n'est plus le fond de piece : un micro debruite plafonne vers -76/-82 dB,
#: donc c'est le FICHIER qui est vide. Cf. le point 3 de l'en-tete.
PLANCHER_FICHIER = -100.0
#: Vide en tete a partir duquel `tetes` le signale. `tighten_cuts.MIN_DEAD_AIR` vaut 0,08 -
#: on liste plus large pour voir la DISTRIBUTION, pas seulement ce qui depasse.
TETE_MIN = 0.10


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


def rushes_de(montage):
    """⚠️ Resolu depuis le dossier de MONTAGE, jamais depuis le `cwd`.

    `source` d'un segment est relatif a la racine du depot. Le resoudre depuis le `cwd`
    rendrait l'outil inutilisable des qu'on le lance d'ailleurs.
    """
    return montage.parent / "_media" / "rushes"


def cmd_montre(args):
    montage = resout_montage(args.cible, args.serie)
    ff = tc.resolve_ffmpeg()
    for spec in args.plages:
        try:
            stem, a, b = spec.rsplit(":", 2)
            a, b = float(a), float(b)
        except ValueError:
            sys.exit(f"plage illisible : {spec!r} - attendu 'rush_016:555.3:556.2'")
        chemin = rushes_de(montage) / f"{stem}.mp4"
        if not chemin.exists():
            sys.exit(f"rush introuvable : {chemin}")
        x = tc.load_audio(ff, str(chemin))
        s0, s1, vrai = tc.plus_long_silence(x, a, b)

        print(f"\n### {stem}  [{a:.3f} - {b:.3f}]   pas {args.pas * 1000:.0f} ms")
        print(f"    plancher de parole {tc.FLOOR_DB} dB   |   "
              f"plus long silence CONTIGU {s0:.3f} -> {s1:.3f}  ({vrai * 1000:.0f} ms)")

        lignes, t, sous_plancher = [], a, 0
        while t <= b + 1e-9:
            d = tc.db_at(x, t)
            if d < PLANCHER_FICHIER:
                sous_plancher += 1
            marque = "." if d >= tc.FLOOR_DB else ("_" if d >= -70 else " ")
            lignes.append((round(t, 3), d, marque))
            t = round(t + args.pas, 3)
        par = max(1, int(round(0.20 / args.pas)))
        for k in range(0, len(lignes), par):
            bloc = lignes[k:k + par]
            print(f"    {bloc[0][0]:9.3f}  " + "".join(m for _, _, m in bloc)
                  + "   " + " ".join(f"{d:6.1f}" for _, d, _ in bloc))
        print("    legende : '.' parole (>= -45 dB)  '_' bas (-45 a -70)  "
              "'espace' sous -70")
        if sous_plancher:
            print(f"    [!] {sous_plancher} trame(s) sous {PLANCHER_FICHIER} dB : "
                  f"ce n'est PAS du fond de piece (micro a -76/-82), le fichier est VIDE la.")


def cmd_tetes(args):
    montage = resout_montage(args.cible, args.serie)
    ff = tc.resolve_ffmpeg()
    segs = json.loads((montage / "edl_v3_timeline.json").read_text(encoding="utf-8"))
    segs = segs["segments"] if isinstance(segs, dict) else segs
    rushes = rushes_de(montage)

    lignes = []
    for s in segs:
        chemin = rushes / f"{s['stem']}.mp4"
        if not chemin.exists():
            continue
        x = tc.load_audio(ff, str(chemin))
        onset = tc.sound_start(x, s["in_ext"], s["out_ext"])
        vide = round(onset - s["in_ext"], 3)
        if vide >= TETE_MIN:
            lignes.append((vide, s, onset, tc.db_at(x, s["in_ext"] + vide / 2)))
    lignes.sort(reverse=True, key=lambda r: r[0])

    refuses = [r for r in lignes if r[0] > tc.MAX_DEAD_AIR]
    print(f"{len(lignes)} segment(s) sur {len(segs)} ouvrent sur >= {TETE_MIN * 1000:.0f} ms "
          f"de vide   (MIN_DEAD_AIR {tc.MIN_DEAD_AIR}, MAX_DEAD_AIR {tc.MAX_DEAD_AIR})\n")
    if not lignes:
        print("Aucun : rien a arbitrer de ce cote.")
        return 0
    print(f"{'vide':>9} {'dB milieu':>10}  {'ligne':<7} {'rush':<11} "
          f"{'in_ext':>9} {'attaque':>9}  texte")
    for vide, s, onset, db in lignes:
        part = "" if s.get("part") is None else f".{s['part']}"
        drapeau = "  <-- REFUSE par MAX_DEAD_AIR" if vide > tc.MAX_DEAD_AIR else ""
        print(f"{vide * 1000:8.0f}ms {db:10.1f}  L{s['i']}{part:<5} {s['stem']:<11} "
              f"{s['in_ext']:9.3f} {onset:9.3f}  {(s.get('text') or '')[:38]}{drapeau}")

    print()
    if refuses:
        print(f"[!] {len(refuses)} segment(s) DEPASSENT `MAX_DEAD_AIR` : `dead_air_head` a "
              f"mesure leur vide correctement PUIS a refuse d'agir, parce que son garde-fou "
              f"ecarte les mesures 'trop grandes pour etre credibles'.")
        print("   Or a -90 dB il n'y a rien de douteux : une mesure ne devient pas suspecte "
              "en devenant grande. Cf. `tighten_cuts.py`, le mot date en avance.")
    print(f"-> {len(refuses)} instance(s) sur {len(segs)} segments. C'est ce rapport qui "
          f"decide du remede :")
    print("  quelques instances = un override sur la TIMELINE, resolu par le TEXTE ;")
    print("  une proportion = un correctif d'OUTIL, a eprouver sur plusieurs montages "
          "et sur les deux profils.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def commun(p):
        p.add_argument("cible", help="slug de la video, ou dossier 1_montage")
        p.add_argument("--serie", help="ignore (conserve pour compatibilite)")

    m = sub.add_parser("montre", help="l'enveloppe RMS d'une ou plusieurs plages de rush")
    commun(m)
    m.add_argument("plages", nargs="+", help="'rush_016:555.3:556.2', en secondes DU RUSH")
    m.add_argument("--pas", type=float, default=0.02,
                   help="pas d'affichage en s (defaut 0.02 ; 0.01 devant une plosive)")
    m.set_defaults(fn=cmd_montre)

    t = sub.add_parser("tetes", help="le vide en tete de CHAQUE segment : instance ou classe ?")
    commun(t)
    t.set_defaults(fn=cmd_tetes)

    args = ap.parse_args()
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
