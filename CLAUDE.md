# shorts-studio

Monter des shorts filmes au telephone, puis les habiller avec HyperFrames. L'utilisateur
n'est pas forcement developpeur : une etape a la fois, en francais simple, et on verifie
avant d'avancer.

## Le parcours

| etape | skill | ce qui en sort |
|---|---|---|
| 0. preparer le poste (une fois) | `/demarrer` | Python, ffmpeg, Node, HyperFrames, cle ElevenLabs |
| 1. ouvrir une video | `/nouvelle-video` | `videos/<slug>/_media/rushes/` |
| 2. monter | `/montage-rushs` | `videos/<slug>/_media/masters/<slug>_montage.mp4` |
| 3. relire | (automatique) | la page de review s'ouvre seule apres le montage |
| 4. habiller | `/habillage` | `videos/<slug>/_media/masters/<slug>_habille.mp4` |

Au demarrage de chaque session, un hook lance `python tools/verifier_poste.py --hook` : il se
tait si tout va bien, sinon il liste ce qui manque. Dans ce cas, le dire a l'utilisateur et
proposer `/demarrer` avant toute autre chose.

## Regles

- Toutes les commandes se lancent depuis la racine du depot : `python tools/...`, `npx ...`.
  Jamais de `cd` dans un sous-dossier.
- `tools/paths.py` est le seul endroit qui sait ou sont les choses ; ne pas recoller un chemin
  a la main dans un script.
- Une video = `videos/<slug>/`, slug en minuscules-avec-tirets. `_media/` n'est jamais
  versionne (rushs et rendus sont lourds) ; `1_montage/` et `2_habillage/` le sont.
- Les rushs d'origine ne sont jamais deplaces ni modifies : on les COPIE.
- Une cle d'API ne se colle jamais dans la conversation : elle va dans `.env` (voir `/demarrer`).
- Quand un outil REFUSE (code de sortie non nul, message `[REFUS]` ou explication), lire son
  message : il dit quoi faire. Ne pas le contourner.
- La transcription recommandee est ElevenLabs. Sans cle, `--engine whisper` marche mais repere
  mal les « euh » : le dire a l'utilisateur, ne jamais basculer en silence.
- Les skills HyperFrames de `.claude/skills/` sont figes dans le depot. Ne jamais lancer
  `npx hyperframes skills update` : il installe en GLOBAL sur la machine (voir `README.md`).
- Livrer = donner le chemin complet du fichier, sa duree, ce qui reste a faire.
