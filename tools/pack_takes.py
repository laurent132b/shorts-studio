"""
tools/pack_takes.py - build a compact take transcript for montage-rushs.

The montage selector works from JSON word timings, but an agent should not
have to read every raw word file to understand a project. This script produces
`takes_packed.md`: a small, scannable map of all rushs, grouped by speech spans,
with confidence hints and vocal markers.

Inputs:
- a project/input directory containing `transcripts/`;
- or a transcripts directory directly.

Supported word files:
- <stem>.words.json      (current tools/transcribe.py output)
- <stem>.words_v2.json   (legacy format)
- <stem>.transcript.json (normalized envelope; words are read from it)

Usage:
    python tools/pack_takes.py videos/<slug>/1_montage
    python tools/pack_takes.py path/to/project/input --out path/to/takes_packed.md
"""

import argparse
import json
import re
import statistics
import time
import unicodedata
from pathlib import Path

PAUSE_BREAK_S = 0.8
MAX_GROUP_WORDS = 32
SENTENCE_END_RE = re.compile(r"[.!?]$")
HESITATION_TOKENS = {"euh", "heu", "euhh", "heuu", "hum", "mmh"}
GOOD_MARKERS = ("top", "bonne", "on la garde", "parfait")
BAD_MARKERS = ("non", "on refait", "attends", "rate", "raté")
# Radicaux de fichiers de reference a ne pas lister (aucun par defaut).
DEFAULT_EXCLUDES = set()


def norm_text(text):
    text = text.lower()
    text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def time_s(seconds):
    return f"{seconds:07.3f}s"


def read_words(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        words = data.get("words", [])
        engine = data.get("engine")
        model = data.get("model")
    else:
        words = data
        engine = None
        model = None
    clean = []
    for i, w in enumerate(words):
        text = (w.get("text") or "").strip()
        if not text or "start" not in w or "end" not in w:
            continue
        clean.append({
            "id": w.get("id", f"w{i}"),
            "text": text,
            "start": round(float(w["start"]), 3),
            "end": round(float(w["end"]), 3),
            "p": w.get("p"),
        })
    return clean, engine, model


def discover_transcripts(input_path):
    p = Path(input_path)
    if (p / "transcripts").is_dir():
        return p / "transcripts"
    return p


def stem_from_word_path(path):
    name = path.name
    for suffix in (".transcript.json", ".words_v2.json", ".words.json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def iter_word_files(tdir, excludes):
    candidates = []
    for pattern in ("*.transcript.json", "*.words_v2.json", "*.words.json"):
        candidates.extend(sorted(tdir.glob(pattern)))

    by_stem = {}
    priority = {".transcript.json": 0, ".words_v2.json": 1, ".words.json": 2}
    for path in candidates:
        stem = stem_from_word_path(path)
        if stem in excludes:
            continue
        suffix = next(s for s in priority if path.name.endswith(s))
        prev = by_stem.get(stem)
        if prev is None or priority[suffix] < priority[prev[0]]:
            by_stem[stem] = (suffix, path)
    for stem in sorted(by_stem):
        yield stem, by_stem[stem][1]


def group_words(words, pause_break=PAUSE_BREAK_S):
    groups = []
    current = []
    for w in words:
        if current:
            gap = w["start"] - current[-1]["end"]
            break_here = (
                gap > pause_break
                or len(current) >= MAX_GROUP_WORDS
                or SENTENCE_END_RE.search(current[-1]["text"])
            )
            if break_here:
                groups.append(current)
                current = []
        current.append(w)
    if current:
        groups.append(current)
    return groups


def confidence(words):
    vals = [w.get("p") for w in words if isinstance(w.get("p"), (int, float))]
    if not vals:
        return None
    return statistics.mean(vals)


MARKER_WINDOW_S = 3.0


def detect_marker(words, end):
    """Un marqueur vocal est ce qui est dit APRES la prise, pas dedans.

    ⚠️ Lire le texte DU GROUPE produirait des faux positifs silencieux : "le TOP du top."
    serait marque `good:top`, et toute prise contenant le mot courant "non" serait marquee
    `bad:non`, c'est-a-dire signalee comme rejetee.

    Meme regle que le scorer (`marker_score` de `select_takes.py`) : seuls comptent les mots
    dont le debut tombe dans les 3 s qui SUIVENT la fin du groupe. `takes_packed.md`, que
    l'agent lit AVANT de choisir, ne raconte donc pas autre chose que le score.
    """
    nearby = [w for w in words if end <= w["start"] <= end + MARKER_WINDOW_S]
    n = f" {norm_text(' '.join(w['text'] for w in nearby))} "
    for marker in BAD_MARKERS:
        if f" {norm_text(marker)} " in n:
            return "bad:" + marker
    for marker in GOOD_MARKERS:
        if f" {norm_text(marker)} " in n:
            return "good:" + marker
    return None


def detect_flags(words, text, all_words=None):
    flags = []
    normed = [norm_text(w["text"]) for w in words]
    if any(t in HESITATION_TOKENS for t in normed):
        flags.append("hesitation")
    for a, b in zip(normed, normed[1:]):
        if a and a == b:
            flags.append("repetition")
            break
    # `all_words` = la prise ENTIERE : il faut les mots d'apres pour lire un marqueur.
    if all_words:
        marker = detect_marker(all_words, words[-1]["end"])
        if marker:
            flags.append(marker)
    if len(words) <= 2 and words[-1]["end"] - words[0]["start"] < 0.7:
        flags.append("micro")
    return flags


def render_take(stem, path, words, engine, model):
    groups = group_words(words)
    lines = [f"## {stem}", ""]
    meta = []
    if engine:
        meta.append(f"engine={engine}")
    if model:
        meta.append(f"model={model}")
    if words:
        # `dernier_mot~`, et non `duration~` : c'est la FIN DU DERNIER MOT, pas la duree du
        # media. Cet outil ne lit que les transcripts : il ne connait pas la duree du
        # fichier, et la nommer ferait croire qu'un rush s'arrete pile sur son dernier mot.
        meta.append(f"dernier_mot~{words[-1]['end']:.1f}s")
        meta.append(f"words={len(words)}")
        meta.append(f"groups={len(groups)}")
    lines.append(f"- Source mots : `{path.name}`" + (f" ({', '.join(meta)})" if meta else ""))
    lines.append("")

    for i, group in enumerate(groups, start=1):
        start = group[0]["start"]
        end = group[-1]["end"]
        text = " ".join(w["text"] for w in group)
        conf = confidence(group)
        flags = detect_flags(group, text, words)
        attrs = [f"id={i:03d}", f"{time_s(start)}->{time_s(end)}", f"dur={end-start:.2f}s"]
        attrs.append(f"p={conf:.2f}" if conf is not None else "p=n/a")
        if flags:
            attrs.append("flags=" + ",".join(flags))
        lines.append("- [" + " | ".join(attrs) + "]")
        lines.append(f"  \"{text}\"")
    lines.append("")
    return lines


def build_document(tdir, excludes):
    lines = [
        "# Takes packed - montage-rushs",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Transcripts dir: `{tdir}`",
        "",
        "Legend: `p` = average word confidence when available; `good:*` and `bad:*` are vocal markers.",
        "",
    ]
    count = 0
    for stem, path in iter_word_files(tdir, excludes):
        words, engine, model = read_words(path)
        if not words:
            continue
        lines.extend(render_take(stem, path, words, engine, model))
        count += 1
    if count == 0:
        lines.append("_No word transcript found._")
    return "\n".join(lines).rstrip() + "\n", count


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="Project input directory or transcripts directory")
    parser.add_argument("--out", default=None, help="Default: <input>/takes_packed.md")
    parser.add_argument("--include-reference", action="store_true", help="Do not exclude the reference files listed in DEFAULT_EXCLUDES")
    args = parser.parse_args()

    tdir = discover_transcripts(args.input)
    if not tdir.exists():
        raise SystemExit(f"Transcripts directory not found: {tdir}")

    input_path = Path(args.input)
    out = Path(args.out) if args.out else (
        input_path / "takes_packed.md" if input_path.is_dir() else tdir / "takes_packed.md"
    )
    excludes = set() if args.include_reference else set(DEFAULT_EXCLUDES)
    text, count = build_document(tdir, excludes)
    out.write_text(text, encoding="utf-8")
    print(f"OK -> {out} ({count} take(s))")


if __name__ == "__main__":
    main()
