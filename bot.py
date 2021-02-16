import asyncio
from datetime import datetime
import importlib
import os
import sys
import traceback

import discord
from discord.ext import commands

from config import *

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=BOT_PREFIX, description='Werewolf', intents=intents)


initial_extensions = [
    'cogs.owner',
    'cogs.meta',
    'game.game'
]

if __name__ == '__main__':
    for extension in initial_extensions:
        bot.load_extension(extension)


@bot.event
async def on_ready():
    print('------')
    print('Logged in as')
    print(bot.user.name)
    print(bot.user.id)
    print(datetime.utcnow().strftime("%d/%m/%Y %I:%M:%S%p UTC"))
    print('------')
    await bot.change_presence(status=discord.Status.online, activity=discord.Game(name=PLAYING_MESSAGE))
    bot.uptime = datetime.utcnow()

    bot.recentcog = None

    print('Starting async init')
    bot._app_info = await bot.application_info()
    bot.owner = bot._app_info.owner

    while True:
        await asyncio.sleep(1)
        bot.WEREWOLF_SERVER = bot.get_guild(WEREWOLF_SERVER_ID)
        if bot.WEREWOLF_SERVER: break
    
    if not bot.WEREWOLF_SERVER:
      await bot.shutdown(f'Error: could not find guild with id {WEREWOLF_SERVER_ID}.')
    # bot.GAME_CHANNEL = bot.WEREWOLF_SERVER.get_channel(GAME_CHANNEL_ID)
    bot.LOG_CHANNEL = bot.WEREWOLF_SERVER.get_channel(LOG_CHANNEL_ID)
    # bot.ADMINS_ROLE = bot.WEREWOLF_SERVER.get_role(ADMINS_ROLE_ID)
    bot.PLAYERS_ROLE = bot.WEREWOLF_SERVER.get_role(PLAYERS_ROLE_ID)
    # required_fields = ('GAME_CHANNEL', 'DEBUG_CHANNEL', 'ADMINS_ROLE', 'PLAYERS_ROLE')
    # for field in required_fields:
    #   if not getattr(bot, field):
    #       await bot.shutdown(f'Error: could not find {field}. '
    #                           f'Please double-check {field}_ID in config.py.')
    print('async init complete')






try: bot.run(TOKEN)
except Exception as e:
    print("Whoops, bot failed to connect to Discord.")
    print(e)