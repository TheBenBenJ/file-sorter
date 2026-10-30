# tri-icloud

Automatic sorting of iCloud Drive and Downloads on macOS: Claude proposes a destination and a clean name for each file; a plain script validates and moves it. Never overwrites, and when in doubt the file stays where it is.

Tri automatique d'iCloud Drive et des Téléchargements : Claude propose une destination et un nom, un script valide et déplace. Rien n'est jamais écrasé, et dans le doute le fichier reste à sa place.

## Principes

- Le modèle n'a que l'outil Read : il ne peut rien déplacer lui-même.
- `tri.py` n'accepte qu'une destination dans l'arborescence existante (un sous-niveau au plus), au-dessus d'un seuil de confiance. Sinon le fichier reste dans `00 À trier`.
- `renommer.py` et `appliquer-plan.py` procèdent par plan vérifié, puis écrivent un journal d'annulation dans `donnees/`.
- `tri.py --dry-run` ne déplace rien : commence par là.

## Installation

Il faut macOS, la [CLI Claude Code](https://code.claude.com) et une arborescence numérotée dans iCloud Drive, avec un dossier `00 À trier`.

```sh
cp reglages.exemple.json reglages.json   # ton identité et le chemin du Drive
cp regles.exemple.md regles.md           # tes règles de classement
cp convention.exemple.md convention.md   # ta convention de nommage
mkdir donnees
python3 tri.py --dry-run
```

Planifie ensuite `tri.py` avec launchd ou cron. Il lit tes documents et en envoie des extraits à l'API de Claude : à toi de juger si cela te convient pour tes fichiers sensibles. `tri.py` suppose le dossier `~/.local/share/tri-icloud` et Claude dans `/opt/homebrew/bin`.
