# Fiabilisation Vercel — avancement du 8 septembre 2026

Ce document décrit les changements locaux et les travaux restants du plan en quatre lots. Il ne constitue pas une validation de production. Aucun déploiement, migration de production ou envoi réel n'a été effectué.

Mise à jour : 9 septembre 2026.

## Changements implémentés

### Sécurité et configuration

- FastAPI 0.141.1 / Starlette 1.6.0 ; dépendances vulnérables associées mises à jour. `requirements.lock` contraint les dépendances transitives, en tenant compte de Python 3.12/Linux et de l'environnement local. L'audit CI utilise ce verrou et bloque sur une vulnérabilité connue. Le verrou ne contient pas de hashes.
- Les deux adaptateurs SMTP utilisent `ssl.create_default_context()` pour STARTTLS. Un refus TLS interrompt le traitement avant l'authentification SMTP.
- Middleware ASGI : formulaires limités à 64 Kio avant parsing, y compris les flux sans `Content-Length` ou dont l'en-tête ment ; réponse 413. CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`. CSS relatif.
- `APP_URL` est l'origine HTTPS canonique. `ALLOWED_HOSTS` contient les noms d'hôtes supplémentaires, et `AUTH_ALLOWED_ORIGINS` leurs origines HTTPS autorisées pour les formulaires. Une autorité arbitraire reçue n'est plus ajoutée en environnement hébergé. Le comportement permissif local reste réservé au développement hors Vercel.
- Sur Vercel et en preview : rejet de SQLite et des clés de développement. Une preview exige `PREVIEW_ISOLATED=true`. Cette déclaration ne prouve pas l'isolation des secrets ou de la base : les réglages privés doivent toujours être vérifiés.
- Configuration Vercel sans `builds`, avec `functions.api/index.py.maxDuration=60`. Exclusions des audits, sauvegardes et fichiers privés ; contrôle de `.vercel/output` avant livraison de l'artefact précompilé. Le véritable build Vercel reste à exécuter en CI.
- Variables GitHub `APP_URL`, `ALLOWED_HOSTS`, `AUTH_ALLOWED_ORIGINS` propagées au site ; domaine canonique historique conservé par défaut.

### PostgreSQL et authentification

- `MIGRATION_DATABASE_URL` est réservée à la connexion Neon directe des migrations ; `DATABASE_URL` reste dédiée au pooler de l'application et des workers. La révision additive `20260909_0007` ajoute uniquement les tables de travaux, snapshots et nonces OAuth.
- Pool applicatif 2 connexions, dépassement 3, attente 5 s, connexion 5 s ; délai de verrou 2 s par transaction et `pool_pre_ping`. Paramètres configurables dans `.env.example`.
- Le jeton et la réservation de l'e-mail d'authentification sont persistés avant SMTP. L'autorisation est revérifiée, puis la transaction est fermée avant l'appel externe. La finalisation vérifie le statut, le numéro de tentative et sa date de réservation.
- Une révocation pendant SMTP peut prendre le verrou utilisateur sans attendre le fournisseur. Elle supprime les jetons et annule la demande ; la finalisation ne rétablit pas le statut envoyé. Un message déjà confié au fournisseur ne peut pas être rappelé.
- Une entrée indéchiffrable est marquée en échec définitif et ne bloque plus les suivantes. L'invitation associée est également marquée en échec. Les erreurs sont expurgées et les demandes en attente reçoivent un backoff ; la reprise administrative est ciblée (`retry-failed --kind auth --id ...`).
- Nouveau workflow `authentication.yml`, toutes les cinq minutes, indépendant des collecteurs. Les demandes publiques sont prioritaires, puis les invitations sont traitées. La tâche après réponse reste la première tentative rapide. Le traitement d'authentification dispose d'un budget de lancement de 90 s ; un appel SMTP déjà commencé peut dépasser ce budget et être coupé par GitHub.
- Livraison au moins une fois : une panne après acceptation SMTP mais avant confirmation en base peut produire un doublon. Aucune garantie « exactement une fois ».

### Alertes, digests et files Notion/historiques

- Réservations persistées avant l'appel distant, puis finalisation conditionnelle sur la réservation. Les verrous utilisateur/source et connexions SQL sont libérés pendant les envois des workers.
- Alertes/digests : lots de 100 offres, parcours des utilisateurs par lots SQL de 100. Les réservations actives empêchent un second envoi simultané pour le même utilisateur. Les tentatives interrompues finissent en échec après épuisement des reprises.
- Un digest supérieur à 100 offres est livré en plusieurs messages, sur des invocations successives. Le jour n'est marqué terminé qu'après traitement du reliquat. Une arrivée après clôture du digest attend toujours le lendemain.
- Synchronisation Notion : lot de 100 travaux, bail dans `next_attempt_at` pour le statut `processing`, compteur de tentatives et finalisation conditionnelle. Vérification du budget avant chaque requête. Une révocation ou suppression concurrente ne peut pas être annulée par la finalisation.
- File historique : lot de 100, même séparation réservation/appel/finalisation. JSON invalide mis en échec définitif sans bloquer la suite. Un nouveau payload n'est pas remplacé par le résultat d'un ancien envoi ; il conserve l'échéance de réservation avant sa reprise.
- Supervision : inclusion des échecs et retards historiques, comptes et ancienneté des files d'authentification, alertes et historique.

### Dashboard

- Filtres, tri, total et pagination passent en SQL. Vingt offres maximum sont hydratées, puis leurs sources chargées en lot. Les anciennes offres sans source associée gardent leur affichage et leurs options de filtrage.
- Test avec 10 000 offres et 20 utilisateurs : vingt objets Offer chargés pour la deuxième page. Le scénario local PostgreSQL à 25 requêtes concurrentes a mesuré 0,283 s au premier appel, 0,205 s à chaud, médiane 1,118 s et maximum 1,689 s. Il ne représente ni Neon ni un démarrage à froid Vercel.

## Vérifications

- Suite complète avec PostgreSQL éphémère : 203 tests réussis, aucun test ignoré.
- Validation syntaxique de tous les workflows GitHub et audit des dépendances : valides ; aucune vulnérabilité connue dans le verrou runtime.
- La migration 0007 est testée dans les deux états : le web de transition accepte 0006 et 0007, tandis que les workers ne mutent pas avant 0007. La procédure de release candidate, de migration drainée et de rollback est couverte par test sans appeler Vercel.
- Régressions sur deux workers, jeton persisté avant envoi, révocation entre réservation et envoi, révocation pendant SMTP bloqué, panne du commit après SMTP.
- Contrôles des formulaires trop volumineux sans Content-Length, certificats refusés / STARTTLS absent, origines étrangères et alias explicites, dépendances, exclusions d'artefact et pagination bornée.
- Environnement local Python 3.14 ; CI configurée pour Python 3.12. Deux avertissements de dépréciation Starlette/TestClient demeurent sans échec fonctionnel.

## Fonctions restantes à valider hors dépôt

Les mécanismes prévus sont implémentés : workers séparés par source, baux et générations, matching en lots, création Notion durable et récupération incertaine, nonce OAuth monousage lié à la session, CSV PostgreSQL/export administrateur, purge, supervision et rollback contrôlé.

La recette sur une preview Vercel isolée reste nécessaire, car elle dépend des secrets, domaines, Neon, SMTP et quotas réels : invitation, confirmation, premier mot de passe, reset, multi-onglets, logout/reconnexion, sauvegardes et mesure du démarrage à froid. Aucun déploiement ni e-mail réel n'ont été faits depuis ce dépôt.

## Configuration avant la première livraison

- Fournir `MIGRATION_DATABASE_URL` directe dans les secrets GitHub production ; `DATABASE_URL` désigne le pooler Neon.
- Si des alias sont nécessaires : renseigner conjointement `ALLOWED_HOSTS` et `AUTH_ALLOWED_ORIGINS`. Un alias Vercel généré n'est pas accepté automatiquement.
- Pour une preview : base séparée, clés séparées, origine propre et `PREVIEW_ISOLATED=true`, avec SMTP de recette. Ne pas recopier les secrets de production.
- Exécuter le build contrôlé en CI et la recette isolée avant de considérer ce lot validé sur Vercel.

Références de configuration : [Vercel](https://vercel.com/docs/project-configuration/vercel-json), [Python TLS](https://docs.python.org/3.12/library/ssl.html).
