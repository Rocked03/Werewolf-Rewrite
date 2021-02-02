from .role import Role


class VillageTeam(Role):
    def __init__(self, player):
    	super().__init__(player)
    	
    	self._team = 'village'