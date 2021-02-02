from collections import OrderedDict
from datetime import datetime, timedelta

class Session:
	def __init__(self, _id):
		self.id = _id

		self._phase = None                 # phase?
		self._in_session = False           # playing?
		self._players = []                 # {players dict}
		# self._events = {}                 # events

		self._daynight = False             # is day?
		self._day_start = 0                # day time start
		self._night_start = 0              # night time start
		self._day_elapsed = timedelta(0)   # day time elapsed
		self._night_elapsed = timedelta(0) # night time elapsed
		self._latest_day_elapsed = None    # latest day elapsed
		self._latest_night_elapsed = None  # latest day elapsed
		self._day_count = 0                # day count
		self._night_count = 0              # night count

		self._first_join = 0               # first join
		self._gamemode = None              # gamemode
		self._original_roles_amount = {}   # {original roles amount}
		self._reveal = True                # reveal

		self._guild = None                 # Guild object
		self._channel = None               # Channel object

		self._num_wolf_kills = None        # number of wolf kills


		self.send = self._channel.send     # messageable shortcut

	@property
	def phase(self):
		return self._phase

	@property
	def in_session(self):
		return self._in_session
	
	
	@property
	def players(self):
		return self._players

	# @property
	# def events(self):
	# 	return self._events
	

	@property
	def day(self):
		return self._daynight

	@property
	def night(self):
		return not self._daynight

	def set_day(self):
		self._daynight = True

	def set_night(self):
		self._daynight = False
	
	@property
	def day_start(self):
		return self._day_start

	@property
	def night_start(self):
		return self._night_start
	
	@property
	def day_elapsed(self):
		return self._day_elapsed

	@property
	def night_elapsed(self):
		return self._night_elapsed
	
	@property
	def latest_day_elapsed(self):
		return self._latest_day_elapsed
	
	@property
	def latest_night_elapsed(self):
		return self._latest_night_elapsed
	
	@property
	def daycount(self):
		return self._daycount
	
	@property
	def nightcount(self):
		return self._nightcount


	@property
	def firstjoin(self):
		return self._firstjoin
	
	@property
	def gamemode(self):
		return self._gamemode
	
	@property
	def originalrolesamount(self):
		return self._originalrolesamount

	@property
	def reveal(self):
		return self._reveal
	

	@property
	def guild(self):
		return self._guild
	
	@property
	def channel(self):
		return self._channel
	
	@property
	def num_wolf_kills(self):
		return self._num_wolf_kills
	