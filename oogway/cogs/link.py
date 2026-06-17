# cogs/link.py
import logging
import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError
from oogway.database import (
    SessionLocal, User, LinkedAccount, init_db, find_puuid_owner,
)
from oogway.riot.client import RiotClient, RiotAPIError
from oogway.config import settings
log = logging.getLogger("oogway.link")
DEFAULT_REGION = "euw1"


class LinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.riot = RiotClient(settings.RIOT_API_KEY)
        logging.basicConfig(level=logging.INFO)

    # ─────────────────────────── Helpers ───────────────────────────
    async def _resolve_account(self, identifier: str):
        """Résout un summoner/RiotID → (puuid, real_name). Lève ValueError/RiotAPIError."""
        if "#" in identifier:
            game, tag = identifier.split("#", 1)
            acct = await self.riot.get_account_by_name_tag(DEFAULT_REGION, game, tag)
            if not acct:
                raise ValueError(f"Account not found: {identifier}")
            return acct["puuid"], acct["gameName"]
        summ = await self.riot.get_summoner_by_name(DEFAULT_REGION, identifier)
        if not summ:
            raise ValueError(f"Summoner not found: {identifier}")
        return summ["puuid"], summ["name"]

    @app_commands.command(
        name="link",
        description="Lier votre compte LoL (ex: /link Rekkles ou /link PaSsiN0#3050)."
    )
    @app_commands.describe(
        summoner_name="Nom d'invocateur ou RiotID (avec #tagLine)",
        smurf="Lier un compte secondaire (smurf) en plus de ton compte principal",
    )
    async def link(
        self,
        interaction: discord.Interaction,
        summoner_name: str,
        smurf: bool = False,
    ):
        if interaction.channel_id != settings.LINK_CHANNEL_ID:
            return await interaction.response.send_message(
                f"❌ Utilise cette commande dans <#{settings.LINK_CHANNEL_ID}>.",
                ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        identifier = summoner_name
        try:
            puuid, real_name = await self._resolve_account(identifier)
        except (RiotAPIError, ValueError) as e:
            log.warning(f"Failed to find summoner {identifier}: {e}")
            return await interaction.followup.send(
                f"❌ Impossible de trouver **{identifier}** en {DEFAULT_REGION.upper()}.",
                ephemeral=True
            )
        except Exception as e:
            log.error(f"Unexpected error fetching {identifier}: {e}", exc_info=True)
            return await interaction.followup.send(
                "❌ Erreur inattendue lors de la recherche du compte.",
                ephemeral=True
            )

        discord_id = str(interaction.user.id)
        if smurf:
            return await self._link_smurf(interaction, discord_id, puuid, real_name)
        return await self._link_main(interaction, discord_id, puuid, real_name)

    # ─────────────────────────── Compte principal ──────────────────
    async def _link_main(self, interaction, discord_id: str, puuid: str, real_name: str):
        try:
            with SessionLocal() as session:
                # Anti-usurpation : ce compte Riot appartient-il déjà à quelqu'un d'autre ?
                owner = find_puuid_owner(session, puuid)
                if owner and owner != discord_id:
                    return await interaction.followup.send(
                        f"❌ **{real_name}** est déjà lié par un autre membre.",
                        ephemeral=True
                    )

                user = session.get(User, discord_id)
                if user:
                    user.puuid = puuid
                    user.summoner_name = real_name
                    user.region = DEFAULT_REGION
                    session.commit()
                    msg = f"🔄 Mise à jour du compte principal : **{real_name}**."
                else:
                    session.add(User(
                        discord_id=discord_id,
                        puuid=puuid,
                        summoner_name=real_name,
                        region=DEFAULT_REGION,
                    ))
                    session.commit()
                    msg = f"✅ Compte principal lié : **{real_name}**."
        except SQLAlchemyError as e:
            log.error(f"Database error linking account: {e}", exc_info=True)
            return await interaction.followup.send(
                "❌ Erreur lors de la sauvegarde du lien.",
                ephemeral=True
            )
        await interaction.followup.send(msg, ephemeral=True)

    # ─────────────────────────── Compte smurf ──────────────────────
    async def _link_smurf(self, interaction, discord_id: str, puuid: str, real_name: str):
        try:
            with SessionLocal() as session:
                # Il faut un compte principal avant d'ajouter un smurf.
                user = session.get(User, discord_id)
                if not user:
                    return await interaction.followup.send(
                        "❌ Lie d'abord ton compte principal avec `/link` (sans `smurf`).",
                        ephemeral=True
                    )

                # Anti-usurpation / doublons.
                owner = find_puuid_owner(session, puuid)
                if owner == discord_id:
                    return await interaction.followup.send(
                        f"ℹ️ **{real_name}** est déjà lié à ton profil.",
                        ephemeral=True
                    )
                if owner:
                    return await interaction.followup.send(
                        f"❌ **{real_name}** est déjà lié par un autre membre.",
                        ephemeral=True
                    )

                session.add(LinkedAccount(
                    discord_id=discord_id,
                    puuid=puuid,
                    summoner_name=real_name,
                    region=DEFAULT_REGION,
                ))
                session.commit()
        except SQLAlchemyError as e:
            log.error(f"Database error linking smurf: {e}", exc_info=True)
            return await interaction.followup.send(
                "❌ Erreur lors de la sauvegarde du smurf.",
                ephemeral=True
            )
        await interaction.followup.send(
            f"✅ Smurf lié : **{real_name}**.", ephemeral=True
        )

    # ─────────────────────────── /comptes ──────────────────────────
    @app_commands.command(
        name="comptes",
        description="Affiche tes comptes liés (principal + smurfs).",
    )
    async def comptes(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        with SessionLocal() as session:
            user = session.get(User, discord_id)
            smurfs = session.query(LinkedAccount).filter_by(discord_id=discord_id).all()

        if not user:
            return await interaction.response.send_message(
                "🔗 Aucun compte lié. Utilise `/link` d'abord.", ephemeral=True
            )

        lines = [f"👑 **Principal** — {user.summoner_name}"]
        for s in smurfs:
            lines.append(f"🎭 Smurf — {s.summoner_name}")
        embed = discord.Embed(
            title="Tes comptes liés",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────── /unlink ───────────────────────────
    @app_commands.command(
        name="unlink",
        description="Délie un compte smurf (utilise /comptes pour voir tes comptes).",
    )
    @app_commands.describe(summoner_name="Nom d'invocateur ou RiotID du smurf à retirer")
    async def unlink(self, interaction: discord.Interaction, summoner_name: str):
        await interaction.response.defer(ephemeral=True)
        try:
            puuid, real_name = await self._resolve_account(summoner_name)
        except (RiotAPIError, ValueError):
            puuid, real_name = None, summoner_name

        discord_id = str(interaction.user.id)
        try:
            with SessionLocal() as session:
                q = session.query(LinkedAccount).filter_by(discord_id=discord_id)
                # On retrouve le smurf par puuid si résolu, sinon par nom.
                smurf = (
                    q.filter_by(puuid=puuid).first() if puuid else None
                ) or q.filter(LinkedAccount.summoner_name.ilike(summoner_name)).first()

                if not smurf:
                    return await interaction.followup.send(
                        f"❌ Aucun smurf **{real_name}** lié à ton profil. "
                        f"(Le compte principal se change avec `/link`.)",
                        ephemeral=True
                    )
                name = smurf.summoner_name
                session.delete(smurf)
                session.commit()
        except SQLAlchemyError as e:
            log.error(f"Database error unlinking smurf: {e}", exc_info=True)
            return await interaction.followup.send(
                "❌ Erreur lors de la suppression du smurf.", ephemeral=True
            )
        await interaction.followup.send(f"🗑️ Smurf délié : **{name}**.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkCog(bot))
