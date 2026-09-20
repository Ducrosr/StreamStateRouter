# Build / validation status — 2.0.14

## Base

- Base de travail : **2.0.13**.
- Branche d'intégration : `feat/astra-architecture-roadmap`.
- Objectif : appliquer la feuille de route d'architecture Astra sans créer d'architecture parallèle.

## État d'implémentation

Les 18 propositions du plan recommandé sont présentes sur la branche, dans l'ordre de développement indiqué par l'audit :

- Phase A : A2, A6, A7, A8, A16 ;
- Phase B : A3, A1, A4, A5 ;
- Phase C : A9, A10, A11, A13, A14, A15 ;
- Phase D : A17, A12, A18.

Des commits de stabilisation supplémentaires corrigent les interactions relevées pendant la revue : nettoyage de fondu interrompu, diagnostics d'applications partielles, provenance de build, import de validation regex et contexte de restauration.

## Validation source

Les tests de régression correspondants ont été ajoutés au dépôt. Dans l'environnement de cette session, l'accès réseau direct au dépôt depuis le conteneur est indisponible, ce qui empêche de cloner la branche et d'exécuter localement la suite complète.

À exécuter sur Windows avant fusion/release :

```powershell
python -m unittest discover -s tests -v
python -m ruff check .
python main.py --check-config

Set-Location .\streamdeck-plugin
npm install --ignore-scripts --no-audit --no-fund
npm run typecheck
npm run build
npm run validate
```

Le workflow de release 2.0.14 ajoute en plus un build PyInstaller, un smoke test de l'EXE, la construction du plugin Stream Deck et de l'installateur Inno Setup avec vérification explicite des artefacts.

## GitHub Actions

Les exécutions GitHub-hosted récentes du dépôt ont échoué avant toute étape de job (`runner_id = 0` / `steps = null`). Tant que ce comportement persiste, ces runs ne constituent pas une validation du code.

## Validation réelle OBS

Toujours requise. La campagne détaillée dans `TESTING.md` doit notamment couvrir : ciblage après modification structurelle de scène, routage différé A→B→C, reconnexion, preview/undo, recapture héritée, diagnostic partiel et coût des scans sur la vraie collection.
