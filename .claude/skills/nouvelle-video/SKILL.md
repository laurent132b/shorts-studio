---
name: nouvelle-video
description: Ouvrir une nouvelle video - creer son dossier et y importer les rushs filmes au telephone (copie, controle, normalisation HDR et cadence variable). A invoquer sur « nouvelle video », « j'ai filme un short », « voila mes rushs », ou un chemin de fichiers video.
---

# Nouvelle video

Objectif : a la fin, `videos/<slug>/_media/rushes/` contient des rushs prets a monter, et
`videos/<slug>/1_montage/rushes.json` dit ce qui a ete trouve. Ensuite : `/montage-rushs`.

## 1. Le poste est-il pret ?

```powershell
python tools/verifier_poste.py --pour montage
```

Code 1 (un manque BLOQUANT) : s'arreter, proposer `/demarrer`. Un manque OPTIONNEL se dit a
l'utilisateur en une ligne, sans bloquer (sans cle ElevenLabs, le montage marche mais repere
moins bien les « euh »).

## 2. Le nom de la video (le slug)

Demander en une question de quoi parle la video, et proposer un slug : minuscules, chiffres et
tirets, 2 a 5 mots, sans mots vides (`recette-cookies`, `astuce-excel-tableaux`). Verifier
qu'il n'existe pas deja :

```powershell
python tools/paths.py
```

Si le slug existe, demander si on complete cette video ou si on en ouvre une autre.

## 3. Les rushs

L'utilisateur a deux facons de les donner. **S'il n'a donne ni l'une ni l'autre, le DEMANDER**
avant toute commande, avec les deux options :

1. **un chemin absolu** vers les fichiers ou leur dossier (dans l'Explorateur Windows :
   clic droit sur le fichier > « Copier en tant que chemin d'acces ») ;
2. **deposer les fichiers** dans `videos/<slug>/_media/rushes/` (creer le dossier pour lui
   s'il le prefere, et lui donner le chemin complet), puis le dire.

Puis, d'abord en diagnostic, sans rien copier :

```powershell
python tools/importer_rushs.py <slug> "C:\chemin\vers\les\rushs" --sonder
```

Lire le diagnostic AVEC l'utilisateur, en une phrase par point qui compte :
- `HDR` : sera converti en SDR, sinon les couleurs sortent delavees au montage ;
- `VFR` (cadence variable) : sera re-cadence, sinon le son et l'image se decalent aux coupes ;
- `rotation` : normal pour un telephone, rien a faire ;
- un rush en **paysage** : un short est vertical, demander si c'est voulu ;
- moins de 1080 px de large : image molle, le dire.

Puis l'import reel (meme commande sans `--sonder`, ou sans chemin si les fichiers ont ete
deposes dans `_media/rushes/`) :

```powershell
python tools/importer_rushs.py <slug> "C:\chemin\vers\les\rushs"
python tools/importer_rushs.py <slug>
```

L'outil **refuse** (code 2) et n'a alors rien copie : aucun rush trouve, chemin introuvable,
fichier illisible, fichier sans son, deux rushs du meme nom, HDR sans le filtre ffmpeg
necessaire. Lire son message : il dit quoi faire. Ne jamais contourner un refus en copiant
les fichiers a la main.

Les originaux du telephone ne sont jamais modifies : ils sont copies, et ceux qui ont du etre
normalises sont gardes dans `_media/originaux/`.

## 4. Fin

Dire en trois lignes : combien de rushs, leur duree totale, ce qui a ete normalise. Puis
proposer d'enchainer sur `/montage-rushs`.
