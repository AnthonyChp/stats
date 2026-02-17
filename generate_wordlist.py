#!/usr/bin/env python3
"""
Génère TOUS les mots de 5 lettres du français depuis Grammalecte.
Fix: ligatures œ/æ converties AVANT NFD, filtre longueur APRES normalisation.
"""

import argparse
import logging
import sys
import unicodedata
import zipfile
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Set, List, Dict, Tuple

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
log = logging.getLogger(__name__)

GRAMMALECTE_URL = "https://grammalecte.net/dic/lexique-grammalecte-fr-v7.7.zip"
WORD_LENGTH = 5

LIGATURES = {
    "\u0153": "oe",  # œ
    "\u0152": "OE",  # Œ
    "\u00e6": "ae",  # æ
    "\u00c6": "AE",  # Æ
    "\u00df": "ss",  # ß
    "\u0132": "IJ",  # Ĳ
    "\u0133": "ij",  # ĳ
    "\u1d6b": "ue",  # ᵫ
}


def normalize(text: str) -> str:
    """
    Normalise un mot :
    1. Remplacer les ligatures (œ→oe, æ→ae, etc.)
    2. NFD + supprimer les diacritiques
    3. Minuscules
    4. Ne garder que a-z
    """
    # Étape 1 : ligatures explicites
    for src, dst in LIGATURES.items():
        text = text.replace(src, dst)

    # Étape 2 : NFD + suppression diacritiques
    nfd = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")

    # Étape 3 : minuscules + ne garder que a-z
    return "".join(c for c in stripped.lower() if "a" <= c <= "z")


# ──────────────────────────────────────────────────────────────────────────────
# Score pour classer les solutions
# ──────────────────────────────────────────────────────────────────────────────

LETTER_SCORE = {
    "e": 15, "a": 12, "s": 11, "i": 10, "n": 10,
    "t": 10, "r":  9, "u":  9, "l":  8, "o":  8,
    "d":  5, "c":  5, "m":  5, "p":  5,
    "b":  2, "f":  2, "g":  2, "h":  2, "v":  2,
    "j":  0, "q":  0, "x": -5, "y": -3, "z": -5,
    "w": -8, "k": -5,
}

def score_word(word: str, freq: int) -> float:
    base = sum(LETTER_SCORE.get(c, 0) for c in word)
    v = sum(1 for c in word if c in "aeiouy")
    if 2 <= v <= 3:
        base += 10
    elif v == 1 or v == 4:
        base += 3
    else:
        base -= 10
    base += freq * 4
    return base


# ──────────────────────────────────────────────────────────────────────────────
# Téléchargement
# ──────────────────────────────────────────────────────────────────────────────

def download_lexique(cache_dir: Path, force: bool = False) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / "lexique-grammalecte-fr-v7.7.txt"

    if out.exists() and not force:
        log.info(f"Cache : {out} ({out.stat().st_size // 1024} KB)")
        return out

    log.info(f"Telechargement {GRAMMALECTE_URL} ...")
    r = requests.get(GRAMMALECTE_URL, timeout=120)
    r.raise_for_status()
    log.info(f"OK {len(r.content) // 1024} KB")

    with zipfile.ZipFile(BytesIO(r.content)) as zf:
        names = zf.namelist()
        target = next(
            (n for n in names if n.endswith(".txt") and "lexique" in n.lower()),
            next((n for n in names if n.endswith(".txt")), None)
        )
        if not target:
            raise RuntimeError(f"Aucun .txt dans le ZIP : {names}")
        out.write_bytes(zf.read(target))

    log.info(f"Sauvegarde : {out}")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Parsing TSV
# Colonnes : id fid Flexion Lemme Étiquettes ... Indice_fréquence(dernière)
# ──────────────────────────────────────────────────────────────────────────────

# Patterns interdits partout
BAD_PATTERNS = ["aa", "ii", "uu", "ww", "kk"]

# Tags exclus uniquement pour les solutions (pas pour l'accept)
EXCLUDE_SOL_TAGS = ["ppas", "ppre", "ipsi"]


def parse_lexique(path: Path) -> Tuple[Set[str], Dict[str, float]]:
    log.info(f"Parsing : {path}")

    try:
        fh = open(path, encoding="utf-8")
    except UnicodeDecodeError:
        fh = open(path, encoding="latin-1")

    accept: Set[str] = set()
    solutions: Dict[str, float] = {}

    total = skip_propre = skip_len = skip_pattern = 0

    for line in fh:
        total += 1
        line = line.rstrip("\n")
        if not line or line.startswith("#") or line.startswith("id\t"):
            continue

        parts = line.split("\t")
        if len(parts) < 5:
            continue

        flexion = parts[2]
        tags    = parts[4]

        try:
            freq = int(parts[-1])
        except (ValueError, IndexError):
            freq = 0

        # Noms propres
        if flexion and flexion[0].isupper():
            skip_propre += 1
            continue

        # Normaliser (ligatures + accents)
        word = normalize(flexion)

        # Longueur exacte APRES normalisation
        if len(word) != WORD_LENGTH:
            skip_len += 1
            continue

        # Patterns aberrants
        if any(p in word for p in BAD_PATTERNS):
            skip_pattern += 1
            continue

        # Ajouter a l'accept (liste complete)
        accept.add(word)

        # Candidat solution ?
        if (
            not any(t in tags for t in EXCLUDE_SOL_TAGS)
            and sum(1 for c in word if c in "aeiouy") > 0
            and sum(1 for c in word if c in "wxkq") <= 1
        ):
            sc = score_word(word, freq)
            if word not in solutions or sc > solutions[word]:
                solutions[word] = sc

    fh.close()

    log.info(f"{total:,} lignes lues")
    log.info(f"Noms propres exclus : {skip_propre:,}")
    log.info(f"Mauvaise longueur   : {skip_len:,}")
    log.info(f"Patterns aberrants  : {skip_pattern:,}")
    log.info(f"ACCEPT   : {len(accept):,} mots uniques")
    log.info(f"SOL.     : {len(solutions):,} candidats solutions")
    return accept, solutions


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir",    type=Path, default=Path("data"))
    ap.add_argument("--max-solutions", type=int,  default=2000)
    ap.add_argument("--force",         action="store_true")
    ap.add_argument("--debug",         action="store_true",
                    help="Afficher des exemples de mots rejetés pour debug")
    args = ap.parse_args()

    print()
    print("OOGLE - Generateur MAXIMAL v3")
    print("Fix ligatures oe/ae + filtre post-normalisation")
    print()

    cache_dir = args.output_dir / ".cache"
    lexique   = download_lexique(cache_dir, args.force)

    # Debug : tester normalize() sur quelques mots avant parsing complet
    if args.debug:
        test_words = ["bœuf", "cœurs", "œuvre", "sœurs", "mœurs", "nœud",
                      "naïve", "Noël", "château", "après", "état"]
        print("=== DEBUG normalize() ===")
        for w in test_words:
            n = normalize(w)
            print(f"  {w!r:15} -> {n!r:15} (len={len(n)})")
        print()

    accept, candidates = parse_lexique(lexique)

    if not accept:
        log.error("Aucun mot trouve !")
        return 1

    # Trier solutions par score
    ranked   = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
    selected = [w for w, _ in ranked[:args.max_solutions]]

    log.info(f"Top 20 solutions : {', '.join(selected[:20])}")

    # Sauvegarder
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "oogle_words.txt").write_text(
        "\n".join(sorted(selected)) + "\n", encoding="utf-8"
    )
    (args.output_dir / "oogle_accept.txt").write_text(
        "\n".join(sorted(accept)) + "\n", encoding="utf-8"
    )

    # Stats
    print()
    print("=" * 55)
    print(f"  Solutions : {len(selected):,}")
    print(f"  Acceptes  : {len(accept):,}")
    freq = Counter("".join(selected))
    total_l = sum(freq.values())
    print("  Top 8 lettres :")
    for l, c in freq.most_common(8):
        bar = "█" * int(c / total_l * 150)
        print(f"    {l.upper()}  {bar:<25}  {c/total_l*100:.1f}%")

    # Exemples mots avec oe (anciennement œ)
    oe_sample = sorted(w for w in accept if "oe" in w)[:8]
    if oe_sample:
        print(f"  Ex. mots avec oe (ex-œ) : {', '.join(oe_sample)}")
    print("=" * 55)
    print()
    print(f"Fichiers dans : {args.output_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
