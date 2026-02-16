#!/usr/bin/env python3
# generate_wordlist_from_grammalecte.py
"""
Script pour télécharger et filtrer les mots de Grammalecte pour OOGLE.

Grammalecte (https://grammalecte.net/) est le correcteur grammatical open-source
français de référence. Ce script télécharge leur lexique et génère deux fichiers :
- oogle_words.txt : Mots courants pour les solutions quotidiennes
- oogle_accept.txt : Tous les mots valides acceptés comme tentatives

Usage:
    python generate_wordlist_from_grammalecte.py [OPTIONS]

Options:
    --output-dir PATH    Répertoire de sortie (défaut: data/)
    --max-solutions N    Nombre max de solutions (défaut: 800)
    --force              Télécharger même si le cache existe
    --verbose           Mode verbeux

Exemple:
    python generate_wordlist_from_grammalecte.py --output-dir data/ --max-solutions 1000
"""

import argparse
import logging
import re
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
    print("❌ Module 'requests' non installé.")
    print("📦 Installation : pip install requests")
    sys.exit(1)

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# URLs Grammalecte
GRAMMALECTE_LEXIQUE_URL = "https://grammalecte.net/download/fr/lexique-dicollecte-fr-v7.0.zip"
GRAMMALECTE_LEXIQUE_FILENAME = "lexique-dicollecte-fr-v7.0.txt"

# Constantes
WORD_LENGTH = 5


def remove_accents(text: str) -> str:
    """
    Supprime les accents d'un texte.
    
    Examples:
        >>> remove_accents("élève")
        'eleve'
        >>> remove_accents("café")
        'cafe'
    """
    nfd = unicodedata.normalize('NFD', text)
    without_accents = ''.join(
        char for char in nfd 
        if unicodedata.category(char) != 'Mn'
    )
    return without_accents


def is_valid_word(word: str, original: str) -> bool:
    """
    Vérifie si un mot est valide pour OOGLE.
    
    Critères:
    - Exactement 5 lettres après normalisation
    - Uniquement des lettres (pas de chiffres, tirets, etc.)
    - Pas un nom propre (commence par une majuscule dans l'original)
    - Pas de lettres accentuées après normalisation
    
    Args:
        word: Mot normalisé (minuscules, sans accents)
        original: Mot original du dictionnaire
    
    Returns:
        True si le mot est valide
    """
    # Vérifier la longueur
    if len(word) != WORD_LENGTH:
        return False
    
    # Uniquement des lettres
    if not word.isalpha():
        return False
    
    # Pas de lettres accentuées restantes
    if word != remove_accents(word):
        return False
    
    # Exclure les noms propres (heuristique : commence par majuscule)
    # Note: Certains mots peuvent être à la fois noms propres et communs
    if original and original[0].isupper() and not original.isupper():
        return False
    
    return True


def calculate_word_score(word: str) -> float:
    """
    Calcule un score de "popularité" pour un mot.
    
    Critères (scores plus élevés = mots plus courants) :
    - Lettres fréquentes : e, a, s, i, n, t, r, u, l, o
    - Bon équilibre voyelles/consonnes
    - Pas de lettres rares : w, x, y, z, k
    - Pas de doublons de consonnes rares
    
    Returns:
        Score entre 0 et 100
    """
    score = 50.0  # Score de base
    
    # Lettres fréquentes en français
    frequent_letters = {
        'e': 10, 'a': 8, 's': 7, 'i': 6, 'n': 6, 
        't': 6, 'r': 5, 'u': 5, 'l': 4, 'o': 4
    }
    
    # Lettres rares
    rare_letters = {'w': -15, 'x': -10, 'z': -15, 'k': -8, 'y': -5}
    
    # Score basé sur les lettres
    for char in word:
        if char in frequent_letters:
            score += frequent_letters[char]
        elif char in rare_letters:
            score += rare_letters[char]
    
    # Pénalité pour lettres rares répétées
    letter_counts = Counter(word)
    for char, count in letter_counts.items():
        if char in rare_letters and count > 1:
            score -= 20
    
    # Bonus pour bon équilibre voyelles/consonnes
    vowels = sum(1 for c in word if c in 'aeiouy')
    if 2 <= vowels <= 3:  # 2 ou 3 voyelles = idéal
        score += 10
    elif vowels == 1 or vowels == 4:
        score += 5
    else:
        score -= 10
    
    # Pénalité pour patterns difficiles
    if word.count('q') > 0 and 'u' not in word:  # Q sans U
        score -= 20
    
    # Bonus pour patterns courants
    common_patterns = ['tion', 'ment', 'ance', 'ence', 'able', 'ible']
    for pattern in common_patterns:
        if pattern in word:
            score += 5
            break
    
    return max(0, min(100, score))


def download_grammalecte_lexique(cache_dir: Path, force: bool = False) -> Path:
    """
    Télécharge le lexique Grammalecte et l'extrait.
    
    Args:
        cache_dir: Répertoire pour le cache
        force: Force le téléchargement même si le cache existe
    
    Returns:
        Path vers le fichier lexique extrait
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    lexique_path = cache_dir / GRAMMALECTE_LEXIQUE_FILENAME
    
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
        
        # Extraire le fichier du ZIP
        with zipfile.ZipFile(BytesIO(response.content)) as zip_file:
            # Lister les fichiers dans le ZIP
            file_list = zip_file.namelist()
            log.info(f"   Fichiers trouvés : {', '.join(file_list[:3])}...")
            
            # Chercher le fichier lexique
            lexique_file = None
            for filename in file_list:
                if filename.endswith('.txt') and 'lexique' in filename.lower():
                    lexique_file = filename
                    break
            
            if not lexique_file:
                # Prendre le premier fichier .txt
                lexique_file = next((f for f in file_list if f.endswith('.txt')), None)
            
            if not lexique_file:
                raise RuntimeError(f"Aucun fichier lexique trouvé dans le ZIP")
            
            log.info(f"   Extraction de : {lexique_file}")
            
            # Extraire le contenu
            content = zip_file.read(lexique_file)
            
            # Sauvegarder
            with open(lexique_path, 'wb') as f:
                f.write(content)
        
        log.info(f"✅ Lexique extrait : {lexique_path}")
        return lexique_path
        
    except requests.exceptions.RequestException as e:
        log.error(f"❌ Erreur lors du téléchargement : {e}")
        raise
    except zipfile.BadZipFile as e:
        log.error(f"❌ Erreur lors de l'extraction du ZIP : {e}")
        raise


def parse_grammalecte_lexique(lexique_path: Path) -> Set[str]:
    """
    Parse le fichier lexique de Grammalecte et extrait les mots de 5 lettres.
    
    Format du lexique Grammalecte (tab-separated) :
    mot\tlexème\ttags
    
    Exemple :
    table\ttable\tNom:fp
    
    Args:
        lexique_path: Chemin vers le fichier lexique
    
    Returns:
        Ensemble de mots de 5 lettres valides (normalisés)
    """
    log.info(f"📖 Lecture du lexique : {lexique_path}")
    
    valid_words = set()
    total_lines = 0
    
    # Encodages possibles
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
        raise RuntimeError(f"Impossible de lire le fichier avec les encodages : {encodings}")
    
    for line in content.split('\n'):
        total_lines += 1
        
        if not line.strip() or line.startswith('#'):
            continue
        
        # Format: mot\tlexème\ttags ou juste mot
        parts = line.strip().split('\t')
        if not parts:
            continue
        
        original_word = parts[0].strip()
        
        # Normaliser : minuscules, sans accents
        normalized_word = remove_accents(original_word.lower())
        
        # Vérifier si valide
        if is_valid_word(normalized_word, original_word):
            valid_words.add(normalized_word)
    
    log.info(f"✅ {total_lines:,} lignes traitées")
    log.info(f"✅ {len(valid_words):,} mots de {WORD_LENGTH} lettres trouvés")
    
    return valid_words


def select_solution_words(all_words: Set[str], max_count: int) -> List[str]:
    """
    Sélectionne les mots les plus adaptés pour les solutions quotidiennes.
    
    Args:
        all_words: Ensemble de tous les mots valides
        max_count: Nombre maximum de mots à sélectionner
    
    Returns:
        Liste triée des mots sélectionnés
    """
    log.info(f"🎯 Sélection des {max_count} meilleurs mots pour les solutions...")
    
    # Calculer le score de chaque mot
    word_scores = [(word, calculate_word_score(word)) for word in all_words]
    
    # Trier par score décroissant
    word_scores.sort(key=lambda x: x[1], reverse=True)
    
    # Prendre les N premiers
    selected = [word for word, score in word_scores[:max_count]]
    
    # Stats
    top_score = word_scores[0][1] if word_scores else 0
    avg_score = sum(score for _, score in word_scores[:max_count]) / len(selected) if selected else 0
    
    log.info(f"   Score max : {top_score:.1f}")
    log.info(f"   Score moyen : {avg_score:.1f}")
    log.info(f"   Exemples : {', '.join(selected[:10])}")
    
    return selected


def save_word_lists(
    solutions: List[str],
    all_words: Set[str],
    output_dir: Path
):
    """
    Sauvegarde les listes de mots dans des fichiers.
    
    Args:
        solutions: Liste des mots solutions
        all_words: Ensemble de tous les mots acceptés
        output_dir: Répertoire de sortie
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    solutions_file = output_dir / "oogle_words.txt"
    accept_file = output_dir / "oogle_accept.txt"
    
    log.info(f"💾 Sauvegarde des fichiers...")
    
    # Sauvegarder les solutions (triées alphabétiquement)
    with open(solutions_file, 'w', encoding='utf-8') as f:
        for word in sorted(solutions):
            f.write(f"{word}\n")
    
    log.info(f"   ✅ {solutions_file} ({len(solutions)} mots)")
    
    # Sauvegarder tous les mots acceptés (triés alphabétiquement)
    with open(accept_file, 'w', encoding='utf-8') as f:
        for word in sorted(all_words):
            f.write(f"{word}\n")
    
    log.info(f"   ✅ {accept_file} ({len(all_words)} mots)")


def display_statistics(solutions: List[str], all_words: Set[str]):
    """Affiche des statistiques sur les mots générés."""
    log.info("")
    log.info("=" * 60)
    log.info("📊 STATISTIQUES")
    log.info("=" * 60)
    
    # Comptage de base
    log.info(f"Solutions quotidiennes : {len(solutions):,} mots")
    log.info(f"Mots acceptés total    : {len(all_words):,} mots")
    
    # Distribution des lettres
    log.info("")
    log.info("🔤 Lettres les plus fréquentes dans les solutions :")
    all_letters = ''.join(solutions)
    letter_freq = Counter(all_letters)
    for letter, count in letter_freq.most_common(10):
        percentage = (count / len(all_letters)) * 100
        log.info(f"   {letter.upper()} : {count:,} ({percentage:.1f}%)")
    
    # Distribution des voyelles
    vowel_counts = [sum(1 for c in word if c in 'aeiouy') for word in solutions]
    avg_vowels = sum(vowel_counts) / len(vowel_counts) if vowel_counts else 0
    log.info("")
    log.info(f"📈 Moyenne de voyelles par mot : {avg_vowels:.2f}")
    
    # Mots avec lettres rares
    rare_letter_words = [w for w in solutions if any(c in 'wxyz' for c in w)]
    log.info(f"⚠️  Mots avec lettres rares (w,x,y,z) : {len(rare_letter_words)}")
    
    log.info("=" * 60)


def main():
    """Point d'entrée principal."""
    parser = argparse.ArgumentParser(
        description="Génère les listes de mots OOGLE depuis Grammalecte",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
  %(prog)s
  %(prog)s --output-dir data/ --max-solutions 1000
  %(prog)s --force --verbose
        """
    )
    
    parser.add_argument(
        '--output-dir',
        type=Path,
        default=Path('data'),
        help='Répertoire de sortie (défaut: data/)'
    )
    
    parser.add_argument(
        '--max-solutions',
        type=int,
        default=1000,
        help='Nombre max de solutions (défaut: 1000)'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='Télécharger même si le cache existe'
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Mode verbeux'
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Affichage du header
    print()
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 58 + "║")
    print("║" + "  🎮 OOGLE - Générateur de listes de mots".center(58) + "║")
    print("║" + "  📚 Source : Grammalecte".center(58) + "║")
    print("║" + " " * 58 + "║")
    print("╚" + "═" * 58 + "╝")
    print()
    
    try:
        # Télécharger le lexique
        cache_dir = args.output_dir / '.cache'
        lexique_path = download_grammalecte_lexique(cache_dir, args.force)
        
        # Parser et extraire les mots
        all_words = parse_grammalecte_lexique(lexique_path)
        
        if not all_words:
            log.error("❌ Aucun mot valide trouvé !")
            return 1
        
        # Sélectionner les solutions
        solutions = select_solution_words(all_words, args.max_solutions)
        
        # Sauvegarder
        save_word_lists(solutions, all_words, args.output_dir)
        
        # Afficher les statistiques
        display_statistics(solutions, all_words)
        
        log.info("")
        log.info("✅ Génération terminée avec succès !")
        log.info(f"📁 Fichiers créés dans : {args.output_dir}/")
        log.info("")
        log.info("🚀 Vous pouvez maintenant lancer votre bot OOGLE !")
        
        return 0
        
    except KeyboardInterrupt:
        log.warning("\n⚠️  Interrupted par l'utilisateur")
        return 130
    except Exception as e:
        log.error(f"\n❌ Erreur fatale : {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
