"""verifier_poste.py -- ce poste peut-il faire tourner le montage et l'habillage ?

    python tools/verifier_poste.py                   # rapport complet
    python tools/verifier_poste.py --pour montage    # seulement ce qu'il faut pour monter
    python tools/verifier_poste.py --pour habillage  # seulement ce qu'il faut pour habiller
    python tools/verifier_poste.py --hook            # mode SessionStart : muet si tout va bien

Sortie : 0 rien de bloquant - 1 au moins un manque BLOQUANT (en --hook : toujours 0).

Trois regimes, et chaque manque nomme la commande qui le repare :
    OK          present et utilisable
    OPTIONNEL   absent, mais seule une option en depend (punch, ElevenLabs, HDR)
    BLOQUANT    absent, et l'etape ne peut pas tourner

La cle ElevenLabs est testee par un vrai appel (gratuit, sans transcription) : une cle collee
avec un espace en trop ou revoquee passe pour presente, et ne se decouvre sinon qu'au premier
montage. Sans reseau, elle est dite NON VERIFIABLE, jamais valide.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _flux in (sys.stdout, sys.stderr):
    if hasattr(_flux, "reconfigure"):
        _flux.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent.parent
OK, OPT, BLOQ = "OK", "OPTIONNEL", "BLOQUANT"


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=str(ROOT))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def _module(nom: str) -> bool:
    return importlib.util.find_spec(nom) is not None


# --------------------------------------------------------------------------- les controles

def c_python() -> tuple[str, str, str]:
    v = sys.version_info
    if v >= (3, 10):
        return OK, f"Python {v.major}.{v.minor}", ""
    return BLOQ, f"Python {v.major}.{v.minor} (3.10 minimum)", "winget install Python.Python.3.12"


def c_numpy() -> tuple[str, str, str]:
    if _module("numpy"):
        return OK, "numpy", ""
    return BLOQ, "numpy absent", "python -m pip install -r requirements.txt"


def c_ffmpeg() -> tuple[str, str, str]:
    import paths
    for nom in ("ffmpeg", "ffprobe"):
        try:
            b = paths._resolve_binary(nom)
        except SystemExit:
            return BLOQ, f"{nom} introuvable", "winget install Gyan.FFmpeg   (puis rouvrir le terminal)"
        code, _ = _run([b, "-version"])
        if code != 0:
            return BLOQ, f"{nom} ne demarre pas : {b}", "winget install Gyan.FFmpeg"
    return OK, "ffmpeg + ffprobe", ""


def c_zscale() -> tuple[str, str, str]:
    import paths
    try:
        code, out = _run([paths.ffmpeg(), "-hide_banner", "-filters"])
    except SystemExit:
        return OPT, "zscale non verifiable (ffmpeg absent)", ""
    if " zscale " in out:
        return OK, "ffmpeg sait convertir le HDR (zscale)", ""
    return OPT, "ffmpeg sans zscale : les rushs HDR (iPhone) seront refuses",\
        "winget install Gyan.FFmpeg   (version complete)"


def _cle_elevenlabs() -> str | None:
    import transcribe
    return transcribe.find_elevenlabs_key("ELEVENLABS_API_KEY")


def c_elevenlabs(reseau: bool = True) -> tuple[str, str, str]:
    env = ROOT / ".env"
    reparer = ("creer un compte sur elevenlabs.io (plan gratuit), Developers > API Keys, "
               "coller la cle dans .env : ELEVENLABS_API_KEY=...   (skill /demarrer)")
    if not env.exists() and not os.environ.get("ELEVENLABS_API_KEY"):
        return OPT, "pas de fichier .env : transcription locale seulement (moins bonne)", reparer
    cle = _cle_elevenlabs()
    if not cle:
        return OPT, "ELEVENLABS_API_KEY vide : transcription locale seulement (moins bonne)", reparer
    if not reseau:
        return OK, "cle ElevenLabs presente (non testee)", ""
    req = urllib.request.Request("https://api.elevenlabs.io/v1/user",
                                 headers={"xi-api-key": cle})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            if r.status == 200:
                return OK, "cle ElevenLabs valide", ""
    except urllib.error.HTTPError as e:
        corps = e.read().decode("utf-8", "replace")
        if e.code == 401 and "missing_permissions" in corps:
            # cle restreinte sans le droit de lire le compte : elle existe bel et bien
            return OK, "cle ElevenLabs reconnue (droits restreints, OK pour la transcription)", ""
        if e.code in (401, 403):
            return OPT, "cle ElevenLabs REFUSEE par le serveur (fausse, revoquee, espace en trop ?)",\
                "recopier la cle dans .env, sans guillemets ni espace"
        return OPT, f"cle ElevenLabs non verifiable (HTTP {e.code})", ""
    except (urllib.error.URLError, OSError):
        return OPT, "cle ElevenLabs NON VERIFIABLE (pas de reseau ?)", ""
    return OPT, "cle ElevenLabs non verifiable", ""


def c_transcription(reseau: bool = True) -> tuple[str, str, str]:
    """Au moins UN moteur : ElevenLabs (recommande) ou Whisper local."""
    etat, detail, rep = c_elevenlabs(reseau)
    if etat == OK:
        return OK, detail, ""
    if _module("faster_whisper"):
        return OPT, detail + " ; Whisper local disponible (--engine whisper)", rep
    return BLOQ, detail + " ; et Whisper local absent : aucun moteur de transcription",\
        rep + "   OU   python -m pip install faster-whisper"


def c_opencv() -> tuple[str, str, str]:
    if _module("cv2"):
        return OK, "opencv (punch)", ""
    return OPT, "opencv absent : pas de punch-in", "python -m pip install -r requirements.txt"


def c_yunet() -> tuple[str, str, str]:
    import visage
    if visage.est_intact():
        return OK, "modele de visage YuNet (punch)", ""
    return OPT, "modele YuNet absent : pas de reperes de visage pour le punch",\
        "python tools/visage.py telecharger"


def c_node() -> tuple[str, str, str]:
    node = shutil.which("node")
    if not node:
        return BLOQ, "Node.js absent", "winget install OpenJS.NodeJS.LTS   (puis rouvrir le terminal)"
    code, out = _run([node, "-v"])
    m = re.match(r"v(\d+)", out.strip())
    if not m or int(m.group(1)) < 22:
        return BLOQ, f"Node.js {out.strip()} (22 minimum pour HyperFrames)", "winget install OpenJS.NodeJS.LTS"
    return OK, f"Node.js {out.strip()}", ""


def c_hyperframes() -> tuple[str, str, str]:
    pj = ROOT / "node_modules" / "hyperframes" / "package.json"
    if not pj.is_file():
        return BLOQ, "HyperFrames non installe dans le depot", "npm install"
    v = json.loads(pj.read_text(encoding="utf-8")).get("version", "?")
    return OK, f"HyperFrames {v}", ""


def _chromes(cache: Path) -> list[Path]:
    if not cache.is_dir():
        return []
    return sorted(cache.rglob("chrome-headless-shell.exe"), key=lambda p: str(p), reverse=True)


def _demarre(exe: Path) -> bool:
    """Le binaire DEMARRE-t-il ? Sa presence ne suffit pas : une strategie de controle
    d'application Windows peut bloquer un Chrome telecharge, et `render` echoue alors sur
    « Chrome cannot start » alors que `doctor` et `snapshot`, qui en utilisent un autre, passent."""
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
        return r.returncode == 0 and "Chrome" in (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def c_chrome(lent: bool) -> tuple[str, str, str]:
    """Le Chrome que `hyperframes render` utilisera VRAIMENT, et s'il demarre.

    Ordre de `render` : `HYPERFRAMES_BROWSER_PATH` s'il est defini, sinon son propre
    telechargement dans ~/.cache/hyperframes/chrome/. Celui de ~/.cache/puppeteer/ (pris par
    `doctor` et `snapshot`) sert de solution de repli si le premier est bloque.
    """
    force = os.environ.get("HYPERFRAMES_BROWSER_PATH")
    if force:
        if _demarre(Path(force)):
            return OK, "Chrome de rendu (HYPERFRAMES_BROWSER_PATH)", ""
        return BLOQ, f"HYPERFRAMES_BROWSER_PATH ne demarre pas : {force}",\
            "corriger ou retirer la variable HYPERFRAMES_BROWSER_PATH"
    rendu = _chromes(Path.home() / ".cache" / "hyperframes" / "chrome")
    repli = [p for p in _chromes(Path.home() / ".cache" / "puppeteer") if _demarre(p)]
    fixer = ("[Environment]::SetEnvironmentVariable('HYPERFRAMES_BROWSER_PATH', '{}', 'User')"
             "   (dans PowerShell, puis relancer Claude Code)")
    if rendu:
        if _demarre(rendu[0]):
            return OK, "Chrome de rendu", ""
        if repli:
            return BLOQ, "Windows bloque le Chrome telecharge par HyperFrames ; un autre Chrome marche",\
                fixer.format(repli[0])
        return BLOQ, "Windows bloque le Chrome telecharge par HyperFrames",\
            "npx hyperframes browser ensure --force ; si toujours bloque : Securite Windows > " \
            "Controle des applications (Smart App Control)"
    if repli:
        return OPT, "Chrome de rendu pas encore telecharge ; un Chrome compatible existe",\
            fixer.format(repli[0])
    return OPT, "Chrome de rendu pas encore telecharge (se fait au premier rendu)",\
        "npx hyperframes browser ensure"


# --------------------------------------------------------------------------- assemblage

def controles(pour: str | None, lent: bool, reseau: bool) -> list[tuple[str, str, str, str]]:
    commun = [("python", c_python), ("ffmpeg", c_ffmpeg)]
    montage = [("numpy", c_numpy), ("transcription", lambda: c_transcription(reseau)),
               ("hdr", c_zscale), ("opencv", c_opencv), ("yunet", c_yunet)]
    habillage = [("node", c_node), ("hyperframes", c_hyperframes),
                 ("chrome", lambda: c_chrome(lent))]
    jeu = commun + (montage if pour in (None, "montage") else []) \
        + (habillage if pour in (None, "habillage") else [])
    out = []
    for nom, f in jeu:
        try:
            etat, detail, rep = f()
        except Exception as e:  # noqa: BLE001 - un controle qui casse ne doit pas tout casser
            etat, detail, rep = OPT, f"controle impossible ({type(e).__name__}: {e})", ""
        out.append((nom, etat, detail, rep))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pour", choices=("montage", "habillage"), default=None)
    ap.add_argument("--hook", action="store_true",
                    help="mode SessionStart : rien si tout va bien, toujours code 0")
    ap.add_argument("--hors-ligne", action="store_true", help="ne pas tester la cle en ligne")
    a = ap.parse_args()

    res = controles(a.pour, lent=not a.hook, reseau=not a.hors_ligne)
    manques = [r for r in res if r[1] != OK]
    bloquants = [r for r in res if r[1] == BLOQ]

    if a.hook:
        if not manques:
            return 0
        print("[verifier_poste] Ce poste n'est pas completement pret :")
        for nom, etat, detail, rep in manques:
            print(f"  {etat:<9} {detail}" + (f"\n            -> {rep}" if rep else ""))
        if not (ROOT / ".env").exists() or bloquants:
            print("  Proposer a l'utilisateur le skill /demarrer, qui installe ce qui manque.")
        return 0

    titre = {"montage": " pour le montage", "habillage": " pour l'habillage"}.get(a.pour, "")
    print(f"Verification du poste{titre} :")
    for nom, etat, detail, rep in res:
        print(f"  {etat:<9} {detail}")
        if rep and etat != OK:
            print(f"            -> {rep}")
    if bloquants:
        print(f"\n{len(bloquants)} manque(s) BLOQUANT(S) : a reparer avant de continuer.")
        return 1
    if manques:
        print(f"\nRien de bloquant. {len(manques)} option(s) indisponible(s), voir ci-dessus.")
    else:
        print("\nTout est pret.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
