"""
tools/assemble.py - assemble a montage-rushs EDL into a master video.

It consumes `edl_v3.json` produced by tools/select_takes.py and writes:
- <out>.mp4
- <out>.srt
- <out>_rapport.md
- <out>_trous.md, when holes_v3.json is provided

The script intentionally keeps jump cuts simple: no visual transitions, one
FFmpeg filter graph, micro-fades on audio to avoid clicks, and cut padding based
on word-level gaps.

Usage:
    python tools/assemble.py videos/<slug>/1_montage/edl_v3.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import paths  # noqa: E402
from audit_edl import exiger_fenetres_positives  # noqa: E402
from transcribe import build_srt, chunk_words, format_timecode, load_corrections  # noqa: E402

CUT_MARGIN_IN = 0.05
CUT_MARGIN_OUT = 0.15
MAX_GAP_EXTEND_S = 0.8
AUDIO_FADE_S = 0.015
MIN_WORD_DURATION_S = 0.05
MAX_WORD_DURATION_S = 0.9


_duration_cache = {}


def probe_duration(path):
    """Duree d'un rush, memoisee par chemin.

    `compute_cuts` appelle ceci une fois par SEGMENT, pas une fois par source. Sans
    encodage a cote, ces appels ffprobe sont l'essentiel du cout du plan : ce cache est ce
    qui rend le pre-vol (`--plan-only`) quasi gratuit.
    """
    key = str(path)
    if key not in _duration_cache:
        out = subprocess.run(
            [paths.ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True,
            text=True,
        ).stdout.strip()
        #  ⚠️ Un 0.0 silencieux ici fabrique un montage NEGATIF. `compute_cuts` fait
        #  `out_ext = min(dur, out_ext)` : avec dur=0, toute borne de sortie tombe a zero
        #  pendant que l'entree reste positive, donc chaque segment dure moins que rien, et le
        #  seul symptome serait un total negatif. Cause typique : un `source` qui pointe sur
        #  un fichier inexistant.
        if not out:
            raise SystemExit(
                f"ffprobe n'a pas pu lire la duree de :\n  {path}\n"
                "  Le fichier est absent ou illisible. Verifier le champ `source` de l'EDL :\n"
                "  s'il porte une extension qui n'est pas celle du rush ingere, relancer\n"
                "  select_takes.py (la resolution essaie toutes les extensions)."
            )
        _duration_cache[key] = float(out)
    return _duration_cache[key]


def stem_from_word_path(path):
    for suffix in (".transcript.json", ".words_v2.json", ".words.json"):
        if path.name.endswith(suffix):
            return path.name[: -len(suffix)]
    return path.stem


def read_words(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    words = data.get("words", []) if isinstance(data, dict) else data
    raw = [
        {
            "text": w["text"],
            "start": round(float(w["start"]), 3),
            "end": round(float(w["end"]), 3),
            "p": w.get("p"),
        }
        for w in words
        if w.get("text") and "start" in w and "end" in w
    ]
    clean = []
    for i, w in enumerate(raw):
        text_len = max(1, len(w["text"]))
        natural_cap = min(MAX_WORD_DURATION_S, 0.28 + text_len * 0.035)
        end = min(w["end"], w["start"] + natural_cap)
        if i + 1 < len(raw) and raw[i + 1]["start"] > w["start"]:
            end = min(end, max(w["start"] + MIN_WORD_DURATION_S, raw[i + 1]["start"] - 0.03))
        item = dict(w)
        item["end"] = round(max(w["start"] + MIN_WORD_DURATION_S, end), 3)
        clean.append(item)
    return clean


def discover_words(tdir, stems):
    priority = (".transcript.json", ".words_v2.json", ".words.json")
    cache = {}
    for stem in stems:
        found = None
        for suffix in priority:
            p = tdir / f"{stem}{suffix}"
            if p.exists():
                found = p
                break
        if not found:
            matches = [p for p in tdir.glob(f"{stem}*.json") if stem_from_word_path(p) == stem]
            found = matches[0] if matches else None
        if not found:
            raise FileNotFoundError(f"Transcript words introuvable pour {stem} dans {tdir}")
        cache[stem] = read_words(found)
    return cache


def border_words(words, start, end):
    prev_w = None
    next_w = None
    for w in words:
        if w["end"] <= start + 1e-3:
            if prev_w is None or w["end"] > prev_w["end"]:
                prev_w = w
        if w["start"] >= end - 1e-3:
            if next_w is None or w["start"] < next_w["start"]:
                next_w = w
    return prev_w, next_w


def compute_cuts(edl, words_cache, honor_manual=False):
    for e in edl:
        if honor_manual and e.get("in_ext") is not None and e.get("out_ext") is not None:
            e["in_ext"] = round(float(e["in_ext"]), 3)
            e["out_ext"] = round(float(e["out_ext"]), 3)
            continue
        words = words_cache[e["stem"]]
        prev_w, next_w = border_words(words, e["start"], e["end"])
        gap_before = (e["start"] - prev_w["end"]) if prev_w else 1.0
        gap_after = (next_w["start"] - e["end"]) if next_w else 1.0
        if gap_before > MAX_GAP_EXTEND_S:
            in_ext = e["start"]
        else:
            in_ext = e["start"] - min(CUT_MARGIN_IN, max(0.0, gap_before) / 2)
        out_ext = e["end"] + min(CUT_MARGIN_OUT, max(0.0, gap_after) / 2)
        dur = probe_duration(e["source"])
        e["in_ext"] = round(max(0.0, in_ext), 3)
        e["out_ext"] = round(min(dur, out_ext), 3)


DRAFT_WIDTH = 480
DRAFT_FPS = 30
DRAFT_CRF = "30"
DRAFT_PRESET = "ultrafast"
DRAFT_FONTS = ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
               "C:/Windows/Fonts/consola.ttf")


def _esc_filter_value(text):
    """Echappe une valeur d'option de filtre ffmpeg (elle sera entre apostrophes)."""
    return str(text).replace("\\", "/").replace("'", r"\'").replace(":", r"\:").replace("%", r"\%")


def draft_video_chain(label):
    """Chaine video d'un brouillon : reduction, fps bas, puis etiquette incrustee.

    L'etiquette est le point qui compte pour l'usage : trois brouillons se ressemblent, et
    un arbitrage sur la mauvaise variante coute une passe entiere. Le nom du fichier ne
    suffit pas - dans un lecteur en plein ecran on ne le voit plus.

    ⚠️ `fontfile` doit etre ENTRE APOSTROPHES. Non quote, `C\\:/Windows/...` fait echouer
    l'analyse du graphe (verifie : six variantes testees, seule la forme non quotee tombe),
    et le message d'erreur de ffmpeg ne montre pas la position fautive.
    """
    chain = [f"scale={DRAFT_WIDTH}:-2", f"fps={DRAFT_FPS}"]
    font = next((f for f in DRAFT_FONTS if Path(f).exists()), None)
    if label and font:
        chain.append(
            f"drawtext=fontfile='{_esc_filter_value(font)}'"
            f":text='{_esc_filter_value(label)}'"
            ":fontcolor=white:fontsize=18:x=10:y=10"
            ":box=1:boxcolor=black@0.55:boxborderw=7"
        )
    elif label:
        print("[assemble] aucune police trouvee -> brouillon SANS etiquette incrustee, "
              "l'identification repose sur le nom du fichier seul.")
    return ",".join(chain)


def expand_punch(edl, honor_punch=False):
    """Eclate chaque segment en 1..N SOUS-PLANS, un par cadrage.

    Un segment porte un seul `trim`, donc un seul `crop` : basculer le cadre au milieu
    d'un segment continu demande de le couper en deux entrees du graphe. C'est ce qui rend
    le rythme atteignable : les segments longs (plus de 3,2 s) occupent souvent plus de la
    moitie du temps a l'ecran, et sans coupe interne ils restent hors de la fenetre 2-3 s.

    ⚠️ Les micro-fondus audio ne se posent QU'AUX BORDS du segment d'origine. A une coupe
    interne le son est continu : il n'y a aucun clic a masquer, et un `afade` y creuserait
    un trou audible tous les deux ou trois secondes.

    Renvoie une liste de sous-plans : source, bornes, boite de recadrage, et de quel cote
    poser les fondus.
    """
    out = []
    for e in edl:
        in_ext, out_ext = e["in_ext"], e["out_ext"]
        punch = e.get("punch") if honor_punch else None
        steps = (punch or {}).get("steps") or [{"in": in_ext, "out": out_ext, "crop": None}]
        size = (punch or {}).get("out") or [None, None]
        for j, s in enumerate(steps):
            out.append({
                "src": e["source"],
                "in": float(s.get("in", in_ext)),
                "out": float(s.get("out", out_ext)),
                "crop": s.get("crop"),
                "size": size,
                "fade_in": j == 0,
                "fade_out": j == len(steps) - 1,
            })
    return out


def build_filter_complex(edl, source_to_input_idx, video_tail=None, honor_punch=False):
    """Le graphe : un trim par sous-plan, puis un concat unique.

    `honor_punch` insere le recadrage de `tools/punch.py` ICI, et c'est le seul bon endroit :

    - **C'est gratuit en generation.** La passe encode deja en `libx264 -crf 18` plus bas.
      Une passe separee master -> master_punche empilerait un re-encodage h264 PAR-DESSUS
      un agrandissement, soit le pire ordre possible pour la nettete.
    - `concat` exige des dimensions identiques sur toutes ses entrees. Chaque sous-plan est
      donc recadre PUIS remis a la geometrie de la source (`scale` vers `punch["out"]`),
      ce qui preserve l'invariant. `setsar=1` evite qu'un SAR herite d'un `crop` ne fasse
      diverger une entree des autres.

    Le punch deplace le visage : il se decide AVANT l'habillage, qui se pose sur le master
    punche. Les mesures de zone sure se refont donc sur ce master.
    """
    plans = expand_punch(edl, honor_punch=honor_punch)
    parts = []
    for k, p in enumerate(plans):
        m = source_to_input_idx[p["src"]]
        seg_dur = max(0.001, p["out"] - p["in"])
        fade = min(AUDIO_FADE_S, seg_dur / 4)
        vchain = [f"trim=start={p['in']:.3f}:end={p['out']:.3f}", "setpts=PTS-STARTPTS"]
        if p["crop"]:
            w, h, x, y = p["crop"]
            ow, oh = p["size"]
            vchain.append(f"crop={w}:{h}:{x}:{y}")
            if ow and oh:
                vchain.append(f"scale={ow}:{oh}:flags=lanczos")
            vchain.append("setsar=1")
        parts.append(f"[{m}:v]{','.join(vchain)}[v{k}]")
        achain = [f"atrim=start={p['in']:.3f}:end={p['out']:.3f}", "asetpts=PTS-STARTPTS"]
        if p["fade_in"]:
            achain.append(f"afade=t=in:st=0:d={fade:.4f}")
        if p["fade_out"]:
            achain.append(f"afade=t=out:st={max(0.0, seg_dur - fade):.4f}:d={fade:.4f}")
        parts.append(f"[{m}:a]{','.join(achain)}[a{k}]")
    concat_in = "".join(f"[v{k}][a{k}]" for k in range(len(plans)))
    if video_tail:
        # la reduction APRES le concat : une seule passe de scale au lieu d'une par source
        parts.append(f"{concat_in}concat=n={len(plans)}:v=1:a=1[vcat][aout]")
        parts.append(f"[vcat]{video_tail}[vout]")
    else:
        parts.append(f"{concat_in}concat=n={len(plans)}:v=1:a=1[vout][aout]")
    return ";\n".join(parts) + "\n"


def assemble_video(edl, out_path, draft_label=None, honor_punch=False):
    sources = []
    for e in edl:
        src = str(Path(e["source"]))
        if src not in sources:
            sources.append(src)
    source_to_input_idx = {src: i for i, src in enumerate(sources)}

    tail = draft_video_chain(draft_label) if draft_label is not None else None
    filter_file = out_path.parent / f"_{out_path.stem}_filter_complex.txt"
    filter_file.write_text(
        build_filter_complex(edl, source_to_input_idx, video_tail=tail, honor_punch=honor_punch),
        encoding="utf-8",
    )
    cmd = [paths.ffmpeg(), "-y"]
    for src in sources:
        cmd += ["-i", src]
    cmd += ["-filter_complex_script", str(filter_file), "-map", "[vout]", "-map", "[aout]"]
    if tail:
        cmd += ["-c:v", "libx264", "-crf", DRAFT_CRF, "-preset", DRAFT_PRESET,
                "-c:a", "aac", "-b:a", "96k"]
    else:
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "slow",
                "-c:a", "aac", "-b:a", "192k"]
    cmd += [str(out_path)]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if res.returncode != 0:
        print(res.stderr[-4000:])
        raise SystemExit(f"ffmpeg a echoue (code {res.returncode})")
    filter_file.unlink(missing_ok=True)


def build_master_srt(edl, words_cache, out_srt, profile="karaoke", out_words=None):
    """SRT du master, et - si `out_words` est donne - les mots en TEMPS MASTER.

    `profile` : `karaoke` (6 mots, 2,5 s, le defaut) convient a un short, dont les
    sous-titres sont INCRUSTES et lus en karaoke. Une video longue livree avec des
    sous-titres separes (CC) prend `youtube` : 6 mots par bloc y defileraient trop vite.

    `out_words` ecrit les mots en temps master dans `1_montage/transcripts-master/` :
    l'habillage en a besoin pour ancrer un element au mot.
    """
    rules = load_corrections(str(TOOLS_DIR / "corrections.json"))
    all_blocks = []
    master_words = []
    cum = 0.0
    for e in edl:
        seg_dur = e["out_ext"] - e["in_ext"]
        seg_words = []
        for w in words_cache[e["stem"]]:
            if w["end"] <= e["in_ext"] or w["start"] >= e["out_ext"]:
                continue
            new_start = cum + max(0.0, w["start"] - e["in_ext"])
            new_end = cum + min(seg_dur, w["end"] - e["in_ext"])
            if new_end > new_start:
                seg_words.append({
                    "text": w["text"],
                    "start": round(new_start, 3),
                    "end": round(new_end, 3),
                    "p": w.get("p"),
                })
        e["_cum_start"] = round(cum, 3)
        e["_cum_end"] = round(cum + seg_dur, 3)
        if seg_words:
            if profile == "youtube":
                all_blocks.extend(chunk_words(seg_words, "youtube", max_dur=7.0, max_chars=84))
            else:
                all_blocks.extend(chunk_words(seg_words, "karaoke", max_words=6, max_dur=2.5))
            master_words.extend(seg_words)
        cum += seg_dur
    out_srt.write_text(build_srt(all_blocks, rules), encoding="utf-8")
    if out_words is not None:
        out_words.parent.mkdir(parents=True, exist_ok=True)
        out_words.write_text(json.dumps(master_words, ensure_ascii=False), encoding="utf-8")
    return cum, len(all_blocks)


def build_report(edl, holes, total_dur, out_report):
    watch = [e for e in edl if e.get("flags")]
    watch += sorted(edl, key=lambda e: e.get("score", 1.0))[:3]
    seen = set()
    watch_unique = []
    for e in sorted(watch, key=lambda e: e["i"]):
        if e["i"] not in seen:
            seen.add(e["i"])
            watch_unique.append(e)

    lines = [
        "# Rapport de QA ciblee - montage-rushs",
        "",
        f"Duree montage : {total_dur:.1f}s",
        f"Lignes incluses : {len(edl)} | Trous : {len(holes)}",
        "",
        "## Segments a verifier",
        "",
    ]
    if not watch_unique:
        lines.append("Aucun segment flagge automatiquement.")
    for e in watch_unique:
        lines.append(f"### Ligne {e['i']} - \"{e['text']}\"")
        lines.append(f"- Master : {format_timecode(e['_cum_start'])} -> {format_timecode(e['_cum_end'])}")
        lines.append(f"- Source : {e['stem']} [{e['in_ext']:.2f}-{e['out_ext']:.2f}]")
        lines.append(f"- Score : {e.get('score', 0):.3f} | flags : {', '.join(e.get('flags', [])) or '-'}")
        for r in e.get("runner_ups", []):
            lines.append(f"- Runner-up : {r['stem']} [{r['start']:.2f}-{r['end']:.2f}] score={r['score']:.3f}")
        lines.append("")

    lines += ["", "## Table complete", "", "| i | master in | master out | score | take | flags |", "|---|---|---|---:|---|---|"]
    for e in edl:
        lines.append(
            f"| {e['i']} | {format_timecode(e['_cum_start'])} | {format_timecode(e['_cum_end'])} | "
            f"{e.get('score', 0):.3f} | {e['stem']} | {', '.join(e.get('flags', [])) or '-'} |"
        )
    out_report.write_text("\n".join(lines), encoding="utf-8")


def build_holes_report(holes, out_path):
    lines = ["# Trous - lignes exclues du montage", ""]
    if not holes:
        lines.append("Aucun trou.")
    for h in sorted(holes, key=lambda x: x["i"]):
        lines.append(f"## Ligne {h['i']} - \"{h['text']}\"")
        lines.append(f"- Raison : {h.get('reason', '-')}")
        if h.get("suggestion"):
            lines.append(f"- Suggestion : {h['suggestion']}")
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def default_out(edl_path):
    """Ou ecrire le master quand `--out` n'est pas donne.

    Cote montage, c'est l'autre moitie du contrat de raccord : l'EDL vit dans
    `<video>/1_montage/`, donc le master va dans `<video>/_media/masters/`, la ou
    l'habillage ira le chercher. Rien n'est declare, tout est deduit.

    Repli sur `<edl_dir>/out/` pour un EDL hors de `videos/` (bacs a sable), mais il
    **s'annonce** : un chemin devine en silence ne doit pas passer inapercu.
    """
    try:
        v = paths.video_from_path(edl_path)
    except paths.UnknownVideo:
        fallback = Path(edl_path).parent / "out" / "montage_rushs.mp4"
        print(f"[assemble] EDL hors d'un dossier video -> repli sur {fallback}")
        return fallback
    return paths.media(v, "masters", create=True) / f"{v.name}_montage.mp4"


def default_draft_out(edl_path, label):
    """`<video>/_media/masters/drafts/<video>_<label>.mp4`.

    Sous `_media/`, donc deja gitignore et dans la zone effacable : un brouillon est un
    artefact d'ARBITRAGE, jamais un livrable, et il ne doit pas pouvoir etre confondu avec
    un master. D'ou le sous-dossier `drafts/` plutot qu'un suffixe dans `masters/`.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-") or "brouillon"
    try:
        v = paths.video_from_path(edl_path)
    except paths.UnknownVideo:
        d = Path(edl_path).parent / "out" / "drafts"
        d.mkdir(parents=True, exist_ok=True)
        print(f"[assemble] EDL hors d'un dossier video -> repli sur {d}")
        return d / f"brouillon_{slug}.mp4"
    d = paths.media(v, "masters", create=True) / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{v.name}_{slug}.mp4"


def avertit_ecrasement(out_path):
    """Dire, AVANT d'ecraser un master, qu'il existe et depuis quand.

    Cette fonction ecrit sur `<video>_montage.mp4` sans rien demander : le master sur lequel
    portent TOUS les timecodes d'une liste de retours disparait a la seconde ou l'encodage
    suivant demarre. Il faut donc archiver un master relu AVANT de relancer
    `assemble --honor-cuts`, et le rappel est pose sur le chemin de production, la ou on le
    voit au moment de lancer la commande.

    ⚠️ IL AVERTIT, IL N'ARCHIVE PAS : archiver d'office ajouterait une copie a CHAQUE passe
    d'une chaine qu'on relance plusieurs fois, et le disque se remplirait de copies que
    personne n'a relues. Il ne REFUSE pas non plus : un outil qui bloque l'encodage finit
    contourne.
    """
    if not out_path.exists():
        return
    import datetime
    st = out_path.stat()
    quand = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    print(f"[assemble] ATTENTION ECRASEMENT de {out_path.name}")
    print(f"           il existe depuis le {quand} et pese {st.st_size / 1e6:.0f} Mo.")
    print(f"           S'IL A ETE RELU, l'archiver d'abord - les timecodes des "
          f"retours s'y rapportent :")
    print(f"             mv \"{out_path.name}\" \"{out_path.stem}_v<N>-<date>-REMPLACE"
          f"{out_path.suffix}\"")
    print(f"           (Recuperable en relancant l'EDL qui l'a produit, mais tant que la "
          f"passe suivante n'est pas validee, il n'est pas remplacable.)")


def default_reports_dir(edl_path, out_path):
    """Ou ecrire le SRT et les rapports - c'est de la TRACE, pas du MEDIA.

    Les ecrire a cote du master les mettrait dans `_media/masters/`, donc **hors git**
    (`videos/*/_media/` est ignore) et dans la zone effacable. Ils vont dans
    `<video>/1_montage/rapports/`, versionne.
    """
    try:
        v = paths.video_from_path(edl_path)
    except paths.UnknownVideo:
        return out_path.parent
    d = paths.montage_dir(v) / "rapports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("edl", help="Path to edl_v3.json")
    parser.add_argument("--transcripts", default=None, help="Default: <edl_dir>/transcripts")
    parser.add_argument("--holes", default=None, help="Default: <edl_dir>/holes_v3.json if present")
    parser.add_argument("--out", default=None,
                        help="Defaut : <video>/_media/masters/<video>_montage.mp4")
    parser.add_argument("--reports-dir", default=None,
                        help="Defaut : <video>/1_montage/rapports/ (SRT + rapports = TRACE, versionnee)")
    parser.add_argument("--srt-profile", choices=["karaoke", "youtube"], default="karaoke",
                        help="Decoupage du SRT du master. `karaoke` (defaut) pour un short, "
                             "dont les sous-titres sont incrustes ; `youtube` pour une video "
                             "longue livree avec des CC separes - 6 mots par bloc y "
                             "defileraient trop vite.")
    parser.add_argument("--honor-cuts", action="store_true", help="Respect in_ext/out_ext already present in the EDL")
    parser.add_argument("--honor-punch", action="store_true",
                        help="Applique la cle `punch` posee par tools/punch.py : un recadrage "
                             "par segment, cuit dans le master. Sans ce drapeau la cle est "
                             "ignoree, donc poser le punch ne change rien tant qu'on ne le "
                             "demande pas. ATTENTION - un punch DEPLACE le visage : il se "
                             "decide AVANT l'habillage, qui se pose sur le master punche.")
    parser.add_argument("--plan-only", action="store_true",
                        help="Ecrit le timeline, le SRT et les rapports SANS encoder (~2 s). "
                             "C'est l'entree du pre-vol : qa_montage.py --skip-ffmpeg tourne dessus.")
    parser.add_argument("--draft", metavar="ETIQUETTE", default=None,
                        help="Sort un BROUILLON basse definition etiquete dans "
                             "<video>/_media/masters/drafts/, pour faire arbitrer une duree en "
                             "REGARDANT, environ cinq fois plus vite qu'un vrai encodage. "
                             "N'ecrit ni timeline, ni SRT, ni rapport.")
    args = parser.parse_args()
    if args.draft is not None and args.plan_only:
        parser.error("--draft et --plan-only s'excluent : l'un encode, l'autre pas")

    edl_path = Path(args.edl)
    base = edl_path.parent
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    edl.sort(key=lambda e: e["i"])
    if not edl:
        raise SystemExit("EDL vide")

    tdir = Path(args.transcripts) if args.transcripts else base / "transcripts"
    holes_path = Path(args.holes) if args.holes else base / "holes_v3.json"
    holes = json.loads(holes_path.read_text(encoding="utf-8")) if holes_path.exists() else []
    draft = args.draft is not None
    if draft:
        out_path = Path(args.out) if args.out else default_draft_out(edl_path, args.draft)
    else:
        out_path = Path(args.out) if args.out else default_out(edl_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    stems = sorted({e["stem"] for e in edl})
    words_cache = discover_words(tdir, stems)
    compute_cuts(edl, words_cache, honor_manual=args.honor_cuts)
    # ⚠️ LE SEUL ENDROIT OU UNE FENETRE INVERSEE PEUT ENCORE ETRE ARRETEE.
    #
    # `build_filter_complex` en ferait `trim=start=171.120:end=170.790`, et ffmpeg rend ZERO
    # IMAGE sans erreur, code de sortie 0. Le SRT reste COHERENT avec l'image, parce que
    # `build_master_srt` filtre les mots sur `new_end > new_start` : le master n'a pas l'air
    # casse, il a l'air monte serre, avec de la parole en moins.
    #
    # Le controle est ici, sur le chemin de PRODUCTION, et pas seulement dans le pre-vol
    # d'`audit_edl.py`, qu'il faut penser a lancer. Un seul point d'appel couvre les trois
    # modes (`--plan-only`, `--draft`, encodage reel), apres `compute_cuts` et avant la
    # premiere ecriture. La verification vit dans `audit_edl.py` et n'est PAS dupliquee.
    exiger_fenetres_positives(edl, edl_path)
    sous_plans = [s for e in edl for s in ((e.get("punch") or {}).get("steps") or [])]
    n_punch = sum(1 for s in sous_plans if s.get("crop"))
    if args.honor_punch and not sous_plans:
        print("[assemble] --honor-punch demande mais AUCUN segment ne porte de cle `punch`. "
              "Lancer d'abord : python tools/punch.py plan <edl_v3_timeline.json>")
    if not args.plan_only:
        avertit_ecrasement(out_path)
        assemble_video(edl, out_path, draft_label=args.draft, honor_punch=args.honor_punch)

    if draft:
        # ⚠️ Un brouillon n'ecrit NI timeline, NI SRT, NI rapport, et ce n'est pas une
        # economie : une variante est un EDL different, mais toutes partagent le meme dossier.
        # Trois brouillons ecriraient donc trois fois `edl_v3_timeline.json` au meme endroit,
        # et le pipeline reel repartirait ensuite de la DERNIERE variante encodee - qui n'est
        # pas forcement celle qui a ete choisie. Le brouillon ne laisse que son mp4.
        total_dur = sum(e["out_ext"] - e["in_ext"] for e in edl)
        print(f"BROUILLON -> {out_path}")
        print(f"  {total_dur:.1f}s, {len(edl)} segments, {DRAFT_WIDTH}px de large, "
              f"crf {DRAFT_CRF} preset {DRAFT_PRESET}, etiquette \"{args.draft}\"")
        #  Un brouillon sert a ARBITRER : sans cette ligne, rien ne distingue a la lecture
        #  un brouillon punche d'un brouillon qui ne l'est pas, et l'arbitrage porte alors
        #  peut-etre sur la mauvaise variante. L'etiquette incrustee est choisie par
        #  l'appelant, donc elle ne prouve rien.
        if args.honor_punch and n_punch:
            print(f"  punch APPLIQUE : {len(sous_plans)} sous-plans, dont {n_punch} serres")
        elif n_punch:
            print(f"  punch NON applique ({n_punch} sous-plans serres ignores) : "
                  "ajouter --honor-punch")
        print("  artefact d'arbitrage : ni timeline, ni SRT, ni rapport ecrits, "
              "et il ne remplace jamais un master.")
        return

    rep_dir = Path(args.reports_dir) if args.reports_dir else default_reports_dir(edl_path, out_path)
    rep_dir.mkdir(parents=True, exist_ok=True)
    out_srt = rep_dir / f"{out_path.stem}.srt"
    out_report = rep_dir / f"{out_path.stem}_rapport.md"
    out_holes = rep_dir / f"{out_path.stem}_trous.md"
    out_words = base / "transcripts-master" / f"{out_path.stem}.words.json"
    total_dur, n_blocks = build_master_srt(edl, words_cache, out_srt,
                                           profile=args.srt_profile, out_words=out_words)
    build_report(edl, holes, total_dur, out_report)
    build_holes_report(holes, out_holes)

    enriched = base / "edl_v3_timeline.json"
    enriched.write_text(json.dumps(edl, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.plan_only:
        print(f"PLAN -> {enriched} ({total_dur:.1f}s, {n_blocks} blocs SRT, AUCUN encodage)")
        print(f"OK   -> {rep_dir} (SRT, rapport, trous)")
        print(f"\nMaster prevu (non ecrit) : {out_path}")
        # ⚠️ `tighten_cuts` N'EST PROPOSE QUE SI LES BORNES NE FONT PAS DEJA FOI.
        # `--honor-cuts` veut dire "ces `in_ext`/`out_ext` sont la verite", or `tighten_cuts`
        # les RECALCULE depuis zero : le suggerer detruirait tout override pose a la main
        # (un blanc arbitre a l'oreille, une respiration rouverte), sous l'apparence d'une
        # bonne hygiene.
        lignes = ["Pre-vol :"]
        if args.honor_cuts:
            lignes.append("  (pas de tighten_cuts : --honor-cuts dit que ces bornes font "
                          "foi, il les recalculerait)")
        else:
            lignes.append(f"  python tools/tighten_cuts.py \"{enriched}\"")
        lignes += [f"  python tools/qa_montage.py --edl \"{enriched}\" --skip-ffmpeg",
                   f"  python tools/punch.py plan \"{enriched}\"        # rythme de plan, optionnel"]
        print("\n".join(lignes))
    else:
        print(f"OK -> {out_path} ({total_dur:.1f}s, {n_blocks} blocs SRT)")
        if args.honor_punch and n_punch:
            print(f"  punch APPLIQUE : {len(sous_plans)} sous-plans issus de {len(edl)} segments, "
                  f"dont {n_punch} serres -> le visage a bouge.")
            print("  Remesurer les zones sures sur CE master avant d'habiller.")
        elif n_punch:
            print(f"  ATTENTION - {n_punch} sous-plans portent un recadrage mais --honor-punch n'a pas "
                  "ete demande : ce master n'est PAS punche.")
        print(f"OK -> {rep_dir} (SRT, rapport, trous)")

        # La review du montage s'ouvre toute seule : c'est ICI que le fichier relisible
        # vient d'exister. Ni sur `--draft` ni sur `--plan-only` (branches separees plus
        # haut), ni sur un `--out` qui n'est pas le master canonique (un intermediaire).
        canonique = default_out(edl_path)
        if os.path.normcase(os.path.abspath(str(out_path))) != \
           os.path.normcase(os.path.abspath(str(canonique))):
            print(f"[review] pas de review : {Path(out_path).name} n'est pas le master "
                  f"canonique ({Path(canonique).name}), donc c'est un intermediaire.")
            vd = None
        else:
            try:
                vd = paths.video_from_path(out_path)
            except Exception:                    # noqa: BLE001
                # un master ecrit hors d'un dossier video (essai, chemin donne a la main) :
                # rien a ouvrir, et ce n'est pas une anomalie.
                vd = None
        if vd is not None:
            try:
                import retours as _rv
                print(_rv.ouvrir_review(vd, Path(out_path).name))
            except Exception as e:               # noqa: BLE001
                # la review est un canal lateral : elle ne casse jamais un montage
                print(f"[review] ouverture impossible ({e.__class__.__name__}), master intact.")


if __name__ == "__main__":
    main()
