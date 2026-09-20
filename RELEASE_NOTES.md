# Stream State Router 2.0.14

## Feuille de route d'architecture Astra

La 2.0.14 applique les propositions du rapport d'architecture dans l'ordre recommandé :

1. A2 — IDs frais pour les actions OBS classiques ;
2. A6 — récupération du fondu bornée ;
3. A7 — validation stricte et homogène ;
4. A8 — signatures Win32 explicites ;
5. A16 — chaîne de build/release fiabilisée ;
6. A3 — orchestration sérialisée des mutations OBS ;
7. A1 — séparation état désiré / état acquitté ;
8. A4 — obligations de nettoyage durables ;
9. A5 — preview/undo récupérables ;
10. A9 — identité exacte et propriété de visibilité ;
11. A10 — capture complète des LayoutProfiles ;
12. A11 — diagnostics alignés sur l'application réelle ;
13. A13 — brouillon / enregistré / appliqué ;
14. A14 — acquittement API et Stream Deck ;
15. A15 — sauvegardes/export robustes ;
16. A17 — diagnostic transversal ;
17. A12 — réduction des scans et écritures no-op ;
18. A18 — explication de décision sans effet de bord.

## Principales garanties

- Aucun `sceneItemId` OBS n'est traité comme identité durable dans les chemins corrigés.
- Une mutation OBS différée ne peut plus faire confondre état demandé et état réellement appliqué.
- Les opérations live concurrentes sont sérialisées au niveau runtime au lieu de modifier OBS depuis plusieurs chemins indépendants.
- Les erreurs de nettoyage restent visibles et retentables au lieu d'être oubliées.
- Les LayoutProfiles et la visibilité runtime restent séparés.
- Preview et Undo sont liés au contexte OBS dans lequel leur snapshot a été créé.
- Les diagnostics indiquent désormais une application partielle au lieu de présenter un succès global trompeur.
- L'explication de routage réutilise les résolveurs métier et n'envoie aucune mutation à OBS.

## Build et release

La version du paquet est désormais dérivée de `stream_state_router.__version__`. Le workflow de release vérifie la concordance tag/version, les codes de sortie et les artefacts attendus, exécute un smoke test du binaire PyInstaller et construit l'installateur avec la même version.

## Validation

Les modifications incluent des tests de régression dédiés à chaque étape, dont A → B différé → C, ciblage après réutilisation d'un `sceneItemId`, fondu en erreur persistante, contexte Preview/Undo, capture/héritage, diagnostics, scans mutualisés et explication sans mutation.

La validation automatique complète de cette branche doit encore être exécutée dans un environnement Windows disposant des dépendances. Les GitHub-hosted runners de ce dépôt ont récemment échoué avant toute étape de job ; un tel échec d'infrastructure ne doit pas être interprété comme un échec de la suite Python.

Une validation avec la vraie collection OBS reste requise avant de qualifier la 2.0.14 de validée en production.
