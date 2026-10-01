"""habillage_standard.py -- la composition HyperFrames STANDARD d'un short : la video plein
cadre, des sous-titres karaoke et un hook d'ouverture.

    python tools/habillage_standard.py <slug> --hook "L'IA, simplement" --kicker "FORMATION"
    python tools/habillage_standard.py <slug> --hook "..." --hook-duree 3
    python tools/habillage_standard.py <slug> --hook "..." --bande-haut 1200   # forcer la bande

Sortie : 0 composition ecrite - 2 refus (entrees absentes : lancer vers_hyperframes.py).

Il lit ce que `vers_hyperframes.py` a prepare dans `videos/<slug>/2_habillage/`
(`transcript.json`, `metadata.json`, `public/input-video.mp4`) et ecrit :

    public/index.html     la composition, prete pour `npx hyperframes check` puis `render`
    public/fonts/         la police (Inter), copiee depuis le skill talking-head-recut
    public/vendor/        gsap.min.js, idem
    storyboard.json       le plan (une carte : le hook), au format du skill talking-head-recut

Les regles des sous-titres
--------------------------
- un groupe = au plus 2 lignes de 28 caracteres, coupe en fin de phrase, sur une pause de
  plus de 0,25 s, ou sur une virgule une fois 12 caracteres atteints ; un groupe de moins de
  16 caracteres se recolle a son voisin quand ca tient (pas d'orphelin d'un mot) ;
- le mot prononce passe en couleur d'accent, et le reste jusqu'au mot suivant ;
- la ponctuation isolee par la transcription (« ? » seul) est recollee au mot precedent. On
  n'INVENTE pas de ponctuation : un point manquant se corrige dans `transcript.json` ;
- tous les instants sont ramenes a l'image pres (30 i/s), sinon un mot s'allume entre deux
  images et le rendu le saute.

Ou vont les sous-titres
-----------------------
Dans une bande de 320 px placee SOUS le menton et AU-DESSUS des 20 % du bas du cadre (ce que
masquent les boutons de TikTok, Reels et Shorts : y > 1536 sur 1920). Le menton est MESURE sur
le master avec YuNet (`tools/visage.py`), au pire cas de 8 images ; sans modele de visage, la
bande commence a 1180 px et l'outil le dit. Si le menton descend trop bas pour que la bande
tienne au-dessus de 1536, l'outil le signale : ne pas cacher la bouche, regarder une image.

Le style
--------
Neutre par defaut : Inter gras, blanc sur fond noir translucide, accent jaune. Pour un autre
style, ecrire `videos/<slug>/2_habillage/style.json` (ou le recopier d'une video precedente) :

    {"accent": "#ffd23f", "texte": "#ffffff", "fond": "rgba(0,0,0,0.62)", "taille_px": 60}

La composition produite est un POINT DE DEPART : on peut y ajouter des cartes en suivant les
skills HyperFrames (une carte = un `.card-host.clip` avec son propre `id`, on anime ses
ENFANTS, jamais le `.clip` lui-meme).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

for _flux in (sys.stdout, sys.stderr):
    if hasattr(_flux, "reconfigure"):
        _flux.reconfigure(errors="replace")

ROOT = paths.ROOT
ASSETS = ROOT / ".claude" / "skills" / "talking-head-recut" / "assets"
FPS = 30
W, H = 1080, 1920
MAX_LIGNE = 28
PAUSE = 0.25
QUEUE = 0.3
BANDE_H = 320
BAS_SUR = int(H * 0.80)          # 1536 : au-dela, l'interface des plateformes masque
BANDE_DEFAUT = 1180
MARGE_MENTON = 40
STYLE_DEFAUT = {"accent": "#ffd23f", "texte": "#ffffff", "fond": "rgba(0,0,0,0.62)",
                "taille_px": 60}
PONCT_SEULE = {"?", "!", ":", ";", ".", ",", "...", "…"}


def q(t: float) -> float:
    return round(round(t * FPS) / FPS, 4)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def texte(ws: list[dict]) -> str:
    return " ".join(x["text"] for x in ws)


def couper_lignes(ws: list[dict]) -> list[list[dict]] | None:
    """Une ou deux lignes de MAX_LIGNE au plus, la coupe la plus equilibree ; None si impossible."""
    if len(texte(ws)) <= MAX_LIGNE:
        return [ws]
    best = None
    for k in range(1, len(ws)):
        a, b = texte(ws[:k]), texte(ws[k:])
        if len(a) <= MAX_LIGNE and len(b) <= MAX_LIGNE:
            score = abs(len(a) - len(b))
            if best is None or score < best[0]:
                best = (score, k)
    return None if best is None else [ws[:best[1]], ws[best[1]:]]


def fin_phrase(g: list[dict]) -> bool:
    t = g[-1]["text"]
    return t[-1:] in ".?!…"


def grouper(mots: list[dict]) -> list[list[dict]]:
    groupes, cur = [], []
    for i, w in enumerate(mots):
        essai = cur + [w]
        if cur and couper_lignes(essai) is None:
            groupes.append(cur)
            cur = [w]
        else:
            cur = essai
        nxt = mots[i + 1] if i + 1 < len(mots) else None
        trou = (nxt["start"] - w["end"]) if nxt else 0
        if nxt is None or w["text"][-1:] in ".?!…" or trou >= PAUSE \
                or (w["text"][-1:] in ",;:" and len(texte(cur)) >= 12):
            groupes.append(cur)
            cur = []
    if cur:
        groupes.append(cur)
    change = True
    while change:
        change = False
        for gi, g in enumerate(groupes):
            if len(texte(g)) >= 16:
                continue
            cands = []
            if gi > 0 and not fin_phrase(groupes[gi - 1]):
                cands.append((gi - 1, groupes[gi - 1] + g))
            if gi + 1 < len(groupes) and not fin_phrase(g):
                cands.append((gi, g + groupes[gi + 1]))
            for at, fusion in cands:
                if couper_lignes(fusion) is not None:
                    groupes[at:at + 2] = [fusion]
                    change = True
                    break
            if change:
                break
    return groupes


def nettoyer(mots: list[dict], duree: float) -> list[dict]:
    out: list[dict] = []
    for w in mots:
        t = str(w["text"]).strip()
        if not t:
            continue
        if out and t in PONCT_SEULE:
            out[-1]["text"] += ("" if t in {".", ",", "...", "…"} else " ") + t
            out[-1]["end"] = max(out[-1]["end"], float(w["end"]))
            continue
        out.append({"text": t, "start": float(w["start"]), "end": min(float(w["end"]), duree)})
    return out


def menton_max(video: Path, duree: float) -> tuple[int | None, int | None, str]:
    """(menton le plus bas, front le plus haut) en px, sur 8 images du master. None si pas mesurable."""
    try:
        import cv2
        import visage
    except ImportError:
        return None, None, "opencv absent"
    try:  # un avertissement « Targets are not supported » sans consequence, a chaque image
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except AttributeError:
        pass
    modele = visage.modele_yunet()
    if not modele.is_file():
        return None, None, "modele YuNet absent (python tools/visage.py telecharger)"
    tmp = video.parent.parent / "_mesure_visage"
    tmp.mkdir(exist_ok=True)
    mentons, fronts = [], []
    try:
        for k in range(8):
            t = duree * (k + 0.5) / 8
            png = tmp / f"f{k}.png"
            subprocess.run([paths.ffmpeg(), "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video),
                            "-frames:v", "1", str(png)], capture_output=True)
            img = cv2.imread(str(png)) if png.is_file() else None
            if img is None:
                continue
            h, w = img.shape[:2]
            det = cv2.FaceDetectorYN.create(str(modele), "", (w, h), 0.6, 0.3, 5000)
            det.setInputSize((w, h))
            _, faces = det.detect(img)
            if faces is None or not len(faces):
                continue
            f = max(faces, key=lambda r: r[14])
            fronts.append(int(f[1] * H / h))
            mentons.append(int((f[1] + f[3]) * H / h))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not mentons:
        return None, None, "aucun visage detecte"
    return max(mentons), min(fronts), f"{len(mentons)} image(s) mesuree(s)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug")
    ap.add_argument("--hook", required=True, help="titre d'accroche, 3 a 7 mots")
    ap.add_argument("--kicker", default="", help="petit sur-titre en capitales (optionnel)")
    ap.add_argument("--hook-duree", type=float, default=2.8, help="secondes (defaut 2.8)")
    ap.add_argument("--bande-haut", type=int, default=None,
                    help="haut de la bande des sous-titres en px (defaut : mesure sous le menton)")
    a = ap.parse_args()

    try:
        vd = paths.video_dir(a.slug)
    except paths.UnknownVideo as e:
        print(f"[REFUS] {e}", file=sys.stderr)
        return 2
    hab = vd / "2_habillage"
    pub = hab / "public"
    manquants = [p.name for p in (hab / "transcript.json", hab / "metadata.json",
                                   pub / "input-video.mp4") if not p.is_file()]
    if manquants:
        print(f"[REFUS] entrees absentes ({', '.join(manquants)}) : lancer d'abord\n"
              f"  python tools/vers_hyperframes.py {a.slug}", file=sys.stderr)
        return 2
    if not (ASSETS / "vendor" / "gsap.min.js").is_file():
        print(f"[REFUS] assets du skill talking-head-recut introuvables : {ASSETS}", file=sys.stderr)
        return 2

    meta = json.loads((hab / "metadata.json").read_text(encoding="utf-8"))
    duree = round(float((meta.get("format") or {}).get("duration") or 0), 3)
    style = dict(STYLE_DEFAUT)
    if (hab / "style.json").is_file():
        style.update(json.loads((hab / "style.json").read_text(encoding="utf-8")))

    mots = nettoyer(json.loads((hab / "transcript.json").read_text(encoding="utf-8")), duree)
    groupes = grouper(mots)

    menton, front, prov = menton_max(pub / "input-video.mp4", duree)
    if a.bande_haut is not None:
        haut, origine = a.bande_haut, "imposee"
    elif menton is None:
        haut, origine = BANDE_DEFAUT, f"par defaut ({prov})"
    else:
        haut, origine = max(BANDE_DEFAUT - 100, menton + MARGE_MENTON), f"sous le menton ({prov})"
    alertes = []
    if haut + BANDE_H > BAS_SUR:
        haut = BAS_SUR - BANDE_H
        alertes.append(f"le menton descend jusqu'a {menton} px : la bande ne tient pas dessous sans "
                       "passer sous l'interface des plateformes. Bande remontee a "
                       f"{haut} px : REGARDER une image, elle peut toucher la bouche.")
    if front is not None and front < 500:
        alertes.append(f"le haut du visage monte a {front} px : le hook (170-490 px) peut le toucher.")

    (pub / "fonts").mkdir(parents=True, exist_ok=True)
    (pub / "vendor").mkdir(parents=True, exist_ok=True)
    for f in ("Inter-400-latin.woff2", "Inter-700-latin.woff2"):
        shutil.copyfile(ASSETS / "fonts" / f, pub / "fonts" / f)
    shutil.copyfile(ASSETS / "vendor" / "gsap.min.js", pub / "vendor" / "gsap.min.js")

    hook_fin = min(a.hook_duree, duree)
    caps, js = [], []
    for gi, g in enumerate(groupes):
        lignes = couper_lignes(g) or [g]
        nxt = groupes[gi + 1] if gi + 1 < len(groupes) else None
        g0 = q(g[0]["start"])
        g1 = q(min(nxt[0]["start"], g[-1]["end"] + QUEUE) if nxt else min(duree, g[-1]["end"] + QUEUE))
        if g1 <= g0:
            g1 = q(g0 + 1 / FPS)
        parts, wi = [], 0
        for ligne in lignes:
            spans = []
            for w in ligne:
                spans.append(f'<span class="w" id="w-{gi}-{wi}">{esc(w["text"])}</span>')
                wi += 1
            parts.append(f'<div class="line">{" ".join(spans)}</div>')
        caps.append(f'<div class="cap" id="cap-{gi}"><div class="pill">{"".join(parts)}</div></div>')
        js.append(f'tl.set("#cap-{gi}",{{opacity:1}},{g0});tl.set("#cap-{gi}",{{opacity:0}},{g1});')
        for k, w in enumerate(g):
            ws = q(max(g0, w["start"]))
            we = q(max(w["end"], g[k + 1]["start"])) if k + 1 < len(g) else g1
            if we <= ws:
                we = q(ws + 1 / FPS)
            js.append(f'tl.set("#w-{gi}-{k}",{{color:"var(--accent)"}},{ws});'
                      f'tl.set("#w-{gi}-{k}",{{color:"var(--texte)"}},{we});')

    kicker = f'<div class="kicker">{esc(a.kicker)}</div>' if a.kicker else ""
    html = GABARIT
    for cle, val in {
        "__DUR__": str(duree), "__HD__": str(round(hook_fin, 4)), "__HOUT__": str(q(max(0, hook_fin - 0.3))),
        "__KICKER__": kicker, "__TITLE__": esc(a.hook), "__HAUT__": str(haut), "__BH__": str(BANDE_H),
        "__ACCENT__": style["accent"], "__TEXTE__": style["texte"], "__FOND__": style["fond"],
        "__TAILLE__": str(int(style["taille_px"])),
        "__CAPS__": "\n".join("        " + c for c in caps),
        "__JS__": "\n".join("          " + j for j in js),
    }.items():
        html = html.replace(cle, val)
    octets = html.encode("utf-8")
    (pub / "index.html").write_bytes(octets)

    storyboard = {
        "schemaVersion": 3,
        "composition": {"fps": FPS, "width": W, "height": H, "durationSeconds": duree,
                        "layout": "overlay", "themeId": "neutre", "seed": 1},
        "videoTrack": {"sourcePath": "input-video.mp4", "startSec": 0, "endSec": duree,
                       "bounds": {"x": 0, "y": 0, "width": W, "height": H}},
        "subtitles": {"enabled": True, "generateur": "tools/habillage_standard.py",
                      "groupes": len(groupes), "bande_px": [haut, haut + BANDE_H]},
        "cards": [{"id": "card-hook", "intent": "Hook d'ouverture", "startSec": 0,
                   "endSec": round(hook_fin, 3), "accentIndex": 0, "zone": "video-overlay",
                   "contentHints": {"kicker": a.kicker, "title": a.hook}}],
    }
    (hab / "storyboard.json").write_text(json.dumps(storyboard, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")

    print(f"OK  {pub / 'index.html'}")
    print(f"    {len(groupes)} groupe(s) de sous-titres, {len(mots)} mots, {duree:.2f} s")
    print(f"    bande des sous-titres : {haut}-{haut + BANDE_H} px, {origine}")
    print(f"    hook : 0 - {hook_fin:.1f} s")
    courts = [texte(g) for g in groupes if len(g) == 1]
    if courts:
        print(f"[!] {len(courts)} groupe(s) d'un seul mot (souvent une ponctuation manquante dans "
              f"transcript.json) : {', '.join(courts[:5])}")
    for al in alertes:
        print(f"[!] {al}")
    print("    suite : npx hyperframes check, puis snapshot (voir le skill /habillage)")
    return 0


GABARIT = """<!doctype html>
<html lang="fr">
  <head>
    <meta charset="utf-8" />
    <style>
      @font-face { font-family: "Inter"; src: url("fonts/Inter-400-latin.woff2") format("woff2"); font-weight: 400; font-display: block; }
      @font-face { font-family: "Inter"; src: url("fonts/Inter-700-latin.woff2") format("woff2"); font-weight: 700; font-display: block; }
      :root { --accent: __ACCENT__; --texte: __TEXTE__; --fond: __FOND__; }
      * { box-sizing: border-box; }
      html, body { margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; background: #000;
        font-family: "Inter", ui-sans-serif, system-ui, sans-serif; }
      #stage { position: relative; width: 100%; height: 100%; overflow: hidden; }
      .video-wrapper { position: absolute; left: 0; top: 0; width: 1080px; height: 1920px; overflow: hidden; }
      .video-wrapper video { width: 100%; height: 100%; object-fit: cover; }

      /* Sous-titres : sous le menton, au-dessus des 20 % du bas (1536 px). */
      #captions { position: absolute; left: 0; top: __HAUT__px; width: 1080px; height: __BH__px; pointer-events: none; }
      .cap { position: absolute; left: 0; right: 0; bottom: 0; display: flex; justify-content: center; opacity: 0; }
      .pill { max-width: 1020px; padding: 14px 30px 18px; border-radius: 22px; background: var(--fond); text-align: center; }
      .line { font-family: "Inter", sans-serif; font-weight: 700; font-size: __TAILLE__px; line-height: 1.22;
        color: var(--texte); white-space: nowrap; text-shadow: 0 2px 6px rgba(0, 0, 0, 0.6); }
      .w { color: var(--texte); }

      /* Hook : haut du cadre, au-dessus du visage. */
      .card-host { position: absolute; pointer-events: none; overflow: hidden; }
      .hook { position: absolute; left: 60px; right: 60px; top: 170px; display: flex; flex-direction: column;
        align-items: center; padding: 30px 36px 36px; border-radius: 28px; background: rgba(10, 10, 10, 0.78); opacity: 0; }
      .hook .kicker { font-family: "Inter", sans-serif; font-weight: 700; font-size: 26px; letter-spacing: 4px; color: var(--accent); }
      .hook .title { margin-top: 12px; font-family: "Inter", sans-serif; font-weight: 700; font-size: 84px;
        line-height: 1.08; color: var(--texte); text-align: center; }
      .hook .bar { margin-top: 20px; width: 0; height: 8px; border-radius: 4px; background: var(--accent); }
    </style>
  </head>
  <body>
    <div id="stage" data-composition-id="habillage" data-start="0" data-duration="__DUR__" data-fps="30"
      data-width="1080" data-height="1920">
      <div class="video-wrapper" id="video-wrap">
        <video id="bg-video" src="input-video.mp4" muted playsinline data-start="0" data-duration="__DUR__"
          data-track-index="1"></video>
      </div>
      <audio id="source-audio" src="input-video.mp4" data-start="0" data-duration="__DUR__" data-track-index="10"
        data-volume="1"></audio>

      <div class="card-host clip" id="card-hook" data-card-id="card-hook" data-start="0" data-duration="__HD__"
        data-track-index="3" style="left:0;top:0;width:1080px;height:1920px;">
        <div class="hook" id="hook">
          __KICKER__
          <div class="title">__TITLE__</div>
          <div class="bar" id="hook-bar"></div>
        </div>
      </div>

      <div id="captions">
__CAPS__
      </div>

      <script src="vendor/gsap.min.js"></script>
      <script>
        (function () {
          const tl = window.gsap.timeline({ paused: true });
          tl.fromTo("#hook", { opacity: 0, scale: 0.92 }, { opacity: 1, scale: 1, duration: 0.3, ease: "back.out(1.6)" }, 0);
          tl.fromTo("#hook-bar", { width: 0 }, { width: 300, duration: 0.5, ease: "power2.out" }, 0.3);
          tl.to("#hook", { opacity: 0, duration: 0.3, ease: "power2.in" }, __HOUT__);
__JS__
          window.__timelines = window.__timelines || {};
          window.__timelines["habillage"] = tl;
        })();
      </script>
    </div>
  </body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
