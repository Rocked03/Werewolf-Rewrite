from datetime import datetime, timedelta
from discord.ext import commands

from .engine import GameEngine


class Game(commands.Cog, name="Game"):
	def __init__(self, bot):
		self.bot = bot
		self.engine = GameEngine(bot)

		self.lg = self.engine.lg
		self.session_update = self.engine.session_update




bot.add_cog(Game(bot))