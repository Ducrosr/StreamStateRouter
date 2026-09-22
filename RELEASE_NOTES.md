# Stream State Router 2.1.0

## Routage déclaratif gardé

La 2.1.0 introduit le premier chemin d'exécution déclarative de SSR, construit
sur la séparation état désiré / état observé / état acquitté.

Le périmètre exécutable reste volontairement limité à trois propriétés :

- visibilité de Scene Items ;
- mute d'inputs ;
- volume d'inputs en dB.

Le planner reste pur et les LayoutProfiles restent délégués à
`OBSLayoutManager`. Les filtres, settings arbitraires, changements de scène
programme et autres opérations complexes ne sont pas promus dans l'executor.

## Garanties d'exécution

Chaque préparation est liée au contexte OBS et runtime qui l'a produite :

- session WebSocket et génération de session ;
- Scene Collection active ;
- epoch du catalogue ;
- révision de configuration ;
- génération runtime / pause-reprise ;
- état logique courant ;
- identité physique de la cible.

Les écritures refusent les reconnects implicites. Une dernière relecture
physique suit la revalidation lente, puis le runtime réalise son dernier contrôle
local atomiquement avec le `Set*`. L'executor n'effectue ni retry de mutation,
ni rollback spéculatif, ni replan récursif.

Après chaque écriture, SSR effectue un readback ciblé. Un sweep final réobserve
tout le DesiredState. Les résultats partiels distinguent explicitement les steps
`applied`, `not_run`, `unacknowledged` et divergents.

## Volume d'input

`input_volume_db` utilise l'UUID stable de l'input et les requêtes
`GetInputVolume` / `SetInputVolume`.

- la cible est obligatoire ;
- les booléens et chaînes numériques sont refusés ;
- seules les valeurs finies entre `-100` et `+26 dB` sont écrites ;
- une observation physique finie n'est pas artificiellement clampée ;
- planner et executor partagent une tolérance de `1e-4 dB`, adaptée au
  round-trip float32 d'OBS.

## Durcissement et distribution

La pile 2.1.0 intègre également :

- réconciliation cohérente des profils canoniques non gérés ;
- dépendances Python de release figées et vérifiées exactement ;
- `pip check` et installation isolée en CI ;
- lockfile npm et `npm ci` ;
- CodeQL Python, JavaScript/TypeScript et GitHub Actions ;
- provenance de build et hash des locks/dépendances ;
- manifeste SHA-256 strictement revérifié ;
- smoke du binaire PyInstaller ;
- construction Inno Setup suivie d'une installation, exécution et
  désinstallation silencieuses ;
- version du package Stream Deck dérivée de la version SSR au build.

## Validation

Avant consolidation, le head Lot 3 `cc54631f50c7b1d473799b8c9075c007e0432fdd`
a validé :

- 383 tests, 1 ignoré ;
- Ruff ;
- smoke configuration et couverture déclarative ;
- Stream Deck typecheck/build/validation ;
- CodeQL complet.

Le smoke de release dédié #113 a validé le portable, le ZIP, le package
Stream Deck, l'installateur, install/run/uninstall, provenance et SHA-256.
Artefact :

`release-smoke-6d8f84578928b5e2cf0568e640f29e088d6ddde7`

SHA-256 :

`b0cc9133aaaae92ea66600edc3629834f8e29d60e6e3308bcf2156fbf8741d3d`

Une validation réelle sur OBS 32.2.2 a ensuite confirmé la résolution par UUID,
la mutation de volume `0 dB -> -1 dB`, le readback convergent puis la
restauration exacte à `0 dB / inputVolumeMul=1.0`.

Le candidat consolidé 2.1.0 doit repasser les mêmes gates depuis sa vue finale
avant création d'un tag ou d'une GitHub Release.
