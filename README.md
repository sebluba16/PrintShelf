# PrintShelf 📦

Serveur web pour ranger et retrouver ses fichiers **3MF** (Bambu Studio / Orca / Prusa…).

- **Indexation** récursive d'un dossier de fichiers `.3mf` (base SQLite locale)
- **Aperçu** : affiche l'image contenue dans le 3MF pour voir ce qu'il y a dedans
- **Tags** et **description** modifiables depuis le navigateur
- **Recherche** (nom, tag, description) et filtre par tag
- **Ouverture directe dans Bambu Studio** d'un simple clic
- Python 3 seul, **aucune dépendance**

## Démarrage

```bash
python3 server3mf.py /chemin/vers/tes/3mf --port 8000
```

Puis ouvrir `http://<ip-du-serveur>:8000`.

Options :

| Option | Défaut | Rôle |
|---|---|---|
| `root` (argument) | `./3mf` ou `$MF_ROOT` | Dossier à indexer |
| `--host` | `0.0.0.0` | Adresse d'écoute |
| `--port` | `8000` | Port |

Les tags et descriptions sont stockés dans `<dossier>/.library.db`. Le bouton **Réindexer** prend en compte les nouveaux fichiers, sans perdre les tags existants.

## Arborescence

Le dossier de stockage est une vraie arborescence de dossiers.

- Panneau de gauche : navigation par dossier (avec le nombre de fichiers, sous-dossiers inclus).
- **📁 Nouveau dossier** : crée un dossier dans le dossier courant (`Voitures/F1` crée toute la chaîne).
- Dans la fiche d'un fichier, le champ **Dossier** permet de le déplacer (les tags et la description suivent).
- Dossier sélectionné : **✏ Renommer** (les fichiers gardent leurs tags) et **🗑 Supprimer** (uniquement s'il ne contient aucun fichier, pour ne rien perdre par erreur).
- Dans la fiche, le champ **Nom du fichier** permet de renommer un fichier (`.3mf` ajouté si absent).
- À l'import, choisis le **dossier de destination** et décide de **garder l'arborescence** du dossier source ou non.

## Importer depuis plusieurs PC

Un seul serveur = un seul dépôt. Depuis **n'importe quel PC** :

1. Ouvre PrintShelf dans le navigateur.
2. Clique sur **⬆ Importer depuis ce PC** et choisis le dossier local contenant des 3MF.
3. Coche les fichiers à transférer (ou tout cocher / tout décocher), puis **Transférer la sélection**.

Les fichiers arrivent dans le dossier de destination choisi. Les doublons (même contenu, même nom ou non) sont détectés et ignorés ; un nom déjà pris reçoit un suffixe `(1)`.

## Ouvrir dans Bambu Studio

Le bouton utilise le lien `bambustudio://open?file=<url>` : Bambu Studio télécharge le fichier depuis le serveur.

- Ouvre PrintShelf avec une adresse **joignable depuis ton PC** (pas `localhost` si le serveur est ailleurs), car le lien reprend l'adresse du navigateur.
- Bambu Studio doit être installé pour que le navigateur reconnaisse le lien `bambustudio://`.

## Miniatures

Lues dans le 3MF, par ordre de préférence : `Metadata/plate_1.png`, `Metadata/thumbnail.png`, `Metadata/top_1.png`, puis le premier PNG trouvé.

## Supprimer un fichier

Dans la fiche : **🗑 Supprimer** (avec confirmation). Le fichier est effacé du disque et de l'index. C'est définitif.

## Sécurité

Protections en place :
- **Anti-CSRF** : toute modification exige l'en-tête `X-Requested-With: PrintShelf` (donc une requête « preflight » qu'un autre site ne peut pas obtenir) et un `Origin` identique au serveur. Un site malveillant ouvert dans ton navigateur ne peut pas supprimer ou envoyer de fichiers.
- **Chemins** : dossiers et noms nettoyés, `..` et dossiers cachés refusés, fichiers hors du dossier de stockage (liens symboliques) ignorés.
- **Limites** : taille max d'import (`--max-mb`, 2048 par défaut), corps JSON limité, miniature limitée à 20 Mo (anti zip-bomb), import refusé si ce n'est pas un vrai zip.
- **Navigateur** : en-têtes `Content-Security-Policy`, `X-Frame-Options: DENY`, `nosniff` ; tout le contenu affiché est échappé (anti-XSS) ; requêtes SQL paramétrées.

À savoir : **il n'y a pas d'authentification**. Toute personne qui atteint le serveur peut lire, importer et supprimer. Utilise-le sur un réseau de confiance (maison), ou derrière un reverse proxy avec mot de passe / VPN (Tailscale, WireGuard). Ne l'expose pas sur Internet tel quel.

## Licence

Voir [LICENSE](LICENSE).
