#!/usr/bin/env python3
import unicodedata
from pathlib import Path

WORD_LENGTH = 5

GRAMMALECTE_FILE = Path("data/.cache/lexique-grammalecte-fr-v7.7.txt")
HUNSPELL_FILE = Path("/usr/share/hunspell/fr_FR.dic")

LIGATURES = {
    "œ": "oe",
    "Œ": "oe",
    "æ": "ae",
    "Æ": "ae",
}

BAD_PATTERNS = ["kk", "ww", "xx", "zz"]


def normalize(text: str) -> str:
    for src, dst in LIGATURES.items():
        text = text.replace(src, dst)

    nfd = unicodedata.normalize("NFD", text)

    text = "".join(
        c for c in nfd
        if unicodedata.category(c) != "Mn"
    )

    return text.lower()


def clean_word(w: str) -> str | None:
    w = normalize(w)

    if len(w) != WORD_LENGTH:
        return None

    if not w.isalpha():
        return None

    if any(p in w for p in BAD_PATTERNS):
        return None

    return w


def parse_grammalecte() -> set[str]:
    words = set()

    with open(GRAMMALECTE_FILE, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or line.startswith("id\t"):
                continue

            parts = line.split("\t")
            if len(parts) < 3:
                continue

            flexion = parts[2]

            if flexion.isupper():
                continue

            w = clean_word(flexion)
            if w:
                words.add(w)

    return words


def parse_hunspell() -> set[str]:
    words = set()

    with open(HUNSPELL_FILE, encoding="latin-1") as f:
        next(f)  # skip header

        for line in f:
            w = line.split("/")[0]

            if w.isupper():
                continue

            w = clean_word(w)
            if w:
                words.add(w)

    return words


def main():
    print("Parsing Grammalecte...")
    g = parse_grammalecte()
    print(f"  {len(g)} mots")

    print("Parsing Hunspell...")
    h = parse_hunspell()
    print(f"  {len(h)} mots")

    merged = sorted(g | h)

    print(f"\nTOTAL FINAL : {len(merged)} mots")

    Path("data").mkdir(exist_ok=True)

    out = Path("data/oogle_accept.txt")
    out.write_text("\n".join(merged) + "\n")

    print(f"\nFichier généré : {out}")


if __name__ == "__main__":
    main()
