---
name: habillage
description: Habiller un short deja monte avec HyperFrames - sous-titres karaoke et titre d'accroche (hook) obligatoires, le reste au choix. A invoquer sur « habille la video », « ajoute les sous-titres », « fais l'habillage », « mets un titre », une fois le montage valide en review.
---

# Habillage HyperFrames

Ce skill est volontairement mince. Il fixe ce qui est OBLIGATOIRE dans ce depot, et un outil
du depot produit la composition standard. Pour tout ce qu'on ajoute en plus (cartes, emojis,
animations), le savoir-faire appartient aux skills HyperFrames de `.claude/skills/`
(`hyperframes-core`, `hyperframes-animation`, `hyperframes-creative`, `hyperframes-cli`,
`talking-head-recut`, `media-use`...). On les suit, on ne les reecrit pas.

## L'habillage standard (ce qui est obligatoire)

1. **Sous-titres karaoke** sur toute la parole : le mot prononce s'allume quand il est dit.
   Au plus 2 lignes de 28 caracteres, sous le menton, au-dessus des 20 % du bas du cadre
   (masques par les boutons TikTok / Reels / Shorts).
2. **Un hook d'ouverture** : une carte titre sur les 2 a 3 premieres secondes, qui reprend la
   promesse de la video en 3 a 7 mots, au-dessus du visage.

Tout le reste (cartes de mots-cles, listes, chiffres, emojis, appel a l'action de fin, sons)
est **optionnel** : seulement si l'utilisateur le demande, ou le proposer une fois en une
phrase. Les regles de densite de `talking-head-recut` (« minimum 5 cartes ») ne s'appliquent
PAS ici.

## 1. Le poste et le montage

```powershell
python tools/verifier_poste.py --pour habillage
```

Code 1 : s'arreter et reparer avec la commande qu'il nomme. Cas a connaitre : « Windows
bloque le Chrome telecharge par HyperFrames » (strategie de controle d'application) ; il
donne alors la commande qui pointe `HYPERFRAMES_BROWSER_PATH` vers un Chrome qui marche, a
lancer dans PowerShell, puis relancer Claude Code.

Le master doit etre VALIDE : `python tools/retours.py <slug>` doit dire « RIEN D'OUVERT », ou
l'utilisateur dit qu'il est content du montage. Habiller un montage qui va encore bouger fait
tout recommencer.

## 2. Preparer les entrees

```powershell
python tools/vers_hyperframes.py <slug>
```

Il ecrit dans `videos/<slug>/2_habillage/` : `public/input-video.mp4` (le master re-encode
pour le rendu), `transcript.json` (les mots DU MASTER, `[{text, start, end}]`, issus
d'ElevenLabs) et `metadata.json`. Sans cle ElevenLabs : ajouter `--engine whisper`.

Relire `transcript.json` pour les noms propres et les chiffres mal transcrits ; corriger le
`text` d'un mot sans toucher a `start` / `end`. Un point manquant se rajoute ici aussi (sinon
une phrase deborde sur la suivante dans un meme sous-titre).

## 3. La composition standard

Demander le titre du hook en une question (proposer 2 formulations tirees de la premiere
phrase de la video). Si `videos/<slug>/2_habillage/style.json` n'existe pas et que
l'utilisateur a ses couleurs, le creer (format dans la docstring de l'outil) ; sinon, style
neutre.

```powershell
python tools/habillage_standard.py <slug> --hook "Le titre du hook" --kicker "SUR-TITRE"
```

Il ecrit `public/index.html` (video plein cadre + sous-titres + hook), copie la police et
GSAP, et MESURE le visage pour placer la bande des sous-titres. Lire ses `[!]` : un groupe
d'un seul mot (ponctuation manquante dans le transcript), un menton trop bas, un front trop
haut pour le hook.

Puis le controle, et les images, AVANT tout rendu :

```powershell
npx hyperframes check videos/<slug>/2_habillage/public
npx hyperframes snapshot videos/<slug>/2_habillage/public --at 1.5,<milieu>,<fin-3> --no-end -o "$env:TEMP\snaps-<slug>"
```

Regarder `contact-sheet.jpg` (dans le dossier `-o`, jamais dans `public/`) : hook lisible,
sous-titres hors du visage, mot actif visible. Corriger avant de rendre.

## 4. Ajouter des elements (seulement sur demande)

On edite `public/index.html` en suivant `hyperframes-core` et `hyperframes-animation`.
Les regles qui evitent les erreurs de `check` :

- la video reste plein cadre (`#video-wrap`, 1080x1920) : c'est la disposition
  `overlay` / zone `video-overlay` de `talking-head-recut`, jamais `stack` (qui reduit la
  video au tiers haut) ;
- une carte = un `<div class="card-host clip" id="...">` avec `data-start`,
  `data-duration`, `data-track-index` et son propre `id` ; on anime ses ENFANTS, jamais le
  `.clip` lui-meme (pas de `tl.set` de visibilite sur un `.clip`) ;
- jamais de police ou de script charge depuis internet : tout dans `public/` ;
- rien sur le visage plus d'une seconde, rien dans la bande des sous-titres.

Ne pas chercher de sous-titres dans le registre (`npx hyperframes catalog`) : ceux de
l'outil respectent deja les regles, les blocs du registre non (format paysage, polices en
ligne). Pour des sous-titres « cinematiques » qui passent DERRIERE la personne : skill
`embedded-captions`, sur demande seulement (il detoure la video, telecharge un modele
d'environ 170 Mo, et peut refuser un master monte en coupes franches).

## 5. Rendre, relire, livrer

1. Le brouillon, puis la review a la frame :
   ```powershell
   npx hyperframes render videos/<slug>/2_habillage/public --quality draft -o "videos/<slug>/_media/masters/<slug>_habille_brouillon.mp4"
   python tools/retours.py <slug> --ouvrir --rendu <slug>_habille_brouillon.mp4
   ```
   Environ 1 min 30 pour 45 s de video. Le rendu se lance au premier plan, jamais en tache de
   fond : un rendu interrompu laisse un fichier tronque qui a l'air valide.
2. L'utilisateur pose ses notes dans la page. `python tools/retours.py <slug>` les liste avec
   le mot prononce ; une note = une demande ; appliquer ; refaire `check` + snapshot ;
   re-rendre le brouillon ; `--traite` quand tout est fait.
3. Le rendu final :
   ```powershell
   npx hyperframes render videos/<slug>/2_habillage/public --quality delivery -o "videos/<slug>/_media/masters/<slug>_habille.mp4"
   ```
4. Livrer : chemin COMPLET du fichier final, duree, poids. Puis
   `python tools/retours.py --arreter` (le serveur de review tourne en fond depuis `--ouvrir`).

## Ce que les skills HyperFrames disent et qui ne s'applique pas ici

- « keep this skill fresh : `npx hyperframes skills update` » (en tete de chaque skill) :
  **ne jamais le lancer**, il installe dans le dossier GLOBAL de la machine. Les skills de ce
  depot sont figes ; `README.md` dit comment les mettre a jour.
- les questions de direction visuelle de `talking-head-recut` (etape 7) : format, disposition
  et nombre de cartes sont deja tranches ci-dessus ; ne demander que le style, une fois.
- les etapes 2 a 4 de `talking-head-recut` (dossier, audio, transcription Whisper) : faites
  par `vers_hyperframes.py`, avec un transcript plus juste.
- la relecture dans le Studio HyperFrames (`preview`), `feedback`, `history` : la boucle de ce
  depot est brouillon puis review a la frame (§ 5). Ne rien envoyer sur un canal externe.

## Definition de fini

- `videos/<slug>/_media/masters/<slug>_habille.mp4` existe, en 1080x1920, avec le son ;
- sous-titres sur toute la parole, jamais sur le visage ni sous l'interface des plateformes ;
  hook lisible dans les 3 premieres secondes ;
- `npx hyperframes check` passe ; les retours de la review sont traites ;
- rien d'ajoute au-dela du standard sans demande de l'utilisateur.
