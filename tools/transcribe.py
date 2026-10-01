"""
tools/transcribe.py - word-level transcription.

This is the shared transcription entrypoint for:
- habillage: captions/SRT + word timings for overlay sync;
- montage-rushs: precise word timings for take selection and cut points.

Outputs:
- <stem>.words.json: a plain list of word dicts;
- <stem>.transcript.json: the same words in a normalized envelope (engine, model,
  language, segments), read first by the montage tools.

Engines (--engine):
- auto (default): ElevenLabs Scribe if an API key is found (ELEVENLABS_API_KEY in
  the environment or in .env), otherwise REFUSE with the steps to follow. It never
  falls back silently to whisper.
- elevenlabs: ElevenLabs Scribe, high-precision word timings (keeps the "euh").
- whisper: faster-whisper, local, offline and free, but less precise: it drops the
  hesitations, so tics.py barely sees them.

Diarisation (--diarize, Scribe uniquement) :
- ajoute une cle `speaker` a chaque mot, dont la valeur est le `speaker_id` rendu
  par Scribe (`speaker_0`, `speaker_1`...). Sans le drapeau la cle est ABSENTE.
- le drapeau est OPT-IN : la diarisation ne sert qu'a plusieurs voix.
- il REFUSE avec --engine whisper, qui ne separe pas les locuteurs. Ignorer la
  demande en silence rendrait un transcript sans aucune cle `speaker`, ce qui se lit
  comme "un seul locuteur" et non comme "pas mesure".

ATTENTION SCRIBE N'EST PAS DETERMINISTE : d'une passe a l'autre, A ENTREE IDENTIQUE,
quelques % des mots changent. Deux consequences :

- RE-TRANSCRIRE UN MASTER NE REND PAS LE MEME TEXTE. Un plan ou un EDL ancre sur des
  mots peut donc deriver a une re-transcription, sans que rien n'ait change en amont.
  Ne pas re-transcrire "pour verifier" : garder le transcript qui a servi au montage.
- Pour comparer deux transcriptions, comparer les TEXTES et contre ce bruit, jamais
  par POSITION : un seul mot rendu autrement (puis absorbe par le recollage des
  clitiques) decale tous les index qui suivent.

Usage:
    python tools/transcribe.py <video_or_audio> --profile karaoke
    python tools/transcribe.py <video_or_audio> --profile youtube --model medium
    python tools/transcribe.py <video_or_audio> --engine elevenlabs --language fr
    python tools/transcribe.py <itw.mp4> --engine elevenlabs --diarize   # qui parle
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import paths

# Sans ceci, `--help` peut planter : argparse imprime la docstring du module telle quelle,
# et le stdout d'un terminal Windows est en cp1252 (`UnicodeEncodeError` des qu'un
# caractere n'y est pas encodable).
#
# `errors="replace"` et non `encoding="utf-8"` : on ne change pas l'encodage du
# terminal, on rend seulement l'inencodable inoffensif.
for _flux in (sys.stdout, sys.stderr):
    if hasattr(_flux, "reconfigure"):
        _flux.reconfigure(errors="replace")

TOOLS_DIR = Path(__file__).resolve().parent
DEFAULT_LEXIQUE = TOOLS_DIR / "lexique.txt"
DEFAULT_CORRECTIONS = TOOLS_DIR / "corrections.json"

PAUSE_THRESHOLD_S = 0.35
PUNCT_END_RE = re.compile(r"[.!?;:...,]$")
SENTENCE_END_RE = re.compile(r"[.!?...]$")
COMMA_END_RE = re.compile(r",$")
CONFIDENCE_THRESHOLD = 0.5
MIN_WORD_DURATION_S = 0.05
MAX_WORD_DURATION_S = 0.9


def ffmpeg_bin():
    """Delegue a tools/paths.py - cf. son entete pour l'ordre de resolution."""
    return paths.ffmpeg()


def decode_audio_mono16k(src):
    """Decode <src> to a float32 mono 16 kHz numpy array via system ffmpeg.

    faster-whisper would normally decode the file itself, but it does so through
    PyAV, whose DLL can be blocked by a Windows application-control policy.
    Feeding it a numpy array bypasses PyAV entirely.
    """
    import numpy as np

    raw = subprocess.run(
        [
            ffmpeg_bin(), "-nostdin", "-v", "error",
            "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000",
            "-f", "s16le", "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0


def load_lexique(path):
    p = Path(path) if path else None
    if not p or not p.exists():
        return None
    terms = [line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not terms:
        return None
    return "Vocabulaire technique : " + ", ".join(terms) + "."


def load_corrections(path):
    p = Path(path) if path else None
    if not p or not p.exists():
        return []
    mapping = json.loads(p.read_text(encoding="utf-8"))
    return [(re.compile(pattern, re.IGNORECASE), repl) for pattern, repl in mapping.items()]


def apply_corrections(text, rules):
    for pattern, repl in rules:
        text = pattern.sub(repl, text)
    return text


def apply_corrections_to_words(words, rules):
    r"""Applique corrections.json au niveau mot.

    Sans ca, <stem>.words.json garde la graphie brute de l'ASR ("Cloud") alors
    que le SRT est corrige ("Claude") : select_takes.py compare le script aux
    mots bruts et prefere alors une fenetre qui EXCLUT le mot mal transcrit,
    ce qui supprime silencieusement le premier mot de la ligne montee.

    On ne remplace que si la correction reste un seul token, pour ne pas casser
    l'alignement temporel mot a mot.

    Le mot est corrige avec ses DEUX voisins en contexte, sinon une regle
    contextuelle voit un token isole, ne trouve jamais son contexte et remplace
    quand meme : <stem>.words.json divergeait alors de <stem>.segments.json.

    Les deux cotes comptent, et pas seulement le gauche :
    - lookbehind  ("(?<!la )liga" -> l'IA)          a besoin du mot precedent ;
    - lookahead   ("cloud(?= code)" -> Claude)      a besoin du mot suivant.
    Ne passer que le prefixe rendrait toute regle a lookahead inoperante ici, en
    silence : le SRT serait corrige, words.json non.

    ⚠️ CONSEQUENCE NON EVIDENTE : DEUX CORRECTIONS QUI SE TOUCHENT S'ANNULENT.

    Le contexte n'est accepte que si les voisins ressortent INTACTS (`intact`
    ci-dessous). Or si une AUTRE regle reecrit le voisin dans la meme passe, le
    test echoue, non parce que la regle du mot courant est mauvaise, mais parce
    que son voisin a bouge. On retombe alors sur le contexte GAUCHE seul, puis
    sur aucun contexte, et **toute regle a lookahead devient inoperante**, en
    silence.

    Le motif qui declenche : **un NOM PROPRE en deux tokens dont les deux sont
    mal transcrits** (une regle sur le nom, une regle a lookahead sur le prenom
    qui le precede). C'est frequent : l'ASR se trompe sur le prenom ET sur le
    nom du meme inconnu.

    Ce qu'il NE faut pas faire : retirer le lookahead "pour que ca marche". Une
    regle nue sur un prenom courant s'applique a TOUTES les transcriptions, et
    renommer quelqu'un d'autre est un vrai defaut. Le geste juste est une
    correction propre a la video, posee dans son words.json apres la
    transcription et avant `select_takes`.
    """
    for i, w in enumerate(words):
        prefix = (words[i - 1]["text"] + " ") if i else ""
        suffix = (" " + words[i + 1]["text"]) if i + 1 < len(words) else ""
        joined = apply_corrections(prefix + w["text"] + suffix, rules)
        intact = (not prefix or joined.startswith(prefix)) and (
            not suffix or joined.endswith(suffix)
        )
        if intact:
            fixed = joined[len(prefix):len(joined) - len(suffix)]
        else:
            # une regle a aussi touche un voisin : on retombe sur le contexte
            # gauche seul, puis sur aucun contexte.
            joined = apply_corrections(prefix + w["text"], rules)
            if not prefix or joined.startswith(prefix):
                fixed = joined[len(prefix):]
            else:
                fixed = apply_corrections(w["text"], rules)
        if fixed != w["text"] and not re.search(r"\s", fixed.strip()):
            w["text"] = fixed
    return words


CLITIC_LEADS = ("'", "’", "-")


def merge_clitic_fragments(words):
    """Recolle sur le mot precedent les tokens qui commencent par une apostrophe ou un
    trait d'union ("qu" + "'on", "Dis" + "-moi", "est" + "-ce").

    L'ASR detache ces clitiques. Au niveau du TEXTE le defaut est invisible, parce que la
    regle `\\s+'` -> `'` de corrections.json les recolle dans le SRT. Il ne se voit que quand
    le decoupage karaoke tombe PILE entre les deux : on lit alors "le modele qu" puis
    "'on sort".

    Corriger le decoupage ne reglerait qu'un cas ; fusionner au niveau MOT supprime
    la classe entiere, puisque aucun decoupage ne peut plus separer ce qui est un seul token.

    Applique des la sortie de la transcription, donc AVANT `build_blocks` et avant
    `apply_corrections_to_words` :

    - avant le decoupage, sinon le SRT peut encore couper entre les deux moities, ce qui est
      precisement le defaut a supprimer ;
    - avant les corrections, pour que leurs regles voient des tokens entiers.

    Les deux fichiers ecrits en heritent alors par construction. Ca compte, parce que
    `select_takes.py` et `assemble.py` lisent le `.transcript.json` EN PRIORITE et ne
    retombent sur `.words.json` qu'a defaut : une correction appliquee au seul `words.json`
    serait silencieusement ignoree.

    Renvoie aussi la table `ancien index -> nouvel index`, parce que `segment_end_indices`
    designe des mots PAR POSITION : sans remappage, les fins de segment casseraient le SRT
    a cote de leur phrase.
    """
    out = []
    index_map = {}
    merged = 0
    for i, w in enumerate(words):
        text = w.get("text") or ""
        if out and len(text) > 1 and text[0] in CLITIC_LEADS:
            prev = out[-1]
            prev["text"] = prev["text"] + text
            prev["end"] = max(prev["end"], w["end"])
            ps = [x for x in (prev.get("p"), w.get("p")) if isinstance(x, (int, float))]
            prev["p"] = min(ps) if ps else prev.get("p")
            index_map[i] = len(out) - 1     # le fragment devient le mot qui l'absorbe
            merged += 1
            continue
        index_map[i] = len(out)
        out.append(dict(w))
    for i, w in enumerate(out):
        if "id" in w:
            w["id"] = f"w{i}"       # l'id est positionnel, il ne survit pas a une fusion
    return out, merged, index_map


def format_timecode(seconds):
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    h, rem_ms = divmod(total_ms, 3_600_000)
    m, rem_ms = divmod(rem_ms, 60_000)
    s, ms = divmod(rem_ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def chunk_words(words, profile, max_words=None, max_dur=None, max_chars=None, segment_ends=None):
    """Group word-level timestamps into SRT blocks.

    The function is imported by montage assembly scripts, so keep its signature
    stable. `words` may contain p=None when an engine has no word confidence.
    """
    segment_ends = segment_ends or set()
    sentence_re = PUNCT_END_RE if profile == "karaoke" else SENTENCE_END_RE

    blocks = []
    current = []
    for idx, w in enumerate(words):
        if current:
            gap = w["start"] - current[-1]["end"]
            prev_ends_sentence = bool(sentence_re.search(current[-1]["text"]))
            prev_ends_segment = profile == "youtube" and (idx - 1) in segment_ends
            would_be_dur = w["end"] - current[0]["start"]
            would_be_text = " ".join(x["text"] for x in current) + " " + w["text"]

            hard_break = (
                prev_ends_sentence
                or prev_ends_segment
                or gap > PAUSE_THRESHOLD_S
                or (max_words and len(current) >= max_words)
            )
            soft_overflow = (
                (max_dur and would_be_dur > max_dur)
                or (max_chars and len(would_be_text) > max_chars)
            )

            if hard_break:
                blocks.append(current)
                current = []
            elif soft_overflow:
                split_at = None
                if profile == "youtube":
                    for j in range(len(current) - 1, 0, -1):
                        if COMMA_END_RE.search(current[j]["text"]):
                            split_at = j + 1
                            break
                if split_at:
                    blocks.append(current[:split_at])
                    current = current[split_at:]
                else:
                    blocks.append(current)
                    current = []
        current.append(w)
    if current:
        blocks.append(current)
    return blocks


def build_srt(blocks, rules):
    lines = []
    for i, block in enumerate(blocks, start=1):
        start = block[0]["start"]
        end = block[-1]["end"]
        text = apply_corrections(" ".join(w["text"] for w in block), rules)
        lines.append(f"{i}\n{format_timecode(start)} --> {format_timecode(end)}\n{text}\n")
    return "\n".join(lines) + "\n"


def build_review(blocks, stem, profile):
    lines = [
        f"# Relecture - {stem} (profil {profile})",
        "",
        f"## Mots a verifier (p < {CONFIDENCE_THRESHOLD:.1f})",
        "",
    ]
    flagged = []
    unknown_confidence = 0
    total = 0
    for i, block in enumerate(blocks, start=1):
        block_text = " ".join(w["text"] for w in block)
        for w in block:
            total += 1
            p = w.get("p")
            if p is None:
                unknown_confidence += 1
            elif p < CONFIDENCE_THRESHOLD:
                flagged.append((i, w, block_text))

    mesures = total - unknown_confidence

    # Trois regimes. "Aucun mot sous le seuil" sur une assiette VIDE (un moteur qui ne rend
    # aucune probabilite, comme Scribe) serait un zero plausible : la relecture se lirait
    # comme un feu vert alors qu'elle n'a rien mesure. Ne pas confondre "je n'ai pas pu
    # mesurer" avec "c'est bon".
    if not flagged and not mesures:
        lines += [
            "**INDETERMINE - cette relecture n'a rien pu mesurer.**",
            "",
            f"Le moteur n'a fourni AUCUNE probabilite : 0 mot mesure sur {total}. "
            "Ce n'est donc pas un feu vert, c'est une absence de mesure.",
            "",
            "Le filet de remplacement, quand le moteur ne donne pas de probabilites : "
            "relire le VOCABULAIRE que la reconnaissance abime le plus - noms propres, "
            "noms de produits, identifiants de code - en les confrontant a ce que "
            "l'IMAGE affiche (une frame du master aux instants ou le terme est "
            "prononce). Une chaine visible a l'ecran est une "
            "verite plus forte qu'une probabilite.",
        ]
    elif not flagged:
        lines.append(
            f"Aucun mot sous le seuil de confiance, sur {mesures} mot(s) mesure(s)."
        )
    else:
        for i, w, block_text in flagged:
            tc = format_timecode(w["start"])
            lines.append(f"- L{i} [{tc}] \"{w['text']}\" (p={w['p']:.2f}) - contexte : \"{block_text}\"")

    if unknown_confidence:
        lines += [
            "",
            f"Note : {unknown_confidence} mot(s) n'ont pas de probabilite fournie par le moteur.",
        ]

    lines += [
        "",
        "## Corrections appliquees",
        "",
        "_A completer pendant la relecture : mot original -> correction. "
        "Si une correction revient sur >=2 videos, l'ajouter a `tools/corrections.json`._",
        "",
    ]
    return "\n".join(lines)


def normalize_stem(src):
    return src.stem.replace(" ", "_")


def sane_word_end(word, next_word=None):
    """Clamp ASR word ends that accidentally absorb long trailing silence.

    ElevenLabs Scribe can occasionally keep a word open until the next speech
    chunk ("pas." spanning several seconds). That is fine for reading, but bad
    for cut points. Keep starts intact and cap improbable word durations.
    """
    start = float(word["start"])
    end = float(word["end"])
    text_len = max(1, len(re.sub(r"[^A-Za-z0-9]", "", word.get("text", ""))))
    natural_cap = min(MAX_WORD_DURATION_S, 0.28 + text_len * 0.035)
    capped = min(end, start + natural_cap)
    if next_word is not None:
        next_start = float(next_word["start"])
        if next_start > start:
            capped = min(capped, max(start + MIN_WORD_DURATION_S, next_start - 0.03))
    return round(max(start + MIN_WORD_DURATION_S, capped), 3)


def sanitize_word_timings(words):
    clean = []
    for i, w in enumerate(words):
        item = dict(w)
        item["start"] = round(float(item["start"]), 3)
        item["end"] = sane_word_end(item, words[i + 1] if i + 1 < len(words) else None)
        clean.append(item)
    return clean


def build_segments_from_words(words, rules, max_pause=0.8):
    if not words:
        return []
    segments = []
    current = [words[0]]
    for w in words[1:]:
        gap = w["start"] - current[-1]["end"]
        ends_sentence = bool(SENTENCE_END_RE.search(current[-1]["text"]))
        if gap > max_pause or ends_sentence:
            text = apply_corrections(" ".join(x["text"] for x in current), rules)
            segments.append({"start": current[0]["start"], "end": current[-1]["end"], "text": text})
            current = []
        current.append(w)
    if current:
        text = apply_corrections(" ".join(x["text"] for x in current), rules)
        segments.append({"start": current[0]["start"], "end": current[-1]["end"], "text": text})
    return segments


def transcribe_whisper(src, args, initial_prompt, rules):
    from faster_whisper import WhisperModel

    print(f"Loading faster-whisper model '{args.model}' (int8 cpu)...", flush=True)
    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    segments, info = model.transcribe(
        decode_audio_mono16k(src),
        language=args.language,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400),
        initial_prompt=initial_prompt,
    )

    words = []
    seg_lines = []
    segment_end_indices = set()
    wi = 0
    for seg in segments:
        seg_text = apply_corrections(seg.text.strip(), rules)
        seg_lines.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg_text})
        if seg.words:
            for w in seg.words:
                txt = w.word.strip()
                if not txt:
                    continue
                words.append({
                    "id": f"w{wi}",
                    "text": txt,
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "p": round(w.probability, 3),
                })
                wi += 1
            segment_end_indices.add(wi - 1)
        # les DEUX bornes, et pas la seule fin : un horodatage unique `[  13.34s]` se lit
        # comme un DEBUT de segment, et ancrer quelque chose dessus le decalerait de toute
        # la duree de la phrase.
        print(f"  [{seg.start:7.2f}-{seg.end:7.2f}s] {seg_text}", flush=True)

    language = getattr(info, "language", args.language)
    words = sanitize_word_timings(words)
    return words, seg_lines, segment_end_indices, language, args.model


def find_elevenlabs_key(env_name="ELEVENLABS_API_KEY"):
    """La cle ElevenLabs (variable d'environnement, puis `.env` a la racine), ou None."""
    if os.environ.get(env_name):
        return os.environ[env_name].strip()
    for p in [TOOLS_DIR.parent / ".env", Path.cwd() / ".env"]:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(env_name + "="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return None


def load_elevenlabs_key(env_name):
    key = find_elevenlabs_key(env_name)
    if not key:
        sys.exit(f"{env_name} introuvable (variable d'environnement ou .env a la racine). "
                 "Voir le skill /demarrer.")
    return key


def multipart_field(boundary, name, value):
    return (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
        f"{value}\r\n"
    ).encode("utf-8")


def transcribe_elevenlabs(src, args, rules):
    key = load_elevenlabs_key(args.elevenlabs_key_env)
    boundary = "----ScribeBoundary"

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        subprocess.run(
            [
                ffmpeg_bin(),
                "-y",
                "-i",
                str(src),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(wav),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        body = b""
        body += multipart_field(boundary, "model_id", args.elevenlabs_model)
        body += multipart_field(boundary, "timestamps_granularity", "word")
        if getattr(args, "diarize", False):
            body += multipart_field(boundary, "diarize", "true")
        if args.language:
            body += multipart_field(boundary, "language_code", args.language)
        body += (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
            "Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
        body += wav.read_bytes()
        body += f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/speech-to-text",
        data=body,
        headers={
            "xi-api-key": key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=600).read())
    except urllib.error.HTTPError as e:
        sys.exit(f"ElevenLabs Scribe error {e.code}: {e.read().decode('utf-8', errors='ignore')[:500]}")

    words = []
    for i, w in enumerate(resp.get("words", [])):
        if w.get("type", "word") != "word":
            continue
        txt = (w.get("text") or "").strip()
        if not txt or "start" not in w or "end" not in w:
            continue
        mot = {
            "id": f"w{len(words)}",
            "text": txt,
            "start": round(float(w["start"]), 3),
            "end": round(float(w["end"]), 3),
            "p": None,
        }
        # La cle n'est posee QUE si la diarisation est demandee : sans le drapeau, la forme
        # des mots ne change pas.
        #
        # Les trois passes qui suivent (`sanitize_word_timings`, `merge_clitic_fragments`,
        # `apply_corrections_to_words`) recopient chaque mot par `dict(w)` : la cle traverse
        # la chaine sans rien cabler ailleurs.
        if getattr(args, "diarize", False):
            mot["speaker"] = w.get("speaker_id")
        words.append(mot)

    if getattr(args, "diarize", False):
        # L'ASSIETTE avant le verdict : combien de mots portent un locuteur, et
        # lesquels. Une reponse sans diarisation rend une liste vide, pas une erreur.
        vus = [m["speaker"] for m in words if m.get("speaker")]
        uniques = sorted(set(vus))
        print("  diarisation : %d locuteur(s) %s, %d/%d mots etiquetes"
              % (len(uniques), uniques, len(vus), len(words)), flush=True)
        if not uniques:
            # Sur stderr, qu'un `| grep` ne capture pas : un zero plausible se croit.
            sys.stderr.write(
                "  [!!] --diarize demande mais AUCUN speaker_id dans la reponse Scribe.\n"
                "       Verifier que le modele le supporte (scribe_v1 / scribe_v2).\n")

    words = sanitize_word_timings(words)
    seg_lines = build_segments_from_words(words, rules)
    segment_end_indices = set()
    wi = 0
    for seg in seg_lines:
        while wi < len(words) and words[wi]["end"] <= seg["end"] + 0.001:
            wi += 1
        if wi:
            segment_end_indices.add(wi - 1)
        # idem cote elevenlabs : les deux bornes, cf. le commentaire du moteur whisper.
        print(f"  [{seg['start']:7.2f}-{seg['end']:7.2f}s] {seg['text']}", flush=True)

    return words, seg_lines, segment_end_indices, resp.get("language_code", args.language), args.elevenlabs_model


def build_blocks(words, profile, args, segment_end_indices):
    if profile == "karaoke":
        max_words = args.max_words if args.max_words is not None else 6
        max_dur = args.max_dur if args.max_dur is not None else 2.5
        return chunk_words(words, "karaoke", max_words=max_words, max_dur=max_dur)

    max_dur = args.max_dur if args.max_dur is not None else 7.0
    max_chars = args.max_lines * args.chars_per_line
    return chunk_words(
        words,
        "youtube",
        max_dur=max_dur,
        max_chars=max_chars,
        segment_ends=segment_end_indices,
    )


def write_outputs(out_dir, stem, src, args, engine_model, language, words, seg_lines, blocks, rules, elapsed):
    words = apply_corrections_to_words(words, rules)
    (out_dir / f"{stem}.words.json").write_text(
        json.dumps(words, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    (out_dir / f"{stem}.segments.json").write_text(
        json.dumps(seg_lines, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    transcript = {
        "engine": args.engine,
        "model": engine_model,
        "language": language,
        "source": str(src),
        "profile": args.profile,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_sec": round(elapsed, 2),
        "words": words,
        "segments": seg_lines,
    }
    (out_dir / f"{stem}.transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    (out_dir / f"{stem}.{args.profile}.srt").write_text(build_srt(blocks, rules), encoding="utf-8")
    (out_dir / f"{stem}.{args.profile}.review.md").write_text(
        build_review(blocks, stem, args.profile),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="Chemin vers le master video/audio")
    parser.add_argument("--engine", choices=["auto", "whisper", "elevenlabs"], default="auto",
                        help="auto (defaut) : ElevenLabs Scribe si une cle existe, sinon REFUS "
                             "avec la marche a suivre. whisper : local, gratuit, moins bon.")
    parser.add_argument("--profile", choices=["karaoke", "youtube"], default="karaoke")
    parser.add_argument("--language", default="fr", help="Code langue (fr par defaut)")
    parser.add_argument("--model", default="small", choices=["small", "medium"], help="Modele faster-whisper")
    parser.add_argument("--elevenlabs-model", default="scribe_v1")
    parser.add_argument("--elevenlabs-key-env", default="ELEVENLABS_API_KEY")
    parser.add_argument("--diarize", action="store_true",
                        help="Scribe uniquement : ajoute une cle `speaker` par mot. "
                             "Opt-in, pour ne rien changer aux appels existants.")
    parser.add_argument("--out", default=None, help="Dossier de sortie (defaut: <dossier_source>/transcripts)")
    parser.add_argument("--max-words", type=int, default=None, help="karaoke uniquement (defaut 6)")
    parser.add_argument("--max-dur", type=float, default=None, help="defaut : 2.5s (karaoke) / 7.0s (youtube)")
    parser.add_argument("--max-lines", type=int, default=2, help="youtube uniquement (defaut 2)")
    parser.add_argument("--chars-per-line", type=int, default=42, help="youtube uniquement (defaut 42)")
    parser.add_argument("--lexique", default=str(DEFAULT_LEXIQUE))
    parser.add_argument("--corrections", default=str(DEFAULT_CORRECTIONS))
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"Fichier introuvable : {src}")

    out_dir = Path(args.out) if args.out else src.parent / "transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = normalize_stem(src)

    initial_prompt = load_lexique(args.lexique)
    rules = load_corrections(args.corrections)

    # `auto` ne bascule JAMAIS en silence vers Whisper : le local est moins bon (il efface les
    # « euh », donc tics.py ne les voit plus), et ce choix doit etre conscient.
    if args.engine == "auto":
        if find_elevenlabs_key(args.elevenlabs_key_env):
            args.engine = "elevenlabs"
        else:
            sys.exit(
                "Pas de cle ElevenLabs (ELEVENLABS_API_KEY dans .env).\n"
                "  - recommande : creer un compte ElevenLabs (le plan gratuit suffit pour des\n"
                "    shorts), coller la cle dans .env, relancer. Voir le skill /demarrer.\n"
                "  - sinon, en local et gratuit, mais MOINS BON (Whisper efface les 'euh', donc\n"
                "    les tics ne seront presque pas detectes) : relancer avec --engine whisper")

    # Refus plutot qu'ignorer en silence, cf. la docstring du module.
    if getattr(args, "diarize", False) and args.engine != "elevenlabs":
        sys.exit("--diarize n'existe que pour --engine elevenlabs : faster-whisper "
                 "ne separe pas les locuteurs.")

    t0 = time.time()
    print(f"\n=== Transcribing {src.name} (engine={args.engine}, profile={args.profile}) ===", flush=True)
    if args.engine == "whisper":
        words, seg_lines, segment_end_indices, language, engine_model = transcribe_whisper(
            src,
            args,
            initial_prompt,
            rules,
        )
    else:
        words, seg_lines, segment_end_indices, language, engine_model = transcribe_elevenlabs(src, args, rules)

    words, n_merged, index_map = merge_clitic_fragments(words)
    if n_merged:
        segment_end_indices = {index_map[i] for i in segment_end_indices if i in index_map}
        print(f"[transcribe] {n_merged} fragment(s) clitique(s) recolle(s) au niveau mot "
              "(apostrophes, traits d'union)")

    blocks = build_blocks(words, args.profile, args, segment_end_indices)
    elapsed = time.time() - t0
    write_outputs(out_dir, stem, src, args, engine_model, language, words, seg_lines, blocks, rules, elapsed)

    print(
        f"\n-> {len(words)} mots, {len(seg_lines)} segments, {len(blocks)} blocs SRT, {elapsed:.1f}s elapsed",
        flush=True,
    )
    print(f"-> Sortie : {out_dir}", flush=True)


if __name__ == "__main__":
    main()
