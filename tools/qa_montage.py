"""
tools/qa_montage.py - deterministic QA for an assembled montage-rushs master.

This script catches issues that a frame-only visual review cannot see:
- long silent gaps in the assembled master, using SRT gaps and optional FFmpeg
  silencedetect;
- script jumps caused by holes between selected lines;
- long selected segments that often hide false starts, blanks, or unrelated text;
- dangling line endings that make a cut feel unfinished;
- short internal pauses that are too slow for a short-form jump cut;
- nearby repeated subtitle text.

It does not replace a human review. It decides which parts deserve a targeted visual or
audio-visual review.

Le master est OPTIONNEL : sur six controles, un seul le lit (`silencedetect`). Les autres
sont calcules sur des METADONNEES - le texte de l'EDL, les cumuls de duree, les blocs du
SRT - donc rien ne les oblige a attendre un encodage. `assemble.py --plan-only` produit la
timeline et le SRT en moins d'une seconde ; lance dessus, ce script est un PRE-VOL, et les
defauts qu'il trouve se corrigent avant de payer l'encodage.

Usage:
    python tools/qa_montage.py --edl <video>/1_montage/edl_v3_timeline.json --skip-ffmpeg
    python tools/qa_montage.py <master.mp4>
    python tools/qa_montage.py <master.mp4> --edl <...>/edl_v3_timeline.json --ffmpeg ffmpeg

⚠️ `INTERNAL_PAUSE` se calcule sur les horodatages ASR des mots. Il peut annoncer une pause
sur un trou qui n'est PAS muet (une syllabe que l'ASR n'a pas span-ee), que `tighten_cuts`
a deliberement refuse de couper : l'appliquer remettrait le defaut. Verifier le silence reel
(`tools/trous_gardes.py`, `tools/enveloppe.py`) avant d'agir sur cette ligne.

`AUDIO_SILENCE` utilise `silencedetect` a `d=1.0 s` : un blanc de moins d'une seconde ne s'y
voit pas. Pour ceux-la, `tools/blancs.py`.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

SRT_TS_RE = re.compile(
    r"(?P<start>\d\d:\d\d:\d\d,\d{3})\s+-->\s+(?P<end>\d\d:\d\d:\d\d,\d{3})"
)
SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")


def seconds_from_srt(ts):
    hms, ms = ts.split(",")
    h, m, s = [int(x) for x in hms.split(":")]
    return h * 3600 + m * 60 + s + int(ms) / 1000


def format_srt(seconds):
    seconds = max(0.0, float(seconds))
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def norm_tokens(text):
    text = text.lower()
    text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return [t for t in text.split() if t]


def parse_srt(path):
    blocks = []
    if not path or not path.exists():
        return blocks
    raw_blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8", errors="replace").strip())
    for raw in raw_blocks:
        lines = [x.strip() for x in raw.splitlines() if x.strip()]
        if len(lines) < 2:
            continue
        match = None
        ts_index = None
        for i, line in enumerate(lines):
            match = SRT_TS_RE.search(line)
            if match:
                ts_index = i
                break
        if not match:
            continue
        text = " ".join(lines[ts_index + 1 :]).strip()
        blocks.append({
            "start": seconds_from_srt(match.group("start")),
            "end": seconds_from_srt(match.group("end")),
            "text": text,
        })
    return blocks


def load_edl(path):
    if not path or not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_raw_words(tdir, stem):
    """Mots d'un rush avec leurs timings BRUTS, dans l'ordre de priorite des outils.

    Bruts, et non plafonnes comme dans `assemble.read_words` : ce plafond raccourcit la
    fin de chaque mot selon la longueur de son texte, ce qui FABRIQUE du trou la ou l'oreille
    n'entend rien. Pour juger un blanc, la verite est le timing brut. (Les deux mesures
    donnent en pratique le meme verdict a 0,55 s : ce choix evite surtout un faux positif de
    principe.)
    """
    for suffix in (".transcript.json", ".words_v2.json", ".words.json"):
        p = tdir / f"{stem}{suffix}"
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        words = data.get("words", []) if isinstance(data, dict) else data
        return [
            {"text": w["text"], "start": float(w["start"]), "end": float(w["end"])}
            for w in words
            if w.get("text") and "start" in w and "end" in w
        ]
    return []


def words_for_edl(edl, tdir):
    """`{stem: mots}`, tolerant : un transcript manquant n'arrete pas la QA."""
    if not tdir or not Path(tdir).is_dir():
        return {}
    cache = {}
    for stem in sorted({e.get("stem") for e in edl if e.get("stem")}):
        cache[stem] = load_raw_words(Path(tdir), stem)
    return {k: v for k, v in cache.items() if v}


def segment_words(words, e):
    """Mots reellement contenus dans la coupe encodee `[in_ext, out_ext]`.

    Meme predicat que `assemble.build_master_srt` : on mesure ce que le master contiendra,
    pas ce que la selection avait vise.
    """
    a = float(e.get("in_ext", e.get("start", 0.0)))
    b = float(e.get("out_ext", e.get("end", a)))
    return [w for w in words if w["end"] > a and w["start"] < b]


def find_segment_at(edl, master_time):
    for e in edl:
        if e.get("_cum_start", -1) <= master_time <= e.get("_cum_end", -1):
            return e
    return None


def detect_srt_gaps(blocks, edl, threshold):
    issues = []
    for a, b in zip(blocks, blocks[1:]):
        gap = b["start"] - a["end"]
        if gap < threshold:
            continue
        mid = a["end"] + gap / 2
        seg = find_segment_at(edl, mid)
        issues.append({
            "type": "LONG_SUBTITLE_GAP",
            "severity": "high" if gap >= 3.0 else "medium",
            "master_start": round(a["end"], 3),
            "master_end": round(b["start"], 3),
            "duration": round(gap, 3),
            "line": seg.get("i") if seg else None,
            "source": seg.get("source") if seg else None,
            "source_start": seg.get("in_ext") if seg else None,
            "source_end": seg.get("out_ext") if seg else None,
            "message": "Gap long entre deux blocs SRT, probablement blanc ou respiration non coupee.",
        })
    return issues


def detect_line_jumps(edl):
    issues = []
    ordered = sorted(edl, key=lambda e: e.get("_cum_start", e.get("i", 0)))
    for a, b in zip(ordered, ordered[1:]):
        gap = int(b["i"]) - int(a["i"])
        if gap <= 1:
            continue
        issues.append({
            "type": "SCRIPT_JUMP",
            "severity": "medium" if gap <= 3 else "high",
            "from_line": int(a["i"]),
            "to_line": int(b["i"]),
            "missing_lines": list(range(int(a["i"]) + 1, int(b["i"]))),
            "master_time": round(float(a.get("_cum_end", 0.0)), 3),
            "message": "Saut dans le script entre deux lignes montees.",
        })
    return issues


def detect_duplicate_lines(edl):
    """Deux segments qui revendiquent le meme `i`.

    `select_takes.py` ne peut pas en produire (une ligne, un segment) : ca vient d'une
    retouche, et c'est souvent VOULU (une ligne coupee en deux pour retirer un blanc interne).
    Le signalement reste utile pour un vrai doublon : plus rien n'est indexe par `i` de facon
    unique, donc `detect_line_jumps` (qui trie par `i`) et tout rapprochement plan <-> ligne
    deviennent ambigus, et le doublon peut aussi etre un copier-coller accidentel. On
    l'annonce en `low` : c'est une information, pas un defaut.
    """
    # ⚠️ DISTINGUER UNE SCISSION D'UN DOUBLON, sinon ce controle noie son propre signal :
    # `tics.py --appliquer` et `pass_split_internal` scindent des dizaines de lignes, et un
    # controle qui crie au loup quarante fois est un controle qu'on cesse de lire.
    #
    # Ce qui tranche : des fragments de scission sont CONTIGUS dans la timeline (positions
    # consecutives, et donc `_cum_end` de l'un = `_cum_start` du suivant). Un vrai doublon -
    # copier-coller, ancre posee deux fois - remonte la meme ligne a deux endroits ELOIGNES.
    seen, rang = {}, {}
    for k, e in enumerate(edl):
        seen.setdefault(int(e["i"]), []).append(e)
        rang[id(e)] = k
    issues = []
    for i, group in sorted(seen.items()):
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda e: float(e.get("_cum_start", 0.0)))
        places = sorted(rang[id(e)] for e in ordered)
        if places == list(range(places[0], places[0] + len(places))):
            continue      # scission : fragments contigus, rien a signaler
        issues.append({
            "type": "DUPLICATE_LINE",
            "severity": "low",
            "line": i,
            "master_start": round(float(ordered[0].get("_cum_start", 0.0)), 3),
            "master_end": round(float(ordered[-1].get("_cum_end", 0.0)), 3),
            "count": len(group),
            "cuts": [[e.get("stem"), e.get("in_ext"), e.get("out_ext")] for e in ordered],
            "message": f"La ligne {i} est montee en {len(group)} segments NON CONTIGUS, "
                       "donc a deux endroits differents du master. Une scission (fragments "
                       "consecutifs) n'est pas signalee ; celle-ci ne l'est pas.",
        })
    return issues


def detect_truncated_groups(edl, words_cache, threshold):
    """La coupe tombe A L'INTERIEUR d'un groupe de souffle du rush source.

    Exemple : la fenetre s'arrete juste avant un mot qui appartient pourtant au meme elan.
    Personne ne le voit avant l'encodage, et rien d'autre ne le couvre :

    - `select_takes` choisit la fenetre qui ressemble le plus au TEXTE du script, il n'a
      aucune raison de s'aligner sur la respiration ;
    - `tighten_cuts.pass_truncation` traite l'inverse (de l'audio voise que le transcript
      ne couvre PAS) et refuse explicitement d'etendre quand un mot transcrit suit de pres,
      parce que ce mot est d'habitude une prise jetee.

    Ici le mot suivant est transcrit ET colle : c'est un enchainement, pas une prise jetee.
    On REPORTE sans corriger - savoir si le mot manque au sens est un arbitrage editorial.
    """
    issues = []
    for e in edl:
        words = words_cache.get(e.get("stem"))
        if not words:
            continue
        out_ext = float(e.get("out_ext", e.get("end", 0.0)))
        sel = segment_words(words, e)
        if not sel:
            continue
        after = [w for w in words if w["start"] >= sel[-1]["end"] - 1e-3 and w not in sel]
        if not after:
            continue
        nxt = min(after, key=lambda w: w["start"])
        gap = nxt["start"] - out_ext
        if gap >= threshold or gap < -1e-3:
            continue
        tail = [w["text"] for w in sorted(after, key=lambda w: w["start"])[:4]]
        issues.append({
            "type": "TRUNCATED_SOURCE_GROUP",
            "severity": "medium",
            "line": int(e["i"]),
            "master_start": round(float(e.get("_cum_start", 0.0)), 3),
            "master_end": round(float(e.get("_cum_end", 0.0)), 3),
            # `gap` et non `duration` : partout ailleurs `duration` est la longueur du
            # segment ou du defaut, et le rapport l'affiche en suffixe. Ici la mesure est
            # le silence AVANT le mot suivant, deja dit en millisecondes dans le message.
            "gap": round(gap, 3),
            "source": e.get("source"),
            "source_start": e.get("in_ext", e.get("start")),
            "source_end": out_ext,
            "next_words": tail,
            "message": f"La coupe tombe {gap*1000:.0f} ms avant '{' '.join(tail)}', dans le meme "
                       "elan : verifier que la phrase ne s'arrete pas au milieu.",
        })
    return issues


def detect_long_segments(edl, threshold):
    issues = []
    for e in edl:
        start = float(e.get("_cum_start", 0.0))
        end = float(e.get("_cum_end", start))
        dur = end - start
        if dur < threshold:
            continue
        issues.append({
            "type": "LONG_SEGMENT",
            "severity": "high" if dur >= threshold * 1.8 else "medium",
            "line": int(e["i"]),
            "master_start": round(start, 3),
            "master_end": round(end, 3),
            "duration": round(dur, 3),
            "source": e.get("source"),
            "source_start": e.get("in_ext", e.get("start")),
            "source_end": e.get("out_ext", e.get("end")),
            "score": e.get("score"),
            "flags": e.get("flags", []),
            "message": "Segment anormalement long pour une ligne de script, a verifier.",
        })
    return issues


def detect_overlapping_cuts(edl):
    """Deux segments consecutifs du MEME rush dont les fenetres se chevauchent.

    ⚠️ C'est le defaut le plus AUDIBLE d'un montage. Quand `out_ext` du segment de gauche
    depasse `in_ext` du suivant, l'assemblage joue deux fois le meme fragment : on entend
    « pa-par », « de de », « for-force ». Aucun autre controle ne l'attrape - le SRT est
    coherent, les cumuls sont justes, la duree est juste, et le master s'encode sans erreur.
    `tighten_cuts.py` les ecrete, mais un EDL retouche a la main peut en refabriquer - d'ou
    ce controle.

    Seuil a 30 ms : en dessous, le dedoublement n'est pas perceptible.
    """
    issues = []
    for a, b in zip(edl, edl[1:]):
        if a.get("stem") != b.get("stem"):
            continue
        if a.get("in_ext") is None or b.get("in_ext") is None:
            continue
        double = round(float(a["out_ext"]) - float(b["in_ext"]), 3)
        if double <= 0.030:
            continue
        issues.append({
            "type": "OVERLAPPING_CUT",
            "severity": "high" if double >= 0.15 else "medium",
            "line": int(a["i"]),
            "next_line": int(b["i"]),
            "master_start": round(float(a.get("_cum_end", 0.0)) - double, 3),
            "master_end": round(float(a.get("_cum_end", 0.0)), 3),
            "duration": double,
            "source": a.get("source"),
            "source_start": b.get("in_ext"),
            "source_end": a.get("out_ext"),
            "message": (f"Chevauchement de {double * 1000:.0f} ms avec L{b['i']} dans le meme "
                        "rush : cet audio est joue DEUX fois, on entend un debut de mot "
                        "repete. Corriger avec tighten_cuts.py."),
        })
    return issues


def detect_dangling_line_endings(edl):
    issues = []
    dangling_words = {
        "et",
        "mais",
        "donc",
        "comme",
        "avec",
        "dans",
        "pour",
        "par",
        "sur",
        "si",
        "quand",
        "lorsque",
        "que",
        "qui",
        "dont",
    }
    for e in edl:
        text = str(e.get("text", "")).strip()
        norm = norm_tokens(text)
        ends_with_comma = text.endswith((",", ";", ":"))
        ends_with_dangling_word = bool(norm and norm[-1] in dangling_words)
        if not ends_with_comma and not ends_with_dangling_word:
            continue
        start = float(e.get("_cum_start", 0.0))
        end = float(e.get("_cum_end", start))
        issues.append({
            "type": "DANGLING_LINE_END",
            "severity": "medium",
            "line": int(e["i"]),
            "master_start": round(start, 3),
            "master_end": round(end, 3),
            "duration": round(end - start, 3),
            "source": e.get("source"),
            "source_start": e.get("in_ext", e.get("start")),
            "source_end": e.get("out_ext", e.get("end")),
            "score": e.get("score"),
            "flags": e.get("flags", []),
            "message": "La ligne finit sur une articulation inachevee; verifier que la coupe ne sonne pas suspendue.",
        })
    return issues


def detect_internal_pauses(edl, threshold, words_cache=None):
    """Pauses internes perceptibles, mesurees sur la coupe REELLE quand c'est possible.

    ⚠️ `timing_quality.max_internal_gap` decrit la fenetre de MATCH de l'ASR
    (`select_takes.py` le calcule sur `toks[idx:idx+size]`), pas la coupe encodee
    `[in_ext, out_ext]` - que `compute_cuts` puis `tighten_cuts` deplacent tous les deux.
    Les deux intervalles divergent donc PAR CONSTRUCTION des qu'un outil ou une main touche
    les bords, et le champ porte est alors faux dans les DEUX sens :

        faux POSITIF : une ligne coupee en deux pour retirer un blanc ; les deux moities
                       heritent du champ du parent, et l'alerte reste apres la correction.
        faux NEGATIF : la coupe encodee porte un trou plus long que la fenetre de match.

    Le faux positif est le plus couteux ici : un pre-vol qu'on relance apres chaque
    correction ne sert a rien si les alertes ne s'eteignent pas quand le defaut est reparti.
    On recalcule donc depuis les mots, et on ne retombe sur le champ porte qu'a defaut de
    transcripts (en le disant dans `measured_on`).
    """
    issues = []
    words_cache = words_cache or {}
    for e in edl:
        timing = e.get("timing_quality") or {}
        carried = float(timing.get("max_internal_gap") or 0.0)
        words = words_cache.get(e.get("stem"))
        if words:
            sel = segment_words(words, e)
            gaps = [max(0.0, b["start"] - a["end"]) for a, b in zip(sel, sel[1:])]
            max_gap = round(max(gaps) if gaps else 0.0, 3)
            measured_on = "words"
        else:
            max_gap = carried
            measured_on = "timing_quality"
        if max_gap < threshold:
            continue
        start = float(e.get("_cum_start", 0.0))
        end = float(e.get("_cum_end", start))
        severity = "high" if max_gap >= 1.0 else "medium"
        issues.append({
            "type": "INTERNAL_PAUSE",
            "severity": severity,
            "line": int(e["i"]),
            "master_start": round(start, 3),
            "master_end": round(end, 3),
            "duration": round(max_gap, 3),
            "source": e.get("source"),
            "source_start": e.get("in_ext", e.get("start")),
            "source_end": e.get("out_ext", e.get("end")),
            "score": e.get("score"),
            "flags": e.get("flags", []),
            "measured_on": measured_on,
            "carried": round(carried, 3),
            "message": "Pause interne perceptible dans la ligne; verifier le rythme et le regard."
                       + ("" if measured_on == "timing_quality" or abs(carried - max_gap) <= 0.15
                          else f" (timing_quality annonce {carried:.2f}s, perime)"),
        })
    return issues


def detect_repeated_subtitles(blocks, max_gap_s=8.0):
    issues = []
    tokenized = [norm_tokens(b["text"]) for b in blocks]
    for i, toks in enumerate(tokenized):
        if len(toks) < 2:
            continue
        phrase = " ".join(toks)
        for j in range(i + 1, min(len(blocks), i + 6)):
            if blocks[j]["start"] - blocks[i]["end"] > max_gap_s:
                break
            other = " ".join(tokenized[j])
            if not other:
                continue
            same = phrase == other
            overlap = len(set(toks) & set(tokenized[j])) / max(1, min(len(set(toks)), len(set(tokenized[j]))))
            if same or (len(toks) >= 3 and overlap >= 0.8):
                issues.append({
                    "type": "NEARBY_REPETITION",
                    "severity": "medium",
                    "master_start": round(blocks[i]["start"], 3),
                    "master_end": round(blocks[j]["end"], 3),
                    "texts": [blocks[i]["text"], blocks[j]["text"]],
                    "message": "Texte proche repete dans une fenetre courte.",
                })
                break
    return issues


def detect_ffmpeg_silences(video, ffmpeg, noise, duration):
    if not video or not video.exists():
        return []
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-i",
        str(video),
        "-af",
        f"silencedetect=noise={noise}:d={duration}",
        "-f",
        "null",
        "-",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    text = (res.stderr or "") + "\n" + (res.stdout or "")
    silences = []
    current_start = None
    for line in text.splitlines():
        m_start = SILENCE_START_RE.search(line)
        if m_start:
            current_start = float(m_start.group(1))
        m_end = SILENCE_END_RE.search(line)
        if m_end:
            start = current_start if current_start is not None else float(m_end.group(1)) - float(m_end.group(2))
            silences.append({
                "type": "AUDIO_SILENCE",
                "severity": "high" if float(m_end.group(2)) >= 3.0 else "medium",
                "master_start": round(start, 3),
                "master_end": round(float(m_end.group(1)), 3),
                "duration": round(float(m_end.group(2)), 3),
                "message": "Silence detecte par FFmpeg sur le master.",
            })
            current_start = None
    return silences


def attach_segments(issues, edl):
    for issue in issues:
        if issue.get("line") is not None or "master_start" not in issue:
            continue
        seg = find_segment_at(edl, float(issue["master_start"]) + float(issue.get("duration", 0.0)) / 2)
        if seg:
            issue["line"] = int(seg["i"])
            issue["source"] = seg.get("source")
            issue["source_start"] = seg.get("in_ext", seg.get("start"))
            issue["source_end"] = seg.get("out_ext", seg.get("end"))


def build_markdown(issues, out_path):
    lines = [
        "# QA montage - blancs, repetitions, sauts",
        "",
        f"Issues detectees : {len(issues)}",
        "",
    ]
    if not issues:
        lines.append("Aucun probleme detecte selon les seuils actuels.")
    else:
        lines += ["| type | severite | master | ligne | details |", "|---|---|---|---:|---|"]
        for issue in issues:
            if "master_start" in issue:
                master = f"{format_srt(issue['master_start'])} -> {format_srt(issue.get('master_end', issue['master_start']))}"
            elif "master_time" in issue:
                master = format_srt(issue["master_time"])
            else:
                master = "-"
            line = issue.get("line", issue.get("from_line", "-"))
            detail = issue.get("message", "")
            if issue.get("duration"):
                detail += f" ({issue['duration']:.2f}s)"
            if issue.get("missing_lines"):
                detail += f" lignes manquantes: {issue['missing_lines']}"
            lines.append(f"| {issue['type']} | {issue['severity']} | {master} | {line} | {detail} |")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def trace_dirs(master):
    """`(1_montage, 1_montage/rapports)` deduits du master, ou `(None, None)`.

    Le master vit dans `<video>/_media/masters/`, donc son dossier video est son
    arriere-grand-parent - c'est le contrat de raccord, cote QA. L'EDL est dans
    `1_montage/`, le SRT et les rapports dans `1_montage/rapports/` : rien n'est a
    declarer. Repli sur les anciens chemins relatifs au master si on est hors
    `videos/`, et il s'annonce.
    """
    try:
        v = paths.video_from_path(master)
    except paths.UnknownVideo:
        return None, None
    m = paths.montage_dir(v)
    return m, m / "rapports"


def planned_master(edl_path):
    """Le master que `assemble.py` ECRIRAIT pour cet EDL, sans exiger qu'il existe.

    En pre-vol il n'y a pas de master : il faut quand meme un nom, parce que le SRT et les
    rapports en derivent. On delegue a `assemble.default_out` plutot que de recoller le
    chemin ici - sinon le pre-vol chercherait un SRT sous un nom que `--plan-only` n'a pas
    ecrit, et ne verrait aucun bloc (donc aucun blanc, aucune repetition) EN SILENCE.
    """
    import assemble  # local : n'alourdit pas le passage avec master
    return assemble.default_out(edl_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("master", nargs="?", default=None,
                        help="Master MP4 assemble par tools/assemble.py. "
                             "OPTIONNEL : sans lui, --edl est requis et on tourne en PRE-VOL "
                             "(aucun controle ne lit les pixels, cf. l'entete).")
    parser.add_argument("--edl", default=None, help="Default: <video>/1_montage/edl_v3_timeline.json")
    parser.add_argument("--srt", default=None, help="Default: <video>/1_montage/rapports/<master>.srt")
    parser.add_argument("--out-dir", default=None, help="Default: <video>/1_montage/rapports/")
    parser.add_argument("--transcripts", default=None,
                        help="Defaut : <edl_dir>/transcripts. Sert a MESURER les trous internes "
                             "sur la coupe reelle au lieu de croire timing_quality.")
    parser.add_argument("--ffmpeg", default=None, help="Defaut : tools/paths.py (ffmpeg n'est pas dans le PATH)")
    parser.add_argument("--skip-ffmpeg", action="store_true")
    parser.add_argument("--srt-gap-threshold", type=float, default=1.2)
    parser.add_argument("--long-segment-threshold", type=float, default=4.0)
    parser.add_argument("--internal-pause-threshold", type=float, default=0.55)
    parser.add_argument("--group-gap-threshold", type=float, default=0.20,
                        help="Sous ce silence, le mot suivant du rush est considere comme "
                             "appartenant au meme groupe de souffle (calibre, cf. le controle).")
    parser.add_argument("--silence-noise", default="-35dB")
    parser.add_argument("--silence-duration", type=float, default=1.0)
    args = parser.parse_args()

    if args.master:
        master = Path(args.master)
        preflight = False
    else:
        if not args.edl:
            parser.error("sans master, --edl est requis (mode pre-vol)")
        master = planned_master(Path(args.edl))
        preflight = True
        print(f"[qa_montage] PRE-VOL : aucun master, master prevu = {master.name}")

    montage, rapports = trace_dirs(master)
    if montage is None:
        print(f"[qa_montage] master hors d'un dossier video -> chemins relatifs a {master.parent}")

    if args.srt:
        srt = Path(args.srt)
    elif rapports and (rapports / f"{master.stem}.srt").exists():
        srt = rapports / f"{master.stem}.srt"
    else:
        srt = master.with_suffix(".srt")

    if args.edl:
        edl_path = Path(args.edl)
    elif montage and (montage / "edl_v3_timeline.json").exists():
        edl_path = montage / "edl_v3_timeline.json"
    else:
        candidate_a = master.parent.parent / "edl_v3_timeline.json"
        candidate_b = master.parent / "edl_v3_timeline.json"
        edl_path = candidate_a if candidate_a.exists() else candidate_b

    edl = load_edl(edl_path)
    blocks = parse_srt(srt)
    if not blocks:
        # Deux controles sur six partent du SRT. Sans lui ils ne trouvent rien et
        # renvoient une liste vide, ce qui se lit comme « aucun defaut » : on le dit.
        print(f"[qa_montage] ATTENTION : aucun bloc SRT lu depuis {srt}")
        print("[qa_montage] LONG_SUBTITLE_GAP et NEARBY_REPETITION ne peuvent RIEN detecter.")
    tdir = Path(args.transcripts) if args.transcripts else edl_path.parent / "transcripts"
    words_cache = words_for_edl(edl, tdir)
    if not words_cache:
        print(f"[qa_montage] transcripts introuvables dans {tdir} -> les trous internes "
              "retombent sur timing_quality, qui peut etre perime (cf. l'entete du controle).")

    issues = []
    issues.extend(detect_srt_gaps(blocks, edl, args.srt_gap_threshold))
    issues.extend(detect_line_jumps(edl))
    issues.extend(detect_duplicate_lines(edl))
    issues.extend(detect_overlapping_cuts(edl))
    issues.extend(detect_long_segments(edl, args.long_segment_threshold))
    issues.extend(detect_dangling_line_endings(edl))
    issues.extend(detect_internal_pauses(edl, args.internal_pause_threshold, words_cache))
    issues.extend(detect_truncated_groups(edl, words_cache, args.group_gap_threshold))
    issues.extend(detect_repeated_subtitles(blocks))
    if args.skip_ffmpeg or preflight:
        # Le seul controle qui lise les pixels, et le seul qu'un pre-vol ne peut pas rendre.
        print("[qa_montage] AUDIO_SILENCE non teste (pas de master a sonder).")
    else:
        ff = args.ffmpeg or paths.ffmpeg()
        issues.extend(detect_ffmpeg_silences(master, ff, args.silence_noise, args.silence_duration))
    attach_segments(issues, edl)
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: (severity_rank.get(x.get("severity"), 9), x.get("master_start", x.get("master_time", 0))))

    out_dir = Path(args.out_dir) if args.out_dir else (rapports or master.parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_out = out_dir / f"{master.stem}_qa_montage.json"
    md_out = out_dir / f"{master.stem}_qa_montage.md"
    json_out.write_text(json.dumps(issues, ensure_ascii=False, indent=1), encoding="utf-8")
    build_markdown(issues, md_out)
    print(f"OK -> {json_out}")
    print(f"OK -> {md_out}")
    if issues:
        print(f"{len(issues)} issues detectees")


if __name__ == "__main__":
    main()
