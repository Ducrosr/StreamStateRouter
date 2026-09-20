# Stream State Router 2.0.14

## 2.0.14 — feuille de route d'architecture Astra

Cette version applique la feuille de route d'audit dans l'ordre recommandé, sans remplacer l'architecture existante :

- résolution fraîche des `sceneItemId` pour les actions OBS classiques ;
- récupération bornée des filtres de fondu et nettoyage différé des opacités temporaires ;
- validation homogène des nombres finis, regex, conditions et paramètres d'actions ;
- signatures Win32 `ctypes` explicites ;
- chaîne de build/release versionnée depuis une source unique avec smoke test du binaire ;
- mutations OBS live sérialisées par le runtime ;
- séparation entre état désiré et état effectivement acquitté par OBS ;
- obligations de nettoyage conservées après erreur, reconnexion ou arrêt ;
- preview/undo protégés par le contexte OBS et récupérables ;
- identité exacte des cibles d'activation et propriété explicite de visibilité runtime ;
- capture de LayoutProfiles remplacée de manière complète et validée ;
- comparaison/validation des layouts alignées sur le plan réellement appliqué ;
- distinction entre brouillon, configuration enregistrée et configuration appliquée ;
- API et Stream Deck avec acquittement explicite des commandes ;
- sauvegardes atomiques et bornées ;
- diagnostic transversal des décisions et applications incomplètes ;
- scans OBS allégés et suppression des écritures no-op mesurables ;
- explication de routage en lecture seule, construite avec les mêmes résolveurs que l'exécution réelle.

OBS reste l'éditeur visuel ; SSR conserve son rôle d'orchestrateur, de mémoire et de restaurateur d'état.

## Correctif 2.0.13 — activations OBS sérialisées et acquittées

Les activations temporaires OBS sont maintenant exécutées dans **un seul contexte runtime**. Les commandes Qt ne touchent plus directement au scheduler ni à OBS : elles sont placées dans une file, exécutées par le worker `SSR-Router`, puis leur résultat revient à l'interface par signal.

Les principales garanties ajoutées sont :

- `tick`, déclenchement manuel, arrêt, reset de cooldown et réconciliation utilisent le même worker ;
- un arrêt refuse les nouvelles commandes, invalide les commandes en attente et attend qu'un ancien dispatch OBS soit terminé avant d'autoriser un runtime de remplacement ;
- les masquages OBS non acquittés sont conservés avec l'identité exacte de la cible et la Scene Collection, puis retentés avec backoff borné ;
- une réponse incertaine après un `show` programme un hide compensatoire et bloque la politique tant que le nettoyage n'est pas acquitté ;
- une activation exclusive est refusée si une cible concurrente ne peut pas être masquée avec certitude ;
- les mutations de visibilité d'activation résolvent toujours fraîchement le `sceneItemId` par `(container, source)`, sans vider le cache global utilisé par la géométrie ;
- les cibles manuelles sont identifiées par `container + container_kind + source`; l'ancien identifiant par source seule n'est accepté que s'il est unique ;
- l'éditeur propose par défaut uniquement les enfants directs du module ; une scène ou un groupe enfant reste une unité d'activation ;
- les valeurs numériques non finies sont refusées et la pondération évite le débordement des sommes ;
- la simulation copie la politique appliquée puis s'exécute dans un worker séparé du thread Qt **et** du worker d'activation.

Deux comportements existants sont volontairement **caractérisés mais inchangés** dans cette version :

1. perdre l'éligibilité remet la politique à `Idle` et efface son cooldown ;
2. un marqueur persisté `visibility_owner=runtime` reste runtime-owned même si la politique correspondante disparaît.

Aucune ancienne cible n'est automatiquement réactivée lors de la suppression d'une politique.

## Bêta 2.0.12 — observabilité du scheduler

L'éditeur de module expose maintenant l'état runtime (Inactif, Éligible, Déclenché / visible, Cooldown), la raison d'éligibilité, le dernier événement et un journal de diagnostic. Les commandes manuelles empruntent la même machine d'état que le scheduler : un déclenchement manuel ne contourne plus un cooldown actif.

Un bouton **Réinitialiser runtime** applique le fail-safe global et masque toutes les sources temporaires possédées par le scheduler. Une simulation déterministe (nombre de tirages + seed) permet de vérifier chance, poids et anti-répétition sans toucher OBS ni l'état runtime.


## Transition 2.0.11 — timeline globale

Les transitions de LayoutProfile sont synchronisées sur une seule timeline. Tous les éléments se déplacent/fondent ensemble ; la durée configurée correspond au layout complet et non à chaque source individuellement.


## Convention 2.0.10 — `:locked` = hors LayoutProfiles

`[Type:locked] Nom` signifie désormais **SSR ne possède pas ce module**. Il n'apparaît pas dans le catalogue de layout, n'est pas capturé dans `modules`, et si c'est une scène ou un groupe, son sous-arbre n'est pas capturé dans `support_items`. Une recapture ne peut donc pas écraser sa taille ou sa position.

Exemple : `[Webcam:locked] Cadre permanent` reste entièrement sous le contrôle d'OBS et hors des LayoutProfiles. Le verrouillage manuel disponible dans l'éditeur de module reste un réglage local au profil, distinct de cette exclusion OBS.



## Correctif 2.0.9 — invalidation complète des `sceneItemId` avant les opérations de layout

Les identifiants numériques `sceneItemId` d’OBS ne sont pas des identifiants durables. Après une modification structurelle d’une scène (ajout/suppression d’une source, reconstruction d’un groupe, etc.), OBS peut les invalider **ou réutiliser un ancien numéro pour une autre source**. Dans ce second cas, une simple stratégie « retenter si OBS renvoie une erreur » ne suffit pas, car la requête peut réussir sur le mauvais item.

SSR 2.0.9 vide donc systématiquement son cache d’identifiants avant `Appliquer`, `Prévisualiser`, `Comparer` et les snapshots Undo, puis résout chaque source par son couple `(conteneur, nom de source)` dans l’état OBS courant. Cela évite qu’une seule suppression fasse apparaître tout le layout comme introuvable ou cible les mauvais items.

La couche WebSocket distingue aussi maintenant une **erreur de requête OBS** (par exemple une source réellement supprimée) d’une **déconnexion réseau/WebSocket**. Une source absente ne met donc plus le client OBS entier en backoff de reconnexion et n’empêche plus l’application des éléments suivants.

## Correctif 2.0.8 — groupes OBS correctement distingués des scènes

OBS peut exposer un groupe avec `isGroup=true` tout en lui donnant un type de source scène. SSR traite maintenant `isGroup` comme autoritaire et utilise uniquement `GetGroupSceneItemList` pour ce conteneur. Cela supprime l'erreur OBS WebSocket 602 et permet à la passe de stabilisation des groupes de fonctionner réellement.

Cas de référence : `In Game -> [Module] WebCam -> groupe WebCam -> [Module] Avatar -> Avatar Dynamic.png`. Le PNG interne peut n'occuper qu'une partie du canvas : SSR conserve sa transformation interne et compense séparément la taille intrinsèque de la scène imbriquée lors d'un changement de canvas.



SSR applique désormais les descendants d'un groupe avant le groupe lui-même et laisse OBS stabiliser ses bounds entre les passes. Ce correctif cible notamment les compositions `scène -> groupe -> scène imbriquée -> PNG`, pour lesquelles OBS peut recalculer automatiquement la transformation du groupe au cycle de rendu suivant.

**Stream State Router (SSR)** est une application Windows qui transforme l'application réellement au premier plan en un **état logique stable de stream**, puis applique uniquement les changements nécessaires dans OBS.

Le principe est volontairement simple : **OBS reste l'éditeur visuel**. SSR observe, mémorise, compare et restaure les états et dispositions. L'objectif est de remplacer progressivement les macros de routage par application d'Advanced Scene Switcher, sans déplacer cette logique générique dans Dofus Window Manager.

## Correctif 2.0.5 — composition interne des scènes de module

SSR capture désormais aussi les sources d’implémentation **sans préfixe** situées à l’intérieur d’une scène de module gérée (PNG, navigateur, groupe, etc.). Elles restent invisibles dans le catalogue de modules, mais leur transform fait partie du layout. Lors d’un changement de canvas, les éléments directement placés dans une scène suivent le ratio du canvas, tandis que les enfants d’un groupe restent en coordonnées locales. L’application s’effectue du descendant vers le parent pour éviter que le recalcul des bounds d’un groupe ne désaligne ses enfants.

Cas visé : `In Game → [Module] WebCam → groupe WebCam → [Module] Avatar → Avatar Dynamic.png`.

**Important : les LayoutProfiles capturés avant 2.0.5 doivent être recapturés une fois** afin d’enregistrer les sources internes non préfixées, absentes des anciennes configurations.

## Ce que route SSR

Une règle peut produire cinq dimensions indépendantes :

```text
Game
OverlayProfile
CaptureProfile
AudioProfile
LayoutProfile
```

Exemple :

```text
Overwatch.exe
→ Game = Overwatch
→ OverlayProfile = FPS
→ CaptureProfile = HDR
→ AudioProfile = Game
→ LayoutProfile = Overwatch
```

Une application peut aussi être marquée **IGNORE** : elle conserve alors l'état en cours au lieu de déclencher le fallback. C'est utile pour les launchers et fenêtres transitoires.

## Layouts OBS par modules

SSR reconnaît les Scene Items nommés :

```text
[Type de module] Nom du module
```

Exemple :

```text
[Global] Date
[Global] Signature
[Webcam] Cadre
[Webcam] Avatar
[Chat] Browser
[Media] Capture
```

Le texte entre crochets est **une catégorie**, pas un identifiant de regroupement. Ainsi, `[Global] Date` et `[Global] Signature` sont deux modules indépendants. Chaque source OBS détectée conserve sa propre position, taille et visibilité dans le LayoutProfile.

Un layout mémorise notamment :

- position ;
- taille ;
- visibilité ;
- ancre ;
- mode de coordonnées ;
- état verrouillé/géré.

Les modules présents dans la scène sont catalogués même lorsqu'ils sont masqués : **présent dans la scène = détectable ; visible/masqué = état mémorisé**.

## Workflow recommandé

Pour créer un layout `Overwatch` dans la scène `In Game` :

1. placez, redimensionnez et affichez/masquez vos modules directement dans OBS ;
2. dans SSR > **Layouts**, choisissez `In Game` puis **Synchroniser avec OBS** ;
3. créez/sélectionnez `Overwatch` ;
4. cochez les modules qui appartiennent au layout ;
5. cliquez **Capturer depuis OBS** ;
6. affectez `LayoutProfile = Overwatch` à la règle `Overwatch.exe`.

Pour Dofus, réorganisez la **même scène** dans OBS, puis capturez un autre LayoutProfile. Aucune scène par jeu n'est nécessaire.

Le bouton **Éditer dans OBS** applique d'abord le layout choisi. Vous pouvez alors utiliser OBS comme éditeur graphique, ajuster le résultat et recapturer.

## Layout de base et héritage

Un LayoutProfile peut hériter d'un autre :

```text
In Game — Base
└─ FPS
   └─ Overwatch
```

Lors de la capture d'un layout enfant, SSR peut compacter les modules identiques au parent. Le profil enfant ne conserve alors que les différences utiles.

Le même mécanisme existe pour les profils d'actions OBS (`Game`, `Overlay`, `Capture`, `Audio`) : les actions du parent sont exécutées avant celles de l'enfant.

## Coordonnées indépendantes de la résolution

Le mode recommandé est **Normalisé**. Position et taille sont stockées relativement au canvas OBS et sont recalculées lorsque la résolution du canvas change.

Les ancres permettent également de conserver un module à une distance cohérente d'un bord. Deux modes sont disponibles :

- ancre relative au canvas ;
- marge fixe en pixels.

Le mode **Absolu** reste disponible pour les layouts devant conserver des coordonnées pixel exactes.

## Preview, comparaison, validation et Undo

L'onglet Layouts contient des outils de sécurité :

- **Prévisualiser** : applique temporairement le layout ;
- **Annuler aperçu** : restaure l'état OBS capturé avant la preview ;
- **Undo OBS** : restaure le dernier snapshot ;
- **Comparer à OBS** : montre les écarts entre le profil et l'état réel ;
- **Comparer deux layouts** : liste leurs différences ;
- **Valider** : recherche sources manquantes, incohérences et problèmes de canvas ;
- **Restaurer version précédente** : revient à la révision précédente enregistrée avant une nouvelle capture.

SSR conserve un historique local limité des révisions de layouts.

## Transitions de layout

Chaque LayoutProfile peut utiliser :

- `instant` ;
- `move` ;
- `fade` ;
- `move_fade`.

La durée est configurable. Le déplacement interpole les transforms. Le fondu utilise temporairement un filtre OBS `[SSR] Layout Fade`, remis à 100 % à la fin.

> Le filtre de fondu est un filtre **au niveau de la source OBS**. Si une même source est réutilisée simultanément dans plusieurs scènes, son opacité peut donc être affectée brièvement pendant la transition. Utilisez `instant` ou `move` si ce comportement n'est pas souhaité.

## Convention enrichie facultative

Le format simple `[Type de module] Nom du module` reste la norme. Des drapeaux facultatifs peuvent être ajoutés au type :

```text
[Webcam:fixed] Cadre
[Chat:nomove] Browser
[Media:noresize] Capture
[Radio:novis] Cadre
```

Drapeaux pris en charge :

- `fixed` / `locked` : élément protégé ;
- `nomove` : ne suit pas le déplacement du module ;
- `noresize` : ne suit pas son redimensionnement ;
- `novis` : ne suit pas sa visibilité.

Cela permet de protéger ou contraindre un module précis sans alourdir les règles processus.

## Groupes OBS et scènes imbriquées

SSR parcourt les Scene Items directs et prend en charge les **groupes OBS** via leur liste interne. Il tente également de découvrir les scènes imbriquées lorsqu'elles sont exposées par OBS WebSocket.

Les chemins/conteneurs sont mémorisés afin d'éviter de confondre deux modules portant le même nom dans des conteneurs différents. Les scènes imbriquées et transforms complexes doivent toutefois être validés avec votre collection OBS réelle, car leur repère peut dépendre de la structure exacte de la scène.

## Conditions d'application

Les règles processus et profils OBS peuvent être conditionnés par :

- stream actif/inactif ;
- enregistrement actif/inactif ;
- scène programme courante ;
- disponibilité du pilotage OBS.

Une règle peut aussi avoir son propre **délai d'application OBS**. Le changement de focus reste immédiat côté Windows ; seule l'exécution OBS est différée. Si un nouveau processus devient actif entre-temps, l'action OBS devenue obsolète est annulée.

## Override temporaire

Le dashboard permet de forcer manuellement un état, de façon permanente ou pour une durée en minutes. À expiration, SSR reprend automatiquement le routage selon l'application au premier plan.

## Détection des nouveaux modules

Lorsque le catalogue OBS est synchronisé, SSR peut rescanner périodiquement la scène et signaler l'apparition de nouveaux modules `[Type de module] Nom du module` dans le journal et la zone de notification.

## Principe de sécurité

SSR ne doit modifier que ce qu'il connaît explicitement :

- source reconnue comme module ;
- élément inclus dans le LayoutProfile ;
- module `managed = true` ;
- module non verrouillé.

Les éléments décochés ou verrouillés restent intouchés par le layout.

## API locale et Stream Deck

SSR expose facultativement une petite API **localhost uniquement**. Par défaut :

```text
127.0.0.1:8765
```

Elle permet notamment :

```text
GET  /status
POST /pause
POST /auto
POST /reapply
POST /layout/apply
POST /layout/preview
POST /layout/cancel-preview
POST /layout/undo
```

Un plugin Stream Deck natif est fourni dans `streamdeck-plugin/` avec les actions :

- Suspendre / reprendre ;
- Reprendre auto ;
- Réappliquer ;
- appliquer ou prévisualiser un layout nommé ;
- Undo layout ;
- Annuler aperçu.

Le plugin 2.0 utilise actuellement les paramètres API par défaut `127.0.0.1:8765` sans jeton. Si vous modifiez le port ou activez un jeton API, utilisez temporairement les commandes HTTP personnalisées ou conservez les valeurs par défaut jusqu'à l'ajout de paramètres globaux au plugin.

## OBS classique

`Game`, `OverlayProfile`, `CaptureProfile` et `AudioProfile` utilisent des listes d'actions. Sont pris en charge :

- changement de scène programme ;
- afficher/masquer un Scene Item ;
- activer/désactiver un filtre ;
- mute ;
- volume en dB ;
- modification de paramètres d'entrée.

SSR n'exécute que les dimensions réellement modifiées.

## Installation source

Python 3.12+ :

```powershell
.\Install-And-Run.ps1
```

Ou manuellement :

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[desktop,dev]"
python main.py
```

La configuration utilisateur est stockée dans :

```text
%APPDATA%\StreamStateRouter\config.json
```

Le pilotage OBS est **désactivé par défaut** afin qu'un premier lancement ne modifie aucune scène.

## Validation développeur

```powershell
python -m unittest discover -s tests -v
python -m ruff check .
python main.py --check-config
```

## Build Windows complet

```powershell
.\scripts\build_windows.ps1
```

Le script valide Python, construit l'EXE portable, tente de construire/valider/empaqueter le plugin Stream Deck lorsque Node/npm est installé, puis construit l'installateur avec Inno Setup s'il est disponible.

## Migration depuis les versions précédentes

Le schéma de configuration actuel est **v5**. Les configurations plus anciennes prises en charge sont migrées automatiquement en mémoire vers v5.

La migration depuis Advanced Scene Switcher doit rester progressive. La synchronisation de la variable `Game` encore présente dans DWM sera retirée uniquement après validation de SSR avec la vraie collection OBS.

### Modules OBS imbriqués

SSR distingue maintenant deux espaces de coordonnées : `root_canvas` pour les modules directement présents dans la scène du LayoutProfile, et `container_local` pour les modules situés dans une scène ou un groupe imbriqué. Seuls les modules racine sont adaptés au changement de résolution du canvas ; les descendants conservent leurs transforms locaux et suivent naturellement l’échelle de leur parent.