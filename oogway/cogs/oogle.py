# cogs/oogle.py – OOGLE : Wordle français (mots de 5 lettres, sans accents)
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from pathlib import Path
from typing import Dict, List, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from zoneinfo import ZoneInfo

from oogway.config import settings

log = logging.getLogger(__name__)
TZ_PARIS = ZoneInfo("Europe/Paris")

WORD_LENGTH = 5
MAX_ATTEMPTS = 6

# ──────────────────────────────────────────────────────────────────────────────
# Chargement des listes de mots
# ──────────────────────────────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_SOLUTIONS_FILE = _DATA_DIR / "oogle_words.txt"    # ~600 mots courants (solutions)
_ACCEPT_FILE = _DATA_DIR / "oogle_accept.txt"       # ~1700+ mots acceptés en guess


def _load_word_file(path: Path) -> List[str]:
    words: List[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            w = line.strip().lower()
            if len(w) == WORD_LENGTH and w.isalpha():
                words.append(w)
    return words


SOLUTIONS = _load_word_file(_SOLUTIONS_FILE)
if not SOLUTIONS:
    raise RuntimeError("Aucun mot valide trouvé dans oogle_words.txt")

# L'ensemble de mots acceptés = solutions + accept (union)
_accept_extra = _load_word_file(_ACCEPT_FILE)
ACCEPT_SET: Set[str] = set(SOLUTIONS) | set(_accept_extra)

log.info("OOGLE: %d solutions, %d mots acceptés au total", len(SOLUTIONS), len(ACCEPT_SET))


def get_daily_word() -> str:
    """Renvoie le mot du jour (déterministe, basé sur la date Paris)."""
    today = dt.datetime.now(TZ_PARIS).strftime("%Y-%m-%d")
    h = hashlib.sha256(f"oogle-{today}".encode()).hexdigest()
    idx = int(h, 16) % len(SOLUTIONS)
    return SOLUTIONS[idx]


# ──────────────────────────────────────────────────────────────────────────────
# Logique de comparaison
# ──────────────────────────────────────────────────────────────────────────────

# 🟩 = bonne lettre, bonne position
# 🟨 = bonne lettre, mauvaise position
# ⬛ = lettre absente

LETTER_EMOJIS = {
    "A": "🇦", "B": "🇧", "C": "🇨", "D": "🇩", "E": "🇪",
    "F": "🇫", "G": "🇬", "H": "🇭", "I": "🇮", "J": "🇯",
    "K": "🇰", "L": "🇱", "M": "🇲", "N": "🇳", "O": "🇴",
    "P": "🇵", "Q": "🇶", "R": "🇷", "S": "🇸", "T": "🇹",
    "U": "🇺", "V": "🇻", "W": "🇼", "X": "🇽", "Y": "🇾",
    "Z": "🇿",
}


def evaluate_guess(guess: str, target: str) -> List[str]:
    """Renvoie une liste de 5 emojis correspondant à chaque lettre."""
    result = ["⬛"] * WORD_LENGTH
    target_chars = list(target)

    # Premier passage : lettres correctes (vert)
    for i in range(WORD_LENGTH):
        if guess[i] == target_chars[i]:
            result[i] = "🟩"
            target_chars[i] = None  # consommée

    # Second passage : lettres présentes mais mal placées (jaune)
    for i in range(WORD_LENGTH):
        if result[i] == "🟩":
            continue
        if guess[i] in target_chars:
            result[i] = "🟨"
            target_chars[target_chars.index(guess[i])] = None

    return result


def format_grid(attempts: List[Tuple[str, List[str]]], show_words: bool = True) -> str:
    """Formate la grille d'emojis pour l'affichage.
    Si show_words=True, affiche aussi les lettres à côté."""
    lines = []
    for word, emojis in attempts:
        emoji_row = "".join(emojis)
        if show_words:
            spaced = "  ".join(c.upper() for c in word)
            lines.append(f"{emoji_row}  `{spaced}`")
        else:
            lines.append(emoji_row)
    return "\n".join(lines)


def build_keyboard(attempts: List[Tuple[str, List[str]]]) -> str:
    """Construit un clavier visuel montrant l'état de chaque lettre testée."""
    # Priorité : vert > jaune > noir
    letter_status: Dict[str, str] = {}
    for word, emojis in attempts:
        for i, ch in enumerate(word):
            status = emojis[i]
            prev = letter_status.get(ch)
            if prev == "🟩":
                continue  # vert = on garde
            if status == "🟩" or (status == "🟨" and prev != "🟩"):
                letter_status[ch] = status
            elif ch not in letter_status:
                letter_status[ch] = status

    rows = ["azertyuiop", "qsdfghjklm", "wxcvbn"]
    result = []
    for row in rows:
        chars = []
        for ch in row:
            if ch in letter_status:
                st = letter_status[ch]
                if st == "🟩":
                    chars.append(f"**{ch.upper()}**")
                elif st == "🟨":
                    chars.append(f"*{ch.upper()}*")
                else:
                    chars.append(f"~~{ch.upper()}~~")
            else:
                chars.append(ch.upper())
        result.append("  ".join(chars))
    return "\n".join(result)


# ──────────────────────────────────────────────────────────────────────────────
# État des parties en mémoire (clé = (date_str, user_id))
# ──────────────────────────────────────────────────────────────────────────────

class GameState:
    __slots__ = ("target", "attempts", "finished", "won")

    def __init__(self, target: str):
        self.target = target
        self.attempts: List[Tuple[str, List[str]]] = []
        self.finished: bool = False
        self.won: bool = False


# {(date_str, discord_user_id): GameState}
GAMES: Dict[Tuple[str, int], GameState] = {}


def _today_key() -> str:
    return dt.datetime.now(TZ_PARIS).strftime("%Y-%m-%d")


def get_or_create_game(user_id: int) -> GameState:
    key = (_today_key(), user_id)
    if key not in GAMES:
        GAMES[key] = GameState(get_daily_word())
    return GAMES[key]


# ──────────────────────────────────────────────────────────────────────────────
# Modal pour saisir un mot
# ──────────────────────────────────────────────────────────────────────────────

class GuessModal(discord.ui.Modal, title="OOGLE – Devine le mot"):
    mot = discord.ui.TextInput(
        label="Ton mot (5 lettres)",
        placeholder="Ex: table",
        min_length=WORD_LENGTH,
        max_length=WORD_LENGTH,
        required=True,
    )

    def __init__(self, cog: OogleCog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.process_guess(interaction, self.mot.value)


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class OogleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def process_guess(self, interaction: discord.Interaction, raw_mot: str):
        """Logique commune de traitement d'un guess (modal ou commande)."""
        guess = raw_mot.strip().lower()

        if len(guess) != WORD_LENGTH or not guess.isalpha():
            return await interaction.response.send_message(
                f"⛔ Le mot doit contenir exactement {WORD_LENGTH} lettres.", ephemeral=True
            )

        if guess not in ACCEPT_SET:
            return await interaction.response.send_message(
                "⛔ Ce mot n'est pas dans le dictionnaire OOGLE. Essaie un autre mot !", ephemeral=True
            )

        game = get_or_create_game(interaction.user.id)

        if game.finished:
            return await interaction.response.send_message(
                "Tu as déjà terminé l'OOGLE du jour ! Reviens demain 🕛", ephemeral=True
            )

        # Évaluer
        emojis = evaluate_guess(guess, game.target)
        game.attempts.append((guess, emojis))

        won = guess == game.target
        lost = len(game.attempts) >= MAX_ATTEMPTS and not won

        if won or lost:
            game.finished = True
            game.won = won

        # Construire la réponse
        grid = format_grid(game.attempts, show_words=True)
        keyboard = build_keyboard(game.attempts)
        remaining = MAX_ATTEMPTS - len(game.attempts)

        if won:
            response = (
                f"**OOGLE** 🎉 Bravo !\n\n"
                f"{grid}\n\n"
                f"✅ Trouvé en **{len(game.attempts)}/{MAX_ATTEMPTS}**\n\n"
                f"{keyboard}"
            )
        elif lost:
            response = (
                f"**OOGLE** 💀 Perdu !\n\n"
                f"{grid}\n\n"
                f"Le mot était : **{game.target.upper()}**\n\n"
                f"{keyboard}"
            )
        else:
            response = (
                f"**OOGLE** – Essai {len(game.attempts)}/{MAX_ATTEMPTS}\n\n"
                f"{grid}\n\n"
                f"Il te reste **{remaining}** essai{'s' if remaining > 1 else ''}.\n\n"
                f"{keyboard}"
            )

        await interaction.response.send_message(response, ephemeral=True)

        if game.finished:
            await self.post_result(interaction, game)

    @app_commands.command(name="oogle", description="Jouer à OOGLE – le Wordle français du jour")
    @app_commands.describe(mot="Ton mot de 5 lettres (optionnel, ouvre un popup sinon)")
    async def oogle(self, interaction: discord.Interaction, mot: str = None):
        game = get_or_create_game(interaction.user.id)

        if game.finished:
            grid = format_grid(game.attempts, show_words=True)
            score = f"{len(game.attempts)}/{MAX_ATTEMPTS}" if game.won else f"X/{MAX_ATTEMPTS}"
            return await interaction.response.send_message(
                f"Tu as déjà terminé l'OOGLE du jour ! **{score}**\n\n{grid}\n\nReviens demain 🕛",
                ephemeral=True,
            )

        # Si un mot est fourni en paramètre, on le traite directement
        if mot:
            return await self.process_guess(interaction, mot)

        # Sinon on ouvre le modal
        await interaction.response.send_modal(GuessModal(self))

    async def post_result(self, interaction: discord.Interaction, game: GameState):
        """Poste le résultat dans le salon OOGLE (avatar + date + score)."""
        channel = self.bot.get_channel(settings.OOGLE_CHANNEL_ID)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(settings.OOGLE_CHANNEL_ID)
            except Exception:
                log.warning("Impossible de trouver le salon OOGLE (ID=%s)", settings.OOGLE_CHANNEL_ID)
                return

        user = interaction.user
        today = dt.datetime.now(TZ_PARIS).strftime("%d/%m/%Y")
        score = f"{len(game.attempts)}/{MAX_ATTEMPTS}" if game.won else f"X/{MAX_ATTEMPTS}"
        # Pour le résultat public, on ne montre PAS les mots (anti-spoil)
        grid = format_grid(game.attempts, show_words=False)

        embed = discord.Embed(
            title=f"OOGLE — {today}",
            description=f"**{score}**\n\n{grid}",
            colour=0x6AAA64 if game.won else 0x787C7E,
        )
        embed.set_author(
            name=user.display_name,
            icon_url=user.display_avatar.url,
        )

        await channel.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(OogleCog(bot))
