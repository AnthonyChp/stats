# Oogway Bot

Oogway Bot est un bot Discord dédié à League of Legends. Il permet notamment de lier son compte Riot et d'afficher des alertes de parties classées avec statistiques (OogScore, badges, etc.).

## Dépendances

Les principales bibliothèques Python utilisées sont listées dans `requirements.txt` :

- `discord.py`
- `SQLAlchemy` et `alembic`
- `python-dotenv`
- `pydantic` et `pydantic-settings`
- `matplotlib`
- `requests`

Installez-les avec :

```bash
pip install -r requirements.txt
```

## Variables d'environnement

Le bot lit sa configuration depuis un fichier `.env` (voir `config.py`). Les variables requises sont :

- `DISCORD_TOKEN` : jeton du bot Discord
- `RIOT_API_KEY` : clé API Riot Games
- `DB_URL` : URL de la base de données (par défaut `sqlite:///data/oogway.db`)
- `TIMEZONE` : fuseau horaire utilisé (par défaut `Europe/Paris`)
- `ALERT_CHANNEL_ID` : identifiant du salon pour les alertes
- `SUMMARY_CHANNEL_ID` : identifiant du salon pour les récapitulatifs
- `LINK_CHANNEL_ID` : salon où la commande `/link` doit être exécutée
- `APPLICATION_ID` : identifiant de l'application Discord
- `DEBUG_GUILD_ID` : identifiant du serveur de test (optionnel)

Un fichier `.env.example` est fourni à titre d'exemple.

## Base de données

La base SQLite est créée automatiquement au démarrage du bot. Tu peux aussi
l'initialiser manuellement en exécutant :

```bash
python -c "from oogway.database import init_db; init_db()"
```

## Exécution

Après avoir configuré l'environnement et installé les dépendances :

```bash
python -m oogway.bot
```

Le bot se connecte alors à Discord et synchronise ses commandes slash.

## Docker

Un `Dockerfile` et un `docker-compose.yml` sont présents pour faciliter le déploiement. L'image
installe Python ainsi que toutes les dépendances du projet. Le bot peut être lancé directement via
`docker compose` en utilisant les variables de votre fichier `.env` :

```bash
docker compose up --build
```

Le répertoire `oogway/data` est monté dans le conteneur afin de conserver la base SQLite entre les exécutions.

## Licence

Ce projet est distribué sous licence MIT.

