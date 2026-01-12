# cogs/link.py

import logging
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from oogway.database import SessionLocal, User, init_db
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

    @app_commands.command(
        name="link",
        description="Lier votre compte LoL (ex: `/link Rekkles` ou `/link PaSsiN0#3050`)."
    )
    @app_commands.describe(summoner_name="Nom d'invocateur ou RiotID (avec #tagLine)")
    async def link(
        self,
        interaction: commands.Context,
        summoner_name: str,
    ):
        if interaction.channel_id != settings.LINK_CHANNEL_ID:
            return await interaction.response.send_message(
                f"❌ Utilise cette commande dans <#{settings.LINK_CHANNEL_ID}>.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        identifier = summoner_name

        try:
            if "#" in identifier:
                game, tag = identifier.split("#", 1)
                acct = await self.riot.get_account_by_name_tag(DEFAULT_REGION, game, tag)
                if not acct:
                    raise ValueError(f"Account not found: {identifier}")
                puuid = acct["puuid"]
                real_name = acct["gameName"]
            else:
                summ = await self.riot.get_summoner_by_name(DEFAULT_REGION, identifier)
                if not summ:
                    raise ValueError(f"Summoner not found: {identifier}")
                puuid = summ["puuid"]
                real_name = summ["name"]
        except (RiotAPIError, ValueError) as e:
            log.warning(f"Failed to find summoner {identifier}: {e}")
            return await interaction.followup.send(
                f"❌ Impossible de trouver **{identifier}** en `{DEFAULT_REGION.upper()}`.",
                ephemeral=True
            )
        except Exception as e:
            log.error(f"Unexpected error fetching {identifier}: {e}", exc_info=True)
            return await interaction.followup.send(
                "❌ Erreur inattendue lors de la recherche du compte.",
                ephemeral=True
            )

        discord_id = str(interaction.user.id)

        # Use context manager for DB session
        try:
            with SessionLocal() as session:
                user = session.get(User, discord_id)
                if user:
                    user.puuid = puuid
                    user.summoner_name = real_name
                    user.region = DEFAULT_REGION
                    session.commit()
                    msg = f"🔄 Mise à jour du lien pour **{real_name}**."
                else:
                    user = User(
                        discord_id=discord_id,
                        puuid=puuid,
                        summoner_name=real_name,
                        region=DEFAULT_REGION,
                    )
                    session.add(user)
                    session.commit()
                    msg = f"✅ Compte lié : **{real_name}**."
        except SQLAlchemyError as e:
            log.error(f"Database error linking account: {e}", exc_info=True)
            return await interaction.followup.send(
                "❌ Erreur lors de la sauvegarde du lien.",
                ephemeral=True
            )

        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkCog(bot))
