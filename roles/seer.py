from role import Role
from villageteam import VillageTeam


class Seer(VillageTeam):
    role = 'seer'
    commands = ['see']

    def __init__(self, player):
        super().__init__(player)

        self._target = None


    def night_check(self):
        return self._target is not None


    @property
    def targets(self):
    	return self._targets