# Plan de validation — Stream State Router 2.0.14

## 0. Acceptation de la feuille de route Astra

### Phase A — correctifs localisés

- [ ] Modifier structurellement une scène OBS puis exécuter deux actions classiques sur la même source : la résolution doit suivre le nouvel ID et jamais l'ancien item réutilisé.
- [ ] Faire échouer durablement le filtre de fondu : aucune récursion ni rafale infinie ; le nettoyage reste visible/rejouable.
- [ ] Charger des valeurs `NaN`/`Infinity`, une regex invalide ou des paramètres d'action mal typés : la configuration doit être refusée avant application.
- [ ] Vérifier le provider Win32 et l'instance unique sur Windows x64.
- [ ] Injecter un échec de test/build : aucun artefact release ultérieur ne doit être publié.

### Phase B — orchestration et récupération

- [ ] Reproduire A appliqué → B demandé avec délai → C demandé avant B : C doit être calculé depuis le dernier état réellement acquitté, pas depuis B.
- [ ] Envoyer simultanément des mutations depuis routage, UI et API : l'ordre doit rester déterministe et observable.
- [ ] Couper OBS pendant un nettoyage temporaire puis reconnecter : l'obligation doit être conservée et retentée.
- [ ] Créer un preview, changer de Scene Collection puis annuler : le snapshot ancien ne doit jamais être appliqué à la nouvelle collection.
- [ ] Provoquer une restauration partielle : l'UI/API doivent signaler l'incomplétude et permettre un nouveau test.

### Phase C — configuration et ergonomie

- [ ] Deux cibles de même nom dans des conteneurs différents doivent être distinguées par leur identité exacte.
- [ ] Recapturer un LayoutProfile après suppression d'un module : l'ancien module ne doit pas survivre dans le profil capturé.
- [ ] Vérifier qu'un profil hérité compare/valide exactement ce que l'application utiliserait réellement.
- [ ] Modifier une configuration sans l'appliquer : l'UI doit distinguer brouillon, enregistré et runtime appliqué.
- [ ] Depuis API et Stream Deck, une commande doit retourner/acquitter son résultat plutôt qu'être supposée réussie.
- [ ] Interrompre une écriture de configuration/sauvegarde : aucun fichier final partiellement écrit ne doit remplacer la dernière copie valide.

### Phase D — mesure et extensions

- [ ] Provoquer une source manquante : le diagnostic doit identifier décision, domaine et cible incomplets sans exposer de secret.
- [ ] Mesurer les requêtes OBS sur une grande scène avant/après les scans partagés ; aucune optimisation ne doit réintroduire des IDs persistants.
- [ ] Utiliser « Expliquer cette décision » sur MATCH, IGNORE et fallback : l'explication doit correspondre au moteur et ne provoquer aucune mutation OBS.

## 1. Validation automatisée prioritaire

Ces commandes sont exécutées par GitHub Actions sur Windows et restent également utilisables localement :

```powershell
python -m unittest discover -s tests -v
python -m ruff check .
python main.py --check-config
```

Le plugin Stream Deck doit ensuite conserver sa validation habituelle :

```powershell
cd streamdeck-plugin
npm install
npm run typecheck
npm run build
npm run validate
```

Les tests 2.0.13 ajoutés couvrent notamment :

- sérialisation show/stop avec barrières ;
- invalidation des commandes en attente pendant l'arrêt ;
- attente d'un ancien dispatch OBS déjà engagé ;
- hide non acquitté puis retry ;
- show potentiellement appliqué avec réponse perdue puis hide compensatoire ;
- refus d'une activation exclusive si un concurrent n'est pas masqué avec certitude ;
- changement de Scene Collection sans replay des anciennes opérations ;
- identité exacte et rejet du legacy ambigu ;
- cible de poids zéro toujours déclenchable manuellement ;
- NaN, Infinity, `1e309` et poids très élevés ;
- simulation concurrente sans bloquer les hides live ;
- `sceneItemId` ancien encore valide mais attribué à la mauvaise source ;
- propriété runtime de visibilité pendant apply/preview/undo ;
- perte d'éligibilité qui efface le cooldown — test de caractérisation ;
- marqueur `visibility_owner=runtime` persisté après suppression de politique — test de caractérisation.

## 2. Concurrence runtime

### Show suspendu puis Stop

- [ ] Déclencher une cible et suspendre/ralentir volontairement la réponse OBS si un environnement de test le permet.
- [ ] Envoyer `Arrêter` pendant le show.
- [ ] Attendu : le stop attend dans la file ; aucune séquence `hide -> reprise du show` ne doit être possible.
- [ ] Après acquittement du show, le stop doit s'exécuter ensuite et produire l'état de cooldown attendu.

### Arrêt / redémarrage

- [ ] Quitter ou appliquer une nouvelle configuration alors qu'une commande d'activation est en attente.
- [ ] Les nouvelles commandes doivent être refusées dès le début de l'arrêt.
- [ ] Les commandes anciennes en file doivent être annulées.
- [ ] Le nettoyage doit être tenté avant fin du worker.
- [ ] Si un ancien dispatch OBS est encore actif, le runtime de remplacement ne doit pas démarrer.
- [ ] L'UI ne doit pas annoncer « configuration appliquée » si le redémarrage runtime a été refusé.

## 3. Acquittements OBS / fail-safe

### Hide incertain

- [ ] Provoquer un timeout/perte de réponse sur un hide.
- [ ] Le diagnostic doit indiquer un nettoyage en attente.
- [ ] La politique concernée doit devenir inéligible.
- [ ] Aucun nouveau show de cette politique ne doit être accepté.
- [ ] Le hide doit être retenté avec une temporisation bornée jusqu'à acquittement ou changement de contexte.

### Absence confirmée

- [ ] Supprimer réellement une cible OBS.
- [ ] Un hide sur cette cible doit être considéré acquitté si OBS confirme son absence.
- [ ] Aucune fausse déconnexion globale ne doit être créée.

### Show au résultat incertain

- [ ] Simuler un show appliqué côté OBS dont la réponse est perdue.
- [ ] SSR doit enregistrer un hide compensatoire.
- [ ] La politique doit rester bloquée tant que ce hide n'est pas acquitté.

### Exclusivité

- [ ] Configurer deux cibles exclusives.
- [ ] Faire échouer le hide de la cible concurrente.
- [ ] La nouvelle cible ne doit jamais être affichée tant que le concurrent peut encore être visible.

### Reconnexion / Scene Collection

- [ ] Déconnecter puis reconnecter OBS pendant une activation.
- [ ] Au retour, SSR doit réconcilier les cibles temporaires sans annoncer de succès si un hide reste incertain.
- [ ] Changer de Scene Collection avec un hide en attente.
- [ ] Une opération appartenant à l'ancienne collection ne doit jamais être rejouée dans la nouvelle.
- [ ] La nouvelle collection doit recevoir son propre nettoyage selon les politiques actuelles.

## 4. Cibles et hiérarchies imbriquées

- [ ] Ouvrir l'éditeur d'un module contenant directement un PNG, une scène et/ou un groupe.
- [ ] Seuls ses enfants directs doivent être proposés par défaut.
- [ ] Si un enfant direct est une scène ou un groupe, ses propres éléments internes ne doivent pas apparaître automatiquement comme alternatives sœurs.
- [ ] Une ancienne cible profonde unique/cohérente doit rester visible dans la configuration et utilisable.
- [ ] Configurer volontairement un parent et son descendant comme deux cibles : la validation doit refuser ce conflit.
- [ ] Configurer deux cibles exactement identiques : la validation doit les refuser.
- [ ] Deux cibles de même nom dans des conteneurs différents doivent exiger l'identité exacte pour un déclenchement manuel.
- [ ] Une cible de poids `0` doit rester exclue de l'aléatoire mais déclenchable par double-clic/test manuel.

## 5. IDs OBS et LayoutProfiles

- [ ] Capturer un layout, puis modifier structurellement OBS de manière à réattribuer un ancien `sceneItemId`.
- [ ] Déclencher une mutation de visibilité runtime sur la cible déplacée.
- [ ] SSR doit résoudre fraîchement `(container, source)` et ne jamais modifier l'item qui a récupéré l'ancien ID.
- [ ] Vérifier que cette résolution fraîche n'efface pas globalement le cache de transformations.

### Pendant une activation

- [ ] Changer de LayoutProfile pendant qu'une cible temporaire est visible.
- [ ] Prévisualiser puis annuler une preview pendant une activation.
- [ ] Utiliser Undo OBS pendant une activation.
- [ ] Recapturer un layout pendant/après l'activation.
- [ ] La géométrie reste LayoutProfile-owned ; la visibilité runtime ne doit pas être écrasée/restaurée par ces opérations.

## 6. Éligibilité effective

Pour une même politique, vérifier que le statut, le tick et les commandes rapportent la même cause :

- [ ] politique désactivée → `politique désactivée` ;
- [ ] runtime en arrêt → service non opérationnel / commande refusée ;
- [ ] routage suspendu → `routage suspendu` ;
- [ ] nettoyage incertain → `nettoyage OBS en attente` ;
- [ ] OBS déconnecté → motif OBS correspondant ;
- [ ] stream inactif pour `active_when=streaming` ;
- [ ] module absent de la scène programme pour `module_in_program_scene`.

Ouvrir le dialogue et laisser le statut se rafraîchir : cette lecture ne doit générer aucune requête OBS supplémentaire à elle seule.

## 7. Simulation

- [ ] Lancer une simulation longue puis déclencher/arrêter une cible live.
- [ ] Les hides live doivent continuer pendant le calcul.
- [ ] Fermer le dialogue avant la fin : aucun popup tardif ne doit apparaître.
- [ ] Même seed + même fingerprint de configuration → mêmes résultats.
- [ ] La simulation ne doit pas modifier l'état du scheduler live ni son RNG.
- [ ] Modifier/appliquer la politique puis relancer : le fingerprint doit changer si la configuration sérialisée change.

## 8. Comportements caractérisés — ne pas interpréter comme des bugs corrigés

### Cooldown et perte d'éligibilité

Comportement 2.0.13 conservé :

- si une politique en cooldown perd son éligibilité, le scheduler revient à `Idle` ;
- le cooldown est effacé.

Toute modification de cette sémantique nécessite une décision explicite.

### `visibility_owner=runtime` après suppression d'une politique

Comportement 2.0.13 conservé :

- un marqueur `visibility_owner=runtime` déjà persisté dans un LayoutProfile reste runtime-owned même si la politique disparaît ;
- SSR ne réactive pas automatiquement les anciennes cibles.

Toute stratégie de nettoyage/migration de ces marqueurs doit être décidée séparément.

## 9. Régressions historiques

### Convention `:locked`

- [ ] Un module `[Type:locked] Nom` n'apparaît pas dans les LayoutProfiles.
- [ ] Son sous-arbre reste hors capture/apply.
- [ ] Une recapture ne doit pas le réintroduire.

### Transitions 2.0.11

- [ ] Tester `Déplacement` 2000 ms sur un layout chargé.
- [ ] Tous les modules avancent ensemble sur une timeline globale.
- [ ] Refaire avec `Déplacement + fondu`.

### Canvas / groupes / scènes imbriquées

- [ ] Vérifier 1080p → QHD et retour.
- [ ] Vérifier scène → groupe → scène imbriquée → PNG.
- [ ] Vérifier que les correctifs de stabilisation des groupes restent intacts.
