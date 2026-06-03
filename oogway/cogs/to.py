# to.py – Commande /to : vote communautaire pour mettre un membre en time-out vocal
from __future__ import annotations

import asyncio
import time
from typing import Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands

from oogway.config import settings
from oogway.logging_config import get_logger

log = get_logger("oogway.cogs.to")

COLOR_VOTE    = 0xFFA500  # Orange – vote en cours
COLOR_PASSED  = 0xE74C3C  # Rouge  – TO appliqué
COLOR_REFUSED = 0x3498DB  # Bleu   – TO refusé

VOTE_DURATION    = 60   # secondes fixes pour le vote
MAX_TO_SECS      = 120  # durée max du TO
MIN_VOCAL_MEMBERS = 4   # membres min dans le vocal pour lancer un TO
ANTI_ABUSE_SECS  = 30   # durée du TO retour si vote refusé


# ─────────────────────────────────────────────────────────────────────────────
# Vue de vote
# ─────────────────────────────────────────────────────────────────────────────
class ToVoteView(discord.ui.View):
    def __init__(
        self,
        eligible_ids: set[int],
        target: discord.Member,
        duration: int,
    ):
        super().__init__(timeout=None)
        self.eligible_ids = eligible_ids
        self.target = target
        self.duration = duration
        self.votes: dict[int, bool] = {}        # user_id → True=Oui, False=Non
        self.voter_names: dict[int, str] = {}   # user_id → display_name
        self.message: Optional[discord.Message] = None
        self.end_ts: int = 0

    # résultat final
    @property
    def oui(self) -> int:
        return sum(1 for v in self.votes.values() if v)

    @property
    def non(self) -> int:
        return sum(1 for v in self.votes.values() if not v)

    @property
    def passed(self) -> bool:
        return self.oui > self.non

    def build_embed(self, final: bool = False) -> discord.Embed:
        if final:
            color = COLOR_PASSED if self.passed else COLOR_REFUSED
            title = "✅ TO Approuvé" if self.passed else "❌ TO Refusé"
        else:
            color = COLOR_VOTE
            title = "⚖️ Vote Time-Out"

        embed = discord.Embed(title=title, color=color)

        embed.add_field(name="Cible", value=self.target.mention, inline=True)
        embed.add_field(name="Durée TO", value=f"**{self.duration}s**", inline=True)

        if not final:
            embed.add_field(name="Fin du vote", value=f"<t:{self.end_ts}:R>", inline=True)

        # Listes des votants
        oui_names = [name for uid, name in self.voter_names.items() if self.votes[uid]]
        non_names = [name for uid, name in self.voter_names.items() if not self.votes[uid]]

        embed.add_field(
            name=f"✅ Oui — {len(oui_names)}",
            value="\n".join(oui_names) or "*aucun vote*",
            inline=True,
        )
        embed.add_field(
            name=f"❌ Non — {len(non_names)}",
            value="\n".join(non_names) or "*aucun vote*",
            inline=True,
        )

        if self.target.display_avatar:
            embed.set_thumbnail(url=self.target.display_avatar.url)

        if not final:
            embed.set_footer(text="Un vote par personne • Pas de changement possible")

        return embed

    async def _handle_vote(self, interaction: Interaction, choice: bool) -> None:
        if interaction.user.id not in self.eligible_ids:
            return await interaction.response.send_message(
                "Tu n'étais pas dans le vocal au lancement du vote.", ephemeral=True
            )
        if interaction.user.id in self.votes:
            return await interaction.response.send_message(
                "Tu as déjà voté.", ephemeral=True
            )
        if interaction.user.id == self.target.id:
            return await interaction.response.send_message(
                "Tu ne peux pas voter pour ton propre TO.", ephemeral=True
            )

        self.votes[interaction.user.id] = choice
        self.voter_names[interaction.user.id] = interaction.user.display_name

        label = "✅ Oui" if choice else "❌ Non"
        await interaction.response.send_message(
            f"Vote enregistré : **{label}**", ephemeral=True
        )

        # Mise à jour live de l'embed
        if self.message:
            await self.message.edit(embed=self.build_embed(final=False), view=self)

    @discord.ui.button(label="✅ Oui", style=discord.ButtonStyle.danger)
    async def btn_oui(self, interaction: Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, True)

    @discord.ui.button(label="❌ Non", style=discord.ButtonStyle.secondary)
    async def btn_non(self, interaction: Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, False)

    def close(self) -> None:
        """Désactive les boutons sans passer par le timeout de la View."""
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        self.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────
class ToCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="to",
        description="Lance un vote pour mettre un membre en time-out vocal"
    )
    @app_commands.describe(
        user="Le membre à mettre en TO",
        duree="Durée du TO en secondes (max 120)"
    )
    async def to_cmd(
        self,
        interaction: Interaction,
        user: discord.Member,
        duree: int,
    ) -> None:
        caller = interaction.user

        # ── Validations ──────────────────────────────────────────────────────
        if not isinstance(caller, discord.Member) or caller.voice is None:
            return await interaction.response.send_message(
                "Tu dois être dans un channel vocal pour lancer un TO.",
                ephemeral=True
            )

        caller_channel: discord.VoiceChannel = caller.voice.channel  # type: ignore[assignment]

        if user.voice is None or user.voice.channel != caller_channel:
            return await interaction.response.send_message(
                f"{user.mention} n'est pas dans ton channel vocal.",
                ephemeral=True
            )

        if user.id == caller.id:
            return await interaction.response.send_message(
                "Tu ne peux pas te mettre toi-même en TO.",
                ephemeral=True
            )

        # ── Paramètres ───────────────────────────────────────────────────────
        duree = min(duree, MAX_TO_SECS)

        # Snapshot des membres éligibles au vote (présents dans le vocal)
        vocal_members = caller_channel.members
        eligible_ids: set[int] = {m.id for m in vocal_members}

        # Quorum : 4 membres minimum dans le vocal
        if len(vocal_members) < MIN_VOCAL_MEMBERS:
            return await interaction.response.send_message(
                f"Il faut au moins **{MIN_VOCAL_MEMBERS} membres** dans le vocal pour lancer un TO "
                f"({len(vocal_members)} présent(s)).",
                ephemeral=True
            )

        end_ts = int(time.time()) + VOTE_DURATION

        # ── Vue + embed initial ───────────────────────────────────────────────
        view = ToVoteView(eligible_ids=eligible_ids, target=user, duration=duree)
        view.end_ts = end_ts

        await interaction.response.send_message(embed=view.build_embed(), view=view)
        message = await interaction.original_response()
        view.message = message

        # ── Attente fixe de 60s (indépendante des interactions) ──────────────
        await asyncio.sleep(VOTE_DURATION)

        view.close()
        await message.edit(embed=view.build_embed(final=True), view=view)

        if not view.passed:
            log.info("TO refusé pour %s (%d vs %d) – anti-abus : TO %ds sur %s",
                     user, view.oui, view.non, ANTI_ABUSE_SECS, caller)
            await self._apply_to(interaction.guild, caller, ANTI_ABUSE_SECS)  # type: ignore[arg-type]
            return

        # ── Application du TO ─────────────────────────────────────────────────
        log.info("TO approuvé pour %s (%ds) – %d vs %d", user, duree, view.oui, view.non)
        await self._apply_to(interaction.guild, user, duree)  # type: ignore[arg-type]

    async def _apply_to(
        self,
        guild: discord.Guild,
        member: discord.Member,
        duration: int,
    ) -> None:
        mute_role: Optional[discord.Role] = guild.get_role(settings.MUTE_ROLE_ID)
        if not mute_role:
            log.error("MUTE_ROLE_ID introuvable (%s)", settings.MUTE_ROLE_ID)
            return

        # Sauvegarder tous les rôles (hors @everyone, rôles bot/intégration)
        saved_roles = [
            r for r in member.roles
            if r != guild.default_role
            and not r.is_bot_managed()
            and not r.is_integration()
            and r.is_assignable()
        ]

        # Retirer tous les rôles
        try:
            if saved_roles:
                await member.remove_roles(*saved_roles, reason="TO communautaire")
        except discord.HTTPException as e:
            log.error("Impossible de retirer les rôles de %s : %s", member, e)

        # Attribuer le rôle mute
        try:
            await member.add_roles(mute_role, reason="TO communautaire")
        except discord.HTTPException as e:
            log.error("Impossible d'ajouter le rôle mute à %s : %s", member, e)

        # Kick du vocal — move_to(None) nécessite la permission Move Members
        try:
            await member.move_to(None, reason="TO communautaire")
            log.info("Kick vocal appliqué à %s", member)
        except discord.HTTPException as e:
            log.error("Impossible de kick %s du vocal : %s (status=%s, code=%s)", member, e, e.status, e.code)

        # Attendre la durée du TO
        await asyncio.sleep(duration)

        # Retirer le rôle mute
        try:
            await member.remove_roles(mute_role, reason="Fin du TO communautaire")
        except discord.HTTPException as e:
            log.error("Impossible de retirer le rôle mute de %s : %s", member, e)

        # Restaurer les rôles sauvegardés
        if saved_roles:
            roles_to_restore = [r for r in saved_roles if r.is_assignable()]
            try:
                await member.add_roles(*roles_to_restore, reason="Fin du TO communautaire")
            except discord.HTTPException as e:
                log.error("Impossible de restaurer les rôles de %s : %s", member, e)

        log.info("TO terminé pour %s – rôles restaurés : %s", member, [r.name for r in saved_roles])


async def setup(bot: commands.Bot):
    await bot.add_cog(ToCog(bot))
