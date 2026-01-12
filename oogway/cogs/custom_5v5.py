# oogway/cogs/custom_5v5.py — Custom 5 v 5 + Draft hook + Captains Pick
# ============================================================================

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional, Set, List, Tuple

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from oogway.config import settings
from oogway.database import SessionLocal, User

# ───────────────────────────── Logger ─────────────────────────────
logger = logging.getLogger(__name__)

# ───────────────────────────── Helpers DB / Check ─────────────────
def is_user_linked(uid: int) -> bool:
    db = SessionLocal()
    linked = db.query(User).filter_by(discord_id=str(uid)).first() is not None
    db.close()
    return linked


def is_correct_channel(inter: Interaction) -> bool:
    return inter.channel and inter.channel.id == settings.CUSTOM_GAME_CHANNEL_ID


# ───────────────────────────── Constantes ─────────────────────────
GUILD_ID: Optional[int] = settings.DEBUG_GUILD_ID or None
JOIN_PING_ROLE_ID = 1320082142369288244


# ============================================================================
# JoinView — phase d’inscription
# ============================================================================
class JoinView(discord.ui.View):
    def __init__(self, creator: discord.Member, bo: int, fearless: bool, captain_pick: bool):
        super().__init__(timeout=None)
        self.creator: discord.Member = creator
        self.bo, self.fearless = bo, fearless
        self.captain_pick = captain_pick

        self.players: Set[int] = set()
        self.name_cache: dict[int, str] = {}
        self.message: Optional[discord.Message] = None
        self.embed: Optional[discord.Embed] = None

    # ───────────────────────────── Boutons inscription ────────────
    @discord.ui.button(
        label="🎮 Rejoindre",
        style=discord.ButtonStyle.primary,  # type: ignore[arg-type]
        row=0,
    )
    async def join(self, inter: Interaction, _):  # type: ignore[override]
        uid = inter.user.id
        if not is_user_linked(uid):
            return await inter.response.send_message("🔗 `/link` d’abord.", ephemeral=True)
        if uid in self.players:
            return await inter.response.send_message("Déjà inscrit.", ephemeral=True)
        if len(self.players) >= 10:
            return await inter.response.send_message("Partie pleine !", ephemeral=True)

        self.players.add(uid)
        self.name_cache[uid] = inter.user.mention
        await inter.response.defer()
        await self.refresh()

        if len(self.players) == 10:  # passage en phase 2
            self.stop()
            await self.show_confirm(inter)
            await self.message.delete()

    @discord.ui.button(
        label="🚪 Quitter",
        style=discord.ButtonStyle.secondary, # type: ignore[arg-type]
        row=0,
    )
    async def quit(self, inter: Interaction, _):  # type: ignore[override]
        uid = inter.user.id
        if uid not in self.players:
            return await inter.response.send_message("Pas inscrit.", ephemeral=True)

        self.players.remove(uid)
        await inter.response.defer()
        await self.refresh()

    @discord.ui.button(
        label="❌ Annuler la custom",
        style=discord.ButtonStyle.danger, # type: ignore[arg-type]
        row=0,
    )
    async def cancel(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user != self.creator:
            return await inter.response.send_message("⛔", ephemeral=True)

        await inter.response.send_message("❌ Custom annulée.")
        await self.message.delete()
        inter.client._current_match = None  # type: ignore[attr-defined]
        self.stop()

    # Bouton dev (remplissage auto de bots)
    @discord.ui.button(
        label="🔧 Compléter (dev)",
        style=discord.ButtonStyle.secondary, # type: ignore[arg-type]
        row=1)
    async def complete_dev(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user != self.creator:
            return await inter.response.send_message("⛔", ephemeral=True)

        while len(self.players) < 10:
            fake_id = -len(self.players) - 1
            self.players.add(fake_id)
            self.name_cache[fake_id] = f"🤖 Bot{abs(fake_id)}"

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

        # Construction de l’embed
        self.embed.clear_fields()
        self.embed.title = "🎮  Lobby 5 v 5"
        self.embed.description = f"{bar}  **{filled}/10**\n{params}\n\n{roster}"
        self.embed.colour = discord.Colour.orange()

        # Icône du serveur comme vignette (si dispo)
        if (icon := (self.message.guild.icon if self.message.guild else None)):
            self.embed.set_thumbnail(url=icon.url)

        await self.message.edit(embed=self.embed, view=self)

    # ───────────────────────────── Phase “confirm / reroll / captains” ─────────
    async def show_confirm(self, inter: Interaction) -> None:
        ids = list(self.players)
        random.shuffle(ids)

        if self.captain_pick:
            # ── Mode Captains Pick : choisir deux capitaines (humains si possible) et lancer la vue de draft
            human_ids = [i for i in ids if i > 0]
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
            return

        # ── Mode Random : logique actuelle avec ajustement demandé
        view = TeamConfirmView(self.creator, ids[:5], ids[5:], self)
        msg = await inter.channel.send(  # type: ignore[arg-type]
            embed=view.build_embed(inter.guild),
            view=view,
        )
        view.parent_message = msg  # pour edit plus tard


# ============================================================================
# TeamConfirmView — affiché publiquement (mode Random)
# ============================================================================
class TeamConfirmView(discord.ui.View):
    """Affiché publiquement ; seul l’organisateur clique."""

    def __init__(self, creator, team_a, team_b, join_view: JoinView):
        super().__init__(timeout=None)
        self.creator = creator
        self.team_a: List[int] = team_a
        self.team_b: List[int] = team_b
        self.join_view: JoinView = join_view
        self.parent_message: Optional[discord.Message] = None  # défini par show_confirm

    # -------- Helpers ---------------------------------------------------
    def _names(
        self,
        team: list[int],
        captain_id: int | None = None,
        guild: Optional[discord.Guild] = None,
    ) -> str:
        names: list[str] = []
        for uid in team:
            crown = "👑 " if uid == captain_id else ""
            member = guild.get_member(uid) if guild else None
            mention = member.mention if member else f"<@{uid}>"
            names.append(f"{crown}{mention}")
        return "\n".join(names)

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        embed = discord.Embed(
            title="🎲  Équipes générées",
            colour=discord.Colour.from_rgb(66, 133, 244),  # bleu vif
            description="Clique sur 🔄 pour relancer ou ✅ pour valider.",
        )

        # Champs A / B côte à côte
        embed.add_field(
            name="🟦  **TEAM A**",
            value=self._names(self.team_a, guild=guild) or "—",
            inline=True,
        )
        embed.add_field(
            name="🟥  **TEAM B**",
            value=self._names(self.team_b, guild=guild) or "—",
            inline=True,
        )

        # Icône du serveur en vignette si elle existe
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.set_footer(text="🔄 Reroll  •  ✅ Accept")
        return embed

    # -------- Bouton Reroll --------------------------------------------
    @discord.ui.button(
        label="🔄 Reroll",
        style=discord.ButtonStyle.secondary, # type: ignore[arg-type]
        row=0,
        )
    async def reroll(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user != self.creator:
            return await inter.response.send_message("⛔", ephemeral=True)

        ids = self.team_a + self.team_b
        random.shuffle(ids)
        self.team_a, self.team_b = ids[:5], ids[5:]
        await inter.response.edit_message(embed=self.build_embed(inter.guild))

    # -------- Bouton Accept --------------------------------------------
    @discord.ui.button(
        label="✅ Accept",
        style=discord.ButtonStyle.success, # type: ignore[arg-type]
        row=0,
        )
    async def accept(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user != self.creator:
            return await inter.response.send_message("⛔", ephemeral=True)

        all_ids = self.team_a + self.team_b
        has_bot = any(uid < 0 for uid in all_ids)

        if not has_bot:
            # ⟶ Aucun bot : capitaines FULL RANDOM (créateur ≠ capitaine)
            #   On tire un capitaine dans chaque équipe parmi les humains si possible
            humans_a = [u for u in self.team_a if u > 0] or self.team_a
            humans_b = [u for u in self.team_b if u > 0] or self.team_b
            cap_a = random.choice(humans_a)
            cap_b = random.choice(humans_b)
            # si, par malchance, le créateur est sélectionné, ce n'est pas un problème (tu as dit "créateur != capitaine A"
            # mais l'important ici est qu'on ne force PAS le créateur comme capitaine A — il peut être A ou B ou pas capitaine)
        else:
            # ⟶ Il y a des bots : on garde l'ancien flux (créateur = capitaine A)
            cap_a = inter.user.id
            if cap_a in self.team_a:
                pool_b = [u for u in self.team_b if u > 0] or self.team_b
                cap_b = random.choice(pool_b)
            else:
                # le créateur était dans l'équipe B -> on l'échange pour devenir équipe A
                self.team_a, self.team_b = self.team_b, self.team_a
                pool_b = [u for u in self.team_b if u > 0] or self.team_b
                cap_b = random.choice(pool_b)

        await inter.response.defer()
        await launch_ready_and_dispatch(
            inter=inter,
            parent_message=self.parent_message,
            team_a=self.team_a,
            team_b=self.team_b,
            cap_a=cap_a,
            cap_b=cap_b,
            bo=self.join_view.bo
        )

        # fin : nettoyage états
        inter.client._current_match = None  # type: ignore[attr-defined]
        self.join_view.stop()
        self.stop()


# ============================================================================
# CaptainPickView — tirage des 2 capitaines puis draft des teammates
# ============================================================================
class CaptainPickView(discord.ui.View):
    """
    Deux capitaines (cap_a / cap_b) draftent leurs coéquipiers via un Select.
    L'ordre de pick alterne A, B, A, B, ...
    Quand 5v5 est atteint, on affiche l'embed final + READY → draft hook.
    """
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
        self.turn: str = "A"  # à qui de pick

        # Unique Select, réutilisé à chaque pick
        self.select = discord.ui.Select(
            placeholder="Choisis un joueur pour ton équipe",
            min_values=1, max_values=1,
            options=self._make_options()
        )
        self.select.callback = self._on_pick  # type: ignore[assignment]
        self.add_item(self.select)

        # Bouton Reroll (organisateur)
        self.add_item(self._btn_reroll())

        # Bouton Cancel (organisateur)
        self.add_item(self._btn_cancel())

    # ---------- UI builders ----------
    def _make_options(self) -> List[discord.SelectOption]:
        opts: List[discord.SelectOption] = []
        for uid in self.remaining:
            label = f"Bot{abs(uid)}" if uid < 0 else f"Joueur {uid}"
            # si on peut resolve le nom dans le cache du JoinView :
            label = self.join_view.name_cache.get(uid, label)
            opts.append(discord.SelectOption(label=label, value=str(uid)))
        if not opts:
            opts = [discord.SelectOption(label="(plus personne)", value="none", default=True)]
        return opts

    def _btn_reroll(self) -> discord.ui.Button:
        btn = discord.ui.Button(label="🔄 Reroll capitaines", style=discord.ButtonStyle.secondary, row=1)
        async def _cb(inter: Interaction):
            if inter.user != self.creator:
                return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)
            # on remet tout à zéro et on choisit 2 nouveaux capitaines
            all_ids = list(self.join_view.players)
            human_ids = [i for i in all_ids if i > 0]
            if len(human_ids) >= 2:
                self.cap_a, self.cap_b = random.sample(human_ids, 2)
            else:
                self.cap_a, self.cap_b = random.sample(all_ids, 2)
            self.team_a, self.team_b = [self.cap_a], [self.cap_b]
            self.remaining = [p for p in all_ids if p not in (self.cap_a, self.cap_b)]
            self.turn = "A"
            self.select.options = self._make_options()
            await inter.response.edit_message(embed=self.build_embed(inter.guild), view=self)
        btn.callback = _cb  # type: ignore
        return btn

    def _btn_cancel(self) -> discord.ui.Button:
        btn = discord.ui.Button(label="❌ Annuler", style=discord.ButtonStyle.danger, row=1)
        async def _cb(inter: Interaction):
            if inter.user != self.creator:
                return await inter.response.send_message("⛔ Organisateur uniquement.", ephemeral=True)
            try:
                await inter.response.edit_message(content="❌ Custom annulée.", embed=None, view=None)
            except Exception:
                pass
            inter.client._current_match = None  # type: ignore[attr-defined]
            self.stop()
            self.join_view.stop()
        btn.callback = _cb  # type: ignore
        return btn

    # ---------- Embed ----------
    def build_embed(self, guild: Optional[discord.Guild]) -> discord.Embed:
        def _names(team: List[int], captain: int) -> str:
            out = []
            for uid in team:
                crown = "👑 " if uid == captain else ""
                member = guild.get_member(uid) if guild else None
                mention = member.mention if member else f"<@{uid}>"
                out.append(f"{crown}{mention}")
            return "\n".join(out) if out else "—"

        capA_mention = f"<@{self.cap_a}>"
        capB_mention = f"<@{self.cap_b}>"
        title = "🧢 Captains Pick — sélection des équipes"
        desc = f"Tour : **{capA_mention if self.turn=='A' else capB_mention}**"

        embed = discord.Embed(
            title=title,
            description=desc,
            colour=discord.Colour.from_rgb(66, 133, 244),
        )
        embed.add_field(name="🟦 TEAM A", value=_names(self.team_a, self.cap_a), inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="🟥 TEAM B", value=_names(self.team_b, self.cap_b), inline=True)

        remaining_list = [self.join_view.name_cache.get(uid, f"<@{uid}>") for uid in self.remaining] or ["—"]
        embed.add_field(name="🧾 Restants", value="\n".join(remaining_list), inline=False)

        return embed

    # ---------- Pick handler ----------
    async def _on_pick(self, inter: Interaction):
        # Only the current captain can pick
        current_cap = self.cap_a if self.turn == "A" else self.cap_b
        if inter.user.id != current_cap:
            return await inter.response.send_message("⛔ Capitaines only (au tour du capitaine).", ephemeral=True)

        if not self.remaining:
            return await inter.response.send_message("Plus personne à choisir.", ephemeral=True)

        val = self.select.values[0]
        if val == "none":
            return await inter.response.send_message("Aucun joueur disponible.", ephemeral=True)

        try:
            picked = int(val)
        except ValueError:
            return await inter.response.send_message("Sélection invalide.", ephemeral=True)

        if picked not in self.remaining:
            return await inter.response.send_message("Déjà pris.", ephemeral=True)

        # Ajoute au bon côté
        if self.turn == "A":
            if len(self.team_a) >= 5:
                return await inter.response.send_message("Team A est complète.", ephemeral=True)
            self.team_a.append(picked)
            self.turn = "B"
        else:
            if len(self.team_b) >= 5:
                return await inter.response.send_message("Team B est complète.", ephemeral=True)
            self.team_b.append(picked)
            self.turn = "A"

        self.remaining.remove(picked)

        # Fin si 5v5
        if len(self.team_a) == 5 and len(self.team_b) == 5:
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

            inter.client._current_match = None  # type: ignore[attr-defined]
            self.join_view.stop()
            self.stop()
            return

        # Sinon, rafraîchir
        self.select.options = self._make_options()
        await inter.response.edit_message(embed=self.build_embed(inter.guild), view=self)


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
    # Embed VS
    def _names(team: List[int], captain_id: int, guild: Optional[discord.Guild]) -> str:
        out = []
        for uid in team:
            crown = "👑 " if uid == captain_id else ""
            member = guild.get_member(uid) if guild else None
            mention = member.mention if member else f"<@{uid}>"
            out.append(f"{crown}{mention}")
        return "\n".join(out) if out else "—"

    vs = discord.Embed(
        title="⚔️  Équipes prêtes !",
        colour=discord.Colour.from_rgb(30, 136, 229),  # bleu Riot
    )
    vs.add_field(name="🟦  **TEAM A**", value=_names(team_a, cap_a, inter.guild), inline=True)
    vs.add_field(name="\u200b", value="\u200b", inline=True)
    vs.add_field(name="🟥  **TEAM B**", value=_names(team_b, cap_b, inter.guild), inline=True)
    if inter.guild and inter.guild.icon:
        vs.set_thumbnail(url=inter.guild.icon.url)
    vs.set_footer(text="👑 = capitaine  •  Bonne chance & have fun !")

    if parent_message:
        try:
            await parent_message.edit(embed=vs, view=None)
        except Exception:
            await inter.channel.send(embed=vs)  # type: ignore[arg-type]
    else:
        await inter.channel.send(embed=vs)  # type: ignore[arg-type]

    # Déplacement vocal éventuel (Team B vers le channel en dessous si possible)
    guild = inter.guild
    current_vc = None
    for uid in team_a + team_b:
        member = guild.get_member(uid) if guild else None
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
            for uid in team_b:
                m = guild.get_member(uid)
                if m:
                    try:
                        await m.move_to(below)
                    except discord.HTTPException:
                        pass

    # Ready phase
    from oogway.views.ready import ReadyView

    async def _go_draft():
        logger.info("Capitaines prêts → dispatch start_draft")
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
        super().__init__(timeout=None)
        self.author_id = author_id
        self.bestof: int = 1
        self.fearless: bool = False
        self.canceled: bool = False
        self.captain_pick: bool = False
        self.done = asyncio.Event()

    # -------- Choix Bo ----------------------------------------------
    @discord.ui.select(
        placeholder="Bo1",
        options=[discord.SelectOption(label=f"Bo{i}", value=str(i)) for i in (1, 3, 5)],
        row=0,
    )
    async def choose_bo(self, inter: Interaction, sel: discord.ui.Select):  # type: ignore[override]
        if inter.user.id != self.author_id:
            return await inter.response.send_message("⛔", ephemeral=True)

        self.bestof = int(sel.values[0])
        for o in sel.options:
            o.default = o.value == str(self.bestof)
        sel.placeholder = f"Bo{self.bestof}"
        await inter.response.edit_message(view=self)

    # -------- Toggle Fearless ---------------------------------------
    @discord.ui.button(label="Mode : Fearless OFF", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_fearless(self, inter: Interaction, btn: discord.ui.Button):  # type: ignore[override]
        if inter.user.id != self.author_id:
            return await inter.response.send_message("⛔", ephemeral=True)

        self.fearless = not self.fearless
        btn.label = f"Mode : Fearless {'ON' if self.fearless else 'OFF'}"
        btn.style = discord.ButtonStyle.success if self.fearless else discord.ButtonStyle.secondary
        await inter.response.edit_message(view=self)

    # -------- Toggle Captains Pick ----------------------------------
    @discord.ui.button(label="Mode : Captains OFF", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_captains(self, inter: Interaction, btn: discord.ui.Button):
        if inter.user.id != self.author_id:
            return await inter.response.send_message("⛔", ephemeral=True)

        self.captain_pick = not self.captain_pick
        btn.label = f"Mode : Captains {'ON' if self.captain_pick else 'OFF'}"
        btn.style = discord.ButtonStyle.success if self.captain_pick else discord.ButtonStyle.secondary
        await inter.response.edit_message(view=self)

    # -------- Cancel / Start ----------------------------------------
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=2)
    async def cancel(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user.id != self.author_id:
            return await inter.response.send_message("⛔", ephemeral=True)

        self.canceled = True
        await inter.response.edit_message(content="❌ Création annulée.", view=None)
        inter.client._current_match = None  # type: ignore[attr-defined]
        self.done.set()

    @discord.ui.button(label="✅ Start", style=discord.ButtonStyle.success, row=2)
    async def start(self, inter: Interaction, _):  # type: ignore[override]
        if inter.user.id != self.author_id:
            return await inter.response.send_message("⛔", ephemeral=True)

        await inter.response.edit_message(content="✅ Paramètres enregistrés !", view=None)
        self.done.set()


# ============================================================================
# Cog principal
# ============================================================================
class Custom5v5Cog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot._current_match = None  # type: ignore[attr-defined]

    @app_commands.command(
        name="5v5",
        description="Créer une custom 5 v 5",
    )
    @app_commands.guilds(GUILD_ID) if GUILD_ID else (lambda f: f)  # type: ignore
    @app_commands.check(is_correct_channel)
    @app_commands.checks.has_role(settings.ORGANIZER_ROLE_ID)
    async def five_v_five(self, inter: Interaction):
        if self.bot._current_match is not None:  # type: ignore[attr-defined]
            if inter.response.is_done():
                return await inter.followup.send("⚠️ Une custom est déjà active.", ephemeral=True)
            return await inter.response.send_message("⚠️ Une custom est déjà active.", ephemeral=True)

        # Toujours déférer tout de suite (évite l'Unknown interaction si ça prend > 3s)
        await inter.response.defer(ephemeral=True, thinking=False)

        # Config
        setup = SetupView(inter.user.id)  # type: ignore[arg-type]
        await inter.followup.send("🔧 Configure ta partie :", view=setup, ephemeral=True)
        # 3) On attend la fin de la config
        await setup.done.wait()

        # 3.1) Si l'utilisateur a cliqué "Cancel", on sort proprement
        if setup.canceled:
            self.bot._current_match = None  # type: ignore[attr-defined]
            return

        # 4) On envoie le lobby public dans le salon (+ ping rôle)
        embed = (
            discord.Embed(
                title="🎮 Nouvelle custom 5 v 5 !",
                colour=discord.Colour.orange(),
                description="Initialisation…",
            )
            .set_thumbnail(url="https://i.imgur.com/Yc5VdqJ.gif")
        )

        # ---- PING DU RÔLE AU-DESSUS DE L'EMBED ----
        ping_role = None
        if inter.guild and JOIN_PING_ROLE_ID:  # ← utilise la constante locale
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

        # ⚠️ ICI le 4e param est OBLIGATOIRE : setup.captain_pick
        join = JoinView(inter.user, setup.bestof, setup.fearless, setup.captain_pick)
        join.embed = embed
        join.message = await inter.channel.send(embed=embed, view=join)  # type: ignore[arg-type]
        await join.refresh()
        self.bot._current_match = join  # type: ignore[attr-defined]

    @five_v_five.error  # type: ignore[override]
    async def _err(self, inter: Interaction, err: app_commands.AppCommandError):
        try:
            if isinstance(err, app_commands.CheckFailure):
                if inter.response.is_done():
                    await inter.followup.send("⛔ Pas le bon salon ou rôle.", ephemeral=True)
                else:
                    await inter.response.send_message("⛔ Pas le bon salon ou rôle.", ephemeral=True)
            else:
                if inter.response.is_done():
                    await inter.followup.send("❌ Une erreur est survenue.", ephemeral=True)
                else:
                    await inter.response.send_message("❌ Une erreur est survenue.", ephemeral=True)
                raise err
        except discord.NotFound:
            # Interaction expirée ou inconnue
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Custom5v5Cog(bot))
