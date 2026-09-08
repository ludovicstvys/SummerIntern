# Corrections du workflow de connexion — 8 septembre 2026

Les huit constats C01–C08 de `CONNEXION_2026-09-07.md` ont été traités dans le code local. Aucun déploiement, aucune migration de production ni aucun envoi réel d'e-mail effectué.

| Constat | Comportement corrigé | Fichiers principaux |
| --- | --- | --- |
| C01 | GET affiche le compte cible et avertit d'un changement de compte ; seule une confirmation POST avec CSRF consomme le lien et ouvre la session. | `trackr_app/main.py`, `templates/confirm_login.html` |
| C02 | Invitation et travail d'envoi sont persistés, puis chaque jeton est committé avant SMTP. Un échec du commit initial empêche l'envoi ; un échec du commit du statut après SMTP ne supprime pas le jeton envoyé. | `trackr_app/invitations.py`, `trackr_app/auth_mail.py` |
| C03 | Les routes publiques enregistrent une demande pour toute adresse sans rechercher l'utilisateur. SMTP et la recherche du compte s'exécutent après la réponse HTTP, dans une session de base indépendante. | `trackr_app/auth.py`, `trackr_app/main.py`, `trackr_app/auth_mail.py` |
| C04 | Les réussites remboursent leurs réservations de quota. Le quota de connexion par adresse devient un quota adresse/IP : une autre IP ne peut plus bloquer ce quota. Les limites affichent une temporisation explicite et `Retry-After: 900`. | `trackr_app/limits.py`, `trackr_app/auth.py` |
| C05 | File `auth_mail` durable, backoff et cinq tentatives au maximum. Le worker existant reprend les demandes et les traitements interrompus. Statuts et erreurs expurgées sont supervisés. Le message utilisateur confirme la réception de la demande sans promettre un envoi déjà réussi. | `trackr_app/auth_mail.py`, `trackr_app/cli.py`, `trackr_app/monitoring.py` |
| C06 | Cookie signé d'identification du navigateur et jeton de formulaire horodaté séparément : chaque rendu dispose d'une heure, sans invalider les autres onglets. Erreurs CSRF en HTML avec lien de reprise, sans réaffichage du mot de passe. | `trackr_app/auth.py`, `templates/auth_error.html` |
| C07 | Une session déjà invalide peut être déconnectée de façon idempotente : cookie supprimé et retour à la connexion. Le CSRF reste obligatoire pour révoquer une session valide. | `trackr_app/main.py` |
| C08 | Une destination GET locale autorisée est conservée dans un cookie signé d'une heure et restaurée après connexion, définition du mot de passe ou choix de la sauter. Les URL externes, actions POST et callbacks OAuth sont exclus. | `trackr_app/auth.py`, `trackr_app/main.py`, `templates/password_form.html` |

## Livraison et concurrence

- La file chiffre l'adresse destinataire au repos et ne stocke pas le jeton brut. Une tentative crée un nouveau jeton de 15 minutes avant l'envoi.
- Un bail de cinq minutes permet de reprendre un traitement interrompu. Les verrous suivent l'ordre utilisateur → travail → invitation. Le compte et le travail sont revérifiés après le commit du jeton, avant SMTP.
- Réinitialisation, désactivation et récupération administrateur annulent aussi les demandes d'authentification en attente ou en cours. Elles ne peuvent pas être utilisées ultérieurement pour recréer des liens déjà révoqués.
- Les demandes publiques non traitées pendant une heure sont annulées. Les invitations conservent leur mécanisme de reprise et de renvoi administratif.
- Le worker `process-invitations` traite aussi la nouvelle file ; le workflow existant le lance selon sa planification de cinq minutes. Les tâches après réponse fournissent une première tentative rapide, mais la durabilité dépend du worker pour les reprises.
- `/admin/operations` inclut les échecs définitifs d'authentification et les demandes retardées de plus de quinze minutes. Le CLI affiche les états de la file et signale les erreurs en attente.

## Validation effectuée

**163 tests réussis, aucun ignoré**, avec un cluster PostgreSQL local temporaire :

```sh
.venv/bin/python scripts/test_postgres_local.py
```

La suite couvre les huit corrections, les origines étrangères et le lien entre CSRF et navigateur, les onglets simultanés, les limites de connexion par IP, les destinations interdites, le GET non consommant et le POST à usage unique, le backoff et sa limite, les adresses absentes, l'expiration des demandes, la supervision, ainsi que :

- émission effective de la réponse HTTP complète avant le démarrage du SMTP simulé ;
- deux workers PostgreSQL concurrents pour un travail : un seul envoi ;
- révocation entre le commit du jeton et l'envoi : aucun envoi et jeton révoqué ;
- échec du commit après SMTP : jeton livré toujours présent en base ;
- migration et concordance des modèles SQLite/PostgreSQL, conservation des sessions existantes.

Compilation Python, cohérence des dépendances (`pip check`) et `git diff --check` validés. Les avertissements de dépréciation sous Python 3.14 ne sont pas des échecs. Tous les échanges SMTP des tests sont simulés.

Les huit tests qui reproduisaient les défauts ont été convertis et complétés dans `audit/test_auth_review_20260907.py`. Ils vérifient désormais les comportements corrigés.

## Livraison à effectuer

La migration additive `20260908_0006` crée `auth_mail`. Elle doit être appliquée avant de servir ce code et de lancer les workers : `alembic upgrade head`, via le workflow de déploiement habituel. SQLite utilise toujours les migrations au démarrage. Aucune nouvelle dépendance ni aucun nouveau secret ne sont nécessaires ; la file utilise la clé de chiffrement existante.

Vérifier après livraison la migration, le fonctionnement du worker et une réception réelle sur une adresse de test autorisée. Les performances du proxy, l'affichage dans un vrai navigateur et la délivrabilité SMTP de production n'ont pas été mesurés dans cette intervention.

## Limites conservées explicitement

- Les quotas d'e-mail restent globaux par destinataire pour empêcher le bombardement de sa boîte ; ils peuvent toujours imposer une attente partagée entre récupération et lien de connexion. Les messages indiquent maintenant cette limite au lieu d'annoncer un e-mail inexistant. Les utilisateurs derrière une même IP partagent son quota. La suppression du verrou global par adresse pour les mots de passe reporte la protection sur les budgets adresse/IP et IP ; un dispositif supplémentaire serait nécessaire contre une attaque fortement distribuée.
- Une acceptation SMTP suivie d'un crash peut provoquer un nouvel e-mail lors de la reprise. Les liens déjà persistés restent valides jusqu'à expiration ou révocation : aucune garantie d'envoi exactement une fois n'est annoncée.
- Les renforcements séparés de l'audit — MFA, durée particulière pour les administrateurs, gestion des appareils, notifications de changement de mot de passe, purge des anciens jetons et e-mails en texte brut — restent hors de cette correction des huit constats.
