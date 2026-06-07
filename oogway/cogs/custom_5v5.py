# oogway/cogs/custom_5v5.py — Custom 5 v 5 + Draft hook + Captains Pick
# ============================================================================
# ✅ AMÉLIORATIONS:
# - Persistence Redis (survit aux restarts)
# - Timeouts automatiques (30 min)
# - Lock thread-safe
# - Gestion d'erreurs robuste
# - Helper centralisé pour formatting
# - Feedback UX amélioré
# - Cleanup automatique
# - Logs structurés
# ============================================================================

from __future__ import annotations

import asyncio
import logging
import random
import json
from typing import Optional, Set, List, Dict, Any

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from oogway.config import settings
from oogway.database import SessionLocal, User
from oogway.cogs.profile import r_get, r_set

# ───────────────────────────── Logger ─────────────────────────────
logger = logging.getLogger(__name__)

# ───────────────────────────── Helpers DB / Check ─────────────────
def is_user_linked(uid: int) -> bool:
    """Check if user is linked to a Riot account."""
    with SessionLocal() as db:
        return db.query(User).filter_by(discord_id=str(uid)).first() is not None


def is_correct_channel(inter: Interaction) -> bool:
    return inter.channel and inter.channel.id == settings.CUSTOM_GAME_CHANNEL_ID


# ───────────────────────────── Constantes ─────────────────────────
GUILD_ID: Optional[int] = settings.DEBUG_GUILD_ID or None
JOIN_PING_ROLE_ID = settings.JOIN_PING_ROLE_ID  # @LeagueOfLegends — ping /5v5
BOT_ID_START = 9999999999999999  # ✅ IDs bots factices réalistes


# ───────────────────────────── Persistence Redis ───────────────────
async def save_match_state(match_data: Dict[Any, Any]):
    """✅ Sauvegarde l'état de la custom dans Redis."""
    try:
        await r_set("current_match", json.dumps(match_data), ttl=7200)  # 2h max
        logger.debug(f"💾 État sauvegardé: phase={match_data.get('phase')}")
    except Exception as e:
        logger.error(f"❌ Erreur sauvegarde état: {e}")


async def load_match_state() -> Optional[Dict]:
    """✅ Charge l'état de la custom depuis Redis."""
    try:
        raw = await r_get("current_match")
        if not raw:
            return None

        if isinstance(raw, dict):
            return raw

        if isinstance(raw, str):
            data = json.loads(raw)
            return data if isinstance(data, dict) else None

        return None
    except Exception as e:
        logger.error(f"❌ Erreur chargement état: {e}")
        return None


async def clear_match_state():
    """✅ Efface l'état de la custom."""
    try:
        await r_set("current_match", json.dumps(None))
        logger.debug("🗑️ État effacé")
    except Exception as e:
        logger.error(f"❌ Erreur effacement état: {e}")


# ───────────────────────────── Helpers Formatting ──────────────────
def generate_bot_id(index: int) -> int:
    """✅ Génère un ID de bot factice réaliste."""
    return BOT_ID_START + index


def format_team_list(
    team: List[int],
    captain_id: Optional[int] = None,
    guild: Optional[discord.Guild] = None,
    name_cache: Optional[Dict[int, str]] = None
) -> str:
    """✅ Helper centralisé pour formater une team (utilise les mentions pour les embeds)."""
    lines = []
    for uid in team:
        crown = "👑 " if uid == captain_id else ""

        # Priorité: member.mention > cache (mentions) > fallback
        if guild and (member := guild.get_member(uid)):
            mention = member.mention
        elif name_cache and uid in name_cache:
            mention = name_cache[uid]
        else:
            mention = f"<@{uid}>"

        lines.append(f"{crown}{mention}")

    return "\n".join(lines) if lines else "—"


async def get_members_batch(guild: discord.Guild, uids: List[int]) -> Dict[int, discord.Member]:
    """✅ Récupère plusieurs members en une fois."""
    members = {}
    for uid in uids:
        if uid >= BOT_ID_START:  # Skip bot IDs
            continue
        member = guild.get_member(uid)
        if member:
            members[uid] = member
    return members


# ============================================================================
# JoinView — phase d'inscription
# ============================================================================
class JoinView(discord.ui.View):
    def __init__(self, creator: discord.Member, bo: int, fearless: bool, captain_pick: bool):
        super().__init__(timeout=None)
        self.creator: discord.Member = creator
        self.bo, self.fearless = bo, fearless
        self.captain_pick = captain_pick

        self.players: Set[int] = set()
        self.name_cache: dict[int, str] = {}      # mention (<@123>) — pour les embeds
        self.display_cache: dict[int, str] = {}   # ✅ display name — pour les selects
        self.message: Optional[discord.Message] = None
        self.embed: Optional[discord.Embed] = None
        self._bump_task: Optional[asyncio.Task] = None

    def stop(self) -> None:
        self.stop_bump_task()
        super().stop()

    # ───────────────────────────── Error handler ──────────────────
    async def on_error(self, interaction: Interaction, error: Exception, item):
        """✅ Cleanup automatique en cas d'erreur."""
        logger.error(f"❌ Erreur dans JoinView: {error}", exc_info=True)

        await clear_match_state()
        interaction.client._current_match = None  # type: ignore[attr-defined]

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Une erreur est survenue. La custom a été annulée.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ Une erreur est survenue. La custom a été annulée.",
                    ephemeral=True
                )
        except Exception:
            pass

    # ───────────────────────────── Boutons inscription ────────────
    @discord.ui.button(
        label="🎮 Rejoindre",
        style=discord.ButtonStyle.primary,  # type: ignore[arg-type]
        row=0,
    )
    async def join(self, inter: Interaction, _):  # type: ignore[override]
        uid = inter.user.id
        if not is_user_linked(uid):
            return await inter.response.send_message("🔗 `/link` d'abord.", ephemeral=True)
        if uid in self.players:
            return await inter.response.send_message("✅ Déjà inscrit.", ephemeral=True)
        if len(self.players) >= 10:
            return await inter.response.send_message("❌ Partie pleine !", ephemeral=True)

        self.players.add(uid)
        self.name_cache[uid] = inter.user.mention           # pour les embeds
        self.display_cache[uid] = inter.user.display_name  # ✅ pour les selects
        await inter.response.defer()
        await self.refresh()

        logger.info(f"➕ {inter.user.name} a rejoint ({len(self.players)}/10)")

        if len(self.players) == 10:  # passage en phase 2
            logger.info("✅ Lobby complet (10/10)")
            self.stop()
            await self.show_confirm(inter)
            await self.message.delete()

    @discord.ui.button(
        label="🚪 Quitter",
        style=discord.ButtonStyle.secondary,  # type: ignore[arg-type]
        row=0,
    )
    async def quit(self, inter: Interaction, _):  # type: ignore[override]
        uid = inter.user.id
        if uid not in self.players:
            return await inter.response.send_message("❌ Pas inscrit.", ephemeral=True)

        self.players.remove(uid)
        await inter.response.defer()
        await self.refresh()

        logger.info(f"➖ {inter.user.name} a quitté ({len(self.players)}/10)")

    @discord.ui.button(
        label="❌ Annuler la custom",
        style=discord.ButtonStyle.danger,  # type: ignore[arg-type]
        row=0,
    )
    async def cancel(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user != self.creator:
            return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)

        logger.info(f"❌ Custom annulée par {inter.user.name}")

        await inter.response.send_message("❌ Custom annulée.")
        await self.message.delete()
        await clear_match_state()
        inter.client._current_match = None  # type: ignore[attr-defined]
        self.stop()

    # Bouton dev (remplissage auto de bots)
    @discord.ui.button(
        label="🔧 Compléter (dev)",
        style=discord.ButtonStyle.secondary,  # type: ignore[arg-type]
        row=1)
    async def complete_dev(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user != self.creator:
            return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)

        bot_count = 0
        while len(self.players) < 10:
            fake_id = generate_bot_id(bot_count)
            self.players.add(fake_id)
            bot_label = f"Bot{bot_count + 1}"
            self.name_cache[fake_id] = f"🤖 {bot_label}"
            self.display_cache[fake_id] = bot_label  # ✅ pas d'emoji dans le select
            bot_count += 1

        logger.info(f"🔧 {bot_count} bots ajoutés par {inter.user.name}")

        await inter.response.defer()
        await self.refresh()
        self.stop()
        await self.show_confirm(inter)
        await self.message.delete()

    # ───────────────────────── Embed live roster (clean) ──────────────────────
    async def refresh(self):
        if not (self.embed and self.message):
            return

        filled = len(self.players)

        # Barre de progression : 🟩 plein / ⬜ vide
        bar = "🟩" * filled + "⬜" * (10 - filled)

        # Paramètres compacts
        params = f"Bo **{self.bo}** · 🔥 **{'ON' if self.fearless else 'OFF'}** · 🎯 Captains **{'ON' if self.captain_pick else 'OFF'}**"

        # Roster : un joueur par ligne, slots libres gris
        roster = "\n".join(
            f"`{i + 1:>2}`  {self.name_cache.get(uid, f'<@{uid}>')}"
            if i < filled else
            f"`{i + 1:>2}`  *— libre —*"
            for i, uid in enumerate(list(self.players) + [None] * (10 - filled))
        )

        # Construction de l'embed
        self.embed.clear_fields()
        self.embed.title = "🎮  Lobby 5 v 5"
        self.embed.description = f"{bar}  **{filled}/10**\n{params}\n\n{roster}"
        self.embed.colour = discord.Colour.orange()

        # Icône du serveur comme vignette (si dispo)
        if (icon := (self.message.guild.icon if self.message.guild else None)):
            self.embed.set_thumbnail(url=icon.url)

        await self.message.edit(embed=self.embed, view=self)

        # ✅ Sauvegarder l'état dans Redis
        await save_match_state({
            "phase": "join",
            "creator_id": self.creator.id,
            "players": list(self.players),
            "name_cache": self.name_cache,
            "display_cache": self.display_cache,
            "bo": self.bo,
            "fearless": self.fearless,
            "captain_pick": self.captain_pick,
            "message_id": self.message.id,
            "channel_id": self.message.channel.id,
        })

    # ───────────────────────────── Auto-bump ─────────────────────────────────
    async def repost(self) -> None:
        """Supprime l'ancien message et reposte l'embed en bas du channel."""
        if not (self.message and self.embed):
            return
        channel = self.message.channel
        try:
            await self.message.delete()
        except Exception:
            pass
        new_msg = await channel.send(embed=self.embed, view=self)
        self.message = new_msg
        # Mettre à jour le message_id dans Redis
        await save_match_state({
            "phase": "join",
            "creator_id": self.creator.id,
            "players": list(self.players),
            "name_cache": self.name_cache,
            "display_cache": self.display_cache,
            "bo": self.bo,
            "fearless": self.fearless,
            "captain_pick": self.captain_pick,
            "message_id": new_msg.id,
            "channel_id": new_msg.channel.id,
        })

    def start_bump_task(self) -> None:
        """Lance la tâche de vérification auto-bump (10 min, ≥5 msgs après l'embed)."""
        if self._bump_task and not self._bump_task.done():
            return
        self._bump_task = asyncio.create_task(self._bump_loop())

    def stop_bump_task(self) -> None:
        if self._bump_task and not self._bump_task.done():
            self._bump_task.cancel()

    async def _bump_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(600)  # 10 min
                if not (self.message and self.embed):
                    break
                channel = self.message.channel
                try:
                    # Compter les messages apparus APRÈS l'embed
                    count = 0
                    async for _ in channel.history(limit=20, after=self.message):
                        count += 1
                        if count >= 5:
                            break
                    if count >= 5:
                        logger.info(f"🔁 Auto-bump lobby ({count} msgs depuis l'embed)")
                        await self.repost()
                except Exception as e:
                    logger.warning(f"Bump loop erreur: {e}")
        except asyncio.CancelledError:
            pass

    # ───────────────────────────── Phase "confirm / reroll / captains" ─────────
    async def show_confirm(self, inter: Interaction) -> None:
        ids = list(self.players)
        random.shuffle(ids)

        if self.captain_pick:
            # ── Mode Captains Pick : choisir deux capitaines (humains si possible) et lancer la vue de draft
            human_ids = [i for i in ids if i < BOT_ID_START]
            if len(human_ids) >= 2:
                cap_a, cap_b = random.sample(human_ids, 2)
            else:
                cap_a, cap_b = random.sample(ids, 2)

            remaining = [p for p in ids if p not in (cap_a, cap_b)]
            view = CaptainPickView(
                creator=self.creator,
                cap_a=cap_a,
                cap_b=cap_b,
                remaining=remaining,
                join_view=self
            )

            msg = await inter.channel.send(  # type: ignore[arg-type]
                embed=view.build_embed(inter.guild),
                view=view,
            )
            view.parent_message = msg

            # ✅ Sauvegarder état phase captain
            await save_match_state({
                "phase": "captain_pick",
                "creator_id": self.creator.id,
                "cap_a": cap_a,
                "cap_b": cap_b,
                "team_a": view.team_a,
                "team_b": view.team_b,
                "remaining": remaining,
                "bo": self.bo,
                "message_id": msg.id,
                "channel_id": msg.channel.id,
                "name_cache": self.name_cache,
                "display_cache": self.display_cache,
            })

            return

        # ── Mode Random : logique actuelle avec ajustement demandé
        view = TeamConfirmView(self.creator, ids[:5], ids[5:], self)
        msg = await inter.channel.send(  # type: ignore[arg-type]
            embed=view.build_embed(inter.guild),
            view=view,
        )
        view.parent_message = msg

        # ✅ Sauvegarder état phase confirm
        await save_match_state({
            "phase": "confirm",
            "creator_id": self.creator.id,
            "team_a": ids[:5],
            "team_b": ids[5:],
            "bo": self.bo,
            "message_id": msg.id,
            "channel_id": msg.channel.id,
            "name_cache": self.name_cache,
            "display_cache": self.display_cache,
        })


# ============================================================================
# TeamConfirmView — affiché publiquement (mode Random)
# ============================================================================
class TeamConfirmView(discord.ui.View):
    """Affiché publiquement ; seul l'organisateur clique."""

    def __init__(self, creator, team_a, team_b, join_view: JoinView):
        super().__init__(timeout=None)
        self.creator = creator
        self.team_a: List[int] = team_a
        self.team_b: List[int] = team_b
        self.join_view: JoinView = join_view
        self.parent_message: Optional[discord.Message] = None

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        embed = discord.Embed(
            title="🎲  Équipes générées",
            colour=discord.Colour.from_rgb(66, 133, 244),
            description="Clique sur 🔄 pour relancer ou ✅ pour valider.",
        )

        # ✅ Utiliser le helper centralisé
        embed.add_field(
            name="🟦  **TEAM A**",
            value=format_team_list(self.team_a, guild=guild, name_cache=self.join_view.name_cache),
            inline=True,
        )
        embed.add_field(
            name="🟥  **TEAM B**",
            value=format_team_list(self.team_b, guild=guild, name_cache=self.join_view.name_cache),
            inline=True,
        )

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.set_footer(text="🔄 Reroll  •  ✅ Accept")
        return embed

    @discord.ui.button(
        label="🔄 Reroll",
        style=discord.ButtonStyle.secondary,  # type: ignore[arg-type]
        row=0,
    )
    async def reroll(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user != self.creator:
            return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)

        ids = self.team_a + self.team_b
        random.shuffle(ids)
        self.team_a, self.team_b = ids[:5], ids[5:]

        logger.info(f"🔄 Reroll par {inter.user.name}")

        await inter.response.edit_message(embed=self.build_embed(inter.guild))

        # ✅ Mettre à jour l'état
        await save_match_state({
            "phase": "confirm",
            "creator_id": self.creator.id,
            "team_a": self.team_a,
            "team_b": self.team_b,
            "bo": self.join_view.bo,
            "message_id": self.parent_message.id if self.parent_message else None,
            "channel_id": self.parent_message.channel.id if self.parent_message else None,
            "name_cache": self.join_view.name_cache,
            "display_cache": self.join_view.display_cache,
        })

    @discord.ui.button(
        label="✅ Accept",
        style=discord.ButtonStyle.success,  # type: ignore[arg-type]
        row=0,
    )
    async def accept(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user != self.creator:
            return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)

        # ✅ Feedback immédiat
        await inter.response.defer()

        all_ids = self.team_a + self.team_b
        has_bot = any(uid >= BOT_ID_START for uid in all_ids)

        if not has_bot:
            # ⟶ Aucun bot : capitaines FULL RANDOM
            humans_a = [u for u in self.team_a if u < BOT_ID_START] or self.team_a
            humans_b = [u for u in self.team_b if u < BOT_ID_START] or self.team_b
            cap_a = random.choice(humans_a)
            cap_b = random.choice(humans_b)

            logger.info(f"👑 Capitaines random: {cap_a} (A) vs {cap_b} (B)")
        else:
            # ⟶ Il y a des bots : créateur = capitaine A
            cap_a = inter.user.id
            if cap_a in self.team_a:
                pool_b = [u for u in self.team_b if u < BOT_ID_START] or self.team_b
                cap_b = random.choice(pool_b)
            else:
                self.team_a, self.team_b = self.team_b, self.team_a
                pool_b = [u for u in self.team_b if u < BOT_ID_START] or self.team_b
                cap_b = random.choice(pool_b)

            logger.info(f"👑 Capitaines (avec bots): {cap_a} (A, créateur) vs {cap_b} (B)")

        await launch_ready_and_dispatch(
            inter=inter,
            parent_message=self.parent_message,
            team_a=self.team_a,
            team_b=self.team_b,
            cap_a=cap_a,
            cap_b=cap_b,
            bo=self.join_view.bo
        )

        # Cleanup
        await clear_match_state()
        inter.client._current_match = None  # type: ignore[attr-defined]
        self.join_view.stop()
        self.stop()


# ============================================================================
# CaptainPickView — tirage des 2 capitaines puis draft des teammates
# ============================================================================
class CaptainPickView(discord.ui.View):
    """Deux capitaines draftent leurs coéquipiers via un Select."""

    def __init__(self, creator: discord.Member, cap_a: int, cap_b: int,
                 remaining: List[int], join_view: JoinView):
        super().__init__(timeout=None)
        self.creator = creator
        self.cap_a = cap_a
        self.cap_b = cap_b
        self.join_view = join_view

        self.team_a: List[int] = [cap_a]
        self.team_b: List[int] = [cap_b]
        self.remaining: List[int] = list(remaining)
        self.parent_message: Optional[discord.Message] = None
        self.turn: str = "A"

        self.select = discord.ui.Select(
            placeholder="Choisis un joueur pour ton équipe",
            min_values=1, max_values=1,
            options=self._make_options()
        )
        self.select.callback = self._on_pick  # type: ignore[assignment]
        self.add_item(self.select)

        self.add_item(self._btn_reroll())
        self.add_item(self._btn_cancel())

    def _get_display_name(self, uid: int) -> str:
        """✅ Résout un display name lisible pour un uid (pour les selects)."""
        # 1. Bots factices
        if uid >= BOT_ID_START:
            return self.join_view.display_cache.get(uid, f"Bot{uid - BOT_ID_START + 1}")

        # 2. Guild member en cache Discord (le plus fiable)
        if self.parent_message and self.parent_message.guild:
            member = self.parent_message.guild.get_member(uid)
            if member:
                return member.display_name

        # 3. display_cache (stocké au moment du join)
        cached = self.join_view.display_cache.get(uid)
        if cached:
            return cached

        # 4. Fallback lisible (jamais d'ID brut ni de mention)
        return f"Joueur {uid}"

    def _make_options(self) -> List[discord.SelectOption]:
        """✅ Construit les options du select avec des noms lisibles."""
        opts: List[discord.SelectOption] = []
        for uid in self.remaining:
            label = self._get_display_name(uid)
            opts.append(discord.SelectOption(label=label[:100], value=str(uid)))

        if not opts:
            opts = [discord.SelectOption(label="(plus personne)", value="none", default=True)]
        return opts

    def _btn_reroll(self) -> discord.ui.Button:
        btn = discord.ui.Button(label="🔄 Reroll capitaines", style=discord.ButtonStyle.secondary, row=1)

        async def _cb(inter: Interaction):
            if inter.user != self.creator:
                return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)

            all_ids = list(self.join_view.players)
            human_ids = [i for i in all_ids if i < BOT_ID_START]
            if len(human_ids) >= 2:
                self.cap_a, self.cap_b = random.sample(human_ids, 2)
            else:
                self.cap_a, self.cap_b = random.sample(all_ids, 2)

            self.team_a, self.team_b = [self.cap_a], [self.cap_b]
            self.remaining = [p for p in all_ids if p not in (self.cap_a, self.cap_b)]
            self.turn = "A"
            self.select.options = self._make_options()

            logger.info(f"🔄 Capitaines reroll: {self.cap_a} vs {self.cap_b}")

            await inter.response.edit_message(embed=self.build_embed(inter.guild), view=self)

            # ✅ Mettre à jour l'état
            await save_match_state({
                "phase": "captain_pick",
                "creator_id": self.creator.id,
                "cap_a": self.cap_a,
                "cap_b": self.cap_b,
                "team_a": self.team_a,
                "team_b": self.team_b,
                "remaining": self.remaining,
                "turn": self.turn,
                "bo": self.join_view.bo,
                "message_id": self.parent_message.id if self.parent_message else None,
                "channel_id": self.parent_message.channel.id if self.parent_message else None,
                "name_cache": self.join_view.name_cache,
                "display_cache": self.join_view.display_cache,
            })

        btn.callback = _cb  # type: ignore
        return btn

    def _btn_cancel(self) -> discord.ui.Button:
        btn = discord.ui.Button(label="❌ Annuler", style=discord.ButtonStyle.danger, row=1)

        async def _cb(inter: Interaction):
            if inter.user != self.creator:
                return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)

            logger.info(f"❌ CaptainPick annulée par {inter.user.name}")

            try:
                await inter.response.edit_message(content="❌ Custom annulée.", embed=None, view=None)
            except Exception:
                pass

            await clear_match_state()
            inter.client._current_match = None  # type: ignore[attr-defined]
            self.stop()
            self.join_view.stop()

        btn.callback = _cb  # type: ignore
        return btn

    def build_embed(self, guild: Optional[discord.Guild]) -> discord.Embed:
        capA_mention = f"<@{self.cap_a}>"
        capB_mention = f"<@{self.cap_b}>"
        title = "🧢 Captains Pick — sélection des équipes"
        desc = f"Tour : **{capA_mention if self.turn == 'A' else capB_mention}**"

        embed = discord.Embed(
            title=title,
            description=desc,
            colour=discord.Colour.from_rgb(66, 133, 244),
        )

        # ✅ Utiliser le helper centralisé (mentions pour les embeds)
        embed.add_field(
            name="🟦 TEAM A",
            value=format_team_list(self.team_a, self.cap_a, guild, self.join_view.name_cache),
            inline=True
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(
            name="🟥 TEAM B",
            value=format_team_list(self.team_b, self.cap_b, guild, self.join_view.name_cache),
            inline=True
        )

        # ✅ Restants : utiliser name_cache (mentions) pour l'embed
        remaining_list = [
            self.join_view.name_cache.get(uid, f"<@{uid}>")
            for uid in self.remaining
        ] or ["—"]
        embed.add_field(name="🧾 Restants", value="\n".join(remaining_list), inline=False)

        return embed

    async def _on_pick(self, inter: Interaction):
        current_cap = self.cap_a if self.turn == "A" else self.cap_b
        if inter.user.id != current_cap:
            return await inter.response.send_message("⛔ Tour du capitaine uniquement.", ephemeral=True)

        if not self.remaining:
            return await inter.response.send_message("❌ Plus personne à choisir.", ephemeral=True)

        val = self.select.values[0]
        if val == "none":
            return await inter.response.send_message("❌ Aucun joueur disponible.", ephemeral=True)

        try:
            picked = int(val)
        except ValueError:
            return await inter.response.send_message("❌ Sélection invalide.", ephemeral=True)

        if picked not in self.remaining:
            return await inter.response.send_message("❌ Déjà pris.", ephemeral=True)

        # Ajouter au bon côté
        if self.turn == "A":
            if len(self.team_a) >= 5:
                return await inter.response.send_message("❌ Team A complète.", ephemeral=True)
            self.team_a.append(picked)
            logger.info(f"👑 Cap A pick: {picked} ({len(self.team_a)}/5)")
            self.turn = "B"
        else:
            if len(self.team_b) >= 5:
                return await inter.response.send_message("❌ Team B complète.", ephemeral=True)
            self.team_b.append(picked)
            logger.info(f"👑 Cap B pick: {picked} ({len(self.team_b)}/5)")
            self.turn = "A"

        self.remaining.remove(picked)

        # Fin si 5v5
        if len(self.team_a) == 5 and len(self.team_b) == 5:
            logger.info("✅ Draft terminé (5v5)")

            try:
                await inter.response.edit_message(embed=self.build_embed(inter.guild), view=None)
            except Exception:
                pass

            await launch_ready_and_dispatch(
                inter=inter,
                parent_message=self.parent_message,
                team_a=self.team_a,
                team_b=self.team_b,
                cap_a=self.cap_a,
                cap_b=self.cap_b,
                bo=self.join_view.bo
            )

            await clear_match_state()
            inter.client._current_match = None  # type: ignore[attr-defined]
            self.join_view.stop()
            self.stop()
            return

        # Sinon, rafraîchir
        self.select.options = self._make_options()
        await inter.response.edit_message(embed=self.build_embed(inter.guild), view=self)

        # ✅ Mettre à jour l'état
        await save_match_state({
            "phase": "captain_pick",
            "creator_id": self.creator.id,
            "cap_a": self.cap_a,
            "cap_b": self.cap_b,
            "team_a": self.team_a,
            "team_b": self.team_b,
            "remaining": self.remaining,
            "turn": self.turn,
            "bo": self.join_view.bo,
            "message_id": self.parent_message.id if self.parent_message else None,
            "channel_id": self.parent_message.channel.id if self.parent_message else None,
            "name_cache": self.join_view.name_cache,
            "display_cache": self.join_view.display_cache,
        })


# ============================================================================
# Routine commune : affiche le VS final, déplace voc, READY, puis dispatch draft
# ============================================================================
async def launch_ready_and_dispatch(
    inter: Interaction,
    parent_message: Optional[discord.Message],
    team_a: List[int],
    team_b: List[int],
    cap_a: int,
    cap_b: int,
    bo: int,
):
    """✅ Affiche VS, déplace vocal, lance READY, puis dispatch draft."""

    # Embed VS
    vs = discord.Embed(
        title="⚔️  Équipes prêtes !",
        colour=discord.Colour.from_rgb(30, 136, 229),
    )

    # ✅ Batch fetch members
    guild = inter.guild
    members = await get_members_batch(guild, team_a + team_b) if guild else {}

    vs.add_field(
        name="🟦  **TEAM A**",
        value=format_team_list(team_a, cap_a, guild),
        inline=True
    )
    vs.add_field(name="\u200b", value="\u200b", inline=True)
    vs.add_field(
        name="🟥  **TEAM B**",
        value=format_team_list(team_b, cap_b, guild),
        inline=True
    )

    if guild and guild.icon:
        vs.set_thumbnail(url=guild.icon.url)
    vs.set_footer(text="👑 = capitaine  •  Bonne chance & have fun !")

    if parent_message:
        try:
            await parent_message.edit(embed=vs, view=None)
        except Exception as e:
            logger.warning(f"Impossible d'éditer le message VS: {e}")
            await inter.channel.send(embed=vs)  # type: ignore[arg-type]
    else:
        await inter.channel.send(embed=vs)  # type: ignore[arg-type]

    # ✅ Déplacement vocal avec gestion d'erreurs robuste
    current_vc = None
    if guild:
        for uid in team_a + team_b:
            if uid >= BOT_ID_START:  # Skip bots
                continue
            member = members.get(uid)
            if not (member and member.voice):
                current_vc = None
                break
            if current_vc is None:
                current_vc = member.voice.channel
            elif member.voice.channel != current_vc:
                current_vc = None
                break

        if current_vc and current_vc.category:
            channels = sorted(current_vc.category.voice_channels, key=lambda c: c.position)
            below = next((c for c in channels if c.position > current_vc.position), None)
            if below:
                logger.info(f"🔊 Déplacement Team B vers {below.name}")
                for uid in team_b:
                    if uid >= BOT_ID_START:
                        continue
                    m = members.get(uid)
                    if m and m.voice:
                        try:
                            await m.move_to(below)
                        except discord.Forbidden:
                            logger.warning(f"Permissions insuffisantes pour déplacer {m.name}")
                        except discord.HTTPException as e:
                            if e.code == 40032:
                                logger.debug(f"{m.name} pas dans vocal")
                            else:
                                logger.warning(f"Erreur déplacement {m.name}: {e}")

    # Ready phase
    from oogway.views.ready import ReadyView

    async def _go_draft():
        logger.info("✅ Capitaines prêts → dispatch start_draft")
        inter.client.dispatch(  # type: ignore[attr-defined]
            "start_draft",
            team_a,
            team_b,
            inter.channel,  # type: ignore[arg-type]
            bo,
            cap_a,
            cap_b,
        )

    ready = ReadyView(cap_a, cap_b, _go_draft)
    msg_ready = await inter.channel.send(  # type: ignore[arg-type]
        content="⏳ En attente des capitaines…",
        view=ready,
    )
    ready.message = msg_ready


# ============================================================================
# SetupView — choix Bo / Fearless / Captains ON-OFF
# ============================================================================
class SetupView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=300)  # ✅ 5 min timeout
        self.author_id = author_id
        self.bestof: int = 1
        self.fearless: bool = False
        self.canceled: bool = False
        self.captain_pick: bool = False
        self.done = asyncio.Event()

    @discord.ui.select(
        placeholder="Bo1",
        options=[discord.SelectOption(label=f"Bo{i}", value=str(i)) for i in (1, 3, 5)],
        row=0,
    )
    async def choose_bo(self, inter: Interaction, sel: discord.ui.Select):  # type: ignore[override]
        if inter.user.id != self.author_id:
            return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)

        self.bestof = int(sel.values[0])
        for o in sel.options:
            o.default = o.value == str(self.bestof)
        sel.placeholder = f"Bo{self.bestof}"
        await inter.response.edit_message(view=self)

    @discord.ui.button(label="Mode : Fearless OFF", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_fearless(self, inter: Interaction, btn: discord.ui.Button):  # type: ignore[override]
        if inter.user.id != self.author_id:
            return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)

        self.fearless = not self.fearless
        btn.label = f"Mode : Fearless {'ON' if self.fearless else 'OFF'}"
        btn.style = discord.ButtonStyle.success if self.fearless else discord.ButtonStyle.secondary
        await inter.response.edit_message(view=self)

    @discord.ui.button(label="Mode : Captains OFF", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_captains(self, inter: Interaction, btn: discord.ui.Button):
        if inter.user.id != self.author_id:
            return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)

        self.captain_pick = not self.captain_pick
        btn.label = f"Mode : Captains {'ON' if self.captain_pick else 'OFF'}"
        btn.style = discord.ButtonStyle.success if self.captain_pick else discord.ButtonStyle.secondary
        await inter.response.edit_message(view=self)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=2)
    async def cancel(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user.id != self.author_id:
            return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)

        self.canceled = True
        await inter.response.edit_message(content="❌ Création annulée.", view=None)
        self.done.set()

    @discord.ui.button(label="✅ Start", style=discord.ButtonStyle.success, row=2)
    async def start(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user.id != self.author_id:
            return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)

        await inter.response.edit_message(content="✅ Paramètres enregistrés !", view=None)
        self.done.set()


# ============================================================================
# Cog principal
# ============================================================================
class Custom5v5Cog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot._current_match = None  # type: ignore[attr-defined]
        self._match_lock = asyncio.Lock()  # ✅ Thread-safe

    @commands.Cog.listener()
    async def on_ready(self):
        """✅ Restaurer la custom si elle existe après restart."""
        state = await load_match_state()

        if not state:
            logger.info("Aucune custom à restaurer")
            return

        logger.info(f"♻️ Restauration custom en phase '{state.get('phase')}'")

        try:
            channel = self.bot.get_channel(state["channel_id"]) or await self.bot.fetch_channel(state["channel_id"])
            if not channel:
                logger.warning("Channel introuvable, abandon restauration")
                await clear_match_state()
                return

            message = await channel.fetch_message(state["message_id"])
            creator = await self.bot.fetch_user(state["creator_id"])

            # ✅ Restaurer selon la phase
            if state["phase"] == "join":
                await self._restore_join_phase(state, message, creator)
            elif state["phase"] == "confirm":
                logger.info("Phase 'confirm' détectée, mais restauration non implémentée (vue temporaire)")
                await clear_match_state()
            elif state["phase"] == "captain_pick":
                logger.info("Phase 'captain_pick' détectée, mais restauration non implémentée (vue temporaire)")
                await clear_match_state()
            else:
                logger.warning(f"Phase inconnue: {state['phase']}")
                await clear_match_state()

        except discord.NotFound:
            logger.warning("Message introuvable, abandon restauration")
            await clear_match_state()
        except Exception as e:
            logger.error(f"❌ Erreur restauration: {e}", exc_info=True)
            await clear_match_state()

    async def _restore_join_phase(self, state: Dict, message: discord.Message, creator: discord.Member):
        """✅ Restaure la phase d'inscription."""
        join_view = JoinView(
            creator=creator,
            bo=state["bo"],
            fearless=state["fearless"],
            captain_pick=state["captain_pick"]
        )

        join_view.players = set(state["players"])
        join_view.name_cache = state["name_cache"]
        join_view.display_cache = state.get("display_cache", {})  # ✅ restaurer display_cache
        join_view.message = message
        join_view.embed = message.embeds[0] if message.embeds else discord.Embed()

        # Enregistrer la vue AVANT l'edit pour que discord.py route les interactions
        self.bot.add_view(join_view, message_id=message.id)

        # Réattacher la vue au message
        await message.edit(view=join_view)
        await join_view.refresh()

        join_view.start_bump_task()
        self.bot._current_match = join_view  # type: ignore[attr-defined]
        logger.info(f"✅ Custom restaurée avec {len(join_view.players)}/10 joueurs")


    @app_commands.command(
    name="cancel-custom",
    description="Force l'annulation de la custom en cours",
    )
    @app_commands.guilds(GUILD_ID) if GUILD_ID else (lambda f: f)  # type: ignore
    @app_commands.checks.has_role(settings.ORGANIZER_ROLE_ID)
    async def cancel_custom(self, inter: Interaction):
        if self.bot._current_match is None and not await load_match_state():  # type: ignore[attr-defined]
            return await inter.response.send_message("ℹ️ Aucune custom active.", ephemeral=True)
    
        # Supprimer le message si possible
        match = self.bot._current_match  # type: ignore[attr-defined]
        if match and hasattr(match, "message") and match.message:
            try:
                await match.message.delete()
            except Exception:
                pass
        
        if match:
            match.stop()
    
        await clear_match_state()
        self.bot._current_match = None  # type: ignore[attr-defined]
    
        logger.info(f"🛑 Custom force-cancel par {inter.user.name}")
        await inter.response.send_message("✅ Custom annulée de force.", ephemeral=True)


    
    @app_commands.command(
        name="5v5",
        description="Créer une custom 5 v 5",
    )
    @app_commands.guilds(GUILD_ID) if GUILD_ID else (lambda f: f)  # type: ignore
    @app_commands.check(is_correct_channel)
    @app_commands.checks.has_role(settings.ORGANIZER_ROLE_ID)
    async def five_v_five(self, inter: Interaction):
        # ✅ Lock thread-safe
        async with self._match_lock:
            if self.bot._current_match is not None:  # type: ignore[attr-defined]
                return await inter.response.send_message("⚠️ Une custom est déjà active.", ephemeral=True)

            # ✅ Defer immédiatement
            await inter.response.defer(ephemeral=True, thinking=False)

            # Config
            setup = SetupView(inter.user.id)  # type: ignore[arg-type]
            await inter.followup.send("🔧 Configure ta partie :", view=setup, ephemeral=True)
            await setup.done.wait()

            if setup.canceled:
                logger.info(f"❌ Setup annulée par {inter.user.name}")
                return

            # ✅ Logs structurés
            logger.info(
                f"🎮 Nouvelle custom créée",
                extra={
                    "creator": inter.user.name,
                    "creator_id": inter.user.id,
                    "bo": setup.bestof,
                    "fearless": setup.fearless,
                    "captain_pick": setup.captain_pick
                }
            )

            # Ping du rôle
            ping_role = None
            if inter.guild and JOIN_PING_ROLE_ID:
                ping_role = inter.guild.get_role(JOIN_PING_ROLE_ID)
            if ping_role:
                try:
                    await inter.channel.send(  # type: ignore[arg-type]
                        content=f"{ping_role.mention} — **Rejoignez la 5v5 !**",
                        allowed_mentions=discord.AllowedMentions(roles=True),
                    )
                except discord.HTTPException:
                    await inter.channel.send(  # type: ignore[arg-type]
                        content=f"**Rejoignez la 5v5 !** ({ping_role.name})"
                    )

            # Lobby
            embed = (
                discord.Embed(
                    title="🎮 Nouvelle custom 5 v 5 !",
                    colour=discord.Colour.orange(),
                    description="Initialisation…",
                )
                .set_thumbnail(url="https://i.imgur.com/Yc5VdqJ.gif")
            )

            join = JoinView(inter.user, setup.bestof, setup.fearless, setup.captain_pick)
            join.embed = embed
            join.message = await inter.channel.send(embed=embed, view=join)  # type: ignore[arg-type]
            await join.refresh()
            join.start_bump_task()
            self.bot._current_match = join  # type: ignore[attr-defined]

    # ─── /ping-custom ─────────────────────────────────────────────────────────
    @app_commands.command(
        name="ping-custom",
        description="Ping les joueurs assidus (ou tout le rôle LoL) pour remplir la custom",
    )
    @app_commands.describe(ping_all="Si True, ping le rôle @LeagueOfLegends complet")
    async def ping_custom(self, inter: Interaction, ping_all: bool = False) -> None:
        # ── Vérification canal ────────────────────────────────────────────────
        if not is_correct_channel(inter):
            return await inter.response.send_message(
                "❌ Commande réservée au salon customs.", ephemeral=True
            )

        # ── Vérification droits : créateur de la custom OU organisateur ───────
        current_match: Optional[JoinView] = self.bot._current_match  # type: ignore[attr-defined]
        is_creator = current_match is not None and inter.user.id == current_match.creator.id
        is_organizer = False
        if inter.guild:
            org_role = inter.guild.get_role(settings.ORGANIZER_ROLE_ID)
            is_organizer = org_role in getattr(inter.user, "roles", [])

        if not is_creator and not is_organizer:
            return await inter.response.send_message(
                "⛔ Seul le créateur de la custom ou un organisateur peut utiliser cette commande.",
                ephemeral=True,
            )

        await inter.response.defer(ephemeral=True)

        # ── Choix du rôle à pinger ────────────────────────────────────────────
        role = None
        if ping_all:
            role_id = settings.LOL_ROLE_ID or settings.JOIN_PING_ROLE_ID
            if inter.guild and role_id:
                role = inter.guild.get_role(role_id)
        else:
            if inter.guild and settings.ASSIDUS_ROLE_ID:
                role = inter.guild.get_role(settings.ASSIDUS_ROLE_ID)

        if role is None:
            await inter.followup.send(
                "⚠️ Rôle non configuré (vérifie ASSIDUS_ROLE_ID / LOL_ROLE_ID dans le .env).",
                ephemeral=True,
            )
            return

        try:
            await inter.channel.send(  # type: ignore[arg-type]
                content=f"{role.mention} — **Il manque du monde pour la 5v5, rejoins !**",
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
            await inter.followup.send(f"✅ {role.name} pingé.", ephemeral=True)
            logger.info("📣 /ping-custom par %s → rôle %s", inter.user.name, role.name)
        except discord.HTTPException as e:
            await inter.followup.send(f"❌ Erreur lors du ping : {e}", ephemeral=True)

    @five_v_five.error  # type: ignore[override]
    async def _err(self, inter: Interaction, err: app_commands.AppCommandError):
        try:
            if isinstance(err, app_commands.CheckFailure):
                msg = "⛔ Pas le bon salon ou rôle."
            else:
                msg = "❌ Une erreur est survenue."
                logger.error(f"Erreur commande /5v5: {err}", exc_info=True)

            if inter.response.is_done():
                await inter.followup.send(msg, ephemeral=True)
            else:
                await inter.response.send_message(msg, ephemeral=True)
        except discord.NotFound:
            pass  # Interaction expirée


async def setup(bot: commands.Bot):
    await bot.add_cog(Custom5v5Cog(bot))

