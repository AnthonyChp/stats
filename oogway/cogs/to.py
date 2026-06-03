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

VOTE_DURATION = 60   # secondes fixes pour le vote
MAX_TO_SECS   = 120  # durée max du TO


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
        super().__init__(timeout=VOTE_DURATION)
        self.eligible_ids = eligible_ids
        self.target = target
        self.duration = duration
        self.votes: dict[int, bool] = {}  # user_id → True=Oui, False=Non
        self._finished = asyncio.Event()

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
        label = "✅ Oui" if choice else "❌ Non"
        await interaction.response.send_message(
            f"Vote enregistré : **{label}**", ephemeral=True
        )

    @discord.ui.button(label="✅ Oui", style=discord.ButtonStyle.danger)
    async def btn_oui(self, interaction: Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, True)

    @discord.ui.button(label="❌ Non", style=discord.ButtonStyle.secondary)
    async def btn_non(self, interaction: Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, False)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        self._finished.set()

    async def wait_result(self) -> None:
        await self._finished.wait()


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
        eligible_ids: set[int] = {m.id for m in caller_channel.members}

        end_ts = int(time.time()) + VOTE_DURATION

        # ── Embed initial ────────────────────────────────────────────────────
        embed = discord.Embed(
            title="⚖️ Vote Time-Out",
            color=COLOR_VOTE,
        )
        embed.add_field(name="Cible", value=user.mention, inline=True)
        embed.add_field(name="Durée", value=f"**{duree}s**", inline=True)
        embed.add_field(name="Fin du vote", value=f"<t:{end_ts}:R>", inline=True)
        embed.add_field(
            name="Votants autorisés",
            value=f"{len(eligible_ids)} membre(s) du vocal",
            inline=False
        )
        embed.set_footer(text="Un vote par personne • Pas de changement possible")
        if user.display_avatar:
            embed.set_thumbnail(url=user.display_avatar.url)

        view = ToVoteView(eligible_ids=eligible_ids, target=user, duration=duree)

        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()

        # ── Attente de fin du vote ────────────────────────────────────────────
        await view.wait_result()

        # Désactiver les boutons
        for item in view.children:
            item.disabled = True  # type: ignore[attr-defined]

        total = view.oui + view.non
        result_color = COLOR_PASSED if view.passed else COLOR_REFUSED
        result_title = "✅ TO Approuvé" if view.passed else "❌ TO Refusé"
        result_desc = (
            f"**{view.oui} Oui** — **{view.non} Non** ({total} votant(s))"
        )

        embed.title = result_title
        embed.color = result_color
        embed.description = result_desc
        # Retirer le champ "Fin du vote" et le remplacer par résultat
        embed.clear_fields()
        embed.add_field(name="Cible", value=user.mention, inline=True)
        embed.add_field(name="Durée TO", value=f"**{duree}s**", inline=True)
        embed.add_field(name="Score", value=result_desc, inline=False)

        await message.edit(embed=embed, view=view)

        if not view.passed:
            log.info("TO refusé pour %s (%d vs %d)", user, view.oui, view.non)
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
        except discord.Forbidden:
            log.warning("Impossible de retirer les rôles de %s", member)

        # Attribuer le rôle mute
        try:
            await member.add_roles(mute_role, reason="TO communautaire")
        except discord.Forbidden:
            log.warning("Impossible d'ajouter le rôle mute à %s", member)

        # Kick du vocal
        try:
            await member.move_to(None, reason="TO communautaire")
        except discord.Forbidden:
            log.warning("Impossible de déplacer %s hors du vocal", member)

        # Attendre la durée du TO
        await asyncio.sleep(duration)

        # Retirer le rôle mute
        try:
            await member.remove_roles(mute_role, reason="Fin du TO communautaire")
        except discord.Forbidden:
            log.warning("Impossible de retirer le rôle mute de %s", member)

        # Restaurer les rôles sauvegardés
        if saved_roles:
            roles_to_restore = [r for r in saved_roles if r.is_assignable()]
            try:
                await member.add_roles(*roles_to_restore, reason="Fin du TO communautaire")
            except discord.Forbidden:
                log.warning("Impossible de restaurer les rôles de %s", member)

        log.info("TO terminé pour %s – rôles restaurés : %s", member, [r.name for r in saved_roles])


async def setup(bot: commands.Bot):
    await bot.add_cog(ToCog(bot))
