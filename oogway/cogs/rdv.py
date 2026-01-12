# cogs/rdv.py
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional, Literal, Iterable, Dict, List, Set

import dateparser
import discord
from discord import app_commands
from discord.ext import commands
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
TZ_PARIS = ZoneInfo("Europe/Paris")

Status = Literal["YES", "MAYBE", "NO", "WAIT"]

REMINDER_OFFSETS = [
    dt.timedelta(hours=1),
    dt.timedelta(minutes=15),
    dt.timedelta(seconds=0),
]

# ──────────────────────────────────────────────────────────────────────────────
# État en mémoire (pas de DB)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RDV:
    # Identifiants
    guild_id: int
    channel_id: int
    message_id: int
    creator_id: int
    thread_id: Optional[int] = None
    event_id: Optional[int] = None

    # Contenu
    activity: str = ""
    when_utc: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    capacity: Optional[int] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    ping_role_id: Optional[int] = None
    closed: bool = False

    # Participants
    yes: Set[int] = field(default_factory=set)
    maybe: Set[int] = field(default_factory=set)
    no: Set[int] = field(default_factory=set)
    wait: List[int] = field(default_factory=list)
    dm_optin: Set[int] = field(default_factory=set)

    # tâches planifiées
    reminder_tasks: List[asyncio.Task] = field(default_factory=list)

RDVS: Dict[int, RDV] = {}  # key = message_id

# ──────────────────────────────────────────────────────────────────────────────
# Parsing des dates
# ──────────────────────────────────────────────────────────────────────────────

NAT_MAP = {
    "soir": (20, 0),
    "matin": (10, 0),
    "aprem": (15, 0),
    "après-midi": (15, 0),
    "nuit": (23, 0),
}

def parse_when(expr: str) -> Optional[dt.datetime]:
    """Parse une expression libre en datetime Europe/Paris (tz-aware)."""
    s = expr.strip().lower()
    now_paris = dt.datetime.now(TZ_PARIS)

    if s in NAT_MAP:
        h, m = NAT_MAP[s]
        cand = now_paris.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now_paris:
            cand += dt.timedelta(days=1)
        return cand

    settings = {
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": "Europe/Paris",
        "TO_TIMEZONE": "Europe/Paris",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "RELATIVE_BASE": now_paris,
        "LANGUAGE": "fr",
    }
    dt_parsed = dateparser.parse(s, settings=settings, languages=["fr"])
    if dt_parsed is None:
        # HH[:MM] ou 21h
        try:
            s2 = s.replace("h", ":")
            parts = s2.split(":")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            cand = now_paris.replace(hour=h, minute=m, second=0, microsecond=0)
            if cand <= now_paris:
                cand += dt.timedelta(days=1)
            return cand
        except Exception:
            return None
    return dt_parsed.astimezone(TZ_PARIS)

# ──────────────────────────────────────────────────────────────────────────────
# Vue (boutons)
# ──────────────────────────────────────────────────────────────────────────────

class RDVView(discord.ui.View):
    def __init__(self, rdv_message_id: int):
        super().__init__(timeout=None)
        self.rdv_message_id = rdv_message_id

    @discord.ui.button(label="✅ Je viens", style=discord.ButtonStyle.success, custom_id="rdv:yes")
    async def btn_yes(self, i: discord.Interaction, _: discord.ui.Button):
        await handle_status_button(i, "YES", self.rdv_message_id)

    @discord.ui.button(label="❔ Peut-être", style=discord.ButtonStyle.secondary, custom_id="rdv:maybe")
    async def btn_maybe(self, i: discord.Interaction, _: discord.ui.Button):
        await handle_status_button(i, "MAYBE", self.rdv_message_id)

    @discord.ui.button(label="❌ Non", style=discord.ButtonStyle.danger, custom_id="rdv:no")
    async def btn_no(self, i: discord.Interaction, _: discord.ui.Button):
        await handle_status_button(i, "NO", self.rdv_message_id)

    @discord.ui.button(label="🔔 DM", style=discord.ButtonStyle.primary, custom_id="rdv:dm")
    async def btn_dm(self, i: discord.Interaction, _: discord.ui.Button):
        await toggle_dm(i, self.rdv_message_id)

def sanitize_capacity(cap: Optional[int]) -> Optional[int]:
    if cap is None:
        return None
    try:
        cap = int(cap)
    except Exception:
        return None
    return cap if cap > 0 else None

async def handle_status_button(i: discord.Interaction, new_status: Status, msg_id: int):
    await i.response.defer(ephemeral=True)
    rdv = RDVS.get(msg_id)
    if not rdv:
        return await i.followup.send("⛔ RDV introuvable (le bot a peut-être redémarré).", ephemeral=True)
    if rdv.closed:
        return await i.followup.send("🔒 Cet événement est fermé.", ephemeral=True)

    uid = i.user.id

    # Retirer l'utilisateur de toutes les listes
    rdv.yes.discard(uid)
    rdv.maybe.discard(uid)
    rdv.no.discard(uid)
    if uid in rdv.wait:
        rdv.wait.remove(uid)

    if new_status == "YES":
        if rdv.capacity is None or len(rdv.yes) < rdv.capacity:
            rdv.yes.add(uid)
            msg = "👍 Ajouté en ✅ Oui."
        else:
            rdv.wait.append(uid)
            msg = "🕗 Complet, tu passes en file d'attente."
    elif new_status == "MAYBE":
        rdv.maybe.add(uid)
        msg = "👌 Noté en ❔ Peut-être."
    elif new_status == "NO":
        rdv.no.add(uid)
        msg = "❌ Noté en Non."
        promote_waitlist(rdv)
    else:
        msg = "Pris en compte."

    await refresh_message(i.client, i.guild, rdv)
    await i.followup.send(msg, ephemeral=True)

async def toggle_dm(i: discord.Interaction, msg_id: int):
    await i.response.defer(ephemeral=True)
    rdv = RDVS.get(msg_id)
    if not rdv:
        return await i.followup.send("⛔ RDV introuvable.", ephemeral=True)
    uid = i.user.id
    if uid in rdv.dm_optin:
        rdv.dm_optin.remove(uid)
        return await i.followup.send("🔕 DM désactivés.", ephemeral=True)
    rdv.dm_optin.add(uid)
    return await i.followup.send("🔔 DM activés.", ephemeral=True)

def promote_waitlist(rdv: RDV):
    if rdv.capacity is None:
        return
    while rdv.wait and len(rdv.yes) < rdv.capacity:
        uid = rdv.wait.pop(0)
        rdv.yes.add(uid)

# ──────────────────────────────────────────────────────────────────────────────
# Embed
# ──────────────────────────────────────────────────────────────────────────────

def build_embed(guild: discord.Guild, rdv: RDV) -> discord.Embed:
    when_paris = rdv.when_utc.astimezone(TZ_PARIS)
    unix = int(when_paris.timestamp())
    rel = f"<t:{unix}:R>"
    abs_full = f"<t:{unix}:F>"
    title = f"{rdv.activity} — {len(rdv.yes)}/{rdv.capacity or '∞'}"
    e = discord.Embed(
        title=title,
        colour=0x5865F2 if not rdv.closed else 0x95a5a6,
        description=f"**Quand ?** {abs_full}\n{rel}",
    )
    if rdv.location:
        e.add_field(name="Lieu", value=rdv.location, inline=False)
    if rdv.notes:
        e.add_field(name="Notes", value=rdv.notes[:1024], inline=False)

    def fmt(users: Iterable[int]) -> str:
        return "\n".join(f"<@{u}>" for u in users) or "—"

    e.add_field(name=f"✅ Oui ({len(rdv.yes)})", value=fmt(sorted(rdv.yes)), inline=True)
    e.add_field(name=f"❔ Peut-être ({len(rdv.maybe)})", value=fmt(sorted(rdv.maybe)), inline=True)
    e.add_field(name=f"❌ Non ({len(rdv.no)})", value=fmt(sorted(rdv.no)), inline=True)
    if rdv.wait:
        e.add_field(name=f"🕗 File d'attente ({len(rdv.wait)})", value=fmt(rdv.wait), inline=False)
    e.set_footer(text=f"RDV #{rdv.message_id}")
    return e

async def refresh_message(bot: commands.Bot, guild: discord.Guild, rdv: RDV):
    try:
        channel = bot.get_channel(rdv.channel_id) or await bot.fetch_channel(rdv.channel_id)
        msg = await channel.fetch_message(rdv.message_id)
        view = RDVView(rdv.message_id)
        if rdv.closed:
            for c in view.children:
                c.disabled = True
        await msg.edit(embed=build_embed(guild, rdv), view=view)
    except Exception as e:
        log.warning("refresh_message failed: %s", e)

# ──────────────────────────────────────────────────────────────────────────────
# Rappels
# ──────────────────────────────────────────────────────────────────────────────

async def ensure_reminders(bot: commands.Bot, rdv: RDV):
    for t in rdv.reminder_tasks:
        t.cancel()
    rdv.reminder_tasks = []
    now = dt.datetime.now(dt.timezone.utc)
    for off in REMINDER_OFFSETS:
        when = rdv.when_utc - off
        if when <= now:
            continue
        rdv.reminder_tasks.append(asyncio.create_task(reminder_task(bot, rdv.message_id, when)))

async def reminder_task(bot: commands.Bot, msg_id: int, when: dt.datetime):
    await discord.utils.sleep_until(when)
    rdv = RDVS.get(msg_id)
    if not rdv or rdv.closed:
        return
    channel = bot.get_channel(rdv.channel_id) or await bot.fetch_channel(rdv.channel_id)
    ping = " ".join(f"<@{u}>" for u in rdv.yes) or "@here"
    tag = "Rappel" if when < rdv.when_utc else "C'est l'heure !"
    await channel.send(
        f"⏰ **{tag}** pour **{rdv.activity}** à <t:{int(rdv.when_utc.timestamp())}:t>\n{ping}"
    )
    # DMs
    for uid in list(rdv.dm_optin):
        try:
            user = await bot.fetch_user(uid)
            await user.send(
                f"⏰ {tag} — {rdv.activity} à <t:{int(rdv.when_utc.timestamp())}:F>"
            )
        except Exception:
            pass

# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class RendezVous(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(name="rdv", description="Gestion des rendez-vous")

    @group.command(name="new", description="Créer un rendez-vous")
    @app_commands.describe(
        activité="Nom de l'activité (Ciné, Foot, Raid, etc.)",
        quand="Heure ou expression (ex: 21:30, ce soir, demain 15h, dans 30 min)",
        capacité="Nombre de places (optionnel)",
        lieu="Lieu ou lien (optionnel)",
        notes="Détails/brief (optionnel)",
        ping_role="Rôle à notifier (optionnel)",
    )
    async def new(
        self,
        i: discord.Interaction,
        activité: str,
        quand: str,
        capacité: Optional[int] = None,
        lieu: Optional[str] = None,
        notes: Optional[str] = None,
        ping_role: Optional[discord.Role] = None,
    ):
        await i.response.defer()
        when = parse_when(quand)
        if not when:
            return await i.followup.send(
                "⛔ Heure invalide. Exemples : `21:00`, `ce soir`, `demain 15h`, `dans 30 min`.",
                ephemeral=True,
            )
        when_utc = when.astimezone(dt.timezone.utc)
        cap = sanitize_capacity(capacité)

        # Créer d'abord un message vide pour obtenir message_id
        placeholder = await i.channel.send("Préparation du rendez-vous…")

        rdv = RDV(
            guild_id=i.guild.id,
            channel_id=i.channel.id,
            message_id=placeholder.id,
            creator_id=i.user.id,
            activity=activité.strip()[:200],
            when_utc=when_utc,
            capacity=cap,
            location=(lieu or None),
            notes=(notes or None),
            ping_role_id=ping_role.id if ping_role else None,
        )
        # créateur en YES + DM
        rdv.yes.add(i.user.id)
        rdv.dm_optin.add(i.user.id)
        RDVS[placeholder.id] = rdv

        view = RDVView(placeholder.id)
        embed = build_embed(i.guild, rdv)
        content = f"{ping_role.mention if ping_role else ''} **Nouveau RDV !**".strip()
        await placeholder.edit(content=content, embed=embed, view=view)
        await placeholder.pin(reason="RDV")

        # Thread
        try:
            thread = await i.channel.create_thread(name=f"RDV – {activité}", message=placeholder)
            rdv.thread_id = thread.id
        except Exception as e:
            log.warning("create_thread failed: %s", e)

        # Scheduled Event (toujours en EXTERNAL, on ne crée *aucun* salon)
        end_time = when_utc + dt.timedelta(hours=2)
        try:
            event = await i.guild.create_scheduled_event(
                name=f"{activité}",
                start_time=when_utc,
                end_time=end_time,
                entity_type=discord.EntityType.external,
                location=lieu or "",
                description=(notes or "")[:1000],
                privacy_level=discord.PrivacyLevel.guild_only,
            )
            rdv.event_id = event.id
        except Exception as e:
            log.warning("Scheduled event creation failed: %s", e)

        # Rappels
        await ensure_reminders(self.bot, rdv)

        await i.followup.send(f"✅ RDV créé : {placeholder.jump_url}")

    @group.command(name="edit", description="Modifier un RDV")
    @app_commands.describe(
        id="ID du RDV (affiché en bas de l'embed)",
        activité="Nouveau titre (optionnel)",
        quand="Nouvelle heure (optionnel)",
        capacité="Nouvelle capacité (0 = illimité)",
        lieu="Nouveau lieu/lien",
        notes="Nouvelles notes",
    )
    async def edit(
        self,
        i: discord.Interaction,
        id: str,
        activité: Optional[str] = None,
        quand: Optional[str] = None,
        capacité: Optional[int] = None,
        lieu: Optional[str] = None,
        notes: Optional[str] = None,
    ):
        await i.response.defer(ephemeral=True)
        try:
            msg_id = int(id)
        except ValueError:
            return await i.followup.send("⛔ ID invalide.", ephemeral=True)
        rdv = RDVS.get(msg_id)
        if not rdv:
            return await i.followup.send("⛔ RDV introuvable (peut-être après redémarrage).", ephemeral=True)
        if rdv.creator_id != i.user.id and not i.user.guild_permissions.manage_events:
            return await i.followup.send("⛔ Tu n'es pas l'organisateur.", ephemeral=True)

        if activité:
            rdv.activity = activité[:200]
        if quand:
            nw = parse_when(quand)
            if not nw:
                return await i.followup.send("⛔ Heure invalide.", ephemeral=True)
            rdv.when_utc = nw.astimezone(dt.timezone.utc)
        if capacité is not None:
            rdv.capacity = sanitize_capacity(capacité)
            promote_waitlist(rdv)
        if lieu is not None:
            rdv.location = lieu or None
        if notes is not None:
            rdv.notes = notes or None

        await ensure_reminders(self.bot, rdv)
        await refresh_message(self.bot, i.guild, rdv)
        await i.followup.send("✏️ RDV mis à jour.", ephemeral=True)

    @group.command(name="close", description="Fermer un RDV (désactive les boutons)")
    @app_commands.describe(id="ID du RDV (footer de l'embed)")
    async def close(self, i: discord.Interaction, id: str):
        await i.response.defer(ephemeral=True)
        try:
            msg_id = int(id)
        except ValueError:
            return await i.followup.send("⛔ ID invalide.", ephemeral=True)
        rdv = RDVS.get(msg_id)
        if not rdv:
            return await i.followup.send("⛔ RDV introuvable.", ephemeral=True)
        if rdv.creator_id != i.user.id and not i.user.guild_permissions.manage_events:
            return await i.followup.send("⛔ Tu n'es pas l'organisateur.", ephemeral=True)

        rdv.closed = True
        await refresh_message(self.bot, i.guild, rdv)
        await i.followup.send("🔒 RDV fermé.", ephemeral=True)

    @group.command(name="info", description="Voir l'état d'un RDV")
    async def info(self, i: discord.Interaction, id: str):
        await i.response.defer(ephemeral=True)
        try:
            msg_id = int(id)
        except ValueError:
            return await i.followup.send("⛔ ID invalide.", ephemeral=True)
        rdv = RDVS.get(msg_id)
        if not rdv:
            return await i.followup.send("⛔ RDV introuvable.", ephemeral=True)
        await i.followup.send(embed=build_embed(i.guild, rdv), ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self):
        try:
            self.bot.tree.add_command(self.group)
        except Exception:
            pass
        log.info("RDV cog prêt (sans création de salon)")

async def setup(bot: commands.Bot):
    await bot.add_cog(RendezVous(bot))
