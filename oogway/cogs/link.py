# cogs/link.py
# ============================================================================
# ✅ AMÉLIORATIONS:
# - Type hint corrigé (Interaction au lieu de Context)
# - Validation format RiotID
# - Cache Redis (5 min)
# - Feedback si déjà linké au même compte
# - Commande /unlink
# - Retry automatique (3 tentatives)
# - Check niveau minimum
# - Logs structurés
# ============================================================================

import logging
from typing import Tuple, Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError
from tenacity import retry, stop_after_attempt, wait_exponential, RetryError

from oogway.database import SessionLocal, User, init_db
from oogway.riot.client import RiotClient, RiotAPIError
from oogway.config import settings
from oogway.cogs.profile import r_get, r_set

log = logging.getLogger("oogway.link")

DEFAULT_REGION = "euw1"
MIN_SUMMONER_LEVEL = 5  # Niveau minimum requis


class LinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.riot = RiotClient(settings.RIOT_API_KEY)

    # ───────────────────────────── Helper with retry ──────────────────
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True
    )
    async def _fetch_account(self, identifier: str) -> Tuple[str, str]:
        """
        ✅ Fetch account avec retry automatique.
        Returns: (puuid, summoner_name)
        """
        if "#" in identifier:
            parts = identifier.split("#")
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise ValueError("Invalid RiotID format")
            
            game, tag = parts
            acct = await self.riot.get_account_by_name_tag(DEFAULT_REGION, game, tag)
            if not acct:
                raise ValueError(f"Account not found: {identifier}")
            
            return acct["puuid"], acct["gameName"]
        else:
            summ = await self.riot.get_summoner_by_name(DEFAULT_REGION, identifier)
            if not summ:
                raise ValueError(f"Summoner not found: {identifier}")
            
            return summ["puuid"], summ["name"]

    # ───────────────────────────── /link command ──────────────────────
    @app_commands.command(
        name="link",
        description="Lier votre compte LoL (ex: `/link Rekkles` ou `/link Faker#KR1`)."
    )
    @app_commands.describe(summoner_name="Nom d'invocateur ou RiotID (avec #tagLine)")
    async def link(
        self,
        interaction: discord.Interaction,  # ✅ Type hint corrigé
        summoner_name: str,
    ):
        # ✅ Check channel
        if interaction.channel_id != settings.LINK_CHANNEL_ID:
            return await interaction.response.send_message(
                f"❌ Utilise cette commande dans <#{settings.LINK_CHANNEL_ID}>.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        identifier = summoner_name.strip()
        
        # ✅ Validation format RiotID
        if "#" in identifier:
            parts = identifier.split("#")
            if len(parts) != 2 or not parts[0] or not parts[1]:
                return await interaction.followup.send(
                    "❌ **Format invalide**\n"
                    "Utilise : `GameName#TAG` (ex: `Faker#KR1`)",
                    ephemeral=True
                )

        # ✅ Check cache Redis d'abord
        cache_key = f"riot_lookup:{identifier.lower()}"
        cached = await r_get(cache_key)
        
        if cached and isinstance(cached, dict):
            puuid = cached["puuid"]
            real_name = cached["name"]
            log.debug(f"Cache hit for {identifier}")
        else:
            # Fetch from API avec retry
            try:
                puuid, real_name = await self._fetch_account(identifier)
                
                # ✅ Sauvegarder dans cache (5 min)
                await r_set(cache_key, {"puuid": puuid, "name": real_name}, ttl=300)
                
            except ValueError as e:
                log.warning(
                    f"Invalid account lookup",
                    extra={
                        "discord_id": interaction.user.id,
                        "discord_name": interaction.user.name,
                        "identifier": identifier,
                        "error": str(e)
                    }
                )
                return await interaction.followup.send(
                    f"❌ Impossible de trouver **{identifier}** en `{DEFAULT_REGION.upper()}`.",
                    ephemeral=True
                )
            except RetryError:
                log.error(f"Riot API failed after 3 retries for {identifier}")
                return await interaction.followup.send(
                    "❌ L'API Riot ne répond pas. Réessaye dans quelques instants.",
                    ephemeral=True
                )
            except RiotAPIError as e:
                log.error(f"Riot API error for {identifier}: {e}", exc_info=True)
                return await interaction.followup.send(
                    "❌ Erreur lors de la communication avec l'API Riot.",
                    ephemeral=True
                )
            except Exception as e:
                log.error(
                    f"Unexpected error fetching {identifier}: {e}",
                    exc_info=True,
                    extra={
                        "discord_id": interaction.user.id,
                        "identifier": identifier
                    }
                )
                return await interaction.followup.send(
                    "❌ Erreur inattendue lors de la recherche du compte.",
                    ephemeral=True
                )

        # ✅ Check niveau minimum
        try:
            summoner = await self.riot.get_summoner_by_puuid(DEFAULT_REGION, puuid)
            level = summoner.get("summonerLevel", 0)
            
            if level < MIN_SUMMONER_LEVEL:
                return await interaction.followup.send(
                    f"⚠️ Ce compte est **niveau {level}**.\n"
                    f"Niveau minimum requis : **{MIN_SUMMONER_LEVEL}**.",
                    ephemeral=True
                )
        except Exception as e:
            log.warning(f"Failed to check summoner level for {puuid}: {e}")
            # Continue anyway (pas bloquant)

        # ✅ Save to database
        discord_id = str(interaction.user.id)
        
        try:
            with SessionLocal() as session:
                user = session.get(User, discord_id)
                
                if user:
                    # ✅ Check si déjà linké au même compte
                    if user.puuid == puuid:
                        return await interaction.followup.send(
                            f"ℹ️ Tu es déjà lié à **{real_name}**.",
                            ephemeral=True
                        )
                    
                    old_name = user.summoner_name
                    user.puuid = puuid
                    user.summoner_name = real_name
                    user.region = DEFAULT_REGION
                    session.commit()
                    
                    log.info(
                        f"Account updated",
                        extra={
                            "discord_id": discord_id,
                            "old_summoner": old_name,
                            "new_summoner": real_name
                        }
                    )
                    
                    msg = f"🔄 **Mise à jour**\nNouveau compte : **{real_name}**\nAncien : ~~{old_name}~~"
                else:
                    user = User(
                        discord_id=discord_id,
                        puuid=puuid,
                        summoner_name=real_name,
                        region=DEFAULT_REGION,
                    )
                    session.add(user)
                    session.commit()
                    
                    log.info(
                        f"Account linked",
                        extra={
                            "discord_id": discord_id,
                            "summoner_name": real_name
                        }
                    )
                    
                    msg = f"✅ **Compte lié**\n{real_name}"
        
        except SQLAlchemyError as e:
            log.error(
                f"Database error linking account: {e}",
                exc_info=True,
                extra={"discord_id": discord_id, "puuid": puuid}
            )
            return await interaction.followup.send(
                "❌ Erreur lors de la sauvegarde du lien.",
                ephemeral=True
            )

        await interaction.followup.send(msg, ephemeral=True)

    # ───────────────────────────── /unlink command ────────────────────
    @app_commands.command(
        name="unlink",
        description="Délier votre compte LoL"
    )
    async def unlink(self, interaction: discord.Interaction):
        """✅ Commande pour se délier."""
        await interaction.response.defer(ephemeral=True)
        
        discord_id = str(interaction.user.id)
        
        try:
            with SessionLocal() as session:
                user = session.get(User, discord_id)
                
                if not user:
                    return await interaction.followup.send(
                        "❌ Tu n'as pas de compte lié.",
                        ephemeral=True
                    )
                
                summoner_name = user.summoner_name
                session.delete(user)
                session.commit()
                
                log.info(
                    f"Account unlinked",
                    extra={
                        "discord_id": discord_id,
                        "summoner_name": summoner_name
                    }
                )
        
        except SQLAlchemyError as e:
            log.error(f"Database error unlinking account: {e}", exc_info=True)
            return await interaction.followup.send(
                "❌ Erreur lors de la suppression du lien.",
                ephemeral=True
            )
        
        await interaction.followup.send(
            f"✅ Compte **{summoner_name}** délié.",
            ephemeral=True
        )

    # ───────────────────────────── /whoami command ────────────────────
    @app_commands.command(
        name="whoami",
        description="Afficher votre compte lié"
    )
    async def whoami(self, interaction: discord.Interaction):
        """✅ Bonus: afficher son compte actuel."""
        discord_id = str(interaction.user.id)
        
        with SessionLocal() as session:
            user = session.get(User, discord_id)
            
            if not user:
                return await interaction.response.send_message(
                    "❌ Tu n'as pas de compte lié.\nUtilise `/link` pour en lier un.",
                    ephemeral=True
                )
            
            # Fetch rank info
            try:
                summoner = await self.riot.get_summoner_by_puuid(user.region, user.puuid)
                level = summoner.get("summonerLevel", "?")
                
                embed = discord.Embed(
                    title="🔗 Ton compte lié",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Invocateur", value=user.summoner_name, inline=True)
                embed.add_field(name="Région", value=user.region.upper(), inline=True)
                embed.add_field(name="Niveau", value=level, inline=True)
                embed.set_thumbnail(url=f"https://ddragon.leagueoflegends.com/cdn/14.1.1/img/profileicon/{summoner.get('profileIconId', 0)}.png")
                
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            except Exception as e:
                log.warning(f"Failed to fetch summoner info: {e}")
                return await interaction.response.send_message(
                    f"🔗 **Compte lié :** {user.summoner_name}\n"
                    f"📍 **Région :** {user.region.upper()}",
                    ephemeral=True
                )


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkCog(bot))
