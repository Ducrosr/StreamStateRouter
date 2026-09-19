# Stream State Router 2.0.12

## Bêta scheduler / Easter Eggs

Cette version prépare l'audit architectural en rendant le scheduler observable et testable sans modifier OBS.

- état runtime lisible et raison d'éligibilité ;
- journal détaillé des tirages et transitions ;
- commandes manuelles alignées sur la machine d'état automatique ;
- réinitialisation runtime globale / fail-safe ;
- simulation déterministe par seed des chances, poids et anti-répétition.

## Validation

La CI Windows exécute les tests unitaires, Ruff, le smoke test de configuration et la validation du plugin Stream Deck.
