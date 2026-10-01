"""
tools/select_takes.py - generic v3 take selector for montage-rushs.

Editorial philosophy: choose the best spoken take for each script line, list
runner-ups, and prefer honest holes over incoherent montage.

Input layout:
    project_input/
      final_transcript.json       # [{"i":0,"text":"..."}, ...]
      rush_001.mp4
      transcripts/
        rush_001.words.json
        rush_002.transcript.json

Outputs:
    edl_v3.json
    candidates.json
    holes_v3.json
    rapport_selection.md

Usage:
    python tools/select_takes.py path/to/project_input
    python tools/select_takes.py path/to/project_input --script script.json


UN MOT A TRAIT D'UNION VAUT UN MOT POUR L'ASR ET TROIS POUR LE SCRIPT
---------------------------------------------------------------------
La normalisation decoupe sur la ponctuation, donc "Aix-en-Provence" devient trois tokens
(`aix`, `en`, `provence`), alors que le `words.json` de l'ASR en fait un seul mot. Sur une
ligne courte, la fenetre candidate doit alors s'etendre AU-DELA du groupe pour reunir assez
de mots, et elle peut traverser un long silence qui suit : rejet sur `blank_gap` /
`silence_ratio`, donc TROU. Le message accuse un blanc, alors que le groupe source est
propre.

**Le signe qui tranche** : comparer le nombre de mots du script au `size` de la fenetre
candidate dans `holes_v3.json`. Un ecart, avec un groupe source court et propre, c'est ce
cas-la et pas une pause.

**La parade, cote SCRIPT** : tronquer la ligne avant le mot a trait d'union quand le reste
est discriminant. Quand la ligne n'est QUE le mot compose, le trou est le bon choix.

ATTENTION ne PAS "reparer" en desactivant le decoupage sur le trait d'union : il est correct
partout ailleurs ("plus-values", "auto-entrepreneur" se comparent bien mot a mot au script),
et changer la normalisation deplacerait toutes les fenetres qui vont bien.
"""

import argparse
import json
import re
import statistics
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

# Sans ceci, `--help` peut planter : le parseur recoit `description=__doc__`, et le stdout
# d'un terminal Windows est en cp1252.
# `errors="replace"` et non `encoding="utf-8"` : on ne change pas l'encodage du
# terminal, on rend seulement l'inencodable inoffensif.
for _flux in (sys.stdout, sys.stderr):
    if hasattr(_flux, "reconfigure"):
        _flux.reconfigure(errors="replace")

WEIGHTS = {
    "text": 0.45,
    "diction": 0.18,
    "fluidite": 0.14,
    "energie": 0.13,
    "recence": 0.05,
    "marker": 0.05,
}

INCLUDE_TEXT_THRESHOLD = 0.75
SOFT_TEXT_THRESHOLD = 0.66
SOFT_SCORE_THRESHOLD = 0.72
LOW_TEXT_THRESHOLD = 0.60
AMBIGU_GAP_THRESHOLD = 0.05
HESITATION_TOKENS = {"euh", "heu", "euhh", "heuu", "hum", "mmh"}
GOOD_MARKERS = ("top", "bonne", "on la garde", "on garde", "parfait", "nickel", "c'est bon")
# Ce qu'on dit a voix haute pour rejeter une prise. Un rejet non reconnu laisse marker=0.5,
# et la prise ratee peut alors arriver ex aequo avec la bonne et gagner au hasard du tri.
BAD_MARKERS = (
    "non",
    "on refait",
    "on recommence",
    "je reprends",
    "attends",
    "rate",
    "raté",
    "pas bon",
    "j'y arrive pas",
)
# Radicaux de fichiers de reference a ignorer (aucun par defaut ; --include-reference).
REFERENCE_STEMS_EXCLUDE = set()
MIN_WORD_DURATION_S = 0.05
MAX_WORD_DURATION_S = 0.9
PAUSE_GAP_THRESHOLD_S = 0.35
MAX_INTERNAL_GAP_S = 1.2
MAX_SILENCE_RATIO = 0.38
MAX_DUR_OVER_SCRIPT = 2.2
MAX_DUR_FLOOR_S = 3.5
EDGE_NOISE_PENALTY = 0.04

# Tolerance de la contrainte de monotonie par prise. Etait un 0.05 en dur a un seul endroit ;
# nommee parce que le cout d'anticipation doit utiliser EXACTEMENT le meme predicat que le
# choix, sinon il chiffre une consequence qui n'est pas celle qui se produira.
SEQUENCE_TOLERANCE = 0.05

# Ce que « coute » de perdre une ligne entiere, en plus de son score. Perdre une ligne n'est
# pas equivalent a la degrader : c'est un trou dans le script, que quelqu'un devra assumer,
# reformuler ou couvrir en B-roll. Cale a 0,30 : assez pour qu'une ligne sauvee l'emporte sur
# un gain de score marginal ailleurs, trop peu pour justifier de degrader gravement une ligne
# afin d'en sauver une autre.
HOLE_COST = 0.30


def norm(text):
    text = text.lower()
    text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return [t for t in text.split() if t]


def norm_join(text):
    return " " + " ".join(norm(text)) + " "


def stem_from_word_path(path):
    for suffix in (".transcript.json", ".words_v2.json", ".words.json"):
        if path.name.endswith(suffix):
            return path.name[: -len(suffix)]
    return path.stem


def read_words(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    words = data.get("words", []) if isinstance(data, dict) else data
    clean = []
    raw = []
    for i, w in enumerate(words):
        text = (w.get("text") or "").strip()
        if not text or "start" not in w or "end" not in w:
            continue
        raw.append({
            "id": w.get("id", f"w{i}"),
            "text": text,
            "start": round(float(w["start"]), 3),
            "end": round(float(w["end"]), 3),
            "p": w.get("p"),
        })
    for i, w in enumerate(raw):
        text_len = max(1, len(re.sub(r"[^A-Za-z0-9]", "", w["text"])))
        natural_cap = min(MAX_WORD_DURATION_S, 0.28 + text_len * 0.035)
        end = min(w["end"], w["start"] + natural_cap)
        if i + 1 < len(raw) and raw[i + 1]["start"] > w["start"]:
            end = min(end, max(w["start"] + MIN_WORD_DURATION_S, raw[i + 1]["start"] - 0.03))
        item = dict(w)
        item["end"] = round(max(w["start"] + MIN_WORD_DURATION_S, end), 3)
        clean.append(item)
    return clean


def discover_word_files(tdir, include_reference=False):
    paths = []
    for pattern in ("*.transcript.json", "*.words_v2.json", "*.words.json"):
        paths.extend(sorted(tdir.glob(pattern)))
    priority = {".transcript.json": 0, ".words_v2.json": 1, ".words.json": 2}
    by_stem = {}
    for p in paths:
        stem = stem_from_word_path(p)
        if not include_reference and stem in REFERENCE_STEMS_EXCLUDE:
            continue
        suffix = next(s for s in priority if p.name.endswith(s))
        prev = by_stem.get(stem)
        if prev is None or priority[suffix] < priority[prev[0]]:
            by_stem[stem] = (suffix, p)
    return {stem: path for stem, (_, path) in by_stem.items()}


#  Toutes les extensions qu'un rush peut porter apres import (`rush_NNN.<ext>`) : un
#  tournage au telephone arrive souvent en .mov, et une resolution limitee a `.mp4` ne le
#  trouverait pas.
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm")


def source_for_stem(input_dir, rush_dir, stem):
    """Le fichier source d'un radical `rush_NNN`, a plat, quelle que soit son extension.

    ⚠️ Refuse plutot que de renvoyer un chemin fantome : un `.mp4` inexistant partirait tel
    quel dans `edl_v3.json` et n'echouerait qu'a l'assemblage, sur une erreur ffmpeg qui ne
    nomme pas la cause.
    """
    noms = [stem, stem.replace("_", " ")]
    candidates = [d / f"{n}{ext}"
                  for d in (rush_dir, input_dir)
                  for n in noms
                  for ext in VIDEO_EXT]
    for c in candidates:
        if c.exists():
            return c
    raise SystemExit(
        f"Rush introuvable pour \"{stem}\".\n"
        f"  cherche dans : {rush_dir}\n"
        f"           et : {input_dir}\n"
        f"  extensions   : {', '.join(VIDEO_EXT)}\n"
        f"  Un transcript existe pour ce radical mais aucun media : soit le rush n'a pas ete\n"
        f"  ingere, soit il est dans un SOUS-DOSSIER (la resolution est a plat, par contrat)."
    )


def tokens_for_words(words):
    toks = []
    for wi, w in enumerate(words):
        for t in norm(w["text"]):
            toks.append({
                "t": t,
                "start": w["start"],
                "end": w["end"],
                "p": w.get("p"),
                "word_index": wi,
            })
    return toks


def best_windows(line_toks, toks, limit=3):
    """Les `limit` meilleures fenetres de jetons pour une ligne de script.

    ⚠️ DEFAUT CONNU, NON CORRIGE - LA DEDUP PEUT JETER LA MEILLEURE FENETRE

    `seen` deduplique sur `(round(start,2), round(end,2))` en gardant la
    PREMIERE fenetre rencontree, et les tailles sont parcourues du plus PETIT
    au plus grand. Or un mot de l'ASR rend PLUSIEURS jetons au meme
    horodatage - `norm()` decoupe les clitiques et les traits d'union :

        "qu'on"     -> `qu` + `on`     tous deux au meme start
        "lui-meme"  -> `lui` + `meme`  tous deux au meme end

    Deux fenetres de TAILLES differentes partagent donc la meme plage de
    TEMPS, et c'est la plus courte, donc la plus INCOMPLETE (ratio 0,94 au
    lieu de 1,00), qui garde la cle. S'il ne reste alors au top-3 que des
    fenetres enjambant un long silence, la ligne tombe en TROU avec un
    message qui accuse un BLANC.

    Declencheur : y penser AVANT d'accepter un trou dont le rejet parle d'un
    blanc sur une ligne qui COMMENCE et FINIT par un mot a clitique ou trait
    d'union. Le correctif (garder la fenetre de meilleur ratio par plage, et
    non la premiere) change les selections de tous les montages a venir : a
    poser avec une comparaison des EDL avant/apres, en verifiant qu'il ne
    change QUE la ligne cassee.
    """
    n = len(line_toks)
    wins = []
    seen = set()
    for size in range(max(1, n - 3), n + 5):
        for i in range(0, max(0, len(toks) - size) + 1):
            window = toks[i:i + size]
            cand = [x["t"] for x in window]
            ratio = SequenceMatcher(None, line_toks, cand).ratio()
            start, end = window[0]["start"], window[-1]["end"]
            key = (round(start, 2), round(end, 2))
            if key in seen:
                continue
            seen.add(key)
            wins.append({
                "text_ratio": round(ratio, 4),
                "start": round(start, 3),
                "end": round(end, 3),
                "dur": round(end - start, 3),
                "idx": i,
                "size": size,
            })
    wins.sort(key=lambda w: (-w["text_ratio"], w["start"]))
    return wins[:limit]


def diction_score(window):
    vals = [w.get("p") for w in window if isinstance(w.get("p"), (int, float))]
    if not vals:
        return 0.75
    return max(0.0, min(1.0, statistics.mean(vals)))


def fluidite_score(line_toks, toks, idx, size):
    window = toks[idx:idx + size]
    penalty = 0.0
    penalty += 0.15 * sum(1 for w in window if w["t"] in HESITATION_TOKENS)
    for a, b in zip(window, window[1:]):
        if a["t"] == b["t"]:
            penalty += 0.15
        gap = b["start"] - a["end"]
        if gap > PAUSE_GAP_THRESHOLD_S:
            penalty += min(0.5, gap / 3.0)
    lead = [w["t"] for w in toks[max(0, idx - 3):idx]]
    if lead and SequenceMatcher(None, line_toks[:3], lead).ratio() > 0.6:
        penalty += 0.25
    return round(max(0.0, 1.0 - penalty), 4)


def timing_quality(toks, idx, size):
    window = toks[idx:idx + size]
    if not window:
        return {
            "max_internal_gap": 0.0,
            "pause_total": 0.0,
            "silence_ratio": 0.0,
            "words_per_sec": 0.0,
        }
    dur = max(0.001, window[-1]["end"] - window[0]["start"])
    gaps = [max(0.0, b["start"] - a["end"]) for a, b in zip(window, window[1:])]
    long_gaps = [g for g in gaps if g > PAUSE_GAP_THRESHOLD_S]
    pause_total = sum(long_gaps)
    return {
        "max_internal_gap": round(max(gaps) if gaps else 0.0, 3),
        "pause_total": round(pause_total, 3),
        "silence_ratio": round(min(1.0, pause_total / dur), 4),
        "words_per_sec": round(len(window) / dur, 3),
    }


def edge_noise(line_toks, window):
    target = set(line_toks)
    leading = 0
    for w in window:
        if w["t"] in target:
            break
        leading += 1
    trailing = 0
    for w in reversed(window):
        if w["t"] in target:
            break
        trailing += 1
    return min(3, leading + trailing)


def expected_duration_limit(line):
    if line.get("start") is None or line.get("end") is None:
        return None
    expected = max(0.1, float(line["end"]) - float(line["start"]))
    return max(MAX_DUR_FLOOR_S, expected * MAX_DUR_OVER_SCRIPT)


def quality_reject_reasons(c, line):
    reasons = []
    if c.get("max_internal_gap", 0.0) > MAX_INTERNAL_GAP_S:
        reasons.append(f"blank_gap>{MAX_INTERNAL_GAP_S}s")
    if c.get("silence_ratio", 0.0) > MAX_SILENCE_RATIO:
        reasons.append(f"silence_ratio>{MAX_SILENCE_RATIO}")
    dur_limit = expected_duration_limit(line)
    if dur_limit is not None and c["dur"] > dur_limit:
        reasons.append(f"duration>{dur_limit:.2f}s_for_script_line")
    return reasons


def is_soft_text_match(c):
    return (
        c["text_ratio"] >= SOFT_TEXT_THRESHOLD
        and c["score"] >= SOFT_SCORE_THRESHOLD
        and c.get("max_internal_gap", 0.0) <= PAUSE_GAP_THRESHOLD_S
        and c.get("silence_ratio", 0.0) <= 0.10
        and not c.get("reject_reasons")
    )


def is_eligible(c):
    if c.get("reject_reasons"):
        return False
    return c["text_ratio"] >= INCLUDE_TEXT_THRESHOLD or is_soft_text_match(c)


def energie_score(toks, idx, size, dur, median_dur):
    window = toks[idx:idx + size]
    pauses = sum(
        max(0.0, window[i]["start"] - window[i - 1]["end"])
        for i in range(1, len(window))
        if window[i]["start"] - window[i - 1]["end"] > 0.35
    )
    dur_penalty = min(1.0, abs(dur - median_dur) / max(0.3, median_dur))
    pause_penalty = min(1.0, pauses / max(0.5, dur))
    return round(max(0.0, 1.0 - 0.5 * dur_penalty - 0.5 * pause_penalty), 4)


def marker_score(words, end):
    nearby = [w for w in words if end <= w["start"] <= end + 3.0]
    text = norm_join(" ".join(w["text"] for w in nearby))
    for marker in BAD_MARKERS:
        if f" {' '.join(norm(marker))} " in text:
            return 0.0, "bad:" + marker
    for marker in GOOD_MARKERS:
        if f" {' '.join(norm(marker))} " in text:
            return 1.0, "good:" + marker
    return 0.5, None


def candidate_flags(c, runner_up):
    flags = []
    if c["text_ratio"] < LOW_TEXT_THRESHOLD:
        flags.append("LOW_TEXT")
    if c["fluidite"] < 0.7:
        flags.append("HESITANT")
    if runner_up and c["score"] - runner_up["score"] < AMBIGU_GAP_THRESHOLD:
        flags.append("AMBIGU")
    if (c.get("marker_reason") or "").startswith("bad:"):
        flags.append("BAD_MARKER")
    if c["text_ratio"] < INCLUDE_TEXT_THRESHOLD:
        flags.append("VISUAL_REVIEW")
    if c["text_ratio"] < INCLUDE_TEXT_THRESHOLD and is_soft_text_match(c):
        flags.append("SOFT_TEXT_MATCH")
    if c.get("max_internal_gap", 0.0) > MAX_INTERNAL_GAP_S:
        flags.append("BLANK_RISK")
    if c.get("silence_ratio", 0.0) > MAX_SILENCE_RATIO:
        flags.append("SILENCE_RISK")
    if c.get("reject_reasons"):
        flags.append("QUALITY_REJECT")
    return flags


def candidates_for_line(line, takes, recence):
    """Tous les candidats scores d'une ligne, INDEPENDAMMENT des autres lignes.

    Separe de `score_candidates` parce que le choix a besoin de connaitre les candidats des
    lignes SUIVANTES pour ne pas leur voler leur prise (cf. `sequence_steal_cost`) : il faut
    tout calculer avant de choisir quoi que ce soit.
    """
    line_toks = norm(line["text"])
    candidates = []
    for stem, take in takes.items():
        wins = best_windows(line_toks, take["tokens"])
        for w in wins:
            window = take["tokens"][w["idx"]:w["idx"] + w["size"]]
            cand = dict(w)
            cand.update({
                "stem": stem,
                "take": take["source"].name,
                "source": str(take["source"]),
                "diction": round(diction_score(window), 4),
                "fluidite": fluidite_score(line_toks, take["tokens"], w["idx"], w["size"]),
                "recence": round(recence[stem], 4),
            })
            cand.update(timing_quality(take["tokens"], w["idx"], w["size"]))
            cand["edge_noise"] = edge_noise(line_toks, window)
            marker, marker_reason = marker_score(take["words"], cand["end"])
            cand["marker"] = marker
            cand["marker_reason"] = marker_reason
            cand["reject_reasons"] = quality_reject_reasons(cand, line)
            candidates.append(cand)

    if not candidates:
        return []

    by_line_median = statistics.median(c["dur"] for c in candidates)
    for c in candidates:
        take = takes[c["stem"]]
        c["energie"] = energie_score(take["tokens"], c["idx"], c["size"], c["dur"], by_line_median)
        c["score"] = round(
            WEIGHTS["text"] * c["text_ratio"]
            + WEIGHTS["diction"] * c["diction"]
            + WEIGHTS["fluidite"] * c["fluidite"]
            + WEIGHTS["energie"] * c["energie"]
            + WEIGHTS["recence"] * c["recence"]
            + WEIGHTS["marker"] * c["marker"],
            4,
        )
        if c.get("edge_noise"):
            c["score"] = round(max(0.0, c["score"] - EDGE_NOISE_PENALTY * c["edge_noise"]), 4)

    candidates.sort(key=lambda c: -c["score"])
    return candidates


def blocks(cand, stem, upto_end):
    """`cand` serait-il interdit si la prise `stem` etait consommee jusqu'a `upto_end` ?

    Meme predicat que la contrainte de monotonie du choix, a la tolerance pres : garder les
    deux identiques est ce qui rend le cout d'anticipation exact plutot qu'approche.
    """
    return cand["stem"] == stem and cand["start"] < upto_end - SEQUENCE_TOLERANCE


def sequence_steal_cost(cand, later):
    """Ce que choisir `cand` COUTE aux lignes suivantes, en points de score.

    ⚠️ Une selection gloutonne ligne a ligne prend le meilleur candidat du moment, puis marque
    la prise "consommee" jusqu'a la fin de ce candidat. Une ligne servie par une fenetre situee
    LOIN dans une prise interdit donc a toutes les lignes suivantes d'utiliser le debut de
    cette meme prise : pour quelques centiemes de score gagnes, plusieurs lignes suivantes
    deviennent des regressions temporelles, donc des TROUS, sans que le rapport relie la cause
    a l'effet.

    On chiffre donc la consequence au lieu de l'ignorer. Pour chaque ligne suivante, on compare
    son meilleur score AVEC et SANS la region que `cand` consommerait :

    - il lui reste une alternative equivalente  -> cout ~ 0, `cand` ne vole rien ;
    - il ne lui reste qu'un candidat plus faible -> cout = l'ecart ;
    - il ne lui reste RIEN                       -> la ligne devient un trou, cout = son
      meilleur score + HOLE_COST, parce que perdre une ligne est pire que la degrader.

    Le cout est retranche du score, donc la comparaison est homogene : ceder 0,07 sur une ligne
    pour sauver trois lignes a ~0,9 devient arithmetiquement gagnant, sans seuil arbitraire.
    """
    if not later:
        return 0.0
    total = 0.0
    for cands in later:
        elig = [c for c in cands if is_eligible(c)]
        if not elig:
            continue                      # deja perdue pour d'autres raisons, pas notre faute
        best = max(c["score"] for c in elig)
        reste = [c for c in elig if not blocks(c, cand["stem"], cand["end"])]
        if not reste:
            total += best + HOLE_COST
        else:
            total += max(0.0, best - max(c["score"] for c in reste))
    return round(total, 4)


def score_candidates(lines, takes, stems_chrono, lookahead=True):
    recence = {stem: i / max(1, len(stems_chrono) - 1) for i, stem in enumerate(stems_chrono)}
    all_candidates = []
    edl = []
    holes = []
    last_end_by_stem = {}

    # tout calculer d'abord : le choix regarde les lignes suivantes
    per_line = [(line, candidates_for_line(line, takes, recence)) for line in lines]

    for k, (line, candidates) in enumerate(per_line):
        if not candidates:
            holes.append({"i": line["i"], "text": line["text"], "reason": "aucun candidat"})
            continue

        all_candidates.append({"line": line, "candidates": candidates[:5]})

        eligible = [c for c in candidates if is_eligible(c)]
        eligible_in_sequence = [
            c for c in eligible
            if c["start"] >= last_end_by_stem.get(c["stem"], -1.0) - SEQUENCE_TOLERANCE
        ]
        if not eligible:
            best_text = max(candidates, key=lambda c: (c["text_ratio"], -len(c.get("reject_reasons", []))))
            reason = f"aucun candidat >= seuil texte {INCLUDE_TEXT_THRESHOLD} ou seuil souple securise {SOFT_TEXT_THRESHOLD}"
            if best_text.get("text_ratio", 0) >= INCLUDE_TEXT_THRESHOLD and best_text.get("reject_reasons"):
                reason = "candidats texte fiables rejetes par QA timing : " + ", ".join(best_text["reject_reasons"])
            holes.append({
                "i": line["i"],
                "text": line["text"],
                "reason": reason,
                "best_candidate": best_text,
                "suggestion": "a retourner, reformuler, ou couvrir en B-roll",
            })
            continue
        if not eligible_in_sequence:
            best_text = max(eligible, key=lambda c: c["score"])
            holes.append({
                "i": line["i"],
                "text": line["text"],
                "reason": "conflit de sequence : tous les candidats fiables reviennent en arriere dans une prise deja utilisee",
                "best_candidate": best_text,
                "suggestion": "utiliser un runner-up manuel, couvrir en B-roll, ou retourner la phrase",
            })
            continue

        greedy = sorted(eligible_in_sequence, key=lambda c: -c["score"])[0]
        if lookahead:
            later = [cs for _, cs in per_line[k + 1:] if cs]
            for c in eligible_in_sequence:
                c["steal_cost"] = sequence_steal_cost(c, later)
                c["score_net"] = round(c["score"] - c["steal_cost"], 4)
            chosen = sorted(eligible_in_sequence, key=lambda c: (-c["score_net"], -c["score"]))[0]
        else:
            chosen = greedy

        runner_ups = [c for c in candidates if c is not chosen][:3]
        flags = candidate_flags(chosen, runner_ups[0] if runner_ups else None)
        if chosen is not greedy:
            # Trace explicite : le rapport doit dire POURQUOI le meilleur score a ete ecarte,
            # sinon la selection devient une boite noire et personne ne peut la contredire.
            flags.append("LOOKAHEAD_YIELD")
            chosen["yielded_from"] = {
                "stem": greedy["stem"],
                "start": greedy["start"],
                "score": greedy["score"],
                "steal_cost": greedy.get("steal_cost"),
            }
        edl.append({
            "i": line["i"],
            "text": line["text"],
            "take": chosen["take"],
            "stem": chosen["stem"],
            "source": chosen["source"],
            "start": chosen["start"],
            "end": chosen["end"],
            "dur": chosen["dur"],
            "idx": chosen["idx"],
            "size": chosen["size"],
            "score": chosen["score"],
            "score_detail": {
                "text": chosen["text_ratio"],
                "diction": chosen["diction"],
                "fluidite": chosen["fluidite"],
                "energie": chosen["energie"],
                "recence": chosen["recence"],
                "marker": chosen["marker"],
            },
            "timing_quality": {
                "max_internal_gap": chosen.get("max_internal_gap", 0.0),
                "pause_total": chosen.get("pause_total", 0.0),
                "silence_ratio": chosen.get("silence_ratio", 0.0),
                "words_per_sec": chosen.get("words_per_sec", 0.0),
            },
            "text_ratio": chosen["text_ratio"],
            "marker_reason": chosen["marker_reason"],
            "match_mode": "soft" if chosen["text_ratio"] < INCLUDE_TEXT_THRESHOLD else "strict",
            "flags": flags,
            # L'entree d'EDL est reconstruite champ par champ, donc ce qui est pose sur le
            # candidat ne remonte PAS tout seul : il faut le recopier explicitement, sinon la
            # trace du renoncement n'existe que dans la memoire du process.
            "steal_cost": chosen.get("steal_cost"),
            "yielded_from": chosen.get("yielded_from"),
            "runner_ups": [
                {
                    "stem": r["stem"],
                    "take": r["take"],
                    "source": r["source"],
                    "start": r["start"],
                    "end": r["end"],
                    "score": r["score"],
                    "text_ratio": r["text_ratio"],
                    "max_internal_gap": r.get("max_internal_gap"),
                    "silence_ratio": r.get("silence_ratio"),
                    "edge_noise": r.get("edge_noise", 0),
                    "reject_reasons": r.get("reject_reasons", []),
                }
                for r in runner_ups
            ],
        })
        last_end_by_stem[chosen["stem"]] = max(last_end_by_stem.get(chosen["stem"], -1.0), chosen["end"])
    return edl, holes, all_candidates


def load_script(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    lines = []
    for i, item in enumerate(data):
        if isinstance(item, str):
            text = item
            idx = i
            start = None
            end = None
        else:
            text = item.get("text", "")
            idx = item.get("i", i)
            start = item.get("start")
            end = item.get("end")
        if text.strip():
            line = {"i": idx, "text": text.strip()}
            if start is not None and end is not None:
                line["start"] = float(start)
                line["end"] = float(end)
            lines.append(line)
    return lines


def build_report(edl, holes, out_path):
    lines = [
        "# Rapport de selection des prises (select_takes.py v3)",
        "",
        f"WEIGHTS = {WEIGHTS}",
        f"Lignes incluses : {len(edl)} | Trous : {len(holes)}",
        "",
        "## Trous",
        "",
    ]
    if not holes:
        lines.append("Aucun.")
    else:
        for h in sorted(holes, key=lambda x: x["i"]):
            lines.append(f"### Ligne {h['i']} - \"{h['text']}\"")
            lines.append(f"- Raison : {h['reason']}")
            if h.get("best_candidate"):
                c = h["best_candidate"]
                lines.append(f"- Meilleur candidat : {c['stem']} [{c['start']:.2f}-{c['end']:.2f}] text={c['text_ratio']:.2f} score={c['score']:.3f}")
                if c.get("reject_reasons"):
                    lines.append(f"- Rejets qualite : {', '.join(c['reject_reasons'])}")
            if h.get("suggestion"):
                lines.append(f"- Suggestion : {h['suggestion']}")
            lines.append("")

    lines += [
        "",
        "## Lignes incluses",
        "",
        "| i | score | text | take | in | out | max gap | silence | flags |",
        "|---|---:|---:|---|---:|---:|---:|---:|---|",
    ]
    for e in edl:
        tq = e.get("timing_quality", {})
        lines.append(
            f"| {e['i']} | {e['score']:.3f} | {e['text_ratio']:.2f} | {e['stem']} | "
            f"{e['start']:.2f} | {e['end']:.2f} | {tq.get('max_internal_gap', 0):.2f} | "
            f"{tq.get('silence_ratio', 0):.2f} | {', '.join(e['flags']) or '-'} |"
        )

    cedees = [e for e in edl if "LOOKAHEAD_YIELD" in e.get("flags", [])]
    if cedees:
        lines += [
            "",
            "## Renoncements par anticipation",
            "",
            "Lignes ou le MEILLEUR score a ete ecarte parce qu'il aurait consomme une region de",
            "prise dont des lignes suivantes ont besoin. La colonne \"vol\" chiffre ce que le",
            "candidat glouton aurait coute aux lignes suivantes, en points de score (un trou",
            f"compte pour son score + {HOLE_COST}). Sans cette table, la selection serait une",
            "boite noire : c'est le seul endroit qui explique pourquoi le meilleur score a perdu.",
            "",
            "| i | ecarte | son score | vol | retenu | son score | cede |",
            "|---|---|---:|---:|---|---:|---:|",
        ]
        for e in cedees:
            y = e.get("yielded_from") or {}
            perte = (y.get("score") or 0) - e.get("score", 0)
            lines.append(
                f"| {e['i']} | {y.get('stem')}@{y.get('start')} | {y.get('score')} | "
                f"{y.get('steal_cost')} | {e['stem']}@{e['start']} | {e.get('score')} | "
                f"{perte:.4f} |"
            )

    lines += ["", "## Runner-ups", ""]
    for e in edl:
        lines.append(f"### Ligne {e['i']} - \"{e['text']}\"")
        for r in e["runner_ups"]:
            extra = ""
            if r.get("reject_reasons"):
                extra = f" reject={','.join(r['reject_reasons'])}"
            lines.append(
                f"- {r['stem']} [{r['start']:.2f}-{r['end']:.2f}] "
                f"score={r['score']:.3f} text={r['text_ratio']:.2f} "
                f"gap={r.get('max_internal_gap', 0):.2f} silence={r.get('silence_ratio', 0):.2f} "
                f"edge={r.get('edge_noise', 0)}{extra}"
            )
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="Project input directory")
    parser.add_argument("--script", default=None, help="Default: <input>/final_transcript.json")
    parser.add_argument("--transcripts", default=None, help="Default: <input>/transcripts")
    parser.add_argument("--rush-dir", default=None, help="Default: <input>")
    parser.add_argument("--out-prefix", default=None, help="Default: <input>/")
    parser.add_argument("--include-reference", action="store_true")
    parser.add_argument("--no-lookahead", action="store_true",
                        help="Selection gloutonne : ne chiffre plus ce qu'une ligne coute aux suivantes. Sert de reference de comparaison.")
    args = parser.parse_args()

    input_dir = Path(args.input)
    script_path = Path(args.script) if args.script else input_dir / "final_transcript.json"
    tdir = Path(args.transcripts) if args.transcripts else input_dir / "transcripts"
    rush_dir = Path(args.rush_dir) if args.rush_dir else input_dir
    out_dir = Path(args.out_prefix) if args.out_prefix else input_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = load_script(script_path)
    word_files = discover_word_files(tdir, include_reference=args.include_reference)
    takes = {}
    for stem, path in sorted(word_files.items()):
        words = read_words(path)
        if not words:
            continue
        takes[stem] = {
            "words": words,
            "tokens": tokens_for_words(words),
            "source": source_for_stem(input_dir, rush_dir, stem),
        }

    if not takes:
        raise SystemExit(f"Aucune transcription trouvee dans {tdir}")

    edl, holes, candidates = score_candidates(lines, takes, sorted(takes),
                                              lookahead=not args.no_lookahead)
    (out_dir / "edl_v3.json").write_text(json.dumps(edl, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "holes_v3.json").write_text(json.dumps(holes, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=1), encoding="utf-8")
    build_report(edl, holes, out_dir / "rapport_selection.md")
    print(f"OK -> {out_dir / 'edl_v3.json'} ({len(edl)} lignes, {len(holes)} trous)")


if __name__ == "__main__":
    main()
