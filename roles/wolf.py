from .role import Role
from .wolfteam import WolfTeam


class Wolf(WolfTeam):
	def __init__(self, player):
		super().__init__(player)

		self._role = 'wolf'
		self._death_role = 'wolf'
		self._seen_role = 'wolf'
		self._description = 'TBA'

		self._targets = []


	def night_check(self):
		return True


	@property
	def targets(self):
		return self._targets
	