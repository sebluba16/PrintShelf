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

## Ouvrir dans Bambu Studio

Le bouton utilise le lien `bambustudio://open?file=<url>` : Bambu Studio télécharge le fichier depuis le serveur.

- Ouvre PrintShelf avec une adresse **joignable depuis ton PC** (pas `localhost` si le serveur est ailleurs), car le lien reprend l'adresse du navigateur.
- Bambu Studio doit être installé pour que le navigateur reconnaisse le lien `bambustudio://`.

## Miniatures

Lues dans le 3MF, par ordre de préférence : `Metadata/plate_1.png`, `Metadata/thumbnail.png`, `Metadata/top_1.png`, puis le premier PNG trouvé.

## Sécurité

Aucune authentification : à utiliser sur un réseau de confiance, ou derrière un reverse proxy authentifié.

## Licence

Voir [LICENSE](LICENSE).
