#!/usr/bin/env python3
# generate_wordlist_from_grammalecte.py
"""
Script pour télécharger et filtrer les mots de Grammalecte pour OOGLE.

Usage:
    python3 generate_wordlist_from_grammalecte.py
"""

import argparse
import logging
import sys
import unicodedata
import zipfile
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Set, List

try:
    import requests
except ImportError:
    print("❌ Module 'requests' non installé.")
    print("📦 Installation : pip install requests")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# URL correcte (Lexique 7.7 - 2025) - Section "Dictionnaires"
GRAMMALECTE_LEXIQUE_URL = "https://grammalecte.net/dic/lexique-grammalecte-fr-v7.7.zip"
WORD_LENGTH = 5


def remove_accents(text: str) -> str:
    """Supprime les accents d'un texte."""
    nfd = unicodedata.normalize('NFD', text)
    return ''.join(char for char in nfd if unicodedata.category(char) != 'Mn')


def is_valid_word(word: str, original: str) -> bool:
    """Vérifie si un mot est valide pour OOGLE."""
    if len(word) != WORD_LENGTH or not word.isalpha():
        return False
    if word != remove_accents(word):
        return False
    if original and original[0].isupper() and not original.isupper():
        return False
    return True


def calculate_word_score(word: str) -> float:
    """Calcule un score de popularité pour un mot."""
    score = 50.0
    
    frequent_letters = {
        'e': 10, 'a': 8, 's': 7, 'i': 6, 'n': 6, 
        't': 6, 'r': 5, 'u': 5, 'l': 4, 'o': 4
    }
    rare_letters = {'w': -15, 'x': -10, 'z': -15, 'k': -8, 'y': -5}
    
    for char in word:
        if char in frequent_letters:
            score += frequent_letters[char]
        elif char in rare_letters:
            score += rare_letters[char]
    
    letter_counts = Counter(word)
    for char, count in letter_counts.items():
        if char in rare_letters and count > 1:
            score -= 20
    
    vowels = sum(1 for c in word if c in 'aeiouy')
    if 2 <= vowels <= 3:
        score += 10
    elif vowels == 1 or vowels == 4:
        score += 5
    else:
        score -= 10
    
    return max(0, min(100, score))


def download_grammalecte_lexique(cache_dir: Path, force: bool = False) -> Path:
    """Télécharge le lexique Grammalecte."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    lexique_path = cache_dir / "lexique-dicollecte-fr.txt"
    
    if lexique_path.exists() and not force:
        log.info(f"📂 Utilisation du cache : {lexique_path}")
        return lexique_path
    
    log.info(f"📥 Téléchargement du lexique Grammalecte...")
    log.info(f"   URL : {GRAMMALECTE_LEXIQUE_URL}")
    
    try:
        response = requests.get(GRAMMALECTE_LEXIQUE_URL, timeout=60)
        response.raise_for_status()
        
        log.info(f"✅ Téléchargement terminé ({len(response.content) // 1024} KB)")
        log.info(f"📦 Extraction du ZIP...")
        
        with zipfile.ZipFile(BytesIO(response.content)) as zip_file:
            file_list = zip_file.namelist()
            log.info(f"   Fichiers trouvés : {', '.join(file_list[:3])}...")
            
            lexique_file = None
            for filename in file_list:
                if filename.endswith('.txt') and 'lexique' in filename.lower():
                    lexique_file = filename
                    break
            
            if not lexique_file:
                lexique_file = next((f for f in file_list if f.endswith('.txt')), None)
            
            if not lexique_file:
                raise RuntimeError(f"Aucun fichier lexique trouvé dans le ZIP")
            
            log.info(f"   Extraction de : {lexique_file}")
            content = zip_file.read(lexique_file)
            
            with open(lexique_path, 'wb') as f:
                f.write(content)
        
        log.info(f"✅ Lexique extrait : {lexique_path}")
        return lexique_path
        
    except requests.exceptions.RequestException as e:
        log.error(f"❌ Erreur lors du téléchargement : {e}")
        log.error(f"💡 Astuce : L'URL a peut-être changé. Vérifiez https://grammalecte.net/")
        raise
    except zipfile.BadZipFile as e:
        log.error(f"❌ Erreur lors de l'extraction du ZIP : {e}")
        raise


def parse_grammalecte_lexique(lexique_path: Path) -> Set[str]:
    """Parse le fichier lexique de Grammalecte."""
    log.info(f"📖 Lecture du lexique : {lexique_path}")
    
    valid_words = set()
    total_lines = 0
    
    encodings = ['utf-8', 'latin-1', 'iso-8859-1']
    content = None
    
    for encoding in encodings:
        try:
            with open(lexique_path, 'r', encoding=encoding) as f:
                content = f.read()
            log.info(f"✅ Fichier lu avec l'encodage : {encoding}")
            break
        except UnicodeDecodeError:
            continue
    
    if content is None:
        raise RuntimeError(f"Impossible de lire le fichier")
    
    for line in content.split('\n'):
        total_lines += 1
        
        if not line.strip() or line.startswith('#'):
            continue
        
        parts = line.strip().split('\t')
        if not parts:
            continue
        
        original_word = parts[0].strip()
        normalized_word = remove_accents(original_word.lower())
        
        if is_valid_word(normalized_word, original_word):
            valid_words.add(normalized_word)
    
    log.info(f"✅ {total_lines:,} lignes traitées")
    log.info(f"✅ {len(valid_words):,} mots de {WORD_LENGTH} lettres trouvés")
    
    return valid_words


def select_solution_words(all_words: Set[str], max_count: int) -> List[str]:
    """Sélectionne les mots les plus adaptés."""
    log.info(f"🎯 Sélection des {max_count} meilleurs mots...")
    
    word_scores = [(word, calculate_word_score(word)) for word in all_words]
    word_scores.sort(key=lambda x: x[1], reverse=True)
    selected = [word for word, score in word_scores[:max_count]]
    
    if selected:
        log.info(f"   Exemples : {', '.join(selected[:10])}")
    
    return selected


def save_word_lists(solutions: List[str], all_words: Set[str], output_dir: Path):
    """Sauvegarde les listes de mots."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    solutions_file = output_dir / "oogle_words.txt"
    accept_file = output_dir / "oogle_accept.txt"
    
    log.info(f"💾 Sauvegarde des fichiers...")
    
    with open(solutions_file, 'w', encoding='utf-8') as f:
        for word in sorted(solutions):
            f.write(f"{word}\n")
    
    log.info(f"   ✅ {solutions_file} ({len(solutions)} mots)")
    
    with open(accept_file, 'w', encoding='utf-8') as f:
        for word in sorted(all_words):
            f.write(f"{word}\n")
    
    log.info(f"   ✅ {accept_file} ({len(all_words)} mots)")


def display_statistics(solutions: List[str], all_words: Set[str]):
    """Affiche des statistiques."""
    log.info("")
    log.info("=" * 60)
    log.info("📊 STATISTIQUES")
    log.info("=" * 60)
    log.info(f"Solutions quotidiennes : {len(solutions):,} mots")
    log.info(f"Mots acceptés total    : {len(all_words):,} mots")
    
    all_letters = ''.join(solutions)
    letter_freq = Counter(all_letters)
    log.info("")
    log.info("🔤 Top 10 lettres :")
    for letter, count in letter_freq.most_common(10):
        percentage = (count / len(all_letters)) * 100
        log.info(f"   {letter.upper()} : {count:,} ({percentage:.1f}%)")
    
    log.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Génère les listes OOGLE depuis Grammalecte")
    parser.add_argument('--output-dir', type=Path, default=Path('data'))
    parser.add_argument('--max-solutions', type=int, default=800)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    print()
    print("╔" + "═" * 58 + "╗")
    print("║  🎮 OOGLE - Générateur de listes de mots".center(60) + "║")
    print("║  📚 Source : Grammalecte".center(60) + "║")
    print("╚" + "═" * 58 + "╝")
    print()
    
    try:
        cache_dir = args.output_dir / '.cache'
        lexique_path = download_grammalecte_lexique(cache_dir, args.force)
        all_words = parse_grammalecte_lexique(lexique_path)
        
        if not all_words:
            log.error("❌ Aucun mot valide trouvé !")
            return 1
        
        solutions = select_solution_words(all_words, args.max_solutions)
        save_word_lists(solutions, all_words, args.output_dir)
        display_statistics(solutions, all_words)
        
        log.info("")
        log.info("✅ Génération terminée avec succès !")
        log.info(f"📁 Fichiers créés dans : {args.output_dir}/")
        
        return 0
        
    except KeyboardInterrupt:
        log.warning("\n⚠️  Interrompu par l'utilisateur")
        return 130
    except Exception as e:
        log.error(f"\n❌ Erreur : {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
