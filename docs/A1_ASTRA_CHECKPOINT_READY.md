# A1 — Checkpoint Astra prêt à auditer

## Source de vérité

- Dépôt : `Ducrosr/StreamStateRouter`
- Lot : **A1 — durable fade helper ownership / safe recovery**
- Base A0 approuvée : `6a288052001cd01cf7283251bc506d6219ea721c`
- Branche : `feat/a1-helper-recovery`
- HEAD à auditer : `0682913ed2bd847fa71db6e0bb37d2ccd037eb45`
- PR : **#152**, draft
- Ne pas merger / ne pas rendre ready avant verdict Astra + validation réelle Windows/OBS.

## CI du HEAD exact

GitHub Actions sur `0682913ed2bd847fa71db6e0bb37d2ccd037eb45` :

- Tests / windows : **SUCCESS**
  - **677 tests**
  - **1 skipped**
  - Ruff : **SUCCESS**
- Tests / streamdeck : **SUCCESS**
- CodeQL / python : **SUCCESS**
- CodeQL / javascript-typescript : **SUCCESS**
- CodeQL / actions : **SUCCESS**

Runs :

- Tests : `36646679491`
- CodeQL : `36646679458`

## Diff A1 vs A0

Aucun A2/B0/audio/HDR/Stream Deck ajouté.

Fichiers modifiés :

- `main.py`
- `stream_state_router/importers/scene_collection.py`
- `stream_state_router/obs/fade_helpers.py`
- `stream_state_router/obs/layouts.py`
- `stream_state_router/services/recovery.py`
- `tests/test_current_state_capture.py`
- `tests/test_fade_helpers.py`
- `tests/test_importers.py`
- `tests/test_layouts.py`
- `tests/test_recovery.py`
- `tests/test_runtime.py`

## Garanties implémentées

### Ownership durable

- `helper-manifest.json` séparé de `runtime.json`.
- Schéma manifeste 1.
- `helper_id` aléatoire.
- Nom : `[SSR] Layout Fade::<helper_id>`.
- Binding par contexte OBS + collection + UUID input + kind.
- Un helper maximum par source qualifiée.
- `prepared` et `observed` distincts.
- Un état `prepared` ne prouve jamais la propriété d'un filtre existant.
- Rename avec même UUID met à jour l'alias ; même nom avec nouvel UUID = cible distincte.

### Write-ahead

Ordre contrôlé :

1. cible résolue ;
2. manifeste persistant ;
3. obligation cleanup durable ;
4. seulement ensuite Create/Enable/Settings OBS.

L'échec du manifeste ou du journal bloque la première mutation OBS.

### Recovery

Le cleanup peut uniquement :

- observer ;
- neutraliser `opacity` ;
- vérifier ;
- désactiver ;
- vérifier ;
- acquitter l'obligation.

Il ne peut jamais :

- créer ;
- adopter ;
- supprimer un filtre.

Le helper manquant avec contexte fiable est terminal sans création.

Les cas ambigus conservent l'obligation et ne mutent pas OBS :

- source absente ;
- UUID remplacé ;
- collection étrangère ;
- manifeste absent/corrompu/contradictoire ;
- helper renommé/lookalike ;
- modification externe des réglages non temporaires ;
- duplicate obligation / identity.

### Recovery journal

- `cleanup_schema = 3`.
- lecture compatible v2, mais v2 reste legacy/non prouvé.
- écriture atomique temp + flush/fsync + replace.
- schéma futur, JSON corrompu ou obligation schema 3 incompréhensible :
  - fichier préservé ;
  - démarrage du journal refusé ;
  - aucune réécriture vide.
- obligation live impossible à normaliser :
  - écriture refusée fail-closed ;
  - aucun effet temporaire ne doit démarrer sur un journal tronqué.

### Préparation / réutilisation normale

- un helper réutilisable est remis à `opacity=1` avec readback avant réactivation ;
- une réponse perdue pendant cette neutralisation est résolue par observation, sans seconde écriture aveugle ;
- en `move_fade`, toute préparation tentée reste éligible au cleanup immédiat si un effet OBS devient incertain.

### Réponses perdues

- Create appliqué/réponse perdue : observation de l'identité générée, jamais de second Create.
- neutralisation/réponse perdue : readback obligatoire avant acquittement.
- disable/réponse perdue : readback obligatoire.
- fade hidden -> visible/réponse opacity=0 perdue :
  - fallback de visibilité directe ;
  - cleanup conservé ;
  - état final visible.
- move_fade dans le même cas :
  - cible reste dans `touched_fades` ;
  - neutralisation immédiate conservée.

### Capture/import

- helper prouvé et settings compatibles : exclu des réglages métier ;
- helper prouvé mais réglages non temporaires modifiés : ambigu + warning ;
- helper prouvé mais settings illisibles : ambigu + warning ;
- filtre utilisateur : conservé ;
- lookalike ambigu : preuve conservée + warning, jamais converti automatiquement en action exécutable ;
- le current-state capture réutilise la même classification.

### Runtime

- obligations fade importées/exportées avec les autres cleanup ;
- retry au reconnect OBS ;
- invalidation de session ;
- backlog fade conservé au shutdown ;
- mutations OBS toujours dans le runtime sérialisé.

## Fault-injection couverte

Tests dédiés pour :

- manifeste écrit / journal échoué ;
- journal écrit / Create non envoyé ;
- Create accepté / réponse perdue ;
- restart après Create sans second Create ;
- perte réponse opacité ;
- perte réponse opacité lors hidden -> visible en fade et move_fade ;
- readback de cleanup indisponible ;
- perte réponse neutralisation ;
- interruption entre neutralisation et désactivation ;
- perte réponse désactivation ;
- retrait durable d'obligation échoué ;
- helper absent ;
- source absente ;
- même nom / nouvel UUID ;
- UUID absent ou ambigu ;
- collection étrangère ;
- collision utilisateur / legacy ;
- manifeste absent, corrompu, futur, contradictoire ;
- identités dupliquées ;
- obligation v2 ;
- modification externe du helper ;
- reconnexion ;
- shutdown avec backlog ;
- recovery répété/idempotent ;
- transport de cleanup qui lève immédiatement sur tout `CreateSourceFilter` ;
- capture/import excluant uniquement les helpers prouvés.

## Défauts supplémentaires trouvés pendant l'implémentation Sol

### Journal futur/corrompu

Un ancien `RuntimeMarker.start()` pouvait perdre un journal qu'il ne comprenait pas en le réécrivant sous le schéma courant. Corrigé fail-closed.

### Fade-in avec réponse perdue

Une réponse perdue après application réelle de `opacity=0` pouvait laisser le target hidden en mode fade, ou échapper au cleanup immédiat en move_fade. Corrigé et couvert en transition complète.


### Preuve d'ownership trop permissive

Le manifeste acceptait des combinaisons contradictoires `helper_id/purpose/filter_name/filter_kind` et certaines identités numériques pouvaient être coercées depuis des types JSON inattendus. Le schéma A1 exige maintenant l'identité générée canonique et des types persistés stricts.

### Import d'un helper modifié

Un helper historiquement prouvé mais dont les réglages non temporaires avaient été modifiés pouvait encore être classé owned pendant capture/import. Il est désormais mis en quarantaine comme ambigu et signalé.

### Réutilisation avec opacité réservée non neutre

Un helper disabled mais laissé avec une ancienne opacité pouvait être réactivé avant neutralisation. La préparation garantit désormais `opacity=1` + readback avant enable.

## Point volontairement laissé conservateur pour Astra

Fenêtre :

- manifeste `prepared` écrit ;
- obligation écrite ;
- `CreateSourceFilter` a pu réussir ;
- crash avant `mark_observed()`.

Au redémarrage, A1 n'adopte pas le filtre uniquement parce que son nom généré correspond. Il ne le mute pas comme owned.

Conséquence possible :

- un filtre neutre, généré mais non prouvé, peut rester dans OBS ;
- il peut empêcher un nouveau fade automatique jusqu'à réparation/diagnostic.

Raison :

- aucun effet temporaire d'opacité ne peut être envoyé avant `mark_observed()` dans le chemin normal ;
- adopter après restart sur nom seul violerait la règle de non-adoption du recovery.

À Astra de confirmer que ce compromis conservateur est correct pour A1 ou de définir une preuve supplémentaire admissible.

## Validation réelle encore requise

Sur collection OBS jetable, avec exécutable correspondant au HEAD :

- input normal + fade ;
- scene/group/composite : zéro helper ;
- kill/restart pendant fade ;
- helper absent au restart ;
- collision utilisateur ;
- source recréée même nom ;
- collection étrangère ;
- reconnect ;
- shutdown avec backlog ;
- helper final : opacity 1 + disabled ;
- vérifier zéro Create pendant recovery.

Les pertes de réponse sont déjà couvertes par transport simulé.

## Budget UX

- 0 nouveau champ simple ;
- 0 UUID demandé ;
- 0 schéma demandé ;
- 0 décision quotidienne autour des helpers ;
- ambiguïtés -> diagnostic + fallback sûr.

## Verdict attendu d'Astra

Choisir :

- **APPROUVER A1 pour validation réelle / fermeture**, ou
- **BLOQUER A1** avec reproduction déterministe, impact et correction minimale.

Ne pas commencer A2 avant ce checkpoint.
