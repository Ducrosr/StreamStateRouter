# Stream State Router 2.0.13

## Activations temporaires OBS

Cette version applique le handoff d'audit de la 2.0.12 sans réécrire l'architecture : le scheduler reste pur, le contrôleur OBS possède les mutations de visibilité, le runtime orchestre les threads et les LayoutProfiles conservent la géométrie.

### Correctifs principaux

- sérialisation des activations sur le worker runtime ;
- résultats de commandes asynchrones vers Qt ;
- arrêt sécurisé et refus d'un runtime de remplacement tant que l'ancien peut encore écrire dans OBS ;
- hides non acquittés structurés, blocage de politique et retry borné ;
- hide compensatoire après show incertain ;
- exclusivité sûre en présence d'un concurrent non masqué ;
- distinction source absente / erreur OBS incertaine ;
- isolation par Scene Collection ;
- identité exacte des cibles et compatibilité legacy si source unique ;
- sélection des enfants directs et validation des conflits hiérarchiques ;
- résolution fraîche des IDs OBS pour la visibilité d'activation ;
- éligibilité effective unique pour tick, commandes et statut ;
- validation des valeurs finies et pondération sans overflow ;
- simulation hors thread Qt et hors worker d'activation, avec fingerprint de configuration.

### Sémantiques volontairement inchangées

- une perte d'éligibilité efface actuellement le cooldown ;
- un `visibility_owner=runtime` persisté peut rester présent après suppression d'une politique.

Ces deux comportements sont maintenant couverts par des tests de caractérisation et ne seront modifiés qu'après décision explicite.

### Validation

Le rapport Astra sur la base 2.0.12 indiquait **85 tests exécutés en mémoire : 84 réussites et un NameError**, avec un test écrivant sur disque exclu. Le NameError (`build_activation_policies` non importé dans `tests/test_config.py`) est corrigé en 2.0.13.

Les nouveaux tests de concurrence utilisent des barrières/événements et couvrent notamment show→stop sérialisé, cleanup incertain, show à réponse perdue, exclusivité, changement de collection, arrêt/redémarrage, valeurs non finies et simulation concurrente.

Au moment de cette mise à jour, GitHub Actions échoue encore **avant l'exécution de toute étape** sur le dépôt (`steps=null`). Aucun succès de suite complète, Ruff ou validation Windows/OBS réelle n'est donc revendiqué tant que ces commandes n'ont pas réellement tourné.
