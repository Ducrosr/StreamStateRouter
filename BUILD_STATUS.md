# Build / validation status — 2.0.12

## Implémentation

- Observabilité du scheduler : état runtime, raison d'éligibilité, dernier événement et journal détaillé.
- Commandes manuelles alignées sur la machine d'état automatique ; le cooldown n'est plus contourné par défaut.
- Fail-safe global exposé via `Réinitialiser runtime`.
- Simulation déterministe par seed, indépendante du RNG live et sans action OBS.
- Tests de régression ajoutés pour la simulation, les raisons d'éligibilité, les événements bloqués, le cooldown manuel et le reset global.
- Documentation du schéma corrigée : configuration actuelle `v5`.

## Validation automatisée

Les workflows GitHub Actions ont été déclenchés sur Windows et sur un workflow Linux temporaire, mais **aucun runner n'a exécuté la moindre étape** : les jobs se terminent en échec avec `steps=[]` et sans logs de commande.

Par conséquent, au moment de ce snapshot :

- la suite `python -m unittest discover -s tests -v` n'a pas pu être réexécutée sur la 2.0.12 ;
- Ruff n'a pas pu être réexécuté ;
- le smoke test `python main.py --check-config` n'a pas pu être réexécuté ;
- la validation Stream Deck n'a pas pu être réexécutée.

Le diff a fait l'objet d'une revue statique ciblée et deux problèmes ont déjà été corrigés avant fusion : interrogation OBS depuis le thread UI et variable devenue inutile dans `activation_status`.

## Validation réelle

La campagne Windows + OBS décrite dans `TESTING.md` reste à effectuer sur la collection OBS réelle avant de considérer la bêta validée.
