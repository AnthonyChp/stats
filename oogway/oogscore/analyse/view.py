from __future__ import annotations
from io import BytesIO
import discord

class AnalyseView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        embeds: list[discord.Embed],
        image_bytes: dict[int, bytes],  # page_index -> raw PNG bytes (or empty)
        image_names: dict[int, str],    # page_index -> filename
        timeout: float = 180,
    ):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.embeds = embeds
        self.image_bytes = image_bytes
        self.image_names = image_names
        self.index = 0
        self.message: discord.Message | None = None
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.embeds) - 1
        self.counter_btn.label = f"Page {self.index + 1}/{len(self.embeds)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Ce n'est pas ta commande.", ephemeral=True)
            return False
        return True

    async def _show(self, interaction: discord.Interaction):
        self._update_buttons()
        embed = self.embeds[self.index]
        if self.index in self.image_bytes and self.image_bytes[self.index]:
            raw = self.image_bytes[self.index]
            fname = self.image_names.get(self.index, "image.png")
            f = discord.File(BytesIO(raw), filename=fname)
            await interaction.response.edit_message(embed=embed, attachments=[f], view=self)
        else:
            await interaction.response.edit_message(embed=embed, attachments=[], view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="Page 1/3", style=discord.ButtonStyle.grey, disabled=True)
    async def counter_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        pass

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.index = min(len(self.embeds) - 1, self.index + 1)
        await self._show(interaction)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
