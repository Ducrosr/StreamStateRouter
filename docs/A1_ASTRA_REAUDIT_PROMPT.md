# Prompt Astra — checkpoint final A1

Tu agis comme **auditeur technique senior / architecte logiciel** sur StreamStateRouter.

Ta mission est un **ré-audit strictement ciblé sur A1**.

Tu ne dois PAS implémenter de correction.
Tu ne dois PAS modifier de fichier.
Tu ne dois PAS créer de commit.
Tu ne dois PAS pousser.
Tu ne dois PAS merger.
Tu ne dois PAS rendre la PR ready.

Tu peux lire le dépôt, la PR, les commits, les tests, la CI et exécuter des reproductions temporaires hors dépôt.

## Dépôt

`Ducrosr/StreamStateRouter`

## Base A0 déjà fermée

`6a288052001cd01cf7283251bc506d6219ea721c`

A0 est déjà approuvé. Ne le ré-audite que pour détecter une régression introduite par A1.

## PR A1

PR :

`#152 — A1: durable fade helper ownership and safe recovery`

Branche :

`feat/a1-helper-recovery`

HEAD exact à auditer :

`0682913ed2bd847fa71db6e0bb37d2ccd037eb45`

La PR doit rester draft.

## CI connue sur le HEAD exact

Tests run :

`36646679491`

Résultat :

- windows SUCCESS
- streamdeck SUCCESS
- 677 tests
- 1 skipped
- Ruff SUCCESS

CodeQL run :

`36646679458`

Résultat :

- python SUCCESS
- javascript-typescript SUCCESS
- actions SUCCESS

Vérifie toi-même ces résultats.

## But A1 rappelé

A1 doit fournir :

- ownership durable et prouvé des helpers de fade ;
- manifeste persistant séparé du journal runtime ;
- write-ahead obligatoire avant tout effet temporaire ;
- recovery idempotent ;
- aucune création/adoption/suppression depuis cleanup ;
- compatibilité prudente avec les obligations v2 ;
- aucune régression A0 ;
- exclusion capture/import uniquement pour les helpers réellement prouvés ;
- zéro input utilisateur supplémentaire dans le parcours normal.

## Architecture implémentée

### Manifeste

`helper-manifest.json`, schéma 1.

Identité d'un helper :

- purpose `layout_fade`;
- helper_id aléatoire UUID4 hex canonique généré par SSR ;
- connexion OBS host/port ;
- Scene Collection ;
- UUID input ;
- alias source ;
- source kind ;
- filtre exact `[SSR] Layout Fade::<helper_id>`;
- kind `color_filter_v2`;
- version/session de création ;
- état `prepared` ou `observed`;
- réglages non temporaires observés.

Le parseur refuse les contradictions :

- helper_id invalide ;
- purpose différent ;
- filter_name ne correspondant pas au helper_id ;
- filter_kind inattendu ;
- types persistés invalides.

Le manifeste est écrit atomiquement.

### Journal runtime

`cleanup_schema = 3`.

Lecture compatible avec les anciennes obligations ; un layout_fade v2 sans helper_id reste legacy/non prouvé.

Un runtime.json :

- futur ;
- illisible ;
- corrompu ;
- current-schema mais obligation incompréhensible ;

est préservé et bloque la réécriture destructive.

Une obligation live non persistable fait échouer le checkpoint au lieu d'être supprimée silencieusement.

### Ordre write-ahead

Avant la première mutation temporaire :

1. résoudre cible ;
2. vérifier contexte ;
3. préparer identité ;
4. persister manifeste ;
5. persister obligation ;
6. seulement ensuite Create / Enable / Settings.

### Création normale

`CreateSourceFilter` n'existe que dans la préparation normale.

Une réponse perdue après Create provoque une observation de l'identité générée ; aucun second Create n'est envoyé tant que l'issue est incertaine.

Un filtre existant correspondant à une identité seulement `prepared` n'est jamais adopté automatiquement.

### Réutilisation

Un helper observed réutilisable :

- vérifie kind et réglages non temporaires ;
- est remis à opacity=1 avec readback avant activation si nécessaire ;
- seulement ensuite peut être enabled ;
- les réponses perdues sont résolues par readback lorsque possible.

### Mutations temporaires

`_set_source_opacity()` exige un helper actif préparé ; il ne crée jamais.

A0 reste l'autorité d'admission :

- scene/group/composite : direct ;
- type inconnu/stale : direct ;
- source partagée : direct ;
- inventaire incomplet : direct ;
- visibilité inconnue/runtime-owned : direct.

### Cleanup

Le recovery peut seulement :

- observer ;
- neutraliser opacity ;
- vérifier ;
- désactiver ;
- vérifier ;
- acquitter l'obligation.

Il ne peut jamais :

- Create ;
- adopter ;
- supprimer.

Un helper absent avec contexte/targe prouvés et inventaire complet est terminal.

Source absente/recréée, collection étrangère, contexte douteux, readback incertain ou helper modifié extérieurement gardent l'obligation sans mutation aveugle.

### Capture/import

- helper observed + identité/settings compatibles : `owned_helper`, exclu du métier ;
- helper prepared : ambigu ;
- lookalike : ambigu ;
- helper proven mais réglages non temporaires modifiés : ambigu ;
- helper proven mais settings illisibles : ambigu ;
- filtre utilisateur normal : conservé.

Les ambiguïtés produisent un warning et ne deviennent pas automatiquement des actions exécutables.

## Correctifs supplémentaires trouvés pendant l'implémentation

Vérifie spécialement ces zones.

### 1. Recovery journal futur/corrompu

Ancien comportement possible : perte d'information par normalisation/réécriture.

Nouveau comportement : fail-closed + fichier préservé.

### 2. Hidden -> visible + réponse opacity=0 perdue

En mode fade, une réponse perdue pouvait laisser la cible hidden.
En move_fade, elle pouvait échapper au cleanup immédiat.

Nouveau comportement : fallback direct de visibilité + obligation/cleanup conservés.

### 3. Move-fade préparation incertaine

Une réponse perdue sur enable pendant `_prepare_fade_filter()` pouvait laisser le cleanup au retry ultérieur.

Nouveau comportement : toute source dont la préparation est tentée est incluse dans le cleanup immédiat ; une préparation pré-mutation sans obligation reste un no-op.

### 4. Réutilisation helper avec opacity non neutre

Ancien risque : réactiver un helper disabled portant une ancienne opacity.

Nouveau comportement : opacity=1 + readback avant enable.

### 5. Capture d'un helper modifié

Ancien risque : identité historiquement prouvée suffisante pour l'exclure comme owned malgré un réglage non temporaire modifié.

Nouveau comportement : helper ambigu + warning.

### 6. Validation du manifeste

Les champs d'ownership contradictoires ou coercés ne sont plus acceptés comme preuve.

## Tests/fault injection à vérifier

Vérifie au minimum :

- manifeste écrit / journal échoué ;
- journal écrit / Create non envoyé ;
- Create accepté / réponse perdue ;
- crash après création ;
- réponse opacity perdue ;
- readback impossible ;
- neutralisation incertaine ;
- crash entre neutralisation et disable ;
- réponse disable perdue ;
- retrait durable d'obligation échoué ;
- helper absent ;
- source absente ;
- même alias / nouvel UUID ;
- UUID absent ou ambigu ;
- collection étrangère ;
- collision utilisateur ;
- manifeste absent/corrompu/futur/contradictoire ;
- duplicate identities/obligations ;
- v2 legacy ;
- changement externe non temporaire ;
- reconnexion ;
- shutdown avec backlog ;
- recovery répété ;
- capture/import ;
- absence structurelle de Create dans cleanup.

Pour chaque issue incertaine, vérifier :

- aucun filtre non prouvé muté ;
- aucun Create depuis recovery ;
- aucun doublon après issue incertaine ;
- obligation conservée jusqu'à état terminal ;
- géométrie/visibilité directes préservées.

## Point explicitement réservé à ton arbitrage

Cas :

1. manifeste `prepared` persisté ;
2. obligation persistée ;
3. CreateSourceFilter peut avoir été accepté ;
4. crash avant `mark_observed()`.

Le code actuel reste conservateur :

- `prepared` ne prouve pas l'ownership d'un filtre après restart ;
- recovery ne l'adopte pas et ne le mute pas ;
- aucun effet temporaire d'opacité n'a pu être envoyé par le chemin normal avant `mark_observed()`;
- un filtre neutre potentiellement orphelin peut donc rester et bloquer un nouveau fade jusqu'au diagnostic.

Décide explicitement :

A. comportement correct pour A1 ;
B. obligation doit rester en quarantaine plutôt qu'être acquittée ;
C. une preuve supplémentaire permet une promotion sûre prepared -> observed ;
D. autre correction minimale.

N'autorise aucune adoption fondée sur le nom seul.

## Validation Windows/OBS

Elle reste à exécuter sur collection jetable.

Ne bloque pas ton audit statique/fake-OBS sur l'absence de cette preuve, mais distingue clairement :

- code/test acceptable ;
- validation réelle encore requise avant fermeture/release.

Scénarios prévus :

- input normal fade/move_fade ;
- composite sans helper ;
- kill/restart pendant fade ;
- helper absent ;
- collision ;
- source recréée même nom ;
- collection étrangère ;
- reconnect ;
- shutdown backlog ;
- helper final opacity 1 + disabled ;
- zéro Create recovery.

## Outils de validation préparatoires

La branche `prep/a1-helper-recovery` contient désormais des corrections indépendantes :

- inspecteur OBS limité aux Get* ;
- mode partageable anonymisé par défaut ;
- `--raw-local` pour détails locaux ;
- collecteur partageable sans raw logs/paths/command lines/identifiants OBS détaillés ;
- `-RawLocal` explicite pour données sensibles.

Ces outils ne font pas partie du diff moteur de PR #152.

## Sortie attendue

### 1. SHA réellement audité

Doit être :

`0682913ed2bd847fa71db6e0bb37d2ccd037eb45`

### 2. CI vérifiée

### 3. Contrôle du périmètre A1

Signale toute dérive A2/B0/UI/Stream Deck.

### 4. Provenance / ownership

Verdict + défauts éventuels.

### 5. Write-ahead / persistance

Verdict + défauts éventuels.

### 6. Recovery / no-create / idempotence

Verdict + défauts éventuels.

### 7. Legacy v2 / corruption / incompatibilités

Verdict.

### 8. Capture/import

Verdict.

### 9. Régressions A0

Verdict.

### 10. Cas prepared après crash

A/B/C/D + justification.

### 11. Tests manquants éventuels

Uniquement ceux qui changent réellement la confiance.

### 12. Validation Windows/OBS encore nécessaire

Liste exacte.

### 13. Verdict final

Choisir uniquement :

**APPROUVER A1 POUR VALIDATION RÉELLE / FERMETURE**

ou

**BLOQUER A1**

Si BLOQUER, pour chaque défaut :

- sévérité ;
- fichier/symbole ;
- cause ;
- reproduction déterministe ;
- impact ;
- correction minimale ;
- test requis.

Si APPROUVER, indique clairement si SOL peut préparer A2/B0 sans encore commencer les mutations de leur lot avant le gate convenu.

Ne code rien.
Ne merge rien.
