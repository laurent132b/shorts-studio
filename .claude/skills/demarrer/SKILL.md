---
name: demarrer
description: Installer et verifier le poste au premier lancement - Python, ffmpeg, Node, HyperFrames, le modele de visage, et la cle ElevenLabs (optionnelle mais recommandee). A invoquer sur « demarrer », « installe », « premiere fois », « verifie mon poste », « il me manque quelque chose », ou quand le hook de demarrage signale un manque.
---

# Demarrer : preparer le poste

L'utilisateur n'est pas forcement developpeur. Une etape a la fois, une phrase pour dire a
quoi elle sert, et on verifie avant de passer a la suivante. Ne jamais lui demander de coller
une cle dans la conversation.

## 1. L'etat des lieux

```powershell
python tools/verifier_poste.py
```

Si Python lui-meme manque, la commande ne se lance pas : faire installer Python 3.12
(`winget install Python.Python.3.12`), faire ROUVRIR le terminal et Claude Code, puis
recommencer. Pareil pour tout ce que `winget` installe : un terminal deja ouvert ne voit pas
le nouveau programme.

## 2. Reparer ce qui manque, dans cet ordre

Chaque ligne non OK du rapport donne sa commande. L'ordre habituel :

| manque | commande | a quoi ca sert |
|---|---|---|
| ffmpeg | `winget install Gyan.FFmpeg` | couper et assembler la video |
| modules Python | `python -m pip install -r requirements.txt` | mesurer le son, le punch-in, Whisper local |
| Node.js 22+ | `winget install OpenJS.NodeJS.LTS` | faire tourner HyperFrames (habillage) |
| HyperFrames | `npm install` | l'habillage (sous-titres, titres animes) |
| Chrome de rendu | `npx hyperframes browser ensure` | le rendu video de l'habillage |
| modele de visage | `python tools/visage.py telecharger` | le punch-in (zoom cale sur les yeux) |

Apres un `winget`, faire rouvrir le terminal. Relancer `python tools/verifier_poste.py`
apres chaque reparation.

## 3. La transcription : ElevenLabs ou local

Expliquer le choix, simplement :

- **ElevenLabs (recommande)** : une transcription au mot tres precise, qui GARDE les « euh »
  et les hesitations. C'est ce qui permet au montage de les couper. Le plan gratuit suffit
  largement pour des shorts. Il faut un compte et une cle.
- **Local (Whisper)** : gratuit, sans compte, mais moins precis, et il efface les « euh » de
  la transcription, donc le montage ne peut presque plus les reperer. Le montage marche quand
  meme.

S'il choisit ElevenLabs :

1. creer un compte sur elevenlabs.io ;
2. menu Developers > API Keys > Create API Key (cocher au minimum l'acces Speech to Text) ;
3. creer le fichier `.env` a partir du modele, puis l'ouvrir :
   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```
4. il colle sa cle apres `ELEVENLABS_API_KEY=`, sans espace ni guillemets, enregistre, ferme ;
5. verifier : `python tools/verifier_poste.py --pour montage` doit dire « cle ElevenLabs valide ».
   « REFUSEE » = cle mal recopiee ; « NON VERIFIABLE » = pas de reseau.

S'il choisit le local : `.env` peut rester absent. Le dire une fois : les commandes de
transcription prendront `--engine whisper`.

## 4. Fin

`python tools/verifier_poste.py` doit finir par « Rien de bloquant » ou « Tout est pret ».
Dire ce qui reste optionnel et non installe, en une ligne, puis proposer `/nouvelle-video`.
