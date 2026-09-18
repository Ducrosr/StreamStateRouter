# Build / validation status — 2.0.11

- `python -m unittest discover -s tests -v` : **50/50 tests OK**.
- Régression : une transition `move` de 2000 ms est exécutée sur une timeline globale commune à toutes les sources.
- `move_fade` réutilise la même timeline pour déplacement + opacité.
- Stabilisation des groupes OBS conservée après l'animation.
- Validation réelle Windows + OBS : à confirmer sur le test utilisateur.
