# cogs/link.py

import logging
from discord import app_commands
from discord.ext import commands

from oogway.database import SessionLocal, User, init_db
from oogway.riot.client import RiotClient
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
    @app_commands.describe(summoner_name="Nom d’invocateur ou RiotID (avec #tagLine)")
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
                acct = self.riot.get_account_by_name_tag(DEFAULT_REGION, game, tag)
                puuid = acct["puuid"]
                real_name = acct["gameName"]
            else:
                summ = self.riot.get_summoner_by_name(DEFAULT_REGION, identifier)
                puuid = summ["puuid"]
                real_name = summ["name"]
        except Exception:
            log.exception(f"Erreur récupération de {identifier}")
            return await interaction.followup.send(
                f"❌ Impossible de trouver **{identifier}** en `{DEFAULT_REGION.upper()}`.",
                ephemeral=True
            )

        discord_id = str(interaction.user.id)
        session = SessionLocal()
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
        session.close()

        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkCog(bot))
