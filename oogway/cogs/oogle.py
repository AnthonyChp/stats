# cogs/oogle.py – OOGLE : Wordle français amélioré avec leaderboard et notifications
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional
import json

import discord
from discord import app_commands
from discord.ext import commands, tasks
from zoneinfo import ZoneInfo

from oogway.config import settings
from oogway.oogle_database import OogleDatabase

log = logging.getLogger(__name__)
TZ_PARIS = ZoneInfo("Europe/Paris")

WORD_LENGTH = 5
MAX_ATTEMPTS = 6

# ──────────────────────────────────────────────────────────────────────────────
# Chargement des listes de mots
# ──────────────────────────────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_SOLUTIONS_FILE = _DATA_DIR / "oogle_words.txt"
_ACCEPT_FILE = _DATA_DIR / "oogle_accept.txt"


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

def evaluate_guess(guess: str, target: str) -> List[str]:
    """Renvoie une liste de 5 emojis correspondant à chaque lettre."""
    result = ["⬛"] * WORD_LENGTH
    target_chars = list(target)

    # Premier passage : lettres correctes (vert)
    for i in range(WORD_LENGTH):
        if guess[i] == target_chars[i]:
            result[i] = "🟩"
            target_chars[i] = None

    # Second passage : lettres présentes mais mal placées (jaune)
    for i in range(WORD_LENGTH):
        if result[i] == "🟩":
            continue
        if guess[i] in target_chars:
            result[i] = "🟨"
            target_chars[target_chars.index(guess[i])] = None

    return result


def format_grid(attempts: List[Tuple[str, List[str]]], show_words: bool = True) -> str:
    """Formate la grille d'emojis pour l'affichage."""
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
    letter_status: Dict[str, str] = {}
    for word, emojis in attempts:
        for i, ch in enumerate(word):
            status = emojis[i]
            prev = letter_status.get(ch)
            if prev == "🟩":
                continue
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
# Vues pour les boutons du leaderboard
# ──────────────────────────────────────────────────────────────────────────────

class LeaderboardView(discord.ui.View):
    """Vue avec boutons pour naviguer dans le leaderboard."""
    
    def __init__(self, cog: OogleCog):
        super().__init__(timeout=None)
        self.cog = cog
        self.current_page = "streaks"
    
    @discord.ui.button(label="🔥 Streaks", style=discord.ButtonStyle.primary, custom_id="lb_streaks")
    async def streaks_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = "streaks"
        embed = await self.cog.create_leaderboard_embed("streaks", interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label="🏆 Records", style=discord.ButtonStyle.primary, custom_id="lb_records")
    async def records_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = "records"
        embed = await self.cog.create_leaderboard_embed("records", interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label="⚡ Moyennes", style=discord.ButtonStyle.primary, custom_id="lb_avg")
    async def avg_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = "avg"
        embed = await self.cog.create_leaderboard_embed("avg", interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label="📊 Victoires", style=discord.ButtonStyle.primary, custom_id="lb_wins")
    async def wins_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = "wins"
        embed = await self.cog.create_leaderboard_embed("wins", interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label="💯 Taux", style=discord.ButtonStyle.primary, custom_id="lb_winrate")
    async def winrate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = "winrate"
        embed = await self.cog.create_leaderboard_embed("winrate", interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class OogleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = OogleDatabase(settings.DB_URL.replace("sqlite:///", ""))
        self.leaderboard_message_id: Optional[int] = None
        
        # Démarrer les tâches
        self.update_leaderboard.start()
        self.daily_notification.start()

    def cog_unload(self):
        self.update_leaderboard.cancel()
        self.daily_notification.cancel()

    async def process_guess(self, interaction: discord.Interaction, raw_mot: str):
        """Logique commune de traitement d'un guess."""
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
            
            # Sauvegarder en base de données
            today = _today_key()
            self.db.save_game(
                interaction.user.id,
                today,
                len(game.attempts),
                won,
                game.target
            )

        # Construire la réponse
        grid = format_grid(game.attempts, show_words=True)
        keyboard = build_keyboard(game.attempts)
        remaining = MAX_ATTEMPTS - len(game.attempts)

        if won:
            stats = self.db.get_user_stats(interaction.user.id)
            streak_info = f"🔥 Série : **{stats['current_streak']}**" if stats else ""
            
            response = (
                f"**OOGLE** 🎉 Bravo !\n\n"
                f"{grid}\n\n"
                f"✅ Trouvé en **{len(game.attempts)}/{MAX_ATTEMPTS}**\n"
                f"{streak_info}\n\n"
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
            # Mettre à jour le leaderboard
            await self.update_leaderboard_message()

    @app_commands.command(name="oogle", description="Jouer à OOGLE – le Wordle français du jour")
    @app_commands.describe(mot="Ton mot de 5 lettres (optionnel, ouvre un popup sinon)")
    async def oogle(self, interaction: discord.Interaction, mot: str = None):
        game = get_or_create_game(interaction.user.id)

        if game.finished:
            grid = format_grid(game.attempts, show_words=True)
            score = f"{len(game.attempts)}/{MAX_ATTEMPTS}" if game.won else f"X/{MAX_ATTEMPTS}"
            stats = self.db.get_user_stats(interaction.user.id)
            
            stats_text = ""
            if stats:
                stats_text = (
                    f"\n📊 **Tes stats**\n"
                    f"🎯 Victoires : {stats['total_wins']}/{stats['total_games']} ({stats['win_rate']:.1f}%)\n"
                    f"🔥 Série actuelle : {stats['current_streak']}\n"
                    f"⚡ Moyenne : {stats['avg_attempts']:.2f} essais"
                )
            
            return await interaction.response.send_message(
                f"Tu as déjà terminé l'OOGLE du jour ! **{score}**\n\n{grid}{stats_text}\n\nReviens demain 🕛",
                ephemeral=True,
            )

        if mot:
            return await self.process_guess(interaction, mot)

        await interaction.response.send_modal(GuessModal(self))

    @app_commands.command(name="oogle_notification", description="Active ou désactive les notifications OOGLE quotidiennes")
    async def oogle_notification(self, interaction: discord.Interaction):
        current_status = self.db.get_notification_status(interaction.user.id)
        new_status = not current_status
        self.db.set_notification(interaction.user.id, new_status)
        
        if new_status:
            msg = "✅ Notifications activées ! Tu seras pingé chaque jour à 8h pour le nouveau OOGLE."
        else:
            msg = "🔕 Notifications désactivées. Tu ne seras plus pingé pour les nouveaux OOGLE."
        
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="oogle_stats", description="Affiche tes statistiques OOGLE")
    async def oogle_stats(self, interaction: discord.Interaction, user: discord.User = None):
        target_user = user or interaction.user
        stats = self.db.get_user_stats(target_user.id)
        
        if not stats:
            return await interaction.response.send_message(
                f"❌ {'Tu n\'as' if target_user == interaction.user else f'{target_user.mention} n\'a'} "
                f"pas encore joué à OOGLE !",
                ephemeral=True
            )
        
        # Créer un histogramme de distribution
        dist = stats['distribution']
        max_count = max(dist.values()) if dist.values() else 1
        histogram = []
        for i in range(1, 7):
            count = dist.get(str(i), 0)
            bar_length = int((count / max_count) * 10) if max_count > 0 else 0
            bar = "█" * bar_length
            histogram.append(f"`{i}` {bar} **{count}**")
        
        embed = discord.Embed(
            title=f"📊 Statistiques OOGLE de {target_user.display_name}",
            color=0x6AAA64,
            timestamp=dt.datetime.now(TZ_PARIS)
        )
        embed.set_thumbnail(url=target_user.display_avatar.url)
        
        embed.add_field(
            name="🎮 Parties",
            value=f"**{stats['total_games']}** jouées\n**{stats['total_wins']}** gagnées",
            inline=True
        )
        embed.add_field(
            name="📈 Performance",
            value=f"**{stats['win_rate']:.1f}%** de victoires\n**{stats['avg_attempts']:.2f}** essais moy.",
            inline=True
        )
        embed.add_field(
            name="🔥 Séries",
            value=f"**{stats['current_streak']}** actuelle\n**{stats['max_streak']}** record",
            inline=True
        )
        embed.add_field(
            name="📊 Distribution des victoires",
            value="\n".join(histogram),
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=False)

    async def create_leaderboard_embed(self, page: str, guild: Optional[discord.Guild] = None) -> discord.Embed:
        """Crée un embed de leaderboard selon la page demandée."""
        embed = discord.Embed(
            title="🏆 LEADERBOARD OOGLE",
            color=0x6AAA64,
            timestamp=dt.datetime.now(TZ_PARIS)
        )
        
        if page == "streaks":
            data = self.db.get_leaderboard_streaks(10)
            embed.description = "**🔥 Meilleures séries en cours**\n\n"
            for i, (user_id, streak) in enumerate(data, 1):
                user = await self.bot.fetch_user(user_id) if guild else None
                name = user.mention if user else f"User #{user_id}"
                medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"`{i}.`"
                embed.description += f"{medal} {name} — **{streak}** jour{'s' if streak > 1 else ''}\n"
        
        elif page == "records":
            data = self.db.get_leaderboard_max_streaks(10)
            embed.description = "**🏆 Records de séries**\n\n"
            for i, (user_id, max_streak) in enumerate(data, 1):
                user = await self.bot.fetch_user(user_id) if guild else None
                name = user.mention if user else f"User #{user_id}"
                medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"`{i}.`"
                embed.description += f"{medal} {name} — **{max_streak}** jour{'s' if max_streak > 1 else ''}\n"
        
        elif page == "avg":
            data = self.db.get_leaderboard_best_avg(10, min_games=5)
            embed.description = "**⚡ Meilleures moyennes** _(min. 5 victoires)_\n\n"
            for i, (user_id, avg) in enumerate(data, 1):
                user = await self.bot.fetch_user(user_id) if guild else None
                name = user.mention if user else f"User #{user_id}"
                medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"`{i}.`"
                embed.description += f"{medal} {name} — **{avg:.2f}** essais\n"
        
        elif page == "wins":
            data = self.db.get_leaderboard_total_wins(10)
            embed.description = "**📊 Plus de victoires**\n\n"
            for i, (user_id, wins) in enumerate(data, 1):
                user = await self.bot.fetch_user(user_id) if guild else None
                name = user.mention if user else f"User #{user_id}"
                medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"`{i}.`"
                embed.description += f"{medal} {name} — **{wins}** victoire{'s' if wins > 1 else ''}\n"
        
        elif page == "winrate":
            data = self.db.get_leaderboard_win_rate(10, min_games=5)
            embed.description = "**💯 Meilleurs taux de victoire** _(min. 5 parties)_\n\n"
            for i, (user_id, wins, games) in enumerate(data, 1):
                user = await self.bot.fetch_user(user_id) if guild else None
                name = user.mention if user else f"User #{user_id}"
                medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"`{i}.`"
                winrate = (wins / games * 100) if games > 0 else 0
                embed.description += f"{medal} {name} — **{winrate:.1f}%** ({wins}/{games})\n"
        
        embed.set_footer(text="OOGLE • Clique sur les boutons pour changer de page")
        return embed

    async def update_leaderboard_message(self):
        """Met à jour le message du leaderboard dans le channel dédié."""
        try:
            channel = self.bot.get_channel(settings.OOGLE_LEADERBOARD_CHANNEL_ID)
            if not channel:
                channel = await self.bot.fetch_channel(settings.OOGLE_LEADERBOARD_CHANNEL_ID)
            
            view = LeaderboardView(self)
            embed = await self.create_leaderboard_embed("streaks", channel.guild)
            
            # Chercher le dernier message du leaderboard
            async for message in channel.history(limit=10):
                if message.author == self.bot.user and message.embeds:
                    if "LEADERBOARD OOGLE" in message.embeds[0].title:
                        await message.edit(embed=embed, view=view)
                        self.leaderboard_message_id = message.id
                        return
            
            # Si pas trouvé, créer un nouveau message
            msg = await channel.send(embed=embed, view=view)
            self.leaderboard_message_id = msg.id
            
        except Exception as e:
            log.error(f"Erreur lors de la mise à jour du leaderboard: {e}")

    @tasks.loop(minutes=5)
    async def update_leaderboard(self):
        """Mise à jour périodique du leaderboard."""
        await self.update_leaderboard_message()

    @update_leaderboard.before_loop
    async def before_update_leaderboard(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=dt.time(hour=8, minute=0, tzinfo=TZ_PARIS))
    async def daily_notification(self):
        """Envoi quotidien d'une notification à 8h."""
        try:
            channel = self.bot.get_channel(settings.OOGLE_CHANNEL_ID)
            if not channel:
                channel = await self.bot.fetch_channel(settings.OOGLE_CHANNEL_ID)
            
            # Récupérer tous les users avec notifications activées
            user_ids = self.db.get_all_notification_users()
            if not user_ids:
                return
            
            mentions = [f"<@{uid}>" for uid in user_ids]
            mentions_str = " ".join(mentions)
            
            today = dt.datetime.now(TZ_PARIS).strftime("%d/%m/%Y")
            
            embed = discord.Embed(
                title="🌅 Nouveau OOGLE disponible !",
                description=f"**{today}** — Le mot du jour est prêt ! Tape `/oogle` pour jouer !",
                color=0x6AAA64
            )
            embed.set_footer(text="Désactive les notifications avec /oogle_notification")
            
            await channel.send(content=mentions_str, embed=embed)
            
        except Exception as e:
            log.error(f"Erreur lors de l'envoi de la notification quotidienne: {e}")

    @daily_notification.before_loop
    async def before_daily_notification(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(OogleCog(bot))

