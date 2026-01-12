from __future__ import annotations
import discord, asyncio

class ReadyView(discord.ui.View):
    """
    Deux boutons « Capitaine prêt ».
    • Les IDs négatifs (bots) sont considérés prêts d’office.
    • Le message se met à jour après chaque clic.
    """

    def __init__(self, cap_a: int, cap_b: int, on_ready):
        super().__init__(timeout=None)
        self.cap_a, self.cap_b = cap_a, cap_b
        self.ready_a = cap_a < 0          # bots auto-prêts
        self.ready_b = cap_b < 0
        self.on_ready = on_ready          # callback async
        self.message: discord.Message | None = None

    # ───────── boutons ─────────
    @discord.ui.button(label="🟦 Capitaine A prêt", style=discord.ButtonStyle.primary)
    async def ready_a_btn(self, inter: discord.Interaction, _):
        if inter.user.id != self.cap_a:
            return await inter.response.send_message("⛔ Pas ton bouton.", ephemeral=True)
        if self.ready_a:
            return await inter.response.send_message("Déjà prêt !", ephemeral=True)

        self.ready_a = True
        await self._update(inter)

    @discord.ui.button(label="🟥 Capitaine B prêt", style=discord.ButtonStyle.danger)
    async def ready_b_btn(self, inter: discord.Interaction, _):
        if inter.user.id != self.cap_b:
            return await inter.response.send_message("⛔ Pas ton bouton.", ephemeral=True)
        if self.ready_b:
            return await inter.response.send_message("Déjà prêt !", ephemeral=True)

        self.ready_b = True
        await self._update(inter)

    # ───────── helpers internes ─────────
    async def _update(self, inter: discord.Interaction | None = None):
        if self.ready_a and self.ready_b:
            # tout le monde prêt → on désactive les boutons
            for item in self.children:
                item.disabled = True
            if self.message:
                await self.message.edit(content="✅ Les deux capitaines sont prêts !", view=self)
            await asyncio.sleep(1)
            await self.message.delete()
            await self.on_ready()
            return

        # sinon, on modifie le texte d’attente
        if self.message:
            if self.ready_a and not self.ready_b:
                txt = "🟦 Capitaine A prêt ✔️ — en attente du capitaine B…"
            elif self.ready_b and not self.ready_a:
                txt = "🟥 Capitaine B prêt ✔️ — en attente du capitaine A…"
            else:
                txt = "⏳ En attente des capitaines…"
            await self.message.edit(content=txt, view=self)
        if inter:
            await inter.response.edit_message(view=self)
