# bot.py – Point d’entrée principal d’Oogway
# -----------------------------------------------------------------------------
#  • Charge tous les cogs listés dans EXTENSIONS en loguant les erreurs.
#  • Sync les slash‑commands : instantanément sur le serveur DEBUG_GUILD_ID s’il
#    est défini, sinon globalement (⚠️jusqu’à 1h de propagation chez Discord).
#  • Ne supprime PLUS l’arbre de commandes (plus de clear_commands) pour éviter
#    d’effacer /5v5.
#  • Affiche la liste des commandes enregistrées au démarrage pour debug.
# -----------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import os
import traceback

import discord
from discord.ext import commands

from oogway.config import settings
from oogway.logging_config import setup_logging, get_logger

###############################################################################
# Logging --------------------------------------------------------------------
###############################################################################
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
log = get_logger("oogway.bot")

###############################################################################
# Bot & intents --------------------------------------------------------------
###############################################################################
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True  # requis pour les commandes prefix (legacy)

bot = commands.Bot(command_prefix="/", intents=intents)

###############################################################################
# Extensions -----------------------------------------------------------------
###############################################################################
EXTENSIONS: list[str] = [
    "oogway.cogs.link",
    "oogway.cogs.match_alerts",
    "oogway.cogs.leaderboard",
    "oogway.cogs.custom_5v5",
    "oogway.cogs.draft",
    "oogway.cogs.profile",
    "oogway.cogs.rdv",
]


async def load_all_extensions() -> None:
    """Charge chaque extension en loguant la stack‑trace si une erreur survient."""
    for ext in EXTENSIONS:
        try:
            await bot.load_extension(ext)
            log.info("✅ Loaded extension %s", ext)
        except (ImportError, commands.ExtensionError, commands.ExtensionFailed) as e:
            log.error("❌ Failed to load extension %s: %s\n%s", ext, e, traceback.format_exc())
            # Pour prod, on stoppe le bot si un cog plante :
            raise

###############################################################################
# Events ---------------------------------------------------------------------
###############################################################################
@bot.event
async def on_ready():
    log.info("Bot prêt: %s (ID %s)", bot.user, bot.user.id)

    if settings.DEBUG_GUILD_ID:
        guild = discord.Object(id=settings.DEBUG_GUILD_ID)

        # Option : duplique l’arbre global dans la guilde de dev
        bot.tree.copy_global_to(guild=guild)

        synced = await bot.tree.sync(guild=guild)
        log.info("🔁 Synced %d commandes sur la guilde %s", len(synced), settings.DEBUG_GUILD_ID)
    else:
        synced = await bot.tree.sync()
        log.info("🌐 Synced %d commandes globales (peut prendre ~1h)", len(synced))

    for cmd in bot.tree.walk_commands():
        log.info("• /%s – %s", cmd.qualified_name, cmd.description or "(no desc)")



###############################################################################
# Routine principale ---------------------------------------------------------
###############################################################################
async def main() -> None:
    await load_all_extensions()
    await bot.start(settings.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
