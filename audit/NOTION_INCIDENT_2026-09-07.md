# Audit des ajouts massifs dans le Notion legacy — 7 septembre 2026

## Conclusion

La dernière exécution legacy a créé **120 pages et mis à jour 42 pages**. **105 des 120 créations correspondent à des URL déjà présentes dans le CSV de leur source avant l'exécution**. Les 15 autres sont absentes de ces snapshots ; cela ne prouve pas qu'elles n'ont jamais été vues auparavant.

Le code corrigé filtre les nouvelles tâches, mais continue à exécuter les anciennes tâches `pending`. La correction du producteur n'a donc pas suffi à empêcher le rattrapage du stock historique. Le mécanisme est reproduit localement : zéro nouvelle offre par comparaison CSV, mais une synchronisation Notion exécutée à partir d'une tâche préexistante.

## Exécution et preuves

- [Dernière run legacy 34149186086](https://github.com/ludovicstvys/SummerIntern/actions/runs/34149186086) : 7 septembre à **19:49:42 heure de Paris** (17:49:42 UTC), succès, commit `ef3f922e640a2ce8ed734f1798aec1c6c82457fd`.
- Logs : `LEGACY_NOTION_ENABLED: true`, `LEGACY_EMAIL_ENABLED: false`.
- Comptage : 120 lignes `offer create ok`, 120 URL canoniques distinctes, 42 lignes `offer update ok`. Les compteurs `Notion sync` donnent les mêmes totaux.
- Référence antérieure : les six CSV du commit exact exécuté, lus avec `git show ef3f922:<fichier>`. Comparaison avec `trackr_common.canonical_offer_url`, donc indépendante des paramètres de tracking.
- [Exécution legacy précédente 34121423313](https://github.com/ludovicstvys/SummerIntern/actions/runs/34121423313), 14:21:23 heure de Paris, commit `b9a28d9` : déjà 90 créations et 29 mises à jour.
- La run plateforme plus récente, à 20:13, est un autre workflow ; ce rapport attribue les 120 créations au workflow legacy à partir de ses propres logs.

| Collecteur | Créations | Déjà dans son CSV avant la run | Absentes de son CSV |
|---|---:|---:|---:|
| UK Summer | 92 | 79 | 13 |
| France Off-cycle | 14 | 14 | 0 |
| UK Off-cycle | 5 | 3 | 2 |
| Hong Kong Summer | 9 | 9 | 0 |
| France Summer / Hong Kong Off-cycle | 0 | 0 | 0 |
| **Total** | **120** | **105** | **15** |

Inventaire vérifiable : [NOTION_CREATIONS_2026-09-07.csv](NOTION_CREATIONS_2026-09-07.csv). Il liste les créations confirmées, leurs URL normalisées et leur présence dans le snapshot antérieur de la même source.

## Chaîne causale

1. Le commit `b8bca8e` introduit l'outbox persistante. Dans cette version de `run_collector`, toutes les offres sélectionnées sont mises en file pour Notion, indépendamment de `new_urls`. Seuls les emails sont filtrés par nouveauté. Cela transforme une synchronisation incrémentale en rattrapage de toutes les offres sélectionnées.
2. `process_tasks` a un budget de 90 secondes par collecteur. Les tâches non traitées restent en base. L'ancienne version recharge aussi l'inventaire Notion pour chaque tâche, ce qui augmente le temps nécessaire pour vider la file.
3. Le commit `4c00831`, présent dans la dernière run via `ef3f922`, ajoute bien `canonical_offer_url(...) in new_urls` avant `enqueue` pour Notion. Cette correction agit sur les **nouvelles insertions dans la file**.
4. Le worker sélectionne toujours toutes les tâches dues de la source avec `status == 'pending'`, sans vérifier leur origine ni si elles avaient réellement été détectées comme nouvelles. Les anciennes tâches contournent donc le nouveau filtre CSV.
5. La dernière run avait Notion activé. Le coupe-circuit désactivé conserve volontairement les tâches ; le réactiver permet de les traiter. La présence d'une commande de remédiation ne garantit pas que toutes les tâches fautives ont été annulées.
6. `sync_to_notion` crée une page lorsque l'URL n'est pas dans l'inventaire Notion, même si l'offre est ancienne dans le CSV. La déduplication Notion ne remplace donc pas la définition métier de « nouveau stage scrapé ».

La provenance précise de chaque tâche (création et éventuelle remise en attente) n'est pas disponible dans les logs consultés. Elle nécessiterait une lecture de `legacy_tasks` en production. L'accumulation lors de la version antérieure est l'explication étayée par l'historique du code ; le traitement d'offres déjà connues est directement confirmé par les 105 correspondances CSV.

## Pourquoi les protections présentes n'ont pas suffi

- `cancel_legacy_notion_window` ne traite que les tâches `pending` dans une fenêtre de création explicitement fournie. Il ne nettoie pas automatiquement tout le stock historique et n'archive pas les pages créées.
- `sync-last-email-offers` concerne une opération manuelle sur 17 offres. Cette liste ne restreint pas le worker planifié.
- Le README affirme que le workflow passe actuellement Notion à `false`, alors que son YAML utilise la variable GitHub avec un défaut à `true` et que les logs confirment `true`.
- Le test existant de deux collectes identiques part d'une file vide. Il couvre les nouvelles tâches, pas une file héritée de la version fautive.
- Les logs signalent 11 pages sans URL exploitable. Elles peuvent empêcher une reconnaissance de doublons sémantiques, mais ne suffisent pas à expliquer les 105 anciennes offres resynchronisées. Les créations confirmées ne sont pas nécessairement 120 doublons : aucune comparaison exhaustive des titres/entreprises des pages historiques n'a été réalisée.

## Correction recommandée

1. Suspendre les écritures legacy pendant l'inventaire des tâches restantes ; relever les tâches `pending` et `failed`, leurs dates, sources et URL.
2. Identifier les tâches générées par la logique fautive et les mettre en quarantaine de façon ciblée. Préserver les retries d'offres réellement nouvelles : filtrer aveuglément toute tâche contre le CSV courant supprimerait aussi des reprises légitimes, puisque le CSV est écrit avant leur livraison.
3. Rendre durable la provenance des tâches : exécution de détection, motif, version du producteur et preuve de nouveauté. Le worker doit pouvoir distinguer les retries légitimes des reprises historiques non autorisées.
4. Examiner les 105 créations d'offres déjà connues pour préparer un nettoyage ciblé. Une offre connue du CSV n'est pas automatiquement une page à supprimer ; vérifier les pages et les éventuelles modifications personnelles avant archivage.
5. Ajouter une couverture de migration d'une file préexistante, aligner le README avec le workflow et publier les compteurs : nouvelles offres, tâches anciennes traitées, créations et mises à jour.

Un registre durable des URL déjà vues serait également plus robuste qu'un simple snapshot CSV : une offre qui disparaît puis revient peut actuellement redevenir « nouvelle » au sens du snapshot.

## Validation et limites

[test_legacy_backlog_20260907.py](test_legacy_backlog_20260907.py) : **1 test réussi**, avec SQLite en mémoire et services externes simulés. Il constate le défaut actuel ; son succès ne signifie pas que le contrat « nouveaux stages uniquement » est respecté.

Commande : `PYTHON_DOTENV_DISABLED=1 DATABASE_URL=sqlite:// ENVIRONMENT=development .venv/bin/python -m pytest -q audit/test_legacy_backlog_20260907.py`.

Audit fondé sur les logs GitHub réels, l'historique Git et une reproduction locale. Aucun accès à la base de production, aucune écriture Notion, aucun changement de configuration ou de code applicatif effectué. Le nombre de tâches encore en attente et l'éventuel archivage de pages entre les deux runs ne sont pas vérifiés.
