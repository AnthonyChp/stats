#!/usr/bin/env python3
"""
Extrait tous les mots français de 5 lettres du dump Wiktionnaire.
Fix : regex {{langue|fr}} au lieu de l'ancien {{=fr=}} abandonné.
"""

import bz2
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
import requests
from tqdm import tqdm

DUMP_URL  = "https://dumps.wikimedia.org/frwiktionary/latest/frwiktionary-latest-pages-articles.xml.bz2"
DUMP_FILE = Path("data/frwiktionary.xml.bz2")
WORD_LENGTH = 5

LIGATURES = {
    "\u0153": "oe",  # œ
    "\u0152": "oe",  # Œ
    "\u00e6": "ae",  # æ
    "\u00c6": "ae",  # Æ
    "\u00df": "ss",  # ß
}

# ✅ Fix : le vrai format actuel du Wiktionnaire FR
# Les deux formats coexistent dans le dump (ancien + nouveau)
FR_SECTION = re.compile(
    r"==\s*\{\{(?:langue\|fr|=fr=)\}\}\s*==",
    re.IGNORECASE
)


def normalize(text: str) -> str:
    for src, dst in LIGATURES.items():
        text = text.replace(src, dst)
    nfd = unicodedata.normalize("NFD", text)
    return "".join(
        c for c in nfd
        if unicodedata.category(c) != "Mn"
    ).lower()


def clean_word(w: str):
    w = normalize(w)
    if len(w) != WORD_LENGTH:
        return None
    if not w.isalpha():
        return None
    return w


def download_dump():
    if DUMP_FILE.exists():
        size_mb = DUMP_FILE.stat().st_size // (1024 * 1024)
        print(f"Dump deja present ({size_mb} MB)")
        return

    DUMP_FILE.parent.mkdir(exist_ok=True)
    print(f"Telechargement : {DUMP_URL}")

    r = requests.get(DUMP_URL, stream=True)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))

    with open(DUMP_FILE, "wb") as f:
        with tqdm(total=total, unit="B", unit_scale=True, desc="Download") as bar:
            for chunk in r.iter_content(1024 * 1024):
                f.write(chunk)
                bar.update(len(chunk))

    print(f"Telechargement termine : {DUMP_FILE}")


def extract_words() -> set:
    print("Parsing Wiktionnaire (ca peut prendre 5-10 min)...")
    words = set()
    pages_total = 0
    pages_fr = 0

    with bz2.open(DUMP_FILE, "rb") as f:
        context = ET.iterparse(f, events=("end",))
        for event, elem in context:
            if elem.tag.endswith("page"):
                pages_total += 1

                title_el = elem.find(".//{*}title")
                text_el  = elem.find(".//{*}text")

                if title_el is not None and text_el is not None:
                    word    = title_el.text or ""
                    content = text_el.text  or ""

                    # Ignorer les pages de type "Discussion:", "Utilisateur:", etc.
                    if ":" in word:
                        elem.clear()
                        continue

                    # Chercher la section française
                    if FR_SECTION.search(content):
                        pages_fr += 1
                        w = clean_word(word)
                        if w:
                            words.add(w)

                elem.clear()

                # Progression
                if pages_total % 50000 == 0:
                    print(f"  {pages_total:,} pages traitees, {pages_fr:,} FR, {len(words):,} mots 5L...")

    print(f"\nTotal pages    : {pages_total:,}")
    print(f"Pages francais : {pages_fr:,}")
    print(f"Mots 5 lettres : {len(words):,}")
    return words


def main():
    download_dump()
    words = extract_words()

    if not words:
        print("ERREUR : 0 mots trouves !")
        print("Verifiez que le dump est complet (taille > 500MB)")
        return

    # Sauvegarder
    out = Path("data/wiktionary_5.txt")
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(sorted(words)) + "\n", encoding="utf-8")
    print(f"\nFichier cree : {out} ({len(words):,} mots)")


if __name__ == "__main__":
    main()
