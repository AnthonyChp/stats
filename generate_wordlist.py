#!/usr/bin/env python3
import bz2
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
import requests
from tqdm import tqdm

DUMP_URL = "https://dumps.wikimedia.org/frwiktionary/latest/frwiktionary-latest-pages-articles.xml.bz2"
DUMP_FILE = Path("data/frwiktionary.xml.bz2")

WORD_LENGTH = 5

LIGATURES = {
    "œ": "oe",
    "Œ": "oe",
    "æ": "ae",
    "Æ": "ae",
}

FR_SECTION = re.compile(r"==\s*\{\{=fr=\}\}\s*==", re.I)


def normalize(text: str) -> str:
    for src, dst in LIGATURES.items():
        text = text.replace(src, dst)

    nfd = unicodedata.normalize("NFD", text)

    text = "".join(
        c for c in nfd
        if unicodedata.category(c) != "Mn"
    )

    return text.lower()


def clean_word(w: str):
    w = normalize(w)

    if len(w) != WORD_LENGTH:
        return None

    if not w.isalpha():
        return None

    if w[0].isupper():
        return None

    return w


def download_dump():
    if DUMP_FILE.exists():
        print("Dump déjà présent")
        return

    DUMP_FILE.parent.mkdir(exist_ok=True)

    r = requests.get(DUMP_URL, stream=True)

    with open(DUMP_FILE, "wb") as f:
        for chunk in tqdm(r.iter_content(1024 * 1024)):
            f.write(chunk)


def extract_words():
    print("Parsing Wiktionnaire...")

    words = set()

    with bz2.open(DUMP_FILE, "rb") as f:
        context = ET.iterparse(f, events=("end",))

        for event, elem in context:
            if elem.tag.endswith("page"):
                title = elem.find(".//{*}title")
                text = elem.find(".//{*}text")

                if title is not None and text is not None:
                    word = title.text or ""
                    content = text.text or ""

                    if FR_SECTION.search(content):
                        w = clean_word(word)
                        if w:
                            words.add(w)

                elem.clear()

    return words


def main():
    download_dump()
    words = extract_words()

    print(f"\nMots FR 5 lettres trouvés : {len(words)}")

    out = Path("data/wiktionary_5.txt")
    out.write_text("\n".join(sorted(words)) + "\n")

    print(f"Fichier : {out}")


if __name__ == "__main__":
    main()
