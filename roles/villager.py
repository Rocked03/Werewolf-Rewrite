from .role import Role
from .villageteam import VillageTeam


class Villager(VillageTeam):
	def __init__(self, player):
		super().__init__(player)

		self._role = 'villager'
		self._death_role = 'villager'
		self._seen_role = 'villager'
		self._description = 'TBA'


	def night_check(self):
		return True