# Audit du workflow de connexion — 7 septembre 2026

**Mise à jour du 8 septembre :** les constats ci-dessous décrivent le code avant correction. Voir [les corrections et leur validation](CORRECTIONS_CONNEXION_2026-09-08.md). Les tests de reproduction ont depuis été convertis en tests de non-régression.

Audit du code local : invitations, connexion par mot de passe et par lien, première définition/récupération du mot de passe, cookies, renouvellement et révocation, déconnexion, accès utilisateur/admin et transition OAuth Notion. Lecture des modèles, migrations, configuration, templates, envoi SMTP et tests associés. Aucun changement du code applicatif.

**Résultat : 8 défauts confirmés, dont 6 de gravité moyenne et 2 faible. Aucun contournement critique d'authentification démontré.** Les renforcements proposés plus bas ne sont pas comptés comme vulnérabilités avérées.

Validation : suite existante exécutée avec `scripts/test_postgres_local.py` : **136 tests réussis**, PostgreSQL temporaire inclus. Reproductions additionnelles dans `audit/test_auth_review_20260907.py`. Lors de l'audit initial, ces tests décrivaient les défauts : leur réussite confirmait leur présence, pas leur correction. Leur version actuelle vérifie les corrections du 8 septembre. SMTP simulé ; aucune écriture de production, aucun e-mail réel, aucune opération distante Notion. Pas de recette navigateur ni d'inspection du proxy, des secrets ou des journaux de production. Cet audit est indépendant de la tentative Strix antérieure interrompue.

## Défauts confirmés

### C01 — Un GET consomme le lien et change silencieusement le compte — Moyenne

- **Source :** `trackr_app/main.py:137–155`.
- **Constat :** ouvrir `/auth/consume/{raw}` consomme le jeton, accepte l'invitation et remplace le cookie de session sans confirmation ni contrôle du compte déjà connecté.
- **Reproduction :** connecté au compte A, ouvrir un lien valide du compte B : la session devient celle de B. Une seconde ouverture échoue. Couvert par `test_get_consumes_link_and_silently_switches_account`.
- **Impact :** un scanner d'e-mails qui effectue un GET peut épuiser le lien avant l'utilisateur. Un membre malveillant peut transmettre son propre lien et amener une victime à travailler dans son compte, notamment à y rattacher sa connexion Notion si elle poursuit le parcours. Cela ne donne pas directement accès au compte initial de la victime. Le comportement réel des scanners n'a pas été testé.
- **Correction :** GET d'aperçu sans mutation, puis confirmation POST protégée par CSRF, indiquant le compte cible et avertissant d'un changement de compte. Consommer le jeton uniquement au POST.

### C02 — Une invitation peut contenir un lien qui n'a jamais été sauvegardé — Moyenne

- **Source :** `trackr_app/invitations.py:23–36`.
- **Constat :** l'envoi SMTP précède le commit qui rend le MagicLink durable. Les demandes publiques de lien, elles, committent déjà avant envoi.
- **Reproduction :** simuler un envoi accepté puis un échec de commit : une URL a été envoyée mais aucun jeton correspondant ne subsiste après rollback. Couvert par `test_invitation_can_be_sent_before_failed_commit`.
- **Impact :** lien invalide reçu après panne de base ou crash ; fenêtre également possible entre réception et commit. Si l'envoi est accepté puis sa confirmation échoue, le traitement d'erreur retire le lien avant commit. Le retry peut envoyer un autre lien sans rendre le premier utilisable.
- **Correction :** persister le jeton et une intention d'envoi avant SMTP, avec état de livraison/reprise durable ; tenir compte de la durée de validité lors des retries.

### C03 — Le temps de réponse révèle potentiellement l'existence d'un compte actif — Moyenne

- **Source :** `trackr_app/auth.py:129–139`, `trackr_app/main.py:124–133`, `trackr_app/emailing.py:72–79`.
- **Constat :** seul un compte actif déclenche un échange SMTP synchrone. Le texte générique égalise le contenu, pas la durée.
- **Reproduction :** SMTP simulé à 200 ms : la requête pour un compte existant attend ce délai, celle pour un compte absent non. Couvert par `test_email_delivery_adds_account_dependent_latency`.
- **Impact :** canal temporel d'énumération, limité par les quotas et le bruit réseau ; son exploitabilité statistique en production n'a pas été mesurée. Le timeout SMTP immobilise aussi la requête pendant les échanges réseau.
- **Correction :** remettre une demande à un traitement asynchrone durable et répondre par un chemin comparable pour toute adresse, avec quotas conservés.

### C04 — Quotas utilisables pour bloquer un compte et retours trompeurs — Moyenne

- **Source :** `trackr_app/limits.py:14–33`, `trackr_app/auth.py:101–107`, `trackr_app/main.py:122–123`.
- **Constat :** budget par adresse partagé entre tous les clients ; 10 connexions par fenêtre fixe de 15 minutes, y compris celles réussies. Le budget d'envoi est partagé entre récupération et connexion par lien : 1/minute, 5/15 minutes par adresse, 20/15 minutes par IP.
- **Reproduction :** après 10 succès, le bon mot de passe est refusé ; demander un lien de connexion puis immédiatement un reset n'envoie pas le second e-mail, mais annonce qu'il est en route. Deux tests additionnels couvrent ces scénarios ; le test existant `test_login_limits_separate_from_email_limits` confirme le rejet d'un bon mot de passe après 10 échecs.
- **Impact :** quiconque connaît l'adresse peut saturer son budget de connexion. Les utilisateurs derrière une IP commune se pénalisent également. Le refus ressemble à un mauvais mot de passe ou à un e-mail perdu ; la durée restante n'est pas indiquée. Un blocage dure jusqu'à la prochaine fenêtre, au maximum 15 minutes, et peut être entretenu.
- **Correction :** conserver les protections anti-bruteforce tout en distinguant échecs et succès, combiner budgets adresse/IP et temporisation progressive, et afficher un état de temporisation générique avec délai de reprise. Pour les e-mails, expliciter le délai commun aux deux parcours sans révéler l'existence du compte.

### C05 — Les échecs SMTP des liens publics sont perdus sans reprise — Moyenne

- **Source :** `trackr_app/auth.py:132–140`, `trackr_app/main.py:126–134`.
- **Constat :** après création du jeton, une erreur SMTP produit seulement un `print` expurgé. Aucun travail à reprendre ni état de livraison n'est enregistré pour ces deux routes, contrairement aux invitations.
- **Preuve :** lecture des branches d'erreur et test existant `test_mail_failure_is_generic_and_never_logs_token` ; le log est correctement expurgé mais le message utilisateur annonce toujours un lien en route.
- **Impact :** récupération et première connexion échouent silencieusement pendant une panne transitoire. L'utilisateur doit recommencer et son quota a déjà été consommé. `/health` vérifie la base, pas la livraison de ces liens.
- **Correction :** file persistante, retries bornés générant un lien encore valide, supervision des échecs et message générique exact (« demande reçue »). Ne pas remplacer par une erreur réservée aux adresses existantes, ce qui aggraverait C03.

### C06 — Un formulaire fraîchement chargé peut expirer quelques secondes plus tard — Moyenne

- **Source :** `trackr_app/auth.py:45–68`.
- **Constat :** `auth_page` réutilise un jeton signé encore valide et prolonge seulement la durée du cookie. La signature garde son horodatage initial et expire après une heure.
- **Reproduction :** création à T, chargement d'un nouveau formulaire à T+3590 s, envoi à T+3601 s : erreur 403 JSON après seulement 11 secondes sur la nouvelle page. Couvert par `test_recently_loaded_form_can_already_be_expiring`.
- **Impact :** blocage apparent avec identifiants corrects, sortie de l'interface HTML et saisie à refaire. Une page simplement laissée ouverte plus d'une heure subit aussi cette sortie brutale.
- **Correction :** renouveler le jeton proche de l'expiration avec une stratégie compatible multi-onglets ; rendre une page HTML permettant de reprendre, sans conserver le mot de passe. Ne pas supprimer le contrôle CSRF.

### C07 — Déconnexion en erreur après expiration ou révocation — Faible

- **Source :** `trackr_app/main.py:79–82,159–168`.
- **Reproduction :** garder une page ouverte, révoquer sa session, puis utiliser son bouton de déconnexion : 403 JSON, sans suppression du cookie. Couvert par `test_logout_after_revocation_returns_json_error`. L'expiration suit la même branche de validation.
- **Impact :** parcours cassé après reset depuis un autre appareil, désactivation ou session expirée. Cela ne rétablit pas l'accès de la session révoquée.
- **Correction :** rendre la déconnexion idempotente pour une session déjà invalide : supprimer le cookie et revenir à la connexion ; conserver le CSRF pour révoquer une session encore valide.

### C08 — La connexion perd la destination initiale — Faible

- **Source :** `trackr_app/main.py:66–69`, `trackr_app/auth.py:111`, `trackr_app/main.py:154`.
- **Reproduction :** visiter `/preferences` sans session, se connecter : arrivée sur `/dashboard`. Aucun retour à la page demandée. Couvert par `test_login_loses_original_destination`.
- **Impact :** interruption des liens directs et parcours de configuration ; un callback OAuth reçu après perte de session n'est pas repris automatiquement après reconnexion.
- **Correction :** conserver une destination locale validée pour les navigations GET ordinaires ; ne pas rejouer automatiquement un POST ou un callback OAuth périmé. Relancer explicitement OAuth si nécessaire. Refuser les URL externes pour éviter une redirection ouverte.

## Renforcements distincts des défauts

- **Sessions longues et administration :** `sessions.py:11–12` applique 90 jours glissants, plafonnés à un an, aux abonnés comme aux administrateurs. C'est un choix documenté. Il n'existe pas de MFA, de réauthentification pour les actions sensibles, de durée courte optionnelle, ni d'écran pour révoquer les autres appareils. Prévoir ces protections selon le niveau de risque ; une session volée reste exploitable jusqu'à expiration ou révocation.
- **Récupération multi-comptes :** `auth_page` fournit toujours `user=None` et le formulaire de reset ne montre pas le compte cible. Afficher une identité appropriée, ainsi qu'une notification après changement de mot de passe ; aujourd'hui le changement ne déclenche pas d'e-mail d'alerte.
- **Maintenance :** aucune purge périodique des `MagicLink`, `PasswordToken` et `UserSession` expirés trouvée. Ils restent invalides mais s'accumulent. Ajouter une purge bornée et des événements d'authentification expurgés pour le diagnostic.
- **E-mails accessibles :** la partie texte de `send_email` dit seulement d'ouvrir un client HTML (`emailing.py:70`). Inclure aussi l'URL et l'expiration dans le texte brut pour les clients qui n'affichent pas le HTML.

## Contrôles positifs et limites

- Mots de passe hachés via Argon2 ; longueur serveur de 12 à 128 caractères ; hash factice pour les comptes absents/inactifs lors de la connexion.
- Jetons aléatoires de 32 octets, empreintes en base, expiration de 15 minutes, consommation conditionnelle atomique. Le GET de reset ne consomme pas le jeton.
- CSRF sur les POST publics et authentifiés ; contrôle de l'origine lorsqu'elle est fournie sur les formulaires publics ; templates avec échappement par défaut.
- Cookies HttpOnly, SameSite=Lax, Secure lorsque APP_URL est HTTPS ; no-store et no-referrer sur les routes d'authentification et les requêtes portant une session.
- Reset/définition du mot de passe et désactivation révoquent les sessions et liens. Les tests PostgreSQL couvrent notamment les resets et renouvellements concurrents.
- Accès admin contrôlé par rôle ; état OAuth signé, limité dans le temps et lié à l'utilisateur. Sa non-unicité et l'absence de liaison à une session précise méritent un renforcement, mais aucun détournement OAuth autonome n'a été démontré ici.
- Les protections proxy/IP, en-têtes HTTPS/CSP/anti-framing, masquage des URLs secrètes dans les logs d'accès, livraison réelle et migration effectivement déployée restent à vérifier sur l'environnement servi. Leur absence en production n'est pas déduite de la seule lecture du dépôt.

## Ordre conseillé

1. Corriger C01 et C02 : consommation explicite des liens et durabilité des invitations.
2. Traiter ensemble C03/C05 par une livraison asynchrone persistante, puis C04 sans affaiblir l'anti-bruteforce.
3. Corriger C06, C07 et C08 ; compléter par une recette navigateur multi-onglets et multi-comptes.
4. Choisir la politique de sessions administrateur, ajouter supervision/purge et vérifier le déploiement réel.

Reproduire les constats localement sans charger `.env` :

```sh
PYTHON_DOTENV_DISABLED=1 DATABASE_URL=sqlite:// ENVIRONMENT=development .venv/bin/python -m pytest -q audit/test_auth_review_20260907.py
```
