# Plan de validation — Stream State Router 2.0.11

## Test ciblé — convention `:locked`

1. Dans OBS, renommer temporairement un module en `[Webcam:locked] Test`.
2. Dans SSR, cliquer sur **Synchroniser avec OBS**.
3. Attendu : ce module ne doit plus apparaître dans le catalogue de modules du LayoutProfile.
4. Recapturer le LayoutProfile depuis OBS.
5. Attendu : le module `:locked` ne doit pas réapparaître dans le profil.
6. Modifier sa position ou sa taille directement dans OBS, puis réappliquer le LayoutProfile.
7. Attendu : SSR ne doit ni déplacer, ni redimensionner, ni masquer/afficher ce module.
8. Si le module `:locked` est une scène ou un groupe contenant d'autres sources, vérifier que ses descendants restent eux aussi inchangés par le layout.

Pour revenir au comportement normal, retirer `:locked` du nom puis resynchroniser et recapturer le LayoutProfile.


## Régression 2.0.11 — transitions

- Tester `Déplacement` avec 2000 ms sur un layout contenant de nombreux modules.
- L'interface ne doit plus rester bloquée pendant N × 2000 ms.
- Tous les modules doivent progresser ensemble sur une durée proche de 2 secondes.
- Refaire le test avec `Déplacement + fondu`.
