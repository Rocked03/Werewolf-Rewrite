from collections import OrderedDict
from datetime import datetime, timedelta

class Session:
    def __init__(self, _channel):
        self.id = _channel.id

        self.guild = _channel.guild        # Guild object
        self.channel = _channel            # Channel object

        self.phase = None                  # phase?
        self.in_session = False            # playing?
        self.players = []                  # players list
        self.preplayers = []               # players list pregame
        self.player_ids = []               # player ids

        self._daynight = False             # is day?
        self.day_start = None              # day time start
        self.night_start = None            # night time start
        self._day_elapsed = timedelta(0)   # day time elapsed
        self._night_elapsed = timedelta(0) # night time elapsed
        self.latest_day_elapsed = None     # latest day elapsed
        self.latest_night_elapsed = None   # latest day elapsed
        self._day_count = 0                # day count
        self._night_count = 0              # night count

        self.first_join = None             # first join time
        self.gamemode = None               # gamemode
        self.original_roles_amount = {}    # {original roles amount}
        self.reveal = None                 # reveal

        self.num_wolf_kills = None         # number of wolf kills
        self.num_kills = None              # number of kills available


        self.send = self.channel.send      # messageable shortcut
        self.mention = self.channel.mention #channel mention


    @property
    def player_count(self):
        return len(self.players) if self.players else len(self.preplayers) 
    
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
    def day_elapsed(self):
        return self._day_elapsed

    @day_elapsed.setter
    def day_elapsed(self, value):
        self._day_elapsed += value

    @property
    def night_elapsed(self):
        return self._night_elapsed

    @night_elapsed.setter
    def night_elapsed(self, value):
        self._night_elapsed += value
    
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
