<img src="docs/icon.svg" width="96" align="right" alt="">

# TrueNAS SCALE (WebSocket) pour Home Assistant

Intégration Home Assistant pour **TrueNAS SCALE 25.04 et plus récent** (testée pour 25.10),
qui utilise uniquement l'**API JSON-RPC 2.0 sur WebSocket**, la seule conservée par
TrueNAS 26 (l'ancienne API REST y est supprimée).

Elle reprend le schéma de noms d'entités de l'ancienne intégration communautaire
`truenas` : elle peut la remplacer **sans toucher aux automatisations ni aux tableaux de bord**.

> ⚠️ Version 0.1 : premiers tests. Les retours (journaux, diagnostics) sont les bienvenus
> dans les *issues*.

## Fonctions

| Catégorie | Entités | Exemple d'entity_id |
|---|---|---|
| Système | disponibilité depuis (uptime), température CPU, charge 1/5/15 min, CPU %, mémoire %, taille ARC | `sensor.truenas_system_temperature` |
| Pools | espace libre (GiB) + attributs (scrub, état…), santé | `sensor.truenas_system_tank_free`, `binary_sensor.truenas_system_tank_healthy` |
| Réseau | débit RX / TX par interface physique | `sensor.truenas_system_eth0_rx` |
| Datasets | espace utilisé (GiB) + propriétés ZFS | `sensor.truenas_datasets_tank_media` |
| Disques | température (°C) + modèle, série, taille… | `sensor.truenas_disks_sda` |
| Services | en marche / arrêté | `binary_sensor.truenas_services_cifs` |
| Apps | en marche / arrêtée, interrupteur pour démarrer / arrêter, mise à jour installable | `binary_sensor.truenas_apps_my_app`, `switch.truenas_apps_my_app`, `update.truenas_apps_my_app` |
| Réplications | état de la dernière exécution (`SUCCESS`, `FAILED`, `RUNNING`…) | `sensor.truenas_replication_my_task` |
| Synchro cloud | état de la dernière exécution | `sensor.truenas_cloudsync_my_task` |
| Snapshots périodiques | état de la tâche | `sensor.truenas_snapshot_tasks_tank` |
| Système | mise à jour de TrueNAS installable (le NAS redémarre) | `update.truenas_system` |
| Système | boutons redémarrer / éteindre le NAS | `button.truenas_system_reboot`, `button.truenas_system_shutdown` |

Les statistiques en direct (CPU, mémoire, ARC, réseau) arrivent par l'événement
`reporting.realtime`. Le reste est lu toutes les 30 s (réglable), les températures de
disque toutes les 5 min (TrueNAS ne les rafraîchit pas plus souvent) et les mises à jour
toutes les 30 min.

Les nouveaux éléments apparaissent sans redémarrage : pool importé, disque branché, app
installée, nouvelle tâche…

### Actions

`truenas_ws.service_start` / `service_stop` / `service_restart`,
`truenas_ws.app_start` / `app_stop` / `app_restart`,
`truenas_ws.cloudsync_run`, `truenas_ws.replication_run`,
`truenas_ws.dataset_snapshot_create`, `truenas_ws.system_reboot`, `truenas_ws.system_shutdown`,
`truenas_ws.pool_import` / `pool_export` (disque de sauvegarde externe par exemple).
`truenas_ws.replication_run` accepte `wait: true` pour attendre la fin et renvoyer le résultat.

```yaml
action: truenas_ws.replication_run
data:
  name: "Replicate tank"
```

## Installation

1. HACS → menu ⋮ → **Dépôts personnalisés** → URL de ce dépôt, catégorie **Intégration**.
2. Installer **TrueNAS SCALE (WebSocket)**, puis redémarrer Home Assistant.
3. Dans TrueNAS : menu utilisateur (en haut à droite) → **API Keys** → **Add**.
   Une clé liée à un utilisateur en lecture seule suffit pour la surveillance ; les
   actions et mises à jour demandent un utilisateur administrateur.
4. Home Assistant → Paramètres → Appareils et services → **Ajouter une intégration** →
   *TrueNAS SCALE (WebSocket)* → adresse du NAS et clé API.

La connexion se fait **toujours en HTTPS** (`wss://<hôte>/api/current`) : TrueNAS révoque
automatiquement une clé API utilisée en HTTP simple. Avec le certificat auto-signé
d'origine, laissez « Vérifier le certificat SSL » désactivé.

## Remplacer l'ancienne intégration `truenas` en gardant les mêmes entités

Les entity_id sont construits comme avant : `<plateforme>.truenas_<catégorie>_<élément>`.
Pour qu'ils soient repris à l'identique, il faut que les anciens soient libérés **avant**
d'ajouter la nouvelle intégration :

1. Paramètres → Appareils et services → **TrueNAS** (l'ancienne) → ⋮ → **Supprimer**.
   Ses entités disparaissent du registre, l'historique (lié à l'entity_id) est conservé.
2. HACS → ancienne intégration TrueNAS → **Supprimer**, puis redémarrer Home Assistant.
3. Ajouter *TrueNAS SCALE (WebSocket)* comme ci-dessus.
4. Vérifier dans Paramètres → Entités (recherche `truenas_`) qu'aucune entité ne finit
   par `_2` : si c'est le cas, une ancienne entité n'a pas été supprimée ; supprimez-la
   puis renommez la nouvelle.

Différences à connaître :

- Les tailles (pools, datasets, ARC) sont en **GiB** (l'ancienne intégration affichait
  « GB » pour des valeurs en GiB) : les nombres sont identiques, mais Home Assistant peut
  signaler un changement d'unité des statistiques (Outils de développement →
  Statistiques → corriger) : choisir de mettre à jour l'unité **sans convertir**.
- Les températures de disque viennent de `disk.temperatures` (lecture SMART faite par
  TrueNAS) et non plus des graphiques de reporting.
- Une entité de disque est liée au disque (identifiant TrueNAS), pas à sa lettre `sdX`.
- Ne sont pas gérés (pour l'instant) : machines virtuelles, conteneurs, jails (CORE).
- L'entité de mise à jour HACS de l'intégration elle-même change de nom avec le dépôt.

## Icône

L'icône et le logo (dossier `custom_components/truenas_ws/brand/`) sont une création originale du projet, sans lien avec les marques d'iXsystems. Home Assistant 2026.3 et plus récent les affiche automatiquement.

## Dépannage

Journaux détaillés :

```yaml
logger:
  logs:
    custom_components.truenas_ws: debug
```

Paramètres → Appareils et services → TrueNAS SCALE (WebSocket) → ⋮ →
**Télécharger les diagnostics** : contenu lu sur le NAS, clé, adresse, numéros de série et
adresses MAC masqués.

## Licence

MIT
