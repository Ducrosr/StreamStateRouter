# Stream State Router 2.0.11

## Changement principal

Les transitions de LayoutProfile (`Déplacement`, `Fondu`, `Déplacement + fondu`) sont maintenant synchronisées sur une **timeline globale**. Une transition de 2000 ms dure environ 2000 ms pour l'ensemble du layout au lieu de 2000 ms par source.

Cela corrige le gel/crash apparent observé sur une scène comportant de nombreux modules : l'ancienne implémentation bloquait le thread appelant pendant N × durée de transition.

Les groupes OBS conservent leur passe de stabilisation finale afin de préserver les correctifs précédents sur les hiérarchies imbriquées.

## Validation

- 50 tests unitaires passent.
- Régression dédiée : deux sources avec `move = 2000 ms` partagent 8 frames et seulement 7 attentes temporelles globales.
- Validation réelle Windows + OBS à confirmer par le test utilisateur.
