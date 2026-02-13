# cogs/oogle.py – OOGLE : Wordle français (mots de 5 lettres, sans accents)
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
# Chargement de la liste de mots
# ──────────────────────────────────────────────────────────────────────────────

_WORDS_FILE = Path(__file__).resolve().parent.parent / "data" / "oogle_words.txt"


def _load_words() -> List[str]:
    """Charge les mots de 5 lettres depuis le fichier, en minuscules."""
    words: List[str] = []
    with open(_WORDS_FILE, encoding="utf-8") as f:
        for line in f:
            w = line.strip().lower()
            if len(w) == WORD_LENGTH and w.isalpha():
                words.append(w)
    if not words:
        raise RuntimeError("Aucun mot valide trouvé dans oogle_words.txt")
    return words


WORDS = _load_words()
WORD_SET = set(WORDS)


def get_daily_word() -> str:
    """Renvoie le mot du jour (déterministe, basé sur la date Paris)."""
    today = dt.datetime.now(TZ_PARIS).strftime("%Y-%m-%d")
    h = hashlib.sha256(f"oogle-{today}".encode()).hexdigest()
    idx = int(h, 16) % len(WORDS)
    return WORDS[idx]


# ──────────────────────────────────────────────────────────────────────────────
# Logique de comparaison
# ──────────────────────────────────────────────────────────────────────────────

# 🟩 = bonne lettre, bonne position
# 🟨 = bonne lettre, mauvaise position
# ⬛ = lettre absente

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


def format_grid(attempts: List[Tuple[str, List[str]]]) -> str:
    """Formate la grille d'emojis pour l'affichage."""
    lines = []
    for _word, emojis in attempts:
        lines.append("".join(emojis))
    return "\n".join(lines)


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
        guess = self.mot.value.strip().lower()

        # Validation
        if len(guess) != WORD_LENGTH or not guess.isalpha():
            return await interaction.response.send_message(
                f"⛔ Le mot doit contenir exactement {WORD_LENGTH} lettres.", ephemeral=True
            )

        if guess not in WORD_SET:
            return await interaction.response.send_message(
                "⛔ Ce mot n'est pas dans le dictionnaire OOGLE.", ephemeral=True
            )

        game = get_or_create_game(interaction.user.id)

        if game.finished:
            return await interaction.response.send_message(
                "Tu as déjà terminé l'OOGLE du jour ! Reviens demain 🕛", ephemeral=True
            )

        # Évaluer le guess
        emojis = evaluate_guess(guess, game.target)
        game.attempts.append((guess, emojis))

        won = guess == game.target
        lost = len(game.attempts) >= MAX_ATTEMPTS and not won

        if won or lost:
            game.finished = True
            game.won = won

        # Construire la réponse éphémère avec la grille actuelle
        grid = format_grid(game.attempts)
        remaining = MAX_ATTEMPTS - len(game.attempts)

        if won:
            response = f"**OOGLE** 🎉 Bravo !\n\n{grid}\n\n✅ Trouvé en **{len(game.attempts)}/{MAX_ATTEMPTS}**"
        elif lost:
            response = (
                f"**OOGLE** 💀 Perdu !\n\n{grid}\n\n"
                f"Le mot était : **{game.target.upper()}**"
            )
        else:
            response = (
                f"**OOGLE** – Essai {len(game.attempts)}/{MAX_ATTEMPTS}\n\n"
                f"{grid}\n\n"
                f"Il te reste **{remaining}** essai{'s' if remaining > 1 else ''}."
            )

        await interaction.response.send_message(response, ephemeral=True)

        # Si la partie est terminée, poster le résultat dans le salon OOGLE
        if game.finished:
            await self.cog.post_result(interaction, game)


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class OogleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="oogle", description="Jouer à OOGLE – le Wordle français du jour")
    async def oogle(self, interaction: discord.Interaction):
        game = get_or_create_game(interaction.user.id)

        if game.finished:
            return await interaction.response.send_message(
                "Tu as déjà terminé l'OOGLE du jour ! Reviens demain 🕛", ephemeral=True
            )

        # Si le joueur a déjà des essais, montrer la grille avant le modal
        if game.attempts:
            grid = format_grid(game.attempts)
            remaining = MAX_ATTEMPTS - len(game.attempts)
            hint = (
                f"**OOGLE** – Essai {len(game.attempts)}/{MAX_ATTEMPTS}\n\n"
                f"{grid}\n\n"
                f"Il te reste **{remaining}** essai{'s' if remaining > 1 else ''}.\n"
                f"Utilise `/oogle` pour proposer un nouveau mot."
            )
            await interaction.response.send_message(hint, ephemeral=True)
            # Envoyer le modal via followup n'est pas possible,
            # donc on informe juste et le joueur relance /oogle
            return

        await interaction.response.send_modal(GuessModal(self))

    @app_commands.command(name="oogle-guess", description="Proposer un mot pour l'OOGLE du jour")
    @app_commands.describe(mot="Ton mot de 5 lettres")
    async def oogle_guess(self, interaction: discord.Interaction, mot: str):
        guess = mot.strip().lower()

        if len(guess) != WORD_LENGTH or not guess.isalpha():
            return await interaction.response.send_message(
                f"⛔ Le mot doit contenir exactement {WORD_LENGTH} lettres.", ephemeral=True
            )

        if guess not in WORD_SET:
            return await interaction.response.send_message(
                "⛔ Ce mot n'est pas dans le dictionnaire OOGLE.", ephemeral=True
            )

        game = get_or_create_game(interaction.user.id)

        if game.finished:
            return await interaction.response.send_message(
                "Tu as déjà terminé l'OOGLE du jour ! Reviens demain 🕛", ephemeral=True
            )

        emojis = evaluate_guess(guess, game.target)
        game.attempts.append((guess, emojis))

        won = guess == game.target
        lost = len(game.attempts) >= MAX_ATTEMPTS and not won

        if won or lost:
            game.finished = True
            game.won = won

        grid = format_grid(game.attempts)
        remaining = MAX_ATTEMPTS - len(game.attempts)

        if won:
            response = f"**OOGLE** 🎉 Bravo !\n\n{grid}\n\n✅ Trouvé en **{len(game.attempts)}/{MAX_ATTEMPTS}**"
        elif lost:
            response = (
                f"**OOGLE** 💀 Perdu !\n\n{grid}\n\n"
                f"Le mot était : **{game.target.upper()}**"
            )
        else:
            response = (
                f"**OOGLE** – Essai {len(game.attempts)}/{MAX_ATTEMPTS}\n\n"
                f"{grid}\n\n"
                f"Il te reste **{remaining}** essai{'s' if remaining > 1 else ''}."
            )

        await interaction.response.send_message(response, ephemeral=True)

        if game.finished:
            await self.post_result(interaction, game)

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
        grid = format_grid(game.attempts)

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
