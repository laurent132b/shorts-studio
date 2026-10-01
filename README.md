# shorts-studio

Monter automatiquement des shorts filmes au telephone, avec Claude Code : transcription au
mot, choix des meilleures prises, coupe des blancs et des « euh », rythme (resserrage des
coupes, punch-in), relecture a la frame dans le navigateur, puis habillage (sous-titres
karaoke, titre d'accroche) avec [HyperFrames](https://github.com/heygen-com/hyperframes).

## Premier lancement

1. Installer [Claude Code](https://claude.com/claude-code) et ouvrir ce dossier.
2. Taper `/demarrer` : il verifie le poste et installe ce qui manque (Python, ffmpeg, Node,
   HyperFrames), puis aide a creer la cle ElevenLabs (optionnelle, recommandee).

## Faire une video

1. Filmer au telephone, en vertical, en 4K si possible. Laisser une seconde de silence avant
   et apres chaque prise ; refaire la phrase entiere plutot que reprendre au milieu.
2. Copier les fichiers sur l'ordinateur.
3. Dans Claude Code : `/nouvelle-video`, puis donner le chemin des fichiers.
4. `/montage-rushs` : le montage se fait, puis la page de review s'ouvre. Mettre la video en
   pause la ou quelque chose ne va pas, ecrire la remarque (« trop long », « coupe trop
   seche »...), et le dire a Claude.
5. Une fois le montage valide : `/habillage`.

Le resultat est dans `videos/<slug>/_media/masters/`.

## Ce qu'il faut sur la machine

| outil | pourquoi | installation |
|---|---|---|
| Python 3.10+ | toute la chaine de montage | `winget install Python.Python.3.12` |
| ffmpeg (version complete) | couper, assembler, convertir le HDR | `winget install Gyan.FFmpeg` |
| modules Python | `requirements.txt` | `python -m pip install -r requirements.txt` |
| Node.js 22+ | HyperFrames | `winget install OpenJS.NodeJS.LTS` |
| HyperFrames | l'habillage | `npm install` |
| cle ElevenLabs (optionnelle) | une transcription precise qui garde les « euh » | `/demarrer` |

`python tools/verifier_poste.py` dit a tout moment ce qui manque.

## Organisation

```
videos/<slug>/
    1_montage/      transcripts, decoupage (EDL), rapports        (versionne)
    2_habillage/    composition HyperFrames, retours de review    (versionne)
    _media/         rushes/ originaux/ masters/ renders/          (jamais versionne)
tools/              la chaine Python (montage, review, diagnostic)
.claude/skills/     les skills Claude Code
```

## Mettre a jour les skills HyperFrames

Les skills HyperFrames sont copies dans `.claude/skills/` (versions dans `skills-lock.json`).
`npx hyperframes skills update` les installerait dans le dossier GLOBAL de Claude Code de la
machine, pas ici : ne pas l'utiliser. Pour mettre a jour dans ce depot :

```powershell
npx skills add heygen-com/hyperframes --skill hyperframes hyperframes-cli hyperframes-core hyperframes-animation hyperframes-keyframes hyperframes-creative hyperframes-registry media-use embedded-captions talking-head-recut --agent claude-code --yes
```

Puis mettre a jour la version de `hyperframes` dans `package.json` (`npm install hyperframes@latest`).

## Vie privee

Les outils HyperFrames envoient par defaut des statistiques d'usage anonymes a HeyGen. Ce
depot les coupe (`HYPERFRAMES_NO_TELEMETRY=1` dans `.claude/settings.json`) ; retirer la
ligne pour les rallumer. Rien d'autre ne sort de la machine, sauf l'audio des rushs envoye a
ElevenLabs pour la transcription, si une cle ElevenLabs est configuree.

## Licences

- **La chaine de montage, la review, les skills `demarrer`, `nouvelle-video`,
  `montage-rushs`, `habillage`** : licence MIT, voir `LICENSE`.
- **Les skills HyperFrames** (`.claude/skills/hyperframes*`, `media-use`,
  `embedded-captions`, `talking-head-recut`) : copies du depot public
  [heygen-com/hyperframes](https://github.com/heygen-com/hyperframes), licence Apache-2.0
  (texte dans `.claude/skills/HYPERFRAMES-LICENSE.txt`). `talking-head-recut` est lui-meme
  adapte d'un projet MIT, voir son `NOTICE.md`. Les polices qu'ils embarquent sont sous
  licence SIL Open Font License.
- **Les sons de `media-use/audio/assets/sfx/`** ne sont pas inclus : ils viennent de Pixabay,
  dont la licence interdit de les redistribuer tels quels (`CREDITS.md` dit ou les
  telecharger).
- **Le modele de detection de visage YuNet** (telecharge dans `models/` par
  `python tools/visage.py telecharger`) : opencv_zoo, licence Apache-2.0.
