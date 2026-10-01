---
name: montage-rushs
description: Monter automatiquement un short depuis ses rushs - transcription mot a mot, choix des prises, assemblage ffmpeg, coupe des blancs et des tics, resserrage des coupes, punch-in, puis review a la frame. A invoquer sur « monte mes rushs », « fais le montage », « coupe les blancs », « enleve les euh », « choisis les meilleures prises », « c'est trop long / pas assez rythme » sur un montage, ou des que des rushs sont importes dans videos/<slug>/_media/rushes/.
---

# Montage rushs

Ce skill va du dossier de rushs d'une video au master monte :

    videos/<slug>/_media/rushes/*.mp4   ->   videos/<slug>/_media/masters/<slug>_montage.mp4

Il suppose que `/nouvelle-video` a deja cree `videos/<slug>/` et importe les rushs (copies
dans `_media/rushes/`, originaux dans `_media/originaux/`, inventaire dans
`1_montage/rushes.json`). Il se termine en renvoyant vers `/habillage`.

La personne qui filme n'est pas developpeur. L'agent lance les commandes, lit les rapports,
et ne lui pose que des questions de GOUT (quelle prise, quelle duree, quel rythme), jamais
des questions d'outil. Toutes les commandes se lancent depuis la racine du depot, dans
PowerShell, chemins entre guillemets doubles. On ne fait jamais de `cd` dans un sous-dossier.

## Les regles, avant tout le reste

1. **On ne rogne que du silence MESURE, jamais de la parole.** Les horodatages de la
   transcription sont faux de plusieurs facons (attaque des consonnes, mots normalises,
   mots inventes, debut de mot en avance ou en retard). Toute borne se pose sur l'audio
   du rush, pas sur le texte. C'est le metier de `tighten_cuts.py` et d'`enveloppe.py`.
2. **On n'encode qu'a la fin, une fois.** Tout le montage se boucle en mode `--plan-only`
   (une seconde) ; l'encodage du master coute plusieurs minutes et ne sert qu'a livrer.
   Encoder pour DECOUVRIR un defaut est une erreur : les rapports le voient avant.
3. **Le resserrage des coupes (`tighten_cuts.py`) n'est jamais saute.** Sans lui : 0,3 a
   0,5 s de blanc a chaque raccord, attaques de mots rognees, vide en tete de plan.
4. **Apres toute modification d'un EDL ou d'une timeline, on re-planifie** (`--plan-only`)
   avant de relancer un controle. Sinon le SRT et les cumuls decrivent l'ancien montage et
   la QA juge un montage qui n'existe plus.
5. **Une phrase sans prise fiable devient un TROU documente**, jamais un passage force.
   Un trou se signale a la personne ; un passage incomprehensible se publie.
6. **Les outils de mesure POINTENT, ils ne decident pas.** `blancs.py`, `tics.py`,
   `trous_gardes.py`, `fuites_edl.py` disent ou regarder. Ce qu'on coupe se decide en
   lisant ce qu'ils montrent, et ce qui releve du gout se demande.
7. **On ne retouche jamais les reglages d'un outil (son code, ses profils) pour un cas.**
   On passe une option sur la ligne de commande (`--max-internal-gap`, `--targets`...).
   Un seuil change dans le code change le montage de toutes les videos suivantes.
8. **On ne re-transcrit pas un rush deja transcrit.** La transcription n'est pas
   deterministe : relancer change des mots et des timings, donc le montage.
9. **Une retouche se designe par le TEXTE, jamais par un numero de ligne ou de segment.**
   Les numeros bougent a chaque scission ou suppression ; une phrase, non.
10. **Un controle qui rend zero doit avoir tourne sur quelque chose.** Lire combien
    d'objets il a mesures avant de croire son verdict ; ne jamais conclure depuis une sortie
    filtree, et verifier chaque commande dans le fichier qu'elle ecrit.

## 0. Avant de commencer

```powershell
python tools/verifier_poste.py --pour montage
```

Code 1 : un manque BLOQUANT, **s'arreter** et le reparer avec la commande qu'il nomme. Code
0 : on continue, en lisant les lignes OPTIONNEL (sans cle ElevenLabs, transcription Whisper ;
sans modele de visage, pas de punch-in) pour les dire a la personne.

Puis faire l'etat des lieux de la video :

```powershell
python tools/paths.py <slug>
```

Il donne les dossiers et le nombre de fichiers par zone. Ou reprendre :

| ce qu'on trouve | ou reprendre |
|---|---|
| `_media/rushes/` vide | rien a monter : retour a `/nouvelle-video` pour l'import |
| rushs presents, `1_montage/transcripts/` vide | etape 1 |
| transcripts presents | etape 2 ; ne PAS re-transcrire |
| `edl_v3.json` present | lire `rapport_selection.md`, puis etape 5 |
| un master existe deja | lire les retours (etape 9) avant de toucher a quoi que ce soit |

Si un rush est un enregistrement d'ecran et non une prise camera, ne pas couper ses silences
comme ceux d'une prise camera : sur un ecran, le geste continue pendant que la voix se tait.

## 1. Transcrire les rushs

### Le moteur

| | ElevenLabs Scribe (recommande) | Whisper local |
|---|---|---|
| timings | au mot, precis | cales sur un detecteur de voix : la coupe ouvre souvent sur du vide |
| hesitations | garde les « euh » | les efface : `tics.py` ne les voit plus |
| cout | cle API ; le plan gratuit suffit pour des shorts | gratuit, hors ligne |
| quand | toujours, si une cle existe | seulement si la personne refuse de creer un compte |

Par defaut (`--engine auto`), l'outil prend ElevenLabs si la cle `ELEVENLABS_API_KEY` est
dans `.env` a la racine, et **refuse** sinon en disant quoi faire. Il ne bascule jamais en
silence vers Whisper : ce choix doit etre conscient, et il se dit a la personne (« sans cle,
les euh ne seront presque pas detectes »).

### La commande, une fois par fichier de `_media/rushes/` (.mov ou .mp4)

```powershell
python tools/transcribe.py "videos\<slug>\_media\rushes\<rush>.mov" --out "videos\<slug>\1_montage\transcripts"
```

`--out` est obligatoire ici : sans lui, les transcripts iraient a cote des rushs, dans la
zone media, et aucun outil du montage ne les trouverait.

Repli sans cle, en le disant :

```powershell
python tools/transcribe.py "videos\<slug>\_media\rushes\<rush>.mov" --engine whisper --out "videos\<slug>\1_montage\transcripts"
```

Chaque rush produit `<rush>.transcript.json`, `<rush>.words.json`, `<rush>.segments.json`,
un `.srt` et un `.review.md`. Les outils lisent **`<rush>.transcript.json` en priorite**.

### Verifier le moteur avant de monter

Ouvrir un `*.transcript.json` et lire sa cle `engine` : `elevenlabs` attendu. Un montage sur
des transcripts Whisper se rattrape mal : il faut re-transcrire PUIS refaire la selection.

### Le vocabulaire

Noms propres et termes techniques : `tools/lexique.txt` (un terme par ligne) les souffle a
Whisper, et `tools/corrections.json` (motif -> forme juste) corrige les erreurs recurrentes
**au niveau du mot**. Une correction posee seulement dans un SRT ne sert a rien : le
selecteur, comparant le script a un mot mal transcrit, choisit une fenetre qui l'EXCLUT.

## 2. Lire les prises

```powershell
python tools/pack_takes.py "videos\<slug>\1_montage"
```

Sortie : `videos\<slug>\1_montage\takes_packed.md`, une carte compacte de toutes les prises
(timecodes, texte, hesitations, repetitions, marqueurs vocaux). **On lit ce fichier avant
toute decision**, et jamais les JSON mot a mot : ils sont enormes et n'apprennent rien de
plus.

Ce qu'on y cherche :

- **la structure** : une seule prise d'un trait, ou des phrases repetees plusieurs fois ;
- **les marqueurs vocaux** : « top », « bonne », « on garde » apres une bonne prise ;
  « non », « on refait », « on recommence », « pas bon » apres une mauvaise. Le selecteur
  les lit ;
- **les reprises de FIN** : un marqueur de reprise suivi d'un groupe plus court que la
  phrase. Souvent la personne ne redit que la fin. Voir § 3, regle 4 ;
- **les consignes orales** : un groupe d'un a trois mots qui ressemble a une instruction
  (« on garde serveur », « plutot la version courte ») est un arbitrage de la personne, pas
  un faux depart. Aucun outil ne le voit : il faut le lire et l'appliquer au script.

## 3. Ecrire le script : `final_transcript.json`

Le selecteur a besoin d'un SCRIPT : la liste des phrases a monter, dans l'ordre. C'est un
fichier JSON dans `videos\<slug>\1_montage\final_transcript.json` :

```json
[
  {"i": 0, "text": "Premiere phrase, telle qu'elle doit s'entendre."},
  {"i": 1, "text": "Deuxieme phrase."},
  {"i": 2, "text": "Et la chute."}
]
```

- `i` : l'ordre de montage, de 0 a N-1, sans trou ;
- `text` : la phrase **telle qu'elle a ete DITE** dans la prise visee (pas une version
  reecrite : le selecteur cherche ces mots-la dans l'audio).

L'agent l'ecrit avec son outil d'ecriture de fichier, en UTF-8, jamais par une commande
shell (les accents et apostrophes y sont abimes).

### Trois cas

**A. La personne a un script ecrit.** Une ligne par phrase du script. Si elle a improvise
autour, aligner le texte sur ce qu'elle a vraiment dit dans `takes_packed.md`.

**B. Plusieurs prises, sans script ecrit.** Pour chaque phrase, prendre la formulation de la
prise qui sonne le mieux (souvent la derniere, ou celle suivie de « top »), et l'ecrire telle
qu'elle a ete dite. Le selecteur choisira ensuite la meilleure fenetre parmi TOUTES les
prises qui la contiennent.

**C. Une seule prise, d'un trait.** Le script se DERIVE de la prise : reprendre ses phrases
dans l'ordre, une ligne par phrase (ou par groupe de souffle si une phrase est tres longue),
en sautant :

- les faux departs (la phrase commencee puis recommencee : garder la version complete) ;
- les marqueurs (« on refait », « attends ») ;
- ce que la personne a demande de retirer.

Un script derive « utilise tout » par construction : ce n'est pas une preuve qu'il est bon.
D'ou la relecture ci-dessous, qui vaut surtout pour les cas B et C.

### Relire le script comme un SCRIPT, avant de lancer la selection

Quatre questions, et elles coutent moins qu'un montage refait :

1. **La premiere ligne est-elle l'accroche ?** Un short ouvre sur ce qui retient, pas sur
   une annonce (« aujourd'hui je vais te parler de... »). Si l'accroche est plus loin, voir
   regle 5 ci-dessous : l'ordre n'est pas toujours libre.
2. **Une ligne annonce-t-elle ce qui vient plus tard ?** (« et a la fin je te montre... »)
   Sur un short, c'est du poids mort : la retirer.
3. **Une phrase porte-t-elle une incise secondaire** (une comparaison entre parentheses, un
   « comme pour... ») ? C'est le retrait le moins couteux en sens.
4. **Combien de lignes ouvrent sur « Et », « Alors », « Donc » ?** Les retirer du texte de
   la ligne coute une borne et resserre le raccord.

Proposer ces retraits a la personne en une liste courte ; elle tranche.

### Les regles d'ecriture du script

1. **Jamais une ligne de moins de trois mots.** Une ligne trop courte (« d'accord. »,
   « voila. ») n'est pas discriminante : le selecteur la fait servir par un AUTRE rush,
   qui passe alors pour consomme, et ses propres lignes tombent en trou plus loin. La
   fusionner avec sa voisine.
2. **Un mot a trait d'union compte pour un mot dans l'audio et pour plusieurs dans le
   script.** Si une ligne tombe en trou avec un motif de « blanc » alors que la prise est
   propre, c'est souvent ce cas : couper la ligne avant le mot compose.
3. **Un chiffre ou un symbole s'ecrit comme il a ete transcrit** (« 20 % » si le transcript
   dit « 20 % »).
4. **Une reprise de FIN fait DEUX lignes.** Quand la personne dit une phrase, « on refait »,
   puis seulement la fin de la phrase, ecrire la tete de la premiere prise sur une ligne et
   la reprise sur la suivante. Prendre la derniere prise seule amputerait la phrase de sa
   tete ; prendre la premiere livrerait la version refusee.
5. **Dans une meme prise, on ne revient jamais en arriere.** Le selecteur impose l'ordre
   chronologique a l'interieur d'un rush ; entre deux rushs, l'ordre est libre. Pour ouvrir
   sur une phrase dite a la FIN d'une prise unique, il faut la retourner dans un rush a
   part. Le dire a la personne plutot que de forcer.
6. **Retirer un « euh » au milieu d'une ligne : la scinder en deux lignes** qui s'arretent
   et reprennent autour de l'hesitation. Une ligne ne peut perdre des mots qu'a ses
   extremites. Mais d'abord laisser `tics.py` le faire (§ 5) : il le fait sans toucher au
   script.

## 4. Choisir les prises

```powershell
python tools/select_takes.py "videos\<slug>\1_montage" --rush-dir "videos\<slug>\_media\rushes"
```

`--rush-dir` n'est pas optionnel : sans lui l'outil cherche les videos a cote de l'EDL, n'en
trouve aucune et l'EDL ne pourra pas s'assembler. Il ne fait aucun appel reseau.

Sorties dans `1_montage/` : `edl_v3.json` (le montage propose), `candidates.json` (les
meilleurs candidats par ligne), `holes_v3.json` (les lignes sans prise), `rapport_selection.md`.

### Lire `rapport_selection.md`

- **Trous** : chaque ligne sans prise, avec la raison et le meilleur candidat rejete. Un
  rejet `blank_gap` ou `silence_ratio` sur une prise qui semble propre vient presque
  toujours du SCRIPT (ligne trop courte, mot compose, deux phrases fusionnees a tort), pas
  de la prise. Corriger le script et relancer ; ne pas chercher a forcer la ligne.
- **Drapeaux** a ne pas laisser passer :
  - `AMBIGU` : deux prises a moins de 0,05 point. Regarder les deux (voir plus bas) ;
  - `BAD_MARKER` : la prise retenue est suivie d'un « on refait ». Verifier que ce n'est pas
    la version refusee ;
  - `LOW_TEXT`, `SOFT_TEXT_MATCH` : le texte colle mal au script. Verifier les mots ;
  - `HESITANT`, `BLANK_RISK`, `SILENCE_RISK`, `VISUAL_REVIEW` : une hesitation, un blanc
    ou un doute a regarder ;
  - `LOOKAHEAD_YIELD` : le meilleur score a ete ecarte pour ne pas voler une prise aux
    lignes suivantes. Normal ; la table « Renoncements » dit pourquoi.
- **La colonne `text`** (ratio texte) : sous 0,98, comparer le script aux mots couverts. Un
  ratio sous 1 n'est pas toujours une troncature (la normalisation des accents en produit),
  et un ratio de 1 ne prouve rien quand le script est derive des prises : si la
  transcription a rate un mot, il manque aussi au script.

### Un mot de trop, un mot perdu

```powershell
python tools/fuites_edl.py "videos\<slug>\1_montage"
```

A lancer apres chaque `select_takes`, **avant** tout `--plan-only`. Il compare, ligne par
ligne, les mots du script aux mots reellement couverts par la fenetre retenue :

| il signale | c'est | quoi faire |
|---|---|---|
| `FUITE` (tete ou queue) | un vrai mot de trop, souvent le debut d'un « on garde » colle a la phrase | poser la borne (`start` ou `end` de la ligne dans `edl_v3.json`) a la valeur qu'il donne |
| sequence introuvable | un mot manque en bout, souvent le DERNIER, ou un mot mal transcrit | regarder ; c'est le cas le plus frequent de phrase coupee net |
| `SYMBOLE OMIS` | un « % » ou un « € » prononce mais hors fenetre | etendre la borne qu'il donne |
| `ECART DE LEXIQUE` | « 4 » ecrit, « quatre » dit : aucun mot ne manque | rien |

Code 0 : propre. Code 1 : quelque chose a regarder. Le selecteur perd volontiers le dernier
mot d'une ligne quand un silence le precede : rogner du silence lui rapporte plus de score
que le mot n'en rapporte de texte. C'est ce controle qui le voit.

Les corrections de bornes se font dans **`edl_v3.json`** (cles `start` / `end` de la ligne),
jamais dans un `words.json` : les outils re-calculent la duree des mots et ignorent la
retouche en silence. Attention : une relance de `select_takes` reecrit `edl_v3.json` : noter chaque
retouche (phrase visee, cle, valeur) pour la reposer apres.

### Changer de prise

Quand le choix de l'outil est mauvais et qu'un runner-up (liste « Runner-ups » du rapport)
est meilleur, recopier sa source et ses bornes (`stem`, `start`, `end`) dans la ligne de
`edl_v3.json`, puis reprendre au § 5.

**Un marqueur vocal juge la DICTION, jamais le regard ni le cadre**, et aucun score ne voit
un regard qui quitte l'objectif. Avant de trancher entre deux prises, sortir trois images de
chacune et les montrer a la personne :

```powershell
$ff = python tools/paths.py --bin ffmpeg
& $ff -v error -ss 12.4 -i "videos\<slug>\_media\rushes\<rush>.mp4" -frames:v 1 -vf scale=360:-1 "videos\<slug>\_media\captures\prise-a_12.4.png"
```

### Si la duree est a arbitrer

Une variante (version courte, version longue) est un `final_transcript.json` different dans
un sous-dossier, et elle se juge en REGARDANT, jamais sur un tableau de secondes :

```powershell
python tools/select_takes.py "videos\<slug>\1_montage\v_courte" --transcripts "videos\<slug>\1_montage\transcripts" --rush-dir "videos\<slug>\_media\rushes"
python tools/assemble.py "videos\<slug>\1_montage\v_courte\edl_v3.json" --draft "version courte - 45 s - 14 lignes"
```

Le brouillon sort dans `_media/masters/drafts/`, basse definition, etiquette incrustee, et
n'ecrit ni timeline ni SRT. Il juge duree et rythme, jamais l'image : ne pas le faire valider
comme un livrable. La variante retenue devient le script principal de `1_montage/`.

## 5. La boucle de pre-vol, sans encodage

Tout ce qui suit lit et ecrit des metadonnees. Chaque tour coute quelques secondes.

```powershell
# 1. le plan : timeline, SRT, rapports, sans encodage
python tools/assemble.py "videos\<slug>\1_montage\edl_v3.json" --plan-only

# 2. retirer les tics de langage (avant le resserrage, qui resserrera les raccords crees)
python tools/tics.py "videos\<slug>\1_montage" --appliquer

# 3. resserrer les coupes
python tools/tighten_cuts.py "videos\<slug>\1_montage\edl_v3_timeline.json"

# 4. re-planifier : les coupes ont bouge, le SRT et les cumuls doivent suivre
python tools/assemble.py "videos\<slug>\1_montage\edl_v3_timeline.json" --honor-cuts --plan-only

# 5. la QA, sur le plan
python tools/qa_montage.py --edl "videos\<slug>\1_montage\edl_v3_timeline.json" --skip-ffmpeg

# 6. provenance, ordre, chevauchements, fenetres
python tools/audit_edl.py "videos\<slug>\1_montage"
```

**On boucle jusqu'a ce que les rapports soient propres, PUIS on encode une fois** (§ 7).

Chaque nouveau tour repart de l'etape 1 sur **`edl_v3.json`** : elle reecrit la timeline a
neuf. On ne relance jamais `tighten_cuts.py` ou `tics.py --appliquer` sur une timeline qui
les a deja subis. Une retouche qui doit survivre a la boucle se pose donc en amont : dans
le script (`final_transcript.json`, puis `select_takes`) ou dans `edl_v3.json`.

### 5a. Les tics de langage : `tics.py`

Sans `--appliquer`, il ne fait que compter et classer, dans `1_montage/tics.md` et
`tics.json`. Avec, il decoupe la timeline. Lexique par defaut : « euh », « heu », « ben »,
« bah », « beh ». `--aussi voila,hein` ajoute des mots, mais ils portent souvent du sens :
pas par defaut.

| classe | ce que c'est | sort par defaut ? |
|---|---|---|
| `BORD` | premier ou dernier mot du segment : simple troncature | oui |
| `SCISSION` | au milieu, entoure de silence, les deux cotes tiennent | oui (un raccord de plus) |
| `COURTE` | au milieu, mais un cote ferait moins de trois mots | non : `--classes BORD,SCISSION,COURTE` apres avoir regarde |
| `COLLE` | au milieu, sans silence autour | jamais : la coupe s'entendrait |

Ce qu'il faut savoir :

- il lit les mots, pas l'audio : un « euh » non transcrit (ou efface par Whisper) lui est
  invisible. **Son compte est un plancher**, et zero peut etre legitime ;
- il garde un peu de respiration de chaque cote du tic ;
- il boucle tout seul jusqu'a point fixe : une ligne a deux tics se decoupe en trois
  fragments d'un coup.

Montrer la liste a la personne quand elle est longue : elle en refuse souvent quelques-uns.

### 5b. Le resserrage : `tighten_cuts.py`

Il corrige ce que la transcription fait mal, en mesurant l'enveloppe audio (RMS 20 ms) des
rushs :

- **l'attaque** : la transcription date un mot sur sa voyelle, la consonne d'attaque tombe
  avant. Il recule le debut de coupe jusqu'a l'attaque reelle (jamais plus de 0,15 s) ;
- **le vide en tete** : l'inverse, une coupe qui ouvre sur du silence ;
- **les fins tronquees** : de la voix contigue a la phrase qu'aucun mot ne couvre (un mot
  epele, un « et cetera » normalise en « etc. ») ;
- **les silences internes** : un segment est coupe en deux au-dela d'un blanc interne ;
- **la queue** : la coupe se pose peu apres le dernier MOT, pas apres le dernier bruit
  (respiration, froissement, tete qui se tourne) ;
- **les raccords** : ramenes a une cible qui depend du type d'enchainement ;
- **les chevauchements** : deux fenetres qui rejouent le meme fragment (« pa-par ») sont
  ecretees.

Les valeurs par defaut (profil `short`, celui de ce depot) :

| reglage | defaut | option |
|---|---:|---|
| raccord dans une phrase (virgule, enumeration) | 0,10 s | `--targets` |
| raccord entre deux phrases | 0,14 s | `--targets` |
| raccord de section (changement de partie, appel a l'action) | 0,20 s | `--targets` |
| blanc interne au-dela duquel on coupe le segment | 0,50 s | `--max-internal-gap` |
| queue apres le dernier mot | 0,15 s | (profil) |

Le type de raccord se deduit de la ponctuation et des mots d'ouverture (« Deuxiemement »,
« Et enfin »...). Le resserrage n'est PAS uniforme, et c'est voulu : ces micro-respirations
font entendre une liste comme une liste. Ne jamais tout ramener a la meme valeur.

**Quand surcharger** (sur la ligne de commande, jamais dans le code) :

- **le montage sonne mou alors que les raccords sont serres** : sur un script derive d'une
  prise unique, une frontiere de ligne tombe sur une respiration, et le rythme passe sous
  le seuil de blanc INTERNE plutot que sous les cibles de raccord. Descendre ce seuil :

  ```powershell
  python tools/tighten_cuts.py "videos\<slug>\1_montage\edl_v3_timeline.json" --max-internal-gap 0.30
  ```

  Il ne coupe que du silence mesure : un trou rempli de son est refuse, donc descendre le
  seuil n'ouvre pas la porte a une coupe dans un mot. Descendre par paliers (0,30 puis
  0,20), et relire `cut_plan_report.md` a chaque fois ;
- **la personne veut plus d'air entre les phrases** :
  `--targets '{\"phrase\":0.18,\"section\":0.26}'`. Windows PowerShell retire les
  guillemets doubles a l'interieur d'un argument passe a un programme : il faut les
  echapper par `\"`, sinon l'outil recoit un JSON invalide ;
- **voir sans ecrire** : `--dry-run`.

Toujours relire `1_montage/cut_plan_report.md` apres un passage. Ce qu'on y cherche : une
extension « MOT ENTIER » anormalement longue par rapport aux autres (la transcription a
peut-etre invente un mot, et la coupe s'etend vers du vide), et les refus « trou non muet »
(un trou interne garde parce qu'il contient du son : voir `trous_gardes.py`, § 8).

### 5c. La QA du plan : `qa_montage.py`

En pre-vol (`--skip-ffmpeg`, sans master), il lit l'EDL, les transcripts et le SRT. Il
signale, par classe :

| classe | ce que c'est | a lire comme |
|---|---|---|
| `SCRIPT_JUMP` | un trou entre deux lignes montees | une phrase manque : est-ce assume ? |
| `DANGLING_LINE_END` | une ligne finit sur une virgule ou un mot suspendu | la phrase sonne coupee ? |
| `INTERNAL_PAUSE` | une pause interne longue pour un short, mesuree sur la coupe reelle | a ecouter ; voir plus bas |
| `TRUNCATED_SOURCE_GROUP` | la coupe tombe au milieu d'un groupe de souffle du rush | un mot voisin est-il entre ou sorti ? Normal sur une troncature voulue |
| `LONG_SEGMENT` | un segment de plus de 4 s | cache-t-il un faux depart ou un blanc ? |
| `NEARBY_REPETITION` | deux sous-titres voisins qui se repetent | une phrase montee deux fois ? |
| `DUPLICATE_LINE` | deux segments pour une meme ligne | normal apres une scission |
| `OVERLAPPING_CUT` | deux coupes qui rejouent le meme fragment | **doit etre a zero avant d'encoder** |
| `LONG_SUBTITLE_GAP` | un long silence sans sous-titre | un blanc dans le master |
| `AUDIO_SILENCE` | silence audio du master | ne tourne qu'avec le master (§ 7) |

Regles de lecture :

- **lire chaque classe, jamais le total.** Un rapport qui baisse globalement peut cacher la
  seule classe qui monte, et c'est souvent elle qui porte le vrai defaut. Comparer classe par
  classe avec le tour precedent ;
- **un drapeau reste vrai, l'arbitrage est editorial** : une pause de 0,6 s en ouverture se
  coupe (l'attention n'est pas acquise), la meme dans la chute finale peut rester. Mais la
  queue apres le DERNIER mot n'est pas une pause : c'est la fin, elle se raccourcit ;
- en pre-vol, il annonce en tete que `AUDIO_SILENCE` n'a pas ete teste. Ce n'est pas un
  zero, c'est une absence.

### 5d. Les controles qu'aucun rapport ne porte : `audit_edl.py`

Quatre controles : **provenance** (une ligne servie par un autre rush que prevu, qui fait
tomber des lignes en trou plus loin), **ordre** des rushs, **chevauchement** des coupes,
**fenetres** inversees (une coupe dont la fin passe avant le debut rend zero image, sans
erreur, et fausse tous les cumuls suivants). Code 0 propre, 1 defaut, 2 impossible a
mesurer.

Sur un short face camera, un « retour » au meme rush est souvent sain : comparer les
`in_ext` des lignes en cause. S'ils croissent, c'est un retour au FICHIER, pas dans le temps.

A relancer apres CHAQUE modification du script : supprimer une ligne libere une prise, et une
ligne courte ailleurs peut aussitot s'y faire servir.

## 6. Le punch-in (optionnel)

Le punch-in alterne le cadre entre large (100 %) et serre (~115 %) pour donner un changement
de plan toutes les 2 a 3 s. Il se pose **apres** le resserrage (bornes finales) et **avant**
l'encodage du master, et toujours avant l'habillage : il deplace le visage. La personne peut
le refuser ; c'est alors l'etape qu'on saute, sans rien changer d'autre.

**Un rush 1080x1920 n'a aucune marge** : a 115 % on recadre puis on re-etire, c'est un vrai
agrandissement sur une source deja compressee. Un tournage en 4K vertical rend le punch
gratuit. On ne monte pas l'amplitude « pour faire plus » : 100 % / 115 % suffit.

### 6.1 Mesurer le visage

```powershell
python tools/punch.py reperes "videos\<slug>\_media\rushes" --at 2,8,15,25
```

Il detecte le visage (YuNet ; `python tools/visage.py telecharger` si le modele manque) et
rend front, yeux, menton et centre `cx` a chaque instant, puis leur plage. Mesurer sur les
RUSHS, jamais sur un master punche, et sur assez d'instants pour couvrir toute la prise.

L'ancre du recadrage est **la ligne des yeux** : `--anchor <cx>,<yeux>` avec la mediane
mesuree. Sans ancre, le recadrage est centre sur le cadre et le visage derive a chaque
bascule. Ce qu'on vise : les yeux entre 33 et 42 % de la hauteur du cadre ; l'outil avertit
hors de cette fenetre.

`--borne-basse <y>` donne, si on veut exclure quelque chose du bas du cadre (un bureau, des
jambes), le zoom minimum par hauteur d'yeux visee.

### 6.2 Arbitrer l'amplitude en REGARDANT

```powershell
python tools/punch.py stills "videos\<slug>\_media\rushes\<rush>.mp4" --at 6.0 --scales 1.0,1.10,1.15,1.20 --anchor 540,700
```

Une planche par instant, dans `_media/captures/mods/`. La montrer a la personne : c'est
elle qui juge la nettete. Choisir un instant ou elle est en plan rapproche, la ou
l'agrandissement se voit le plus.

### 6.3 Poser le plan

```powershell
python tools/punch.py plan "videos\<slug>\1_montage\edl_v3_timeline.json" --anchor 540,700 --snap-s 99 --eviter-blancs
```

- `--snap-s 99` : les bascules ne se font que sur les VRAIES coupes du montage. Un short
  monte depuis des rushs coupe deja toutes les 3 s environ ; ajouter des bascules au milieu
  des segments doublerait les changements d'image et se lit comme « trop de jump cuts ».
  Si l'outil annonce des plans nettement plus longs que 3 s (prise unique peu coupee),
  relancer sans `--snap-s 99` : il coupera alors aussi dans les segments longs ;
- `--eviter-blancs` : une bascule collee a un blanc fait croire a un plan rapporte d'une
  autre prise. L'outil detecte toujours le cas et l'imprime ; cette option le deplace. La
  mettre sur tout short punche ;
- `--dry-run` pour voir le plan sans l'ecrire ; `--scale-in 1.12` pour une amplitude plus
  douce.

La cle `punch` est inerte tant qu'on n'encode pas avec `--honor-punch`. Toute nouvelle
passe de la boucle (§ 5) reecrit la timeline : le punch se repose apres.

## 7. Encoder le master

Une seule fois, quand le pre-vol est propre :

```powershell
python tools/assemble.py "videos\<slug>\1_montage\edl_v3_timeline.json" --honor-cuts --honor-punch
```

(sans `--honor-punch` si la personne ne veut pas de punch-in.)

Aucun chemin a donner, tout se deduit de l'emplacement de l'EDL :

| sortie | ou |
|---|---|
| `<slug>_montage.mp4` (le master) | `videos\<slug>\_media\masters\` |
| `<slug>_montage.srt`, `_rapport.md`, `_trous.md` | `videos\<slug>\1_montage\rapports\` |
| `<slug>_montage.words.json` (les mots en temps master) | `videos\<slug>\1_montage\transcripts-master\` |

Tous les rushs d'une video doivent sortir du meme reglage de camera (meme resolution, meme
cadence) : la concatenation l'exige. Le master garde la geometrie des rushs.

**Si un master existe deja, l'outil l'annonce avant de l'ecraser.** Si la personne l'a relu
et y a pose des notes, l'archiver d'abord sous un nom qui dit ce qu'il est :

```powershell
Move-Item "videos\<slug>\_media\masters\<slug>_montage.mp4" "videos\<slug>\_media\masters\<slug>_montage_v1-REMPLACE.mp4"
```

Puis la QA du master, qui ajoute le seul controle qui lit l'audio encode (`AUDIO_SILENCE`) :

```powershell
python tools/qa_montage.py "videos\<slug>\_media\masters\<slug>_montage.mp4"
```

C'est une confirmation : un defaut de metadonnees qui y apparait n'a pas ete re-planifie.

Quand `assemble.py` ecrit le master, **la page de review s'ouvre toute seule** dans le
navigateur (§ 9).

## 8. La QA ciblee : quand on entend un blanc ou un « euh »

`qa_montage.py` ne voit pas les blancs de moins d'une seconde, et rien ne voit un « euh » non
transcrit. Trois outils, chacun pour une question.

### Les blancs du master : `blancs.py`

```powershell
python tools/blancs.py <slug>
```

A lancer sur tout master avant de le faire relire. Il liste les plages calmes de plus de
0,10 s du master, les remonte au rush avec leur timecode, et les classe :

| | ce que c'est | ce qu'on en fait |
|---|---|---|
| **MORT** | le silence tombe au plancher du fichier | s'entend comme une panne : a couper |
| **HABITE** | un souffle, une amorce de consonne | s'entend comme une respiration : a garder |

Ce n'est pas la DUREE qui decide, c'est ce qui remplit le silence : deux blancs de meme
duree peuvent recevoir deux verdicts opposes. Donc :

- **ne jamais chercher a supprimer les souffles** : couper une respiration fabrique un blanc
  mort ;
- **sur un raccord, lire la MOYENNE et la duree, pas le minimum** : le micro-fondu pose a
  chaque raccord cree un creux tres bref qui peut faire basculer l'etiquette sans rien
  changer a l'oreille. Sur un silence INTERNE a un segment, le minimum garde son sens ;
- **le seuil MORT est absolu** : sur un micro de telephone au fond de piece plus bruyant,
  comparer le minimum du blanc au fond de la prise plutot qu'au seuil ;
- **un minimum tres bas (sous -100 dB) n'est plus un fond de piece** : le fichier est vide.
  Soit la transcription a invente un mot et la coupe s'est etendue vers lui, soit une tete
  de segment ouvre trop tot. Regarder l'enveloppe (ci-dessous).

Un blanc MORT interne se coupe par `--max-internal-gap` (§ 5b) ; un blanc HABITE de meme
duree se garde. Aucun seuil de duree ne separe les deux : c'est pour ca que le profil ne se
touche pas.

### Les trous internes gardes : `trous_gardes.py`

```powershell
python tools/trous_gardes.py <slug>
```

`blancs.py` ne regarde que ce qui tombe sous -45 dB ; un « euuuh » trainant vit vers -30 dB
et lui est invisible. Celui-ci liste les trous internes que le resserrage a decide de
GARDER, avec ce qu'il y a dedans (duree du son, niveau, dispersion). C'est le complement
exact de `blancs.py`. Sa sortie est une liste de CANDIDATS : seule l'oreille dit lequel est
une hesitation. Le reflexe quand la personne signale un « euh » que rien ne montre.

### Regarder l'audio avant de poser une borne : `enveloppe.py`

```powershell
python tools/enveloppe.py montre <slug> rush_003:12.30:13.10
python tools/enveloppe.py montre <slug> rush_003:12.30:13.10 --pas 0.01
python tools/enveloppe.py tetes <slug>
```

`montre` affiche l'enveloppe d'une plage du RUSH (en secondes du rush). Trois choses qu'il
montre et que rien d'autre ne dit :

- **un creux n'existe pas toujours** : entre deux mots lies, l'audio peut etre plat. Il n'y
  a alors pas de coupe propre possible, et il faut le dire plutot que de la promettre ;
- **un « euuuh » se VOIT** : un plateau long et plat entre deux mots ;
- **`--pas 0.01` devant une consonne** : a 20 ms, le silence bref d'une plosive (« D » de
  « Decouvre ») passe pour un blanc, et une coupe posee la fait entendre « couvre ».

`tetes` mesure le vide en tete de CHAQUE segment : il repond a « est-ce un cas isole ou une
classe ? ». Un cas isole se corrige dans `edl_v3.json` ; jamais en changeant un reglage.

### Une borne a corriger a la main

Mesurer avec `enveloppe.py montre`, corriger `start` / `end` de la ligne dans `edl_v3.json`,
relancer la boucle du § 5 depuis le debut, puis verifier dans `cut_plan_report.md` et le SRT
que la borne a tenu : le resserrage la defait en partie s'il n'y trouve pas de silence.

## 9. La review a la frame, et les retours

La page de review s'ouvre seule quand le master est ecrit. La personne y arrete le lecteur
sur une image et y ecrit une note (« trop long ici », « pas assez rythme », « on entend un
euh »). Les notes sont enregistrees dans `videos\<slug>\2_habillage\retours\`.

Si la page ne s'est pas ouverte, ou pour la rouvrir :

```powershell
python tools/retours.py <slug> --ouvrir
```

### Lire les notes

```powershell
python tools/retours.py <slug>
```

Chaque note vient avec l'instant, le MOT prononce a cet instant et la phrase autour. Code 0 :
aucun retour ouvert ; 1 : des retours attendent ; 2 : impossible de savoir.

### Une note = une demande

Avant de corriger, **reecrire chaque note en une demande par ligne**. Une phrase qui contient
« et », « alors que » ou une comparaison en porte souvent deux (« c'est trop long et ca
manque de peps au debut » = raccourcir + retravailler l'ouverture). Cocher chaque demande
contre ce qui a ete fait. Reparer la plus visible et clore le retour laisse l'autre en
plan.

Quand une note designe une chose par une propriete (« la phrase qui ne se termine pas »),
compter combien d'objets la portent. S'il y en a plusieurs, demander laquelle.

Resoudre chaque note en **ancre texte** (la phrase qu'elle vise) avant de re-monter : apres
re-encodage, les instants ont bouge, les phrases non.

### Ce que chaque note demande, et ou ca se traite

| la note dit | d'abord | puis |
|---|---|---|
| « trop long », « ca traine » | relire le script (les quatre questions du § 3) : ce qui se retire est presque toujours de la parole, pas du silence | retirer les lignes, relancer depuis `select_takes` |
| « pas assez rythme », « mou » | `blancs.py` et le `cut_plan_report.md` : ou est le temps mort ? | `--max-internal-gap 0.30`, puis le punch-in s'il n'y en a pas ; jamais une retouche du profil |
| « un blanc a X s » | `blancs.py` : MORT ou HABITE ? interne ou raccord ? | MORT interne : `--max-internal-gap` ; HABITE : expliquer pourquoi on le garde, ou demander |
| « un euh », « j'hesite » | `tics.py` (classe ?) puis `trous_gardes.py` | `--classes BORD,SCISSION,COURTE`, ou scinder la ligne dans le script |
| « un mot est coupe », « il manque un mot » | `fuites_edl.py`, puis `enveloppe.py montre` sur la fin de la ligne | etendre la borne dans `edl_v3.json` |
| « mauvaise prise », « je regarde ailleurs » | les images des deux prises (§ 4) | changer de prise dans `edl_v3.json` |
| « un mot de trop », « on entend " on " » | `fuites_edl.py` | resserrer la borne dans `edl_v3.json` |

Corriger plus serre n'est jamais la reponse par defaut a « c'est mou » : le plus souvent, ce
qui manque est un retrait EDITORIAL (une annonce, une incise, une redite), que seul le
script peut faire.

### Fermer le retour

Apres avoir applique, re-monte (§ 5 puis § 7) et verifie :

```powershell
python tools/retours.py <slug>
python tools/retours.py <slug> --traite
```

**Relancer `retours.py <slug>` juste avant `--traite`**, et comparer le nombre de notes a
celles qu'on a traitees : la personne relit souvent en direct, et `--traite` clot TOUT le
fichier, y compris une note posee pendant qu'on corrigeait la precedente. Une note arrivee
apres coup : `--rouvrir`.

## 10. Passer a l'habillage

Le master est pret quand la personne l'a valide. On passe alors a `/habillage` avec :

- `videos\<slug>\_media\masters\<slug>_montage.mp4` : la source video definitive ;
- `videos\<slug>\1_montage\rapports\<slug>_montage.srt` : les sous-titres cales sur le
  master ;
- `videos\<slug>\1_montage\transcripts-master\<slug>_montage.words.json` : les mots en temps
  master, d'ou se regenerent les sous-titres et s'ancrent les animations ;
- une ligne qui dit ce qui a ete accepte en connaissance de cause (un trou assume, une pause
  gardee, un tic `COLLE` laisse en place), pour que l'habillage ne le « repare » pas.

Si un SRT karaoke est a regenerer depuis les mots :

```powershell
python tools/rechunk_karaoke.py "videos\<slug>\1_montage\transcripts-master\<slug>_montage.words.json" -o "videos\<slug>\1_montage\rapports\<slug>_montage.karaoke.srt"
python tools/cues_phrases.py <slug>
```

`cues_phrases.py` verifie qu'aucun sous-titre ne melange deux lignes du montage. Sur un
script DERIVE, une cue qui enjambe une virgule n'est pas un defaut : lire ses signalements
avant tout `--appliquer`.

**Une relecture de sous-titres se fait dans un fichier NOMME**, pas dans
`<slug>_montage.srt` : une relance d'`assemble.py` l'ecrase sans prevenir.

On ne lance pas l'habillage sur un montage qui bouge encore : un recoupage apres l'habillage
coute bien plus cher qu'avant.

## Conseils de tournage au telephone

A donner a la personne avant son prochain tournage, en quelques lignes. Ce sont ceux qui
changent le resultat de cette chaine :

- **1 seconde de silence avant et apres chaque prise.** C'est la marge ou le resserrage
  pose ses coupes ; sans elle il coupe dans la premiere syllabe.
- **Refaire la phrase ENTIERE plutot que reprendre au milieu.** Une reprise partielle oblige
  a recoller deux morceaux de prise, et le raccord s'entend souvent.
- **Dire « top » apres une bonne prise, « on refait » apres une mauvaise.** Le selecteur
  les lit.
- **Filmer en 4K vertical si le telephone le permet** : le punch-in devient gratuit en
  nettete.
- **Le micro pres de la bouche** (micro-cravate ou telephone proche) : moins d'echo, des
  silences plus francs, donc des coupes plus propres.
- **Le meme reglage de camera pour tous les rushs**, et **l'objectif dans les yeux** en
  refaisant une phrase : un regard qui part ne se rattrape pas au montage.
- **Une accroche qu'on veut en ouverture se filme dans son propre rush**.

## Definition de fini

Le montage est fini quand :

- `verifier_poste.py --pour montage` a rendu 0, et le moteur des transcripts est connu
  (ElevenLabs, ou Whisper annonce a la personne avec ce que ca coute) ;
- `takes_packed.md` a ete lu, `final_transcript.json` relu comme un script, chaque trou de
  `rapport_selection.md` explique ou assume ;
- `fuites_edl.py` rend 0 (ou ne laisse que des `ECART DE LEXIQUE`), `audit_edl.py` rend 0
  (ou ses signalements sont expliques), `OVERLAPPING_CUT` est a zero ;
- la boucle de pre-vol a tourne jusqu'a des rapports propres AVANT l'encodage, et
  `cut_plan_report.md` a ete relu ;
- le master vient d'un `assemble.py ... --honor-cuts` (jamais du premier plan brut), avec
  `--honor-punch` si un punch-in a ete pose ;
- `qa_montage.py` a tourne sur le master, et `blancs.py` aussi ;
- la review a ete ouverte, chaque note traitee demande par demande, et `--traite` pose apres
  une derniere relecture des notes ;
- le passage a `/habillage` liste le master, le SRT, les mots en temps master et ce qui a
  ete accepte.

## Erreurs a eviter

Les regles generales sont en tete de ce fichier. S'y ajoutent :

- **relancer `tighten_cuts.py` ou `tics.py --appliquer` sur une timeline qui les a deja
  subis** : on repart d'`edl_v3.json` ;
- **retoucher un `words.json` pour corriger le montage** : les outils lisent
  `transcript.json` en priorite et re-calculent la duree des mots, la retouche est ignoree
  en silence. Les bornes se corrigent dans `edl_v3.json` ;
- **choisir une prise parce qu'elle colle au texte** : une prise lue ou chuchotee colle
  souvent trop bien, et une prise retenue par son marqueur peut avoir le regard ailleurs ;
- **supprimer les souffles**, ou **resserrer tous les raccords a la meme valeur** ;
- **repondre a « c'est mou » en serrant plus** avant d'avoir cherche le retrait editorial ;
- **corriger la plus visible des demandes d'une note et clore le retour**, ou `--traite`
  sans relire les notes juste avant ;
- **ecraser un master relu sans l'archiver**, quand ses notes doivent encore servir ;
- **ecrire un JSON ou un texte accentue par une commande shell** : utiliser l'outil
  d'ecriture de fichier.

## Ce qui n'existe pas ici

Ce depot monte des shorts tournes au telephone, et rien d'autre. Pas d'etalonnage dans cette
chaine, pas de juge visuel automatique (ni Gemini ni autre), pas de video longue, pas de
variantes de hook, pas de nettoyage de voix externe, pas de re-decoupe d'un master deja
fini : un changement de montage repart de l'EDL. Si une de ces demandes arrive, le dire a la
personne plutot que d'improviser un outil.
