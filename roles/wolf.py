from role import Role
from wolfteam import WolfTeam


class Wolf(WolfTeam):
	role = 'wolf'
	description = 'TBA'
	commands = ['kill']

	def __init__(self, player):
		super().__init__(player)

		self._targets = []


	def night_check(self):
		return True


	@property
	def targets(self):
		return self._targets
	