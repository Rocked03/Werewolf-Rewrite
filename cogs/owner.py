import asyncio
import inspect
import io
import sys
import traceback

import discord
from discord.ext import commands


def get_syntax_error(e):
    if e.text is None:
        return f'```py\n{e.__class__.__name__}: {e}\n```'
    return f'```py\n{e.text}{"^":>{e.offset}}\n{e.__class__.__name__}: {e}```'


class Owner(commands.Cog, name="Owner"):
    '''Owner-only commands.'''

    def __init__(self, bot):
        self.bot = bot

    async def __local_check(self, ctx):
        return await self.bot.is_owner(self, ctx.author)

    @bot.command(name='shutdown')
    async def shutdown(self, ctx):
        """Shuts down the bot"""
        try:
            embed = discord.Embed(timestamp=datetime.datetime.utcnow(), colour=0x7289da)
            embed.add_field(name="Shutting down...", value="Discord Werewolf")
            await ctx.send(embed=embed)
            totaluptime = datetime.datetime.utcnow() - self.bot.uptime
            totaluptime = strfdelta(totaluptime, "{days} days, {hours} hours, {minutes} minutes, {seconds} seconds")
            print(f'Shutting down... Total uptime: {totaluptime}')
            await bot.logout()
        except Exception: 
            # await ctx.send('Something went wrong.')
            pass


    @commands.group(name="cogs", aliases=["cog"])
    async def cogs(self, ctx):
        """Cog management"""
        return

    @cogs.command(name = 'load')
    async def loadcog(self, ctx, *, cog: str):
        """Loads cog. Remember to use dot path. e.g: cogs.owner"""

        try: bot.load_extension(cog)
        except Exception as e: return await ctx.send(f'**ERROR:** {type(e).__name__} - {e}')
        else: await ctx.send(f'Successfully loaded `{cog}`.')
        print('---')
        print(f'{cog} was loaded.')
        print('---')

    @cogs.command(name = 'unload')
    async def unloadcog(self, ctx, *, cog: str):
        """Unloads cog. Remember to use dot path. e.g: cogs.owner"""

        try: bot.unload_extension(cog)
        except Exception as e: return await ctx.send(f'**ERROR:** {type(e).__name__} - {e}')
        else: await ctx.send(f'Successfully unloaded `{cog}`.')
        print('---')
        print(f'{cog} was unloaded.')
        print('---')

    @cogs.command(name = 'reload')
    async def reloadcog(self, ctx, *, cog: str):
        """Reloads cog. Remember to use dot path. e.g: cogs.owner"""

        try: bot.reload_extension(cog)
        except Exception as e: return await ctx.send(f'**ERROR:** {type(e).__name__} - {e}')
        else: await ctx.send(f'Successfully reloaded `{cog}`.')
        bot.recentcog = cog
        print('---')
        print(f'{cog} was reloaded.')
        print('---')

    @bot.command(hidden = True, aliases = ['crr'])
    async def cogrecentreload(self, ctx):
        """Reloads most recent reloaded cog"""
        if not bot.recentcog: return await ctx.send("You haven't recently reloaded any cogs.")

        try: bot.reload_extension(bot.recentcog)
        except Exception as e: await ctx.send(f'**ERROR:** {type(e).__name__} - {e}')
        else: await ctx.send(f'Successfully reloaded `{bot.recentcog}`.')
        print('---')
        print(f'{bot.recentcog} was reloaded.')
        print('---')


def setup(bot):
    bot.add_cog(Owner(bot))
