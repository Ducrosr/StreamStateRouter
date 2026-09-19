# Build / validation status — 2.0.13

## Base

- Base auditée : **2.0.12**
- Commit audité : `6f4eb032edfd4d7f8148563956427ee8f8e60d9d`
- La branche de correction a été créée directement depuis ce commit ; aucun changement ultérieur de `main` n'était présent au démarrage.

## Résultat communiqué par l'audit Astra

Le handoff Astra indique qu'il a exécuté **85 tests en mémoire** sur la base 2.0.12 :

- **84 réussites** ;
- **1 échec par `NameError`** dans `tests/test_config.py` : `build_activation_policies` était utilisé sans être importé ;
- **1 test écrivant sur disque exclu** de cette exécution en mémoire.

Ces chiffres décrivent **l'exécution de l'audit**, pas une validation 2.0.13 et pas un essai Windows/OBS réel.

Le `NameError` a été corrigé en 2.0.13 par ajout de l'import manquant.

## Implémentation 2.0.13

Réalisé :

- file de commandes d'activation consommée par `SSR-Router` ;
- résultats asynchrones vers Qt par `request_id` et signal ;
- sérialisation tick/trigger/stop/reset/reconcile ;
- arrêt avec refus des nouvelles commandes, invalidation des commandes anciennes et attente des dispatches OBS déjà engagés ;
- pending hides structurés par politique/cible/collection ;
- retry borné, hide compensatoire après show incertain, exclusivité sûre ;
- distinction absence OBS confirmée / résultat incertain ;
- isolation des opérations entre Scene Collections ;
- identité exacte `container/container_kind/source` et legacy source-only seulement si unique ;
- cibles par défaut limitées aux enfants directs ;
- rejet des doublons exacts et conflits ancêtre/descendant ;
- résolution fraîche pair-local du `sceneItemId` pour les mutations de visibilité ;
- éligibilité effective centralisée et statut snapshot sans I/O OBS ;
- validation `math.isfinite` côté config et scheduler ;
- somme de poids protégée contre l'overflow ;
- simulation sur copie de politique dans un executor distinct, avec fingerprint de configuration ;
- tests de caractérisation des deux comportements laissés volontairement inchangés.

L'isolation du RNG de `test_roll()` n'a pas été modifiée : ce point était facultatif dans l'audit. Le RNG de simulation reste indépendant du live.

## Tests ajoutés/modifiés

La suite source contient maintenant des tests dédiés pour :

- course show/stop avec barrières ;
- commandes annulées pendant l'arrêt ;
- dispatch OBS ancien encore actif pendant l'arrêt ;
- cleanup pending qui bloque statut/tick/trigger ;
- hide échoué puis repris ;
- absence confirmée ;
- show avec réponse perdue ;
- échec du hide d'un concurrent exclusif ;
- changement de Scene Collection ;
- identité exacte/ambiguë ;
- cibles directes et conflit parent/enfant ;
- `sceneItemId` réutilisé mais encore accepté ;
- NaN/Infinity/`1e309` et poids `1e308 + 1e308` ;
- simulation concurrente ;
- preview/undo et visibilité runtime ;
- sémantiques de cooldown et de `visibility_owner=runtime`.

## Exécution locale Windows 2.0.13

Une validation locale a été exécutée sur Windows 11 / PowerShell 7.6.6 depuis la branche `fix/activation-runtime-serialization`.

Résultats obtenus sur le commit `d8beba5` avant le nettoyage Ruff final :

- `python -m unittest discover -s tests -v` : **109 tests exécutés, 109 réussites** ;
- durée de la suite : **1,450 s** ;
- `python main.py --check-config` : **Configuration valide** ;
- Ruff : **40 diagnostics de style/qualité**, sans échec fonctionnel des tests.

Les 40 diagnostics Ruff observés étaient composés de :

- 37 occurrences `E702` (plusieurs instructions sur une même ligne) dans `ui/main_window.py` ;
- 2 imports inutilisés (`Iterable` et `time`) ;
- 1 variable locale inutilisée (`current_enabled`).

Ces diagnostics ont été corrigés sur la branche après cette exécution. **Ruff doit encore être relancé sur le nouveau head avant de déclarer Ruff OK.**

## GitHub Actions

GitHub Actions continue à échouer au niveau infrastructure **avant toute étape de job**, le quota Actions du dépôt privé étant épuisé. Les jobs retournent `conclusion=failure` avec `steps=null`.

Ces échecs ne constituent donc pas un résultat de test du code 2.0.13.

Une tentative de récupération directe de l'archive GitHub dans l'environnement d'exécution a également été bloquée par l'accès réseau.

## Revue statique effectuée pendant l'implémentation

La revue du diff a notamment détecté et corrigé avant fusion :

- ordre invalide des champs dataclass de `SimulationResult` après ajout du fingerprint ;
- risque de mutation partielle d'état avec nombres non finis, corrigé par validation des politiques avant installation dans le scheduler ;
- possibilité qu'un ancien dispatch OBS différé écrive encore après l'arrêt du worker principal, corrigée par une barrière de dispatch ;
- message UI « appliqué » incorrect si le runtime précédent ne s'arrête pas ;
- risque de proposer des descendants profonds comme alternatives d'activation au lieu des seuls enfants directs.

## Validation Windows / OBS réelle

**Non réalisée dans cette session.**

La campagne manuelle détaillée dans `TESTING.md` reste nécessaire sur la vraie collection OBS avant de qualifier la 2.0.13 de validée en production.
