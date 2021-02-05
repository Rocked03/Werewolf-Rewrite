from collections import OrderedDict
from datetime import datetime, timedelta

class Session:
    def __init__(self, _channel):
        self.id = _channel.id

        self.guild = _channel.guild        # Guild object
        self.channel = _channel            # Channel object

        self._phase = None                 # phase?
        self._in_session = False           # playing?
        self._players = []                 # players list
        self._preplayers = []              # players list pregame
        self.player_ids = []               # player ids

        self._daynight = False             # is day?
        self._day_start = None             # day time start
        self._night_start = None           # night time start
        self._day_elapsed = timedelta(0)   # day time elapsed
        self._night_elapsed = timedelta(0) # night time elapsed
        self._latest_day_elapsed = None    # latest day elapsed
        self._latest_night_elapsed = None  # latest day elapsed
        self._day_count = 0                # day count
        self._night_count = 0              # night count

        self._first_join = None            # first join time
        self._gamemode = None              # gamemode
        self._original_roles_amount = {}   # {original roles amount}
        self._reveal = None                # reveal

        self._num_wolf_kills = None        # number of wolf kills
        self._num_kills = None             # number of kills available


        self.send = self.channel.send      # messageable shortcut
        self.mention = self.channel.mention #channel mention

    @property
    def phase(self):
        return self._phase

    @phase.setter
    def phase(self, value):
        self._phase = value

    @property
    def in_session(self):
        return self._in_session

    @in_session.setter
    def in_session(self, value):
        self._in_session = value
    
    
    @property
    def players(self):
        return self._players

    @property
    def preplayers(self):
        return self._preplayers

    @property
    def player_count(self):
        return len(self._players) if self._players else len(self._preplayers) 
    
    
    

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

    @day_start.setter
    def day_start(self, value):
        self._day_start = value

    @property
    def night_start(self):
        return self._night_start

    @night_start.setter
    def night_start(self, value):
        self._night_start = value
    
    @property
    def day_elapsed(self):
        return self._day_elapsed

    @day_elapsed.setter
    def day_elapsed(self, value):
        self._day_elapsed += value

    @property
    def night_elapsed(self):
        return self._night_elapsed

    @night_elapsed.setter
    def dnight_elapsed(self, value):
        self._night_elapsed += value

    @property
    def latest_day_elapsed(self):
        return self._latest_day_elapsed

    @latest_day_elapsed.setter
    def latest_day_elapsed(self, value):
        self._latest_day_elapsed = value
    
    @property
    def latest_night_elapsed(self):
        return self._latest_night_elapsed

    @latest_night_elapsed.setter
    def latest_night_elapsed(self, value):
        self._latest_night_elapsed = value
    
    @property
    def day_count(self):
        return self._day_count

    @day_count.setter
    def day_count(self, value):
        self._day_count += value
    
    @property
    def night_count(self):
        return self._night_count

    @night_count.setter
    def night_count(self, value):
        self._night_count += value


    @property
    def firstjoin(self):
        return self._firstjoin

    @firstjoin.setter
    def firstjoin(self, value):
        self._firstjoin = value
    
    @property
    def gamemode(self):
        return self._gamemode

    @gamemode.setter
    def gamemode(self, value):
        self._gamemode = value
    
    @property
    def originalrolesamount(self):
        return self._originalrolesamount

    @originalrolesamount.setter
    def originalrolesamount(self, value):
        self._originalrolesamount = value

    @property
    def reveal(self):
        return self._reveal
    

    @property
    def num_wolf_kills(self):
        return self._num_wolf_kills
    
    @num_wolf_kills.setter
    def num_wolf_kills(self, value):
        self._num_wolf_kills = value

    @property
    def num_kills(self):
        return self._num_kills
    
    @num_kills.setter
    def num_kills(self, value):
        self._num_kills = value    
