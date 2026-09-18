# Historique

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
