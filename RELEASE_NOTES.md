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

Les runners GitHub-hosted fonctionnent désormais normalement. Sur le candidat Lot 2 `8655cedeaa383945b4a91714ff8f20ee6bad3d59`, les jobs Windows et Stream Deck sont entièrement verts : tests unitaires, Ruff, smoke test configuration, typecheck, build et validation du plugin.

Une campagne OBS dédiée au premier executor déclaratif a également été réalisée avec succès dans la Scene Collection `SSR Executor Lab`. Elle couvre notamment mutation + readback, zéro écriture lorsque l'état est déjà convergé, redémarrage OBS, changement de Scene Collection, suppression/recréation de ressources sous le même nom, changement d'occurrences, invalidation de conditions et invalidation de ticket après reprise.

Cette campagne qualifie le périmètre Lot 2 ; elle ne remplace pas les autres scénarios manuels de release décrits dans `TESTING.md`.
