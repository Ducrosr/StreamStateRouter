# Historique

## 2.1.0 — routage déclaratif gardé et distribution durcie

- Ajout du planner/observateur/catalogue déclaratifs avec DesiredState et diagnostics de migration.
- Executor opt-in sérialisé sur `SSR-Router`, ticket mono-usage et garde-fous de contexte.
- Exécution déclarative limitée à la visibilité de Scene Items, au mute et au volume d'inputs.
- Dernier contrôle runtime atomique avec le `Set*`, readback ciblé et sweep final de convergence.
- Binding physique par UUID pour les inputs et fingerprint d'occurrences pour les Scene Items.
- `input_volume_db` strict : cible explicite finie, plage `[-100,+26]`, tolérance float32 partagée `1e-4 dB`.
- Réconciliation des domaines par défaut non gérés harmonisée entre planner et dispatcher legacy.
- Préservation des résultats partiels et `replan_required` après interruption, dérive de contexte ou observation invalide.
- Dépendances Python de release figées, `npm ci`, CodeQL, provenance et vérification SHA-256 stricte.
- Smoke complet portable + installateur avec install/run/uninstall.
- Version du package Stream Deck alignée sur la version SSR.
- Validation réelle OBS 32.2.2 du chemin volume par UUID, mutation `0 -> -1 dB`, readback puis restauration exacte.

## 2.0.14 — feuille de route d'architecture Astra

- **A2 — IDs OBS frais** : les actions classiques de visibilité résolvent désormais le `sceneItemId` à chaque mutation au lieu de conserver un cache durable susceptible de cibler un autre item après une modification structurelle d'OBS.
- **A6 — Fondu borné** : la récupération du filtre de fondu n'est plus récursive ; seules les absences confirmées déclenchent une création/réactivation, avec une unique relance et un nettoyage compensatoire suivi dans le runtime.
- **A7 — Validation homogène** : rejet des `NaN`/`Infinity`, regex invalides, types de conditions incohérents, paramètres d'actions incomplets et transitions invalides.
- **A8 — Win32** : signatures ABI `ctypes` explicites pour user32/kernel32 et le mutex d'instance unique.
- **A16 — Build/release** : version Python unique, noms d'artefacts cohérents, contrôle des codes de sortie, smoke test de l'EXE, vérification tag/version, dépendances Stream Deck directes verrouillées et provenance de build enregistrée.
- **A3 — Orchestration OBS** : les mutations live provenant du runtime, de l'UI et de l'API passent par une orchestration sérialisée, avec résultats structurés.
- **A1 — État désiré/acquitté** : le runtime distingue l'état demandé de l'état effectivement appliqué par OBS, y compris le scénario A → B différé → C.
- **A4 — Obligations de nettoyage** : les nettoyages temporaires restent explicitement suivis et retentables après erreur, reconnexion et arrêt.
- **A5 — Preview/Undo** : les contextes de restauration sont invalidés quand le contexte OBS change et les restaurations partielles sont signalées au lieu d'être considérées réussies.
- **A9 — Identité d'activation** : cibles identifiées exactement par conteneur/type/source, propriété de visibilité explicite et compatibilité legacy uniquement lorsqu'elle reste non ambiguë.
- **A10 — Capture** : une recapture remplace complètement l'état prévu du profil et valide l'héritage/les modules supprimés plutôt que de conserver des fragments obsolètes.
- **A11 — Diagnostics Layout** : compare/validate réutilisent le même plan logique que l'application réelle, notamment pour héritage, exclusions et visibilité runtime-owned.
- **A13 — Révisions de configuration** : distinction entre brouillon, version enregistrée et version réellement appliquée au runtime.
- **A14 — API / Stream Deck** : acquittement explicite des commandes et paramètres de connexion configurables.
- **A15 — Sauvegardes/export** : sauvegardes atomiques, rotation bornée et export partageable plus sûr.
- **A17 — Observabilité** : diagnostics corrélés par décision, domaines demandés/appliqués/échoués, détails d'erreur et compteurs de requêtes OBS ; état UI/API « application incomplète ».
- **A12 — Performance** : scans topologiques légers mutualisés et écritures de layout inutiles supprimées tout en conservant la stabilisation des groupes.
- **A18 — Explication de décision** : explication de routage en lecture seule pour MATCH/IGNORE/fallback, debounce, héritage, conditions et opérations prévues, sans mutation OBS.

## 2.0.13 — sérialisation et acquittement des activations temporaires OBS

- Les commandes d'activation Qt sont désormais asynchrones et consommées par le worker `SSR-Router`.
- Tick, trigger, stop, reset cooldown, réconciliation et nettoyage d'arrêt partagent le même contexte d'exécution.
- L'arrêt invalide les commandes en attente et attend aussi les dispatches OBS différés déjà engagés avant qu'un runtime de remplacement puisse démarrer.
- Les hides non acquittés sont structurés par politique, cible exacte et Scene Collection, puis retentés avec backoff borné.
- Un show au résultat incertain crée un hide compensatoire ; la politique reste bloquée jusqu'à acquittement.
- En mode exclusif, une nouvelle cible n'est pas affichée si un concurrent n'a pas pu être masqué avec certitude.
- Les absences OBS confirmées sont distinguées des erreurs de requête ou de transport incertaines.
- Les opérations anciennes ne sont jamais rejouées dans une nouvelle Scene Collection.
- Les cibles utilisent une identité exacte `container/container_kind/source`; le mode source seule reste compatible uniquement s'il est non ambigu.
- Les enfants directs seulement sont proposés par défaut ; les conflits ancêtre/descendant et les doublons exacts sont rejetés.
- Les mutations de visibilité d'activation utilisent une résolution fraîche du `sceneItemId` sans invalider globalement le cache de transforms.
- L'éligibilité effective est centralisée et le statut UI reste un snapshot sans I/O OBS.
- Les nombres non finis sont rejetés en config et dans le scheduler ; les poids très élevés sont normalisés avant sommation.
- La simulation utilise une copie de la politique et un worker séparé, avec empreinte de configuration dans le résultat.
- Les sémantiques historiques « perte d'éligibilité efface le cooldown » et « visibility_owner=runtime persisté survit à la suppression de politique » sont documentées et couvertes sans changement.
- Correction du `NameError` dans `tests/test_config.py` par import de `build_activation_policies`.


## 2.0.12 — observabilité et validation bêta du scheduler

- Affichage de l'état runtime d'une politique : inactif, éligible, déclenché/visible ou cooldown.
- Affichage de la raison d'éligibilité OBS et du dernier événement.
- Journal de diagnostic par politique : tirages réussis/échoués, déclenchements, masquages, cooldowns, blocages, erreurs et fail-safe.
- Les déclenchements manuels respectent désormais le cooldown par défaut au lieu de le contourner.
- Ajout d'une réinitialisation runtime globale qui remet le scheduler à zéro et masque toutes les sources temporaires.
- Ajout d'une simulation déterministe par seed, sans effet sur OBS, l'état runtime ou le RNG live.
- La simulation rapporte succès de chance, échecs, blocages sans cible et distribution pondérée des sources.
- Le contrôleur OBS expose la raison précise d'inéligibilité (OBS déconnecté, stream inactif, module absent, etc.).
- Tests unitaires ajoutés pour la simulation, les raisons d'éligibilité, le cooldown manuel et le fail-safe manuel.


## 2.0.11 — transitions de layout sur une timeline globale

- Correction d'un gel/crash apparent lorsqu'une transition `Déplacement` longue était appliquée à de nombreux éléments.
- Avant 2.0.11, chaque source exécutait sa propre transition complète : 40 sources × 2000 ms pouvaient bloquer l'application pendant environ 80 secondes.
- Les transformations utilisent désormais une seule timeline globale : tous les éléments avancent ensemble et la durée demandée n'est payée qu'une fois.
- `move_fade` partage la même timeline pour le déplacement et le fondu.
- Les appels WebSocket sont pris en compte dans le calcul du délai entre frames afin d'éviter que le nombre de sources allonge fortement la transition.
- La stabilisation finale spécifique aux groupes OBS est conservée après la transition.
- Ajout d'un test de régression à 2 éléments / 2000 ms vérifiant qu'il n'y a que 7 attentes pour 8 frames, et non 7 attentes par source.
- 50 tests unitaires passent.

## 2.0.10 — `:locked` devient une exclusion dure des LayoutProfiles

- La convention OBS `[Type:locked] Nom` n'est plus capturée dans un LayoutProfile.
- Le module `:locked` n'apparaît plus dans le catalogue de layout après synchronisation.
- Son sous-arbre est également ignoré : une scène ou un groupe `:locked` n'entraîne plus la capture indirecte de ses PNG, navigateurs ou scènes internes via `support_items`.
- Une recapture d'un LayoutProfile ne peut donc plus modifier indirectement sa géométrie, sa visibilité ou sa composition.
- Le verrouillage manuel dans l'éditeur reste disponible comme comportement propre à un profil et est renommé pour éviter la confusion avec la convention OBS `:locked`.
- 49 tests unitaires passent.

## 2.0.9 — cache des Scene Item OBS rendu sûr après modifications structurelles

- Le cache `sceneItemId` est désormais vidé avant chaque application, comparaison et snapshot de layout.
- SSR résout à nouveau les items par `(conteneur, nom de source)` avant d’agir, au lieu de supposer qu’un identifiant numérique mémorisé est encore valide.
- Cela couvre également le cas plus dangereux où OBS **réutilise** un ancien `sceneItemId` pour une autre source : l’ancien mécanisme de retry sur erreur ne pouvait pas détecter ce cas car la requête restait valide.
- Ajout d’un test de régression simulant un ID réutilisé par une autre source après une modification de scène.
- Les erreurs OBS de type `OBSSDKRequestError` (ressource absente, requête refusée) ne sont plus traitées comme une perte de connexion : elles ne déclenchent plus le délai de reconnexion global qui faisait échouer toutes les sources suivantes.
- 48 tests unitaires passent.

## 2.0.8 — correctif groupes OBS identifiés comme scènes

- Correction d'un bug réel remonté par OBS WebSocket : certains groupes OBS annoncent un type de source scène tout en exigeant `GetGroupSceneItemList`. SSR donnait priorité au type scène et pouvait envoyer `GetSceneItemList` au groupe, provoquant l'erreur 602 `The specified source is not a scene. (Is group)`.
- `isGroup` est désormais autoritaire lors de la découverte récursive : un groupe n'est plus parcouru comme une scène imbriquée.
- Les groupes sont enregistrés explicitement avec `source_type = group`, ce qui leur fait suivre la passe de stabilisation après application des descendants.
- Compatibilité avec les LayoutProfiles capturés en 2.0.5/2.0.6 : SSR infère les groupes depuis la topologie du profil, même si un ancien profil les avait marqués `scene`.
- Le cas réel `In Game -> [Module] WebCam -> groupe WebCam -> [Module] Avatar -> Avatar Dynamic.png` est couvert, y compris lorsqu'un groupe annonce `OBS_SOURCE_TYPE_SCENE`.
- 44 tests unitaires passent.

## 2.0.6 — correctif groupes OBS et modules imbriqués

- Correction du changement de canvas avec une hiérarchie scène → groupe OBS → scène imbriquée → source interne, par exemple `In Game -> [Module] WebCam -> groupe WebCam -> [Module] Avatar -> Avatar Dynamic.png`.
- Les descendants sont appliqués avant le groupe, puis SSR attend brièvement que OBS recalcule les limites du groupe avant de restaurer la transformation du groupe.
- Une seconde passe de stabilisation réapplique uniquement la transformation finale du groupe après un nouveau cycle de rendu OBS.
- Les sources internes non préfixées restent des données de support invisibles dans le catalogue utilisateur.
- Ajout d'un test simulant le recalcul différé des bounds d'un groupe OBS après modification d'un enfant.
- 43 tests unitaires passent.

## 2.0.5

Voir les versions précédentes pour l'historique antérieur.
