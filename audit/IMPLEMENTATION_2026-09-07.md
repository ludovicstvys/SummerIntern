# Implémentation du plan du 7 septembre 2026

Le rapport initial reste une photographie de l’audit avant corrections. Les sept marqueurs `xfail` ont été retirés : ces régressions font désormais partie des tests ordinaires.

## Corrections livrées

| Audit | Implémentation |
|---|---|
| F01 | `SECRET_KEY` et `ENCRYPTION_KEY` locales renouvelées, après vérification de l’absence de sessions et de connexions chiffrées dans la base locale ; `.env` privé en mode 600. Les credentials fournisseurs ne sont pas renouvelables par une simple modification de code. |
| F02 | Suppression des séparateurs du mot de passe applicatif Gmail, y compris les espaces insécables ; correction du `.env` local ; respect des mots de passe des autres fournisseurs. |
| F03 | Indicateur partagé `NOTION_SYNC_ENABLED`, désactivé par défaut ; interface et worker cohérents ; commande présente dans le scheduler mais inactive tant que le service est suspendu. |
| F04 | Invitations persistantes, statut visible, retry avec nouveau lien, temporisation et limite d’essais ; upsert des comptes et exclusion des doubles invitations rapides. |
| F05 | Outbox historique persistante en PostgreSQL, indépendante de Notion et du CSV ; états séparés par destinataire ; sauvegarde avant CSV ; reprise possible après panne SMTP. |
| F06 | Table `offer_sources` avec région, programme, saison, trimestre, dates et fermeture par source ; une livraison par offre/utilisateur, libellés multirégions. |
| F07 | Correspondances recalculées après mise à jour et réouverture ; conservation de l’historique des offres déjà vues/envoyées. |
| F08 | Retour aux critères et réactivation reprennent les tâches non envoyées éligibles, sans renvoyer les tâches terminées. |
| F09 | Dashboard filtré par critères courants et ouverture, historique explicite, statut fermé, pagination. |
| F10 | Validation des dates et exclusion des offres futures ou clôturées ; les dates sont également vérifiées avant envoi. |
| F11 | URL HTTP(S) absolues uniquement ; refus des URL avec credentials ou port invalide ; suppression des téléchargements arbitraires de descriptions employeur. |
| F12 | Distinction entre vide explicitement confirmé par métadonnées et vide ambigu ; fermeture après deux snapshots complets ; conservation des données sur panne. L’indisponibilité réelle de la source reste externe. |
| F13 | Les six scripts utilisent extraction/validation/déduplication communes et remplacement atomique des CSV. |
| F14 | Renvoi forcé indépendant de Notion, mise à jour des offres connues via l’outbox ; TODO historiques disponibles par indicateur explicite. |
| F15 | Tous les comptes de plateforme, même désactivés, sont exclus des mails historiques ; messages individuels ; import explicite des anciens abonnés ; `email.csv` retiré du suivi Git, conservé localement. |
| F16 | Backoff, codes d’erreur expurgés, CLI de reprise ciblée par identifiant ; reconnexion Notion remet les synchronisations échouées en file. |
| F17 | Intention de création Notion persistante, exclusion des doubles clics, blocage des répétitions après résultat incertain, récupération d’une base existante avec vérification du parent et du schéma. |
| F18 | Commande explicite de récupération/promotion administrateur avec révocation des sessions et liens. |
| F19 | Horodatages par source/worker, endpoint admin de supervision et contrôle CLI ; budgets par worker, timeouts séparés, poursuite des autres étapes après échec, sérialisation des workflows avec les migrations. |
| F20 | PostgreSQL 17 local temporaire, tests de migration et de concurrence réellement exécutés, tests de reprise/legacy/Notion additionnels, respect de `PYTHON_DOTENV_DISABLED` par le code historique. |

## Données et configuration locales

- Base SQLite sauvegardée dans `.trackr-backups/before-workflow-20260907.db`, puis migrée vers `20260907_0004`. Ce dossier est ignoré par Git.
- `alembic check` ne détecte pas de dérive entre la base locale migrée et les modèles.
- Seules les clés applicatives locales ont été renouvelées ; les tokens SMTP, Notion et Vercel existants nécessitent leur rotation chez les fournisseurs.
- Les modifications préexistantes de `.env.example` et la suppression déjà préparée de `.DS_Store` ont été préservées.
- PostgreSQL 17 a été installé localement pour les tests ; les clusters utilisés par le script sont temporaires et arrêtés après exécution. Aucun service de démarrage automatique n’a été activé.

## Choix fonctionnels retenus

- Une offre déjà envoyée ou enregistrée comme baseline n’est pas renvoyée automatiquement lors d’une réouverture.
- Les offres non envoyées annulées reprennent si les critères et l’activité du compte les rendent à nouveau éligibles ; celles qui ont épuisé leurs essais nécessitent une reprise explicite.
- Une date de clôture explicite passée prime aussi pour les offres rolling.
- Notion personnel reste suspendu tant que `NOTION_SYNC_ENABLED` et les identifiants OAuth ne sont pas configurés ensemble.
- Le canal historique reste actif pour les adresses non migrées (`LEGACY_EMAIL_ENABLED=true` par défaut), mais ne contacte jamais une adresse ayant déjà un compte plateforme.
- Les opérations SMTP restent à livraison au moins une fois : un crash après acceptation distante mais avant commit peut encore produire un doublon. Aucun faux engagement d’exactement-un-envoi n’est introduit.

## Recette et étapes externes

Résultat final : **79 tests réussis, aucun ignoré et aucun `xfail`**, PostgreSQL inclus. Compilation Python, cohérence des dépendances, syntaxe YAML des quatre workflows, absence de dérive Alembic locale et `git diff --check` validés. Le smoke test local retourne 200 pour `/health` et `/login`, et 303 vers la connexion pour `/admin`. Les 21 avertissements de dépréciation proviennent des bibliothèques sous Python 3.14 ; ils ne sont pas des échecs. La CI reste configurée sous Python 3.12.

La suite complète s’exécute avec :

```sh
.venv/bin/python scripts/test_postgres_local.py
```

Elle couvre notamment les sept régressions de l’audit, le parcours invitation/connexion/activation/scraping/envoi/déconnexion, le backoff, les reprises indépendantes SMTP/Notion, les destinataires historiques, les doubles créations Notion, les migrations PostgreSQL, les limites de connexion atomiques et les collecteurs/invitations/digests concurrents.

La collecte réelle de contrôle la plus récente a reçu six réponses vides ambiguës de Trackr et a correctement remonté six échecs sans insertion ni fermeture. Ce résultat valide la protection contre une source indisponible, pas la disponibilité de l’amont. Une recette réelle du scraping devra être répétée lorsque les sources fourniront des réponses exploitables.

Avant déploiement :

1. Révoquer/remplacer les credentials fournisseurs exposés et synchroniser les secrets de production ; traiter la clé de chiffrement de production avec une migration des tokens ou une reconnexion, sans la remplacer aveuglément.
2. Configurer `TO_ADDRS` dans GitHub si les anciens destinataires provenaient uniquement de `email.csv`, désormais privé ; éventuellement importer les abonnés via la commande documentée.
3. Revoir les indicateurs Notion personnel/TODO historiques et configurer OAuth si le service doit être activé.
4. Déployer la migration et le code via le workflow, puis effectuer une recette avec une adresse et un espace Notion de test autorisés et contrôler les Actions planifiées.

Aucun mail réel envoyé, aucune écriture Notion ou modification de production effectuée pendant cette implémentation. Aucun commit ou déploiement créé.

## Ajout — connexion par mot de passe et sessions persistantes

- `/login` propose la connexion e-mail/mot de passe, la définition initiale et la récupération. Les invitations et liens de connexion existants restent disponibles ; les comptes sans mot de passe sont invités à en définir un après connexion par lien.
- Mots de passe de 12 à 128 caractères hachés avec Argon2 (`pwdlib[argon2]==0.3.0`). Les liens de définition/récupération expirent après 15 minutes ; leur ouverture ne les consomme pas. La validation du formulaire consomme le jeton atomiquement, révoque les autres sessions et liens, puis ouvre une nouvelle session.
- Sessions persistantes de 90 jours, renouvelées au maximum une fois par jour d'activité authentifiée, avec plafond de 365 jours depuis leur création. Validation et cookies sont centralisés ; aucune session expirée ou révoquée n'est réactivée. La déconnexion supprime immédiatement la session courante.
- Protection CSRF des formulaires publics, vérification de l'origine lorsqu'elle est fournie, réponses génériques, quotas distincts pour les connexions par mot de passe et les envois d'e-mails. Les réponses privées ne sont pas mises en cache.
- Migration `20260907_0005` : `users.password_hash`, `user_sessions.renewed_at` et table `password_tokens`. Les comptes et sessions existants sont conservés. La désactivation et la récupération administrateur révoquent également les jetons de récupération.

Validation : **136 tests réussis, aucun ignoré**, avec `scripts/test_postgres_local.py` sur un cluster PostgreSQL temporaire et isolé. Les contrôles incluent migrations SQLite/PostgreSQL et absence de dérive Alembic, accès des comptes existants, limites de mots de passe, erreurs/CSRF/quotas, récupération concurrente, renouvellement concurrent, expiration et révocation. Compilation Python, cohérence des dépendances et `git diff --check` validés. Les 35 avertissements sont des dépréciations des bibliothèques sous Python 3.14.

Les modifications locales préexistantes sont conservées. Aucun déploiement ni migration de production n'a été effectué. À la prochaine livraison, installer les dépendances et appliquer `alembic upgrade head` avant de servir la nouvelle version ; SQLite applique également ses migrations au démarrage local. SMTP reste nécessaire pour les invitations et la récupération, mais pas pour les connexions par mot de passe.
