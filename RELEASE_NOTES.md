# Stream State Router 2.0.15

## Correctif de reconnexion OBS

Cette version corrige le bruit de console observé lorsque SSR démarre avant OBS ou pendant qu'OBS n'est pas encore prêt.

- SSR continue de retenter automatiquement la connexion selon le backoff configuré.
- Les tracebacks internes répétitifs d'`obsws-python` sont absorbés, car SSR transforme déjà ces exceptions en état de connexion et en diagnostic contrôlé.
- Une réponse OBS 207 est présentée comme « OBS est encore en cours d'initialisation ».
- Le numéro de version de la fenêtre est désormais dérivé de la version du paquet.

La logique de routage, les LayoutProfiles et les politiques d'activation ne changent pas.

## Validation réelle attendue

1. Lancer SSR avec OBS fermé : la fenêtre doit rester utilisable et la console ne doit plus afficher de pile répétée.
2. Lancer OBS ensuite : SSR doit passer automatiquement à « connecté ».
3. Redémarrer OBS pendant que SSR reste ouvert : SSR doit signaler la déconnexion puis se reconnecter sans flood de tracebacks.
4. Pendant les quelques secondes où OBS répond 207, SSR doit afficher un état d'initialisation puis se connecter normalement.
