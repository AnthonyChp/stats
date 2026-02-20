# moderation.py – Commandes de modération /report, /mute et /unmute
# -----------------------------------------------------------------------------
#  • /report @membre raison  : Signale un membre (accessible à tous)
#  • /mute @membre raison    : Mute un membre (rôle ORGANIZER requis)
#  • /unmute @membre         : Unmute un membre et restaure ses rôles
# -----------------------------------------------------------------------------

from __future__ import annotations

import datetime as dt

import discord
from discord import app_commands, Interaction
from discord.ext import commands

from oogway.config import settings
from oogway.database import SessionLocal, MutedUser
from oogway.logging_config import get_logger

log = get_logger("oogway.cogs.moderation")

# Couleurs des embeds
COLOR_REPORT = 0xFFA500  # Orange
COLOR_MUTE = 0xE74C3C    # Rouge
COLOR_UNMUTE = 0x3498DB  # Bleu
COLOR_SUCCESS = 0x2ECC71  # Vert


class ModerationCog(commands.Cog):
    """Cog pour les commandes de modération."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─────────────────────────────────────────────────────────────────────────
    # /report
    # ─────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="report",
        description="Signaler un membre au staff"
    )
    @app_commands.describe(
        membre="Le membre à signaler",
        raison="La raison du signalement"
    )
    async def report(
        self,
        interaction: Interaction,
        membre: discord.Member,
        raison: str
    ):
        """Signale un membre et envoie un embed dans le channel de modération."""
        await interaction.response.defer(ephemeral=True)

        # Récupérer le channel de modération
        mod_channel = self.bot.get_channel(settings.MODERATION_CHANNEL_ID)
        if not mod_channel:
            return await interaction.followup.send(
                "Erreur: Channel de modération introuvable.",
                ephemeral=True
            )

        # Créer l'embed de report
        embed = discord.Embed(
            title="NOUVEAU REPORT",
            color=COLOR_REPORT,
            timestamp=dt.datetime.now(dt.timezone.utc)
        )

        # Barre décorative en haut
        embed.description = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

        # Champs de l'embed
        embed.add_field(
            name="Membre signalé",
            value=f"{membre.mention}\n`{membre.name}` • ID: `{membre.id}`",
            inline=False
        )

        embed.add_field(
            name="Raison",
            value=f"```{raison}```",
            inline=False
        )

        embed.add_field(
            name="Signalé par",
            value=f"{interaction.user.mention}\n`{interaction.user.name}`",
            inline=True
        )

        embed.add_field(
            name="Salon",
            value=f"{interaction.channel.mention}" if interaction.channel else "N/A",
            inline=True
        )

        # Thumbnail avec l'avatar du membre signalé
        if membre.display_avatar:
            embed.set_thumbnail(url=membre.display_avatar.url)

        # Footer avec l'icône du serveur
        if interaction.guild and interaction.guild.icon:
            embed.set_footer(
                text=f"{interaction.guild.name} • Système de modération",
                icon_url=interaction.guild.icon.url
            )
        else:
            embed.set_footer(text="Système de modération")

        # Envoyer dans le channel de modération
        await mod_channel.send(embed=embed)

        # Confirmation à l'utilisateur
        confirm_embed = discord.Embed(
            title="Report envoyé",
            description=f"Votre signalement contre {membre.mention} a été transmis au staff.",
            color=COLOR_SUCCESS
        )
        confirm_embed.set_footer(text="Merci pour votre vigilance")

        await interaction.followup.send(embed=confirm_embed, ephemeral=True)
        log.info("Report de %s contre %s: %s", interaction.user, membre, raison)

    # ─────────────────────────────────────────────────────────────────────────
    # /mute
    # ─────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="mute",
        description="Mute un membre (retire ses rôles et attribue le rôle mute)"
    )
    @app_commands.describe(
        membre="Le membre à mute",
        raison="La raison du mute"
    )
    @app_commands.checks.has_role(settings.ORGANIZER_ROLE_ID)
    async def mute(
        self,
        interaction: Interaction,
        membre: discord.Member,
        raison: str
    ):
        """Mute un membre en retirant tous ses rôles et en lui donnant le rôle mute."""
        await interaction.response.defer(ephemeral=True)

        # Vérifications
        if membre.bot:
            return await interaction.followup.send(
                "Impossible de mute un bot.",
                ephemeral=True
            )

        if membre.id == interaction.user.id:
            return await interaction.followup.send(
                "Vous ne pouvez pas vous mute vous-même.",
                ephemeral=True
            )

        # Récupérer le rôle mute et le channel de modération
        mute_role = interaction.guild.get_role(settings.MUTE_ROLE_ID)
        mod_channel = self.bot.get_channel(settings.MODERATION_CHANNEL_ID)

        if not mute_role:
            return await interaction.followup.send(
                "Erreur: Rôle mute introuvable.",
                ephemeral=True
            )

        if not mod_channel:
            return await interaction.followup.send(
                "Erreur: Channel de modération introuvable.",
                ephemeral=True
            )

        # Sauvegarder les rôles actuels (pour l'affichage et la restauration)
        roles_to_remove = [
            role for role in membre.roles
            if role != interaction.guild.default_role  # @everyone
            and role != mute_role  # déjà le rôle mute
            and not role.is_bot_managed()  # rôles de bot
            and not role.is_integration()  # rôles d'intégration
            and role.is_assignable()  # rôles assignables par le bot
        ]

        roles_removed_names = [role.name for role in roles_to_remove]
        roles_removed_ids = [str(role.id) for role in roles_to_remove]

        # Sauvegarder en base de données pour pouvoir restaurer plus tard
        with SessionLocal() as session:
            # Supprimer l'ancien enregistrement si existant
            session.query(MutedUser).filter(MutedUser.discord_id == str(membre.id)).delete()

            # Créer le nouvel enregistrement
            muted_user = MutedUser(
                discord_id=str(membre.id),
                role_ids=",".join(roles_removed_ids),
                muted_at=dt.datetime.now(dt.timezone.utc),
                muted_by=str(interaction.user.id),
                reason=raison
            )
            session.add(muted_user)
            session.commit()

        # Retirer tous les rôles
        try:
            if roles_to_remove:
                await membre.remove_roles(*roles_to_remove, reason=f"Mute par {interaction.user}: {raison}")
        except discord.Forbidden:
            return await interaction.followup.send(
                "Erreur: Je n'ai pas la permission de retirer les rôles de ce membre.",
                ephemeral=True
            )

        # Ajouter le rôle mute
        try:
            await membre.add_roles(mute_role, reason=f"Mute par {interaction.user}: {raison}")
        except discord.Forbidden:
            return await interaction.followup.send(
                "Erreur: Je n'ai pas la permission d'ajouter le rôle mute.",
                ephemeral=True
            )

        # Créer l'embed de mute
        embed = discord.Embed(
            title="MEMBRE MUTE",
            color=COLOR_MUTE,
            timestamp=dt.datetime.now(dt.timezone.utc)
        )

        # Barre décorative
        embed.description = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

        # Champs
        embed.add_field(
            name="Membre",
            value=f"{membre.mention}\n`{membre.name}` • ID: `{membre.id}`",
            inline=False
        )

        embed.add_field(
            name="Raison",
            value=f"```{raison}```",
            inline=False
        )

        embed.add_field(
            name="Modérateur",
            value=f"{interaction.user.mention}\n`{interaction.user.name}`",
            inline=True
        )

        embed.add_field(
            name="Rôle attribué",
            value=f"{mute_role.mention}",
            inline=True
        )

        # Liste des rôles retirés
        if roles_removed_names:
            roles_list = ", ".join(f"`{name}`" for name in roles_removed_names[:10])
            if len(roles_removed_names) > 10:
                roles_list += f" *et {len(roles_removed_names) - 10} autres...*"
            embed.add_field(
                name=f"Rôles retirés ({len(roles_removed_names)})",
                value=roles_list,
                inline=False
            )
        else:
            embed.add_field(
                name="Rôles retirés",
                value="*Aucun rôle à retirer*",
                inline=False
            )

        # Thumbnail avec l'avatar du membre mute
        if membre.display_avatar:
            embed.set_thumbnail(url=membre.display_avatar.url)

        # Footer
        if interaction.guild and interaction.guild.icon:
            embed.set_footer(
                text=f"{interaction.guild.name} • Système de modération",
                icon_url=interaction.guild.icon.url
            )
        else:
            embed.set_footer(text="Système de modération")

        # Envoyer dans le channel de modération
        await mod_channel.send(embed=embed)

        # Confirmation au modérateur
        confirm_embed = discord.Embed(
            title="Mute effectué",
            description=(
                f"{membre.mention} a été mute avec succès.\n\n"
                f"**Rôles retirés:** {len(roles_removed_names)}\n"
                f"**Rôle attribué:** {mute_role.mention}"
            ),
            color=COLOR_SUCCESS
        )

        await interaction.followup.send(embed=confirm_embed, ephemeral=True)
        log.info("Mute de %s par %s: %s (rôles retirés: %s)", membre, interaction.user, raison, roles_removed_names)

    @mute.error
    async def mute_error(self, interaction: Interaction, error: app_commands.AppCommandError):
        """Gestion des erreurs pour /mute."""
        if isinstance(error, app_commands.MissingRole):
            embed = discord.Embed(
                title="Permission refusée",
                description="Vous n'avez pas la permission d'utiliser cette commande.",
                color=COLOR_MUTE
            )
            embed.set_footer(text="Rôle requis: Organisateur")

            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            log.error("Erreur /mute: %s", error)
            raise error

    # ─────────────────────────────────────────────────────────────────────────
    # /unmute
    # ─────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="unmute",
        description="Unmute un membre et restaure ses rôles"
    )
    @app_commands.describe(
        membre="Le membre à unmute"
    )
    @app_commands.checks.has_role(settings.ORGANIZER_ROLE_ID)
    async def unmute(
        self,
        interaction: Interaction,
        membre: discord.Member
    ):
        """Unmute un membre en lui redonnant ses rôles d'origine."""
        await interaction.response.defer(ephemeral=True)

        # Vérifications
        mute_role = interaction.guild.get_role(settings.MUTE_ROLE_ID)
        mod_channel = self.bot.get_channel(settings.MODERATION_CHANNEL_ID)

        if not mute_role:
            return await interaction.followup.send(
                "Erreur: Rôle mute introuvable.",
                ephemeral=True
            )

        if mute_role not in membre.roles:
            return await interaction.followup.send(
                f"{membre.mention} n'est pas mute.",
                ephemeral=True
            )

        # Récupérer les rôles sauvegardés en base de données
        roles_to_restore = []
        mute_info = None

        with SessionLocal() as session:
            muted_user = session.query(MutedUser).filter(
                MutedUser.discord_id == str(membre.id)
            ).first()

            if muted_user and muted_user.role_ids:
                mute_info = {
                    "muted_at": muted_user.muted_at,
                    "muted_by": muted_user.muted_by,
                    "reason": muted_user.reason
                }
                role_ids = [int(rid) for rid in muted_user.role_ids.split(",") if rid]
                for role_id in role_ids:
                    role = interaction.guild.get_role(role_id)
                    if role and role.is_assignable():
                        roles_to_restore.append(role)

                # Supprimer l'enregistrement
                session.delete(muted_user)
                session.commit()

        # Retirer le rôle mute
        try:
            await membre.remove_roles(mute_role, reason=f"Unmute par {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                "Erreur: Je n'ai pas la permission de retirer le rôle mute.",
                ephemeral=True
            )

        # Restaurer les rôles
        roles_restored_names = []
        if roles_to_restore:
            try:
                await membre.add_roles(*roles_to_restore, reason=f"Unmute par {interaction.user}")
                roles_restored_names = [role.name for role in roles_to_restore]
            except discord.Forbidden:
                log.warning("Impossible de restaurer certains rôles pour %s", membre)

        # Créer l'embed de unmute
        embed = discord.Embed(
            title="MEMBRE UNMUTE",
            color=COLOR_UNMUTE,
            timestamp=dt.datetime.now(dt.timezone.utc)
        )

        # Barre décorative
        embed.description = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

        # Champs
        embed.add_field(
            name="Membre",
            value=f"{membre.mention}\n`{membre.name}` • ID: `{membre.id}`",
            inline=False
        )

        embed.add_field(
            name="Unmute par",
            value=f"{interaction.user.mention}\n`{interaction.user.name}`",
            inline=True
        )

        # Infos sur le mute original
        if mute_info:
            muted_by_user = interaction.guild.get_member(int(mute_info["muted_by"]))
            muted_by_str = muted_by_user.mention if muted_by_user else f"ID: `{mute_info['muted_by']}`"
            embed.add_field(
                name="Mute original par",
                value=muted_by_str,
                inline=True
            )
            if mute_info["reason"]:
                embed.add_field(
                    name="Raison du mute",
                    value=f"```{mute_info['reason']}```",
                    inline=False
                )

        # Liste des rôles restaurés
        if roles_restored_names:
            roles_list = ", ".join(f"`{name}`" for name in roles_restored_names[:10])
            if len(roles_restored_names) > 10:
                roles_list += f" *et {len(roles_restored_names) - 10} autres...*"
            embed.add_field(
                name=f"Rôles restaurés ({len(roles_restored_names)})",
                value=roles_list,
                inline=False
            )
        else:
            embed.add_field(
                name="Rôles restaurés",
                value="*Aucun rôle à restaurer*",
                inline=False
            )

        # Thumbnail avec l'avatar du membre
        if membre.display_avatar:
            embed.set_thumbnail(url=membre.display_avatar.url)

        # Footer
        if interaction.guild and interaction.guild.icon:
            embed.set_footer(
                text=f"{interaction.guild.name} • Système de modération",
                icon_url=interaction.guild.icon.url
            )
        else:
            embed.set_footer(text="Système de modération")

        # Envoyer dans le channel de modération
        if mod_channel:
            await mod_channel.send(embed=embed)

        # Confirmation au modérateur
        confirm_embed = discord.Embed(
            title="Unmute effectué",
            description=(
                f"{membre.mention} a été unmute avec succès.\n\n"
                f"**Rôles restaurés:** {len(roles_restored_names)}"
            ),
            color=COLOR_SUCCESS
        )

        await interaction.followup.send(embed=confirm_embed, ephemeral=True)
        log.info("Unmute de %s par %s (rôles restaurés: %s)", membre, interaction.user, roles_restored_names)

    @unmute.error
    async def unmute_error(self, interaction: Interaction, error: app_commands.AppCommandError):
        """Gestion des erreurs pour /unmute."""
        if isinstance(error, app_commands.MissingRole):
            embed = discord.Embed(
                title="Permission refusée",
                description="Vous n'avez pas la permission d'utiliser cette commande.",
                color=COLOR_MUTE
            )
            embed.set_footer(text="Rôle requis: Organisateur")

            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            log.error("Erreur /unmute: %s", error)
            raise error


async def setup(bot: commands.Bot):
    """Charge le cog de modération."""
    await bot.add_cog(ModerationCog(bot))
