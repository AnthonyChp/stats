#!/usr/bin/env python3
"""
Génère TOUTES les mots de 5 lettres du français depuis Grammalecte.
Stratégie : 
  - oogle_words.txt  = solutions (mots courants, sans accents, scorés)
  - oogle_accept.txt = TOUS les mots valides (pour l'acceptation des guesses)
"""

import argparse
import logging
import sys
import unicodedata
import zipfile
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Set, List, Tuple

try:
    import requests
except ImportError:
    print("❌ pip install requests")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
log = logging.getLogger(__name__)

GRAMMALECTE_URL = "https://grammalecte.net/dic/lexique-grammalecte-fr-v7.7.zip"
WORD_LENGTH = 5


# ──────────────────────────────────────────────────────────────────────────────
# Utilitaires
# ──────────────────────────────────────────────────────────────────────────────

def strip_accents(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def is_acceptable(word_no_accent: str, original: str) -> bool:
    """
    Critères MINIMALISTES : on accepte quasi tout.
    On exclut seulement ce qui est vraiment injouable.
    """
    # Longueur exacte et lettres uniquement
    if len(word_no_accent) != WORD_LENGTH:
        return False
    if not word_no_accent.isalpha():
        return False

    # Doit être entièrement en minuscules après normalisation
    if not word_no_accent.islower():
        return False

    # Exclure noms propres (commence par majuscule dans le fichier source)
    if original and original[0].isupper():
        return False

    return True


def is_good_solution(word: str, tags: str, freq_index: int) -> bool:
    """
    Critères pour les SOLUTIONS (mot du jour) : un peu plus stricts
    pour éviter les mots vraiment obscurs comme mot du jour.
    On garde quand même énormément de mots.
    """
    # Pas de doubles voyelles typiquement non-françaises
    for pat in ("ee", "aa", "ii", "oo", "uu"):
        if pat in word:
            return False

    # Pas trop de lettres exotiques
    exotic = sum(1 for c in word if c in "wxkq")
    if exotic > 1:
        return False

    # Pas trop de consonnes d'affilée (5 consonnes = injouable)
    vowels_pos = [i for i, c in enumerate(word) if c in "aeiouy"]
    if len(vowels_pos) == 0:
        return False

    # Exclure formes verbales ultra-rares comme solution
    bad_for_solution = ("ipsi", "ppas", "ppre")
    if any(t in tags for t in bad_for_solution):
        return False

    # Fréquence : exclure les mots avec indice de fréquence très bas (< 3)
    if freq_index < 3:
        return False

    return True


# ──────────────────────────────────────────────────────────────────────────────
# Score pour trier les solutions (mots courants en premier)
# ──────────────────────────────────────────────────────────────────────────────

LETTER_SCORE = {
    "e": 15, "a": 12, "s": 11, "i": 10, "n": 10,
    "t": 10, "r":  9, "u":  9, "l":  8, "o":  8,
    "d":  5, "c":  5, "m":  5, "p":  5,
    "b":  2, "f":  2, "g":  2, "h":  2, "v":  2,
    "j":  0, "q":  0, "x": -5, "y": -5, "z": -8,
    "w": -10, "k": -8,
}

def score_word(word: str, freq_index: int) -> float:
    base = sum(LETTER_SCORE.get(c, 0) for c in word)

    # Bonus voyelles équilibrées
    v = sum(1 for c in word if c in "aeiouy")
    if 2 <= v <= 3:
        base += 10
    elif v == 1 or v == 4:
        base += 3
    else:
        base -= 10

    # Bonus fréquence (0–9 dans le lexique)
    base += freq_index * 3

    return base


# ──────────────────────────────────────────────────────────────────────────────
# Téléchargement
# ──────────────────────────────────────────────────────────────────────────────

def download_lexique(cache_dir: Path, force: bool = False) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / "lexique-grammalecte-fr-v7.7.txt"

    if out.exists() and not force:
        log.info(f"📂 Cache : {out}")
        return out

    log.info(f"📥 Téléchargement depuis {GRAMMALECTE_URL} ...")
    r = requests.get(GRAMMALECTE_URL, timeout=120)
    r.raise_for_status()
    log.info(f"✅ {len(r.content)//1024} KB téléchargés")

    with zipfile.ZipFile(BytesIO(r.content)) as zf:
        names = zf.namelist()
        log.info(f"   Fichiers ZIP : {names}")
        target = next(
            (n for n in names if n.endswith(".txt") and "lexique" in n.lower()),
            next((n for n in names if n.endswith(".txt")), None)
        )
        if not target:
            raise RuntimeError(f"Aucun .txt dans le ZIP : {names}")
        log.info(f"   Extraction : {target}")
        out.write_bytes(zf.read(target))

    log.info(f"✅ Lexique sauvegardé : {out}")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Parsing TSV
# ──────────────────────────────────────────────────────────────────────────────

def parse_lexique(path: Path) -> Tuple[Set[str], List[Tuple[str, float]]]:
    """
    Retourne :
      - accept_set  : TOUS les mots valides de 5 lettres (pour les guesses)
      - solutions   : [(mot, score)] pour les solutions triées
    """
    log.info(f"📖 Parsing : {path}")

    accept_set: Set[str] = set()
    solution_candidates: List[Tuple[str, float]] = []

    try:
        f = open(path, encoding="utf-8")
    except UnicodeDecodeError:
        f = open(path, encoding="latin-1")

    total = skipped = 0

    for line in f:
        total += 1
        line = line.rstrip("\n")

        # Ignorer entêtes et commentaires
        if not line or line.startswith("#") or line.startswith("id\t"):
            continue

        parts = line.split("\t")
        # Colonnes TSV : id fid Flexion Lemme Étiquettes Métagraphe ... Indice_fréquence
        if len(parts) < 5:
            skipped += 1
            continue

        flexion   = parts[2]   # Mot fléchi
        tags      = parts[4]   # Étiquettes morphologiques

        # Indice de fréquence = dernière colonne (0–9)
        try:
            freq_index = int(parts[-1])
        except (ValueError, IndexError):
            freq_index = 0

        # Normaliser
        word = strip_accents(flexion.lower())

        # Test acceptabilité (liste complète)
        if not is_acceptable(word, flexion):
            skipped += 1
            continue

        accept_set.add(word)

        # Test solution
        if is_good_solution(word, tags, freq_index):
            sc = score_word(word, freq_index)
            solution_candidates.append((word, sc))

    f.close()

    log.info(f"   {total:,} lignes lues, {skipped:,} ignorées")
    log.info(f"✅ {len(accept_set):,} mots acceptés (liste complète)")
    log.info(f"✅ {len(solution_candidates):,} candidats solutions")

    return accept_set, solution_candidates


# ──────────────────────────────────────────────────────────────────────────────
# Sélection et sauvegarde
# ──────────────────────────────────────────────────────────────────────────────

def select_solutions(candidates: List[Tuple[str, float]], max_count: int) -> List[str]:
    # Dédupliquer puis trier par score décroissant
    seen: dict = {}
    for word, sc in candidates:
        if word not in seen or sc > seen[word]:
            seen[word] = sc

    ranked = sorted(seen.items(), key=lambda x: x[1], reverse=True)
    selected = [w for w, _ in ranked[:max_count]]
    log.info(f"🎯 {len(selected)} solutions sélectionnées")
    if selected:
        log.info(f"   Top 15 : {', '.join(selected[:15])}")
    return selected


def save(solutions: List[str], accept: Set[str], output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    sol_file = output_dir / "oogle_words.txt"
    acc_file = output_dir / "oogle_accept.txt"

    sol_file.write_text("\n".join(sorted(solutions)) + "\n", encoding="utf-8")
    acc_file.write_text("\n".join(sorted(accept)) + "\n", encoding="utf-8")

    log.info(f"💾 {sol_file.name} : {len(solutions):,} mots")
    log.info(f"💾 {acc_file.name} : {len(accept):,} mots")


def stats(solutions: List[str], accept: Set[str]):
    print()
    print("=" * 60)
    print("📊  STATISTIQUES")
    print("=" * 60)
    print(f"  Solutions  : {len(solutions):,}")
    print(f"  Acceptés   : {len(accept):,}")

    freq = Counter("".join(solutions))
    total_letters = sum(freq.values())
    print()
    print("  Top 10 lettres dans les solutions :")
    for letter, count in freq.most_common(10):
        pct = count / total_letters * 100
        print(f"    {letter.upper()} : {'█' * int(pct)} {pct:.1f}%")

    exotic = [w for w in solutions if any(c in "wxkqz" for c in w)]
    print(f"\n  Mots avec W/X/K/Q/Z : {len(exotic)} ({len(exotic)/len(solutions)*100:.1f}%)")
    print("=" * 60)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Génère les listes OOGLE depuis Grammalecte (version complète)")
    ap.add_argument("--output-dir",    type=Path, default=Path("data"))
    ap.add_argument("--max-solutions", type=int,  default=1500,
                    help="Nombre maximum de mots solutions (défaut : 1500)")
    ap.add_argument("--force",         action="store_true",
                    help="Re-télécharger même si le cache existe")
    args = ap.parse_args()

    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  🎮 OOGLE – Générateur de listes COMPLET                    ║")
    print("║  📚 Source : Grammalecte (tous les mots 5 lettres du FR)    ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    cache_dir  = args.output_dir / ".cache"
    lexique    = download_lexique(cache_dir, args.force)
    accept, candidates = parse_lexique(lexique)

    if not accept:
        log.error("❌ Aucun mot trouvé !")
        return 1

    solutions = select_solutions(candidates, args.max_solutions)
    save(solutions, accept, args.output_dir)
    stats(solutions, accept)

    print()
    print("✅  Génération terminée !")
    print(f"📁  Fichiers dans : {args.output_dir}/")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
