# Plan de validation — Stream State Router 2.0.12

## Bêta 2.0.12 — scheduler / Easter Eggs

Cette campagne doit être réalisée sur la vraie collection OBS avant l'audit final Astra.

### État runtime et diagnostics

- [ ] Ouvrir l'éditeur d'un module avec une politique aléatoire.
- [ ] Vérifier les états `Inactif`, `Éligible`, `Déclenché / visible` et `Cooldown`.
- [ ] Vérifier que la raison d'éligibilité correspond au contexte OBS réel.
- [ ] Vérifier que le journal affiche les tirages réussis/échoués, show/hide, cooldown, blocages et fail-safe.

### Chance et pondération

- [ ] Tester une chance à 100 % : chaque tirage dû doit réussir si une cible est disponible.
- [ ] Tester une chance à 0 % : aucun déclenchement ne doit avoir lieu.
- [ ] Configurer plusieurs sources avec des poids très différents et vérifier la distribution avec la simulation déterministe.
- [ ] Relancer la même simulation avec la même seed : le résultat doit être identique.
- [ ] Vérifier que la simulation ne modifie ni OBS, ni l'état runtime, ni le prochain comportement aléatoire live.
- [ ] Activer l'anti-répétition et vérifier qu'une autre source est choisie lorsqu'une alternative est disponible.

### Commandes manuelles

- [ ] `Tester le tirage` ne doit modifier ni OBS ni l'état runtime.
- [ ] `Déclencher maintenant` doit emprunter la même machine d'état que le scheduler.
- [ ] Pendant un cooldown, `Déclencher maintenant` doit être refusé.
- [ ] Un double-clic sur une source participante doit la déclencher uniquement si la politique est éligible.
- [ ] `Arrêter` pendant `Visible` doit masquer la source puis entrer en cooldown.
- [ ] `Arrêter` hors `Visible` doit rester un no-op explicite dans le diagnostic.
- [ ] `Réinitialiser le cooldown` doit remettre la politique dans l'état cohérent avec son éligibilité.
- [ ] `Réinitialiser runtime` doit masquer toutes les sources temporaires possédées par le scheduler et remettre ses états à zéro.

### Intégration LayoutProfile / runtime

- [ ] Déclencher une source temporaire, puis changer de LayoutProfile pendant qu'elle est visible.
- [ ] La visibilité appartenant au scheduler ne doit pas être écrasée par le LayoutProfile.
- [ ] Recapturer un LayoutProfile pendant/après un Easter Egg et vérifier que la visibilité runtime n'est pas mémorisée comme état du layout.
- [ ] Vérifier qu'un élément `[Type:locked]` reste totalement hors LayoutProfile et n'est pas modifié indirectement.

### Fail-safe OBS

- [ ] Supprimer ou renommer une source participante puis provoquer un tirage : SSR doit journaliser l'erreur sans casser le runtime global.
- [ ] Déconnecter/reconnecter OBS pendant une activation : au retour, toutes les sources temporaires doivent être masquées.
- [ ] Changer de Scene Collection : le scheduler doit être réinitialisé et ses sources temporaires masquées.
- [ ] Fermer SSR pendant une activation normale : le fail-safe d'arrêt doit tenter de masquer les sources temporaires.
- [ ] Relancer SSR avec OBS connecté : aucune ancienne activation temporaire ne doit rester considérée comme active.

## Régression — convention `:locked`

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

- [ ] Tester `Déplacement` avec 2000 ms sur un layout contenant de nombreux modules.
- [ ] L'interface ne doit plus rester bloquée pendant N × 2000 ms.
- [ ] Tous les modules doivent progresser ensemble sur une durée proche de 2 secondes.
- [ ] Refaire le test avec `Déplacement + fondu`.
