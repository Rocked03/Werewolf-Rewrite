import asyncio
import datetime

import discord
from discord.ext import commands



class Meta(commands.Cog, name="Meta"):
    '''Cog for all bot-related stuff'''

    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def ping(self, ctx):
        """Checks the bot's latency"""
        latency = self.bot.latency*1000
        latency = round(latency,2)
        latency = str(latency)
        embed = discord.Embed(colour=0x7289da, timestamp=datetime.datetime.utcnow())
        embed.set_author(name="Ping!")
        embed.add_field(name='Bot latency', value=latency+"ms")
        embed.set_footer(
                        text=f"{str(ctx.author)} | {self.bot.user.name} | {ctx.prefix}{ctx.command.name}",
                        icon_url=self.bot.user.avatar_url)
        await ctx.send(embed=embed)


def setup(bot):
    bot.add_cog(Meta(bot))
