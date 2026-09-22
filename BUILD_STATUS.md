# Build / validation status — 2.0.14

## Base actuelle

- Socle architectural historique : **2.0.14**.
- Fondation déclarative canonique : `7d1e41424d0ff64b84f061fda590648578cd1fde`.
- Candidat Lot 2 : PR **#11** — `feat/declarative-executor-mvp`.
- Head validé : `8655cedeaa383945b4a91714ff8f20ee6bad3d59`.
- La PR #11 reste volontairement en **draft** dans l'attente de la relecture Astra.

## Lot 2 — executor déclaratif gardé

Le candidat ajoute un premier chemin d'exécution déclarative opt-in, sérialisé sur le worker `SSR-Router`, limité à :

- visibilité de Scene Items ;
- mute d'inputs.

Le planner reste read-only, les LayoutProfiles restent délégués à `OBSLayoutManager`, et l'executor ne met pas à jour le bookkeeping legacy `_applied_profiles`.

Les tickets d'exécution sont liés au contexte OBS préparé et sont invalidés lorsqu'une hypothèse de sécurité change (session, Scene Collection, catalogue, configuration, état logique ou identité physique).

## Validation locale

Le dernier gate Python/runtime avant les deux commits finaux limités au plugin Stream Deck a donné :

- **329 tests réussis** ;
- **1 test ignoré** ;
- Ruff : **OK** ;
- configuration Lab : **valide**.

Sur le head actuel, le gate Stream Deck local a donné :

- installation npm : **OK**, 0 vulnérabilité signalée ;
- typecheck : **OK** ;
- build : **OK** ;
- validation du plugin Elgato : **OK**.

Après la campagne Lab :

- la configuration de production a été restaurée depuis la sauvegarde pré-Lab ;
- `main.py --check-config` la valide ;
- `SSR_ENABLE_DECLARATIVE_EXECUTION` a été retiré de l'environnement ;
- le worktree local était propre.

## GitHub Actions

Après passage du dépôt en public, le provisioning des runners GitHub-hosted fonctionne normalement.

Le workflow **Tests** du head `8655cedeaa383945b4a91714ff8f20ee6bad3d59` est entièrement vert :

### Windows

- checkout/setup : **OK** ;
- installation Python : **OK** ;
- tests unitaires : **OK** ;
- Ruff : **OK** ;
- smoke test configuration : **OK**.

### Stream Deck

- checkout/setup : **OK** ;
- installation npm : **OK** ;
- typecheck : **OK** ;
- build : **OK** ;
- validation du plugin : **OK**.

Les anciens échecs `runner_id = 0` / `steps = null` étaient donc liés au provisioning/infrastructure et non à un échec du code.

## Validation réelle OBS — Lot 2

Une Scene Collection dédiée `SSR Executor Lab` a été utilisée pour tester le nouvel executor sans perturber la configuration de production.

Scénarios validés :

- découverte/catalogue complet et observation des cibles ;
- mutation nominale de deux propriétés avec readback ciblé et convergence finale ;
- chemin déjà convergé sans écriture ;
- ticket périmé après redémarrage OBS ;
- changement de Scene Collection ;
- changement de cardinalité/ordre d'occurrences d'un Scene Item ;
- suppression/recréation d'un Scene Item sous le même nom avec nouvelle identité physique ;
- suppression/recréation d'un input sous le même nom avec nouvel UUID ;
- conditions OBS devenues fausses après préparation ;
- compatibilité du chemin legacy `reapply` ;
- invalidation d'un ticket après pause → reprise.

Dans les scénarios de contexte ou d'identité périmés, l'exécution a été refusée avant mutation et a demandé un replan.

## Ce qui reste avant fusion du Lot 2

- relecture architecture/code ciblée par Astra ;
- correction et revalidation uniquement si cette revue identifie un blocker réel ;
- fusion de la PR #11 seulement après approbation.

Les validations ci-dessus concernent le Lot 2 et ne signifient pas que chaque scénario manuel historique de `TESTING.md` a été rejoué pour cette passe.
