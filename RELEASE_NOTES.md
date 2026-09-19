# Stream State Router 2.0.12

## Bêta scheduler / Easter Eggs

Cette version prépare l'audit architectural en rendant le scheduler observable et testable sans modifier OBS.

- état runtime lisible et raison d'éligibilité ;
- journal détaillé des tirages et transitions ;
- commandes manuelles alignées sur la machine d'état automatique ;
- réinitialisation runtime globale / fail-safe ;
- simulation déterministe par seed des chances, poids et anti-répétition.

## Validation

Des tests de régression ont été ajoutés et une campagne OBS détaillée est documentée dans `TESTING.md`.

GitHub Actions a été déclenché, mais les jobs Windows et Linux n'ont reçu aucun runner et ont échoué avant toute étape (`steps=[]`). Les résultats automatisés 2.0.12 doivent donc être relancés lorsque l'infrastructure Actions fonctionne ; aucune réussite de CI n'est revendiquée pour cette version.
