from .role import Role


class WolfTeam(Role):
    def __init__(self, player):
    	super().__init__(player)
    	
    	self._team = 'wolf'

    	self._actual_wolf = False


    @property
    def actual_wolf(self):
    	return self._actual_wolf
    
