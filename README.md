# Stream State Router 2.1.0

## 2.2.0 — contrôle Windows et filtres avancés (en développement)

SSR peut désormais associer aux profils existants des actions qui dépassent le
seul état OBS, sans ajouter une nouvelle dimension de routage :

- `app_audio_output` : affecte le périphérique audio Windows d'une application
  via SoundVolumeView `/SetAppDefault`. Le backend est isolé derrière une
  interface SSR afin de pouvoir être remplacé si Windows publie un jour une API
  stable pour cette préférence ;
- `windows_hdr` : active ou désactive HDR/Advanced Color sur l'écran principal
  ou sur tous les écrans compatibles via l'API Win32 DisplayConfig ;
- `source_filter_settings` : modifie les paramètres JSON d'un filtre OBS avec
  `SetSourceFilterSettings`, en fusionnant par défaut avec les réglages
  existants.

Usage recommandé :

- **AudioProfile** : routage du jeu vers le canal/périphérique Windows voulu ;
- **CaptureProfile** : HDR/SDR Windows et réglages liés à la capture ;
- **Game / Overlay / Capture / Audio** : réglages de filtres OBS lorsque cela
  correspond au rôle du profil.

Le chemin SoundVolumeView est configurable dans **Paramètres > Contrôle Windows**.
HDR ne dépend d'aucun utilitaire externe.

### Import de collection OBS / Advanced Scene Switcher

Depuis l'onglet **Profils**, le bouton **Importer collection OBS…** peut lire la
collection OBS courante sans la modifier et projeter dans le profil sélectionné :

- les settings des inputs OBS ;
- mute et volume ;
- état et settings des filtres ;
- la visibilité des Scene Items lorsque l'identité n'est pas ambiguë.

La géométrie reste volontairement gérée par le système spécialisé
**LayoutProfile / Capturer depuis OBS**.

L'importeur peut également lire un export JSON Advanced Scene Switcher ou
détecter automatiquement l'objet `advanced-scene-switcher` stocké dans le
fichier de la collection OBS courante. Les macros ne sont converties que lorsque
SSR peut reproduire exactement leur sémantique. Les macros avec waits, logique
complexe, actions dynamiques, else-actions ou sélecteurs non stables sont
signalées dans le rapport et laissées intactes.

Les exports SSR « partageables » neutralisent les settings OBS importés et le
chemin local SoundVolumeView afin d'éviter la fuite accidentelle d'URL, cookies,
tokens ou informations locales.

## 2.1.0 — routage déclaratif gardé

La 2.1.0 consolide la fondation déclarative construite au-dessus de l'architecture
2.0.14. SSR peut désormais préparer, vérifier puis exécuter un état désiré OBS
sur un périmètre volontairement restreint, sans transformer le planner en macro
runner générique.

Le chemin exécutable couvre actuellement :

- visibilité d'un Scene Item lié à une identité physique vérifiée ;
- mute d'un input OBS lié par UUID ;
- volume d'un input en dB (`input_volume_db`) lié par UUID.

Les écritures déclaratives restent opt-in et gardées : ticket mono-usage,
génération de session OBS, Scene Collection, catalogue, configuration, état
logique et identité physique sont revalidés avant mutation. Le dernier contrôle
runtime est atomique avec le `Set*`, puis chaque écriture est acquittée par une
relecture ciblée et un sweep final vérifie la convergence complète.

Le volume déclaratif impose une cible finie entre `-100` et `+26 dB`,
refuse les valeurs implicites ou coercées, et utilise une tolérance commune de
`1e-4 dB` pour absorber le round-trip float32 natif d'OBS sans masquer une
divergence réelle.

La chaîne de livraison est également durcie : dépendances Python figées,
`npm ci`, CodeQL, provenance, SHA-256, smoke du binaire portable et de
l'installateur, ainsi qu'une version Stream Deck dérivée de la version SSR.

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

La durée est configurable. Le déplacement interpole les transforms sur une timeline fluide pilotée par l'horloge. Le fondu utilise temporairement un filtre OBS `[SSR] Layout Fade`, remis à 100 % à la fin. En mode `move_fade`, `duration_ms` reste la durée totale du déplacement : un élément visible avant et après disparaît pendant les 15 % initiaux, reste transparent pendant les 70 % centraux, puis réapparaît pendant les 15 % finaux (`100 % → 0 %` / invisible / `0 % → 100 %`).

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

Le plugin utilise par défaut `127.0.0.1:8765` sans jeton. Si vous modifiez le port SSR ou activez un jeton API, ajoutez l'action **Connexion SSR** dans Stream Deck, renseignez le port et le jeton dans son Property Inspector puis appuyez une fois sur la touche. Ces valeurs sont enregistrées comme paramètres globaux du plugin et sont ensuite réutilisées par toutes les actions SSR.

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

## Couverture de migration déclarative

Le rapport read-only de couverture compare les actions OBS configurées avec les capacités
déclaratives actuellement disponibles, sans démarrer le runtime ni contacter OBS :

```powershell
python main.py --declarative-coverage
```

Pour la sortie JSON complète exploitable par un script :

```powershell
python main.py --declarative-coverage-json
```

Le rapport distingue notamment :

- `empty` : profil sans aucune action effective, donc exclu d'une interprétation abusive de la couverture ;
- `declarative_executable` : propriété déjà autorisée par l'executor gardé ;
- `declarative_plannable` : intention traduite et planifiable, mais pas encore exécutable ;
- `declarative_intent_only` : intention représentable dont la politique physique reste incomplète ;
- `delegated` : propriété volontairement possédée par un sous-système spécialisé, notamment les LayoutProfiles ;
- `legacy_only` : action encore non représentable comme propriété stable ;
- `invalid` : action/profil invalide ou héritage incohérent.

Les statistiques d'actions comptent chaque déclaration une seule fois. La classification d'un
profil tient toutefois compte de ses actions héritées via `extends`. Les valeurs arbitraires de
`set_input_settings` ne sont jamais incluses dans le rapport.

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



### API locale de diagnostic

Les opérations de lecture OBS du nouveau catalogue restent sérialisées sur le
worker runtime existant. Elles ne créent ni seconde connexion OBS ni writer
supplémentaire.

`POST /catalog/sync` met en file une synchronisation explicite du catalogue et
retourne un `request_id`. Le résultat se lit ensuite avec
`GET /requests/<request_id>`.

`POST /planner/current` met en file un dry-run du state courant. Le corps peut
contenir `{"refresh_catalog": false}` pour réutiliser le dernier catalogue
structurel ; par défaut le catalogue est resynchronisé.

Un premier executor déclaratif expérimental est disponible uniquement avec
`SSR_ENABLE_DECLARATIVE_EXECUTION=1` et lorsque SSR est explicitement en pause.
Il reste opt-in et ne remplace pas le dispatcher historique :

- `POST /planner/prepare_current` prépare un ticket mono-usage lié à la session
  OBS, la Scene Collection, l'époque du catalogue et l'état logique courant ;
- `POST /planner/execute` avec `{"plan_id": "..."}` consomme ce ticket ;
- `GET /requests/<request_id>` expose le résultat asynchrone et son statut métier.

Le chemin expérimental exécute `SetSceneItemEnabled` pour des conteneurs scène
identifiés par UUID, ainsi que `SetInputMute` et `SetInputVolume` pour des
inputs identifiés par UUID. Le volume déclaratif utilise `inputVolumeDb` et
n'accepte comme cible qu'une valeur finie comprise entre -100 et +26 dB.
Toute autre propriété rend le DesiredState non exécutable. Les LayoutProfiles
restent exclusivement délégués à `OBSLayoutManager`.

`GET /status` expose également `obs_catalog`, qui indique si un catalogue a
déjà été synchronisé, s'il est `stale` / partiel et fournit uniquement son résumé.
`POST /catalog/snapshot` renvoie le dernier snapshot structurel déjà en cache,
sans nouvelle lecture OBS et sans settings arbitraires.

Le catalogue est un index de découverte, pas une copie durable de l'état OBS :
une reconnexion ou un changement de Scene Collection l'invalide, et le planner
revalide le contexte avant et après ses lectures ciblées. Une occurrence de
Scene Item dont l'identité ou l'ordre a changé est signalée comme référence
périmée au lieu d'être silencieusement réaffectée.

Les valeurs arbitraires de `inputSettings` et de settings de filtres sont
masquées dans les sorties de diagnostic afin d'éviter de republier d'éventuels
jetons ou URL sensibles.

## Fondation déclarative expérimentale

SSR évolue vers un modèle où la configuration décrit principalement l'état final
souhaité et où un planner calcule les différences avant toute exécution.

La première fondation est volontairement **read-only** :

- `OBSResourceCatalogReader` synchronise un index léger des scènes, groupes,
  occurrences de Scene Items, inputs, transitions, dimensions du canvas et
  requêtes réellement annoncées par la session OBS ;
- les UUID fournis par OBS sont conservés comme indices de référence, tandis que
  les `sceneItemId` restent considérés comme éphémères ;
- les settings d'inputs et de filtres sont lus à la demande plutôt que scannés
  à chaque tick ;
- `DesiredState` décrit uniquement les propriétés explicitement gérées par SSR,
  conserve leur provenance et refuse les valeurs contradictoires ;
- les intentions sans Scene Collection explicite sont liées au snapshot de
  collection actif avant le diff ;
- `observe_desired_state()` relit uniquement les propriétés physiques
  nécessaires au plan ; le catalogue structurel n'est pas assimilé à l'état
  physique courant ;
- `build_execution_plan()` est pur, déterministe et ne réalise aucun I/O ;
- les références manquantes, capacités indisponibles et ressources déléguées
  bloquent la propriété concernée avant toute écriture ;
- une valeur physique inconnue bloque le plan au lieu de produire une écriture
  aveugle ;
- `validate_state_coverage()` peut détecter une propriété gérée par un état mais
  laissée accidentellement indéfinie par un autre état ;
- `OBSDispatcher.plan_state()` réutilise désormais la résolution existante des
  profils et héritages pour exposer aussi l'intention déclarative, au lieu de
  créer une seconde logique de sélection parallèle ;
- un fingerprint de cible permet d'identifier deux résolutions conduisant au
  même résultat physique sans inclure la provenance.

Le planner reste un composant **pur et read-only**. L'exécution existante du
dispatcher n'est pas remplacée. Le chemin expérimental décrit ci-dessus prépare
et acquitte trois propriétés simples via le worker `SSR-Router` — visibilité,
mute et volume dB — avec revalidation de session/collection/identité juste avant
chaque mutation et relecture physique après chaque `Set*`. Il n'alimente pas `_applied_profiles`,
n'effectue ni retry de mutation ni rollback, et exige une nouvelle préparation
si ses hypothèses deviennent périmées. Les LayoutProfiles conservent leur moteur
spécialisé validé.

La pause suspend le routage automatique, la réconciliation périodique et le
départ d'un dispatch différé encore en attente. Les commandes manuelles
explicites et les cleanups de sécurité restent sérialisés sur le worker runtime ;
la pause ne constitue donc pas une promesse « zéro écriture OBS » absolue.

Les scopes d'ownership sont génériques et provider-agnostic. Ils permettent de
déléguer l'intérieur d'un composant OBS à son propriétaire sans introduire de
logique Dofus/Shinra dans le cœur du planner. Une future capture dynamique peut
donc être ajoutée comme extension sans déplacer aujourd'hui le fonctionnement
validé de DWM dans SSR.
