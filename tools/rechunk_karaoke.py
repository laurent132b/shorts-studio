#!/usr/bin/env python3
r"""Re-chunk un transcript word-level (<stem>.words.json) en SRT karaoke propre.

Règles de sous-titres :
  1. Ne JAMAIS couper un mot / une contraction sur deux lignes ou deux cues
     (« l'intelligence », « c'est », « d'être », « qu'on », « no-code »...).
  2. Une ligne ne se termine JAMAIS sur un article/préposition/conjonction nu
     (le, la, les, l', de, des, du, à, et, en, sur, par, pour, avec, plus...).
  3. Jamais de mot seul orphelin sur une 2e ligne : chaque cue tient sur UNE
     ligne (<= MAXLINE caractères), donc aucun retour à la ligne dans la box
     karaoke et aucun orphelin possible (les mots longs seuls restent tolérés).
  4. Les nombres restent groupés AVEC LEUR UNITE (« 20 000 », « 5 minutes »,
     « 20 % », « 15 € »), y compris quand l'unité est un SYMBOLE, qui n'est ni un
     chiffre ni une lettre.

Usage :
  python tools/rechunk_karaoke.py <stem>.words.json -o sortie.srt \
      [--corrections tools/corrections.json] [--maxline 28]

LE FILTRE DE CORRECTIONS N'EN GARDE QU'UNE PARTIE
=================================================
`--corrections` vaut `tools/corrections.json` par défaut, mais cet outil n'en garde PAS tout :
`main()` ne retient que les clés de la forme `\b<mot>\b` sans lookaround, puis apparie **par
TOKEN**. Une clé à deux mots passe le filtre et ne peut jamais apparier un token seul.

`transcribe.py` lit le MÊME fichier autrement : il compile chaque clé en regex et l'applique
au mot **avec ses deux voisins en contexte**, donc les lookarounds et les clés multi-mots y
fonctionnent. Une bonne partie des entrées n'agit donc qu'à la transcription, ce qu'il faut
savoir avant d'écrire une entrée en croyant corriger un sous-titre.

⚠️ Un mot discriminable seulement par son CONTEXTE ne peut PAS être corrigé ici : l'appariement
étant par token, il n'y a aucun contexte à interroger. Il se corrige à la transcription (où le
mot fautif naît), ou dans le `words.json` si le transcript existe déjà.

⚠️ `.replace("\\s?", "")`, dans le chargement ci-dessous, est INATTEIGNABLE : une clé qui porte
`\s?` contient un antislash, que la classe `[\w'\-\s]` du filtre ne matche pas, donc elle est
écartée avant.

MAXLINE = 28 caractères : la largeur d'une ligne de sous-titre lisible sur un short 9:16.
Au-delà, le texte ne tient plus à taille pleine sur la largeur du cadre. Doit rester égal à
`MAX_CAR` de `cues_phrases.py`.
"""
import argparse, json, re, sys
from pathlib import Path

BIND = {
    "le", "la", "les", "l'", "un", "une", "de", "des", "du", "d'", "à", "au", "aux",
    "et", "ou", "en", "ce", "que", "qu'", "sur", "par", "pour", "avec", "plus",
    "son", "sa", "ses", "ne", "se", "te", "tu", "tout", "mon", "ma", "tes", "ton",
    "on", "si", "dans", "sans", "sous",
    # relatifs / pronoms / possessifs : une cue ne doit pas finir dessus non plus
    # (« avec Dispatch qui », « plus loin en lui », « N'hésite pas à nous »,
    #  « faire un tour sur nos »)
    "qui", "dont", "où", "lui", "nous", "vous", "me", "moi", "toi", "eux",
    "notre", "nos", "votre", "vos", "leur", "leurs", "cette", "cet", "ces",
    "il", "elle", "ils", "elles", "je", "j'", "n'", "y", "aussi", "comme",
    "mais", "donc", "car", "ni", "chez", "vers", "entre", "depuis", "pendant",
}


def norm(w):
    return re.sub(r"[^\wàâäéèêëïîôöùûüç']", "", w.lower())


def build_tokens(words, corrections):
    toks = []
    for w in words:
        t = w["text"]
        # ponctuation détachée par l'ASR (« par exemple » + « : ») : on la recolle
        # au mot précédent, sinon une cue peut COMMENCER par « : ».
        if toks and (t.startswith("'") or t.startswith("-") or t in {":", ";", ",", "!", "?", "..."}):
            # typographie française : espace avant : ; ! ? - pas avant , ni …
            sep = " " if t in {":", ";", "!", "?"} else ""
            toks[-1]["text"] += sep + t
            toks[-1]["end"] = w["end"]
        else:
            toks.append({"text": t, "start": w["start"], "end": w["end"]})
    # corrections lexicales (préservent la ponctuation de fin)
    for tk in toks:
        m = re.match(r"^(.*?)([.,!?…]*)$", tk["text"])
        core, punct = m.group(1), m.group(2)
        if core.lower() in corrections:
            tk["text"] = corrections[core.lower()] + punct
    # groupe les nombres : « 20 » + « 000 » → « 20 000 » ; « 5 » + « minutes »
    #
    # ⚠️ Une unité SYMBOLIQUE (« % », « € », « ° ») compte comme une unité : ces
    # tokens ne sont ni des chiffres ni des lettres, d'où le troisième test. Sans
    # lui, quand la frontière des 28 caractères tombe entre les deux, on obtient
    # « probablement que 20 » puis une cue qui OUVRE sur « % ». Même famille que
    # le piège 3 de `tools/fuites_edl.py` : un token qui est de la ponctuation
    # pour la normalisation et un MOT dans la bouche.
    g, i = [], 0
    while i < len(toks):
        t = toks[i]
        if re.match(r"^\d+$", t["text"]) and i + 1 < len(toks):
            nx = toks[i + 1]
            if (re.match(r"^\d+[.,]?$", nx["text"]) or nx["text"][:1].isalpha()
                    or re.match(r"^[%€$£°]+[.,!?…]?$", nx["text"])):
                g.append({"text": t["text"] + " " + nx["text"], "start": t["start"], "end": nx["end"]})
                i += 2
                continue
        g.append(t)
        i += 1
    return g


def J(c):
    return " ".join(x["text"] for x in c)


def sentences(toks):
    """Découpe la liste de mots en phrases (une cue ne mélange jamais 2 phrases)."""
    out, cur = [], []
    for tk in toks:
        cur.append(tk)
        if re.search(r"[.!?…]$", tk["text"]):
            out.append(cur); cur = []
    if cur:
        out.append(cur)
    return out


def line_is_legal(line, maxline, allow_orphan, allow_bind_end):
    """Contrôle les règles dures de segmentation sur UNE cue."""
    if len(J(line)) > maxline:
        return False
    if not allow_bind_end and norm(line[-1]["text"]) in BIND:
        return False
    if not allow_orphan and len(line) == 1 and len(norm(line[0]["text"])) < 8:
        return False
    return True


def line_cost(line, soft):
    """Coût d'une cue : longueur proche de `soft`, bonus si elle finit sur virgule."""
    cost = (len(J(line)) - soft) ** 2
    if re.search(r",$", line[-1]["text"]):
        cost -= 40
    return cost


def split_sentence(sent, maxline, soft, allow_orphan=False, allow_bind_end=False):
    """Meilleur découpage d'une phrase, par programmation dynamique.

    Un glouton ne revient jamais sur une coupe déjà posée : il peut produire
    « concret pour que tu saisisses » / « mieux. » (orphelin court) alors qu'un
    autre équilibre de la MÊME phrase respecte toutes les règles
    (« Je te donne » / « un exemple concret » / « pour que tu saisisses mieux. »).
    On énumère donc tous les découpages légaux et on garde le moins coûteux.
    """
    n = len(sent)
    best = [None] * (n + 1)  # best[i] = (cost, lines) pour les i premiers mots
    best[0] = (0, [])
    for i in range(1, n + 1):
        for j in range(i):
            if best[j] is None:
                continue
            line = sent[j:i]
            if not line_is_legal(line, maxline, allow_orphan, allow_bind_end):
                continue
            cand = (best[j][0] + line_cost(line, soft), best[j][1] + [line])
            if best[i] is None or cand[0] < best[i][0]:
                best[i] = cand
    if best[n] is not None:
        return best[n][1]
    # relâchement progressif des contraintes, de la moins grave à la plus grave
    if not allow_orphan:
        return split_sentence(sent, maxline, soft, True, allow_bind_end)
    if not allow_bind_end:
        return split_sentence(sent, maxline, soft, True, True)
    return [sent]  # aucun découpage possible (mot plus long que maxline)


def chunk(toks, soft, maxline):
    cues = []
    for sent in sentences(toks):
        cues.extend(split_sentence(sent, maxline, soft))
    return cues


def split_long(c, maxline):
    if len(J(c)) <= maxline or len(c) < 2:
        return [c]
    best = None
    for k in range(1, len(c)):
        if norm(c[k - 1]["text"]) in BIND:  # la ligne 1 doit finir sur un mot plein
            continue
        a, b = len(J(c[:k])), len(J(c[k:]))
        if a > maxline:
            continue
        score = abs(a - b) + (1000 if b > maxline else 0)
        if best is None or score < best[0]:
            best = (score, k)
    if best is None:
        return [c]
    k = best[1]
    return split_long(c[:k], maxline) + split_long(c[k:], maxline)


def tc(s):
    ms = int(round(s * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); se, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{se:02d},{ms:03d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("words_json")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--corrections", default=str(Path(__file__).parent / "corrections.json"))
    # 28 caracteres : la largeur d'une ligne de sous-titre lisible sur un short 9:16
    ap.add_argument("--maxline", type=int, default=28)
    ap.add_argument("--soft", type=int, default=22)
    ap.add_argument("--minlen", type=int, default=10)
    a = ap.parse_args()

    words = json.load(open(a.words_json, encoding="utf-8"))
    corr = {}
    cp = Path(a.corrections)
    if cp.exists():
        # tool-name corrections : on ne garde que les remplacements « mot entier »
        raw = json.loads(cp.read_text(encoding="utf-8"))
        for k, v in raw.items():
            m = re.match(r"^\\b([\w'\-\s]+)\\b$", k)
            if m:
                corr[m.group(1).lower().replace("\\s?", "").replace("\\s", "")] = v

    toks = build_tokens(words, corr)
    final = chunk(toks, a.soft, a.maxline)

    out = [f"{i}\n{tc(c[0]['start'])} --> {tc(c[-1]['end'])}\n{J(c)}\n" for i, c in enumerate(final, 1)]
    Path(a.out).write_text("\n".join(out), encoding="utf-8")
    over = sum(1 for c in final if len(J(c)) > a.maxline)
    print(f"-> {len(final)} cues, {over} > {a.maxline} car. -> {a.out}")


if __name__ == "__main__":
    main()
