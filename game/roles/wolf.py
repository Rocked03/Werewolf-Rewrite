from .role import Role
from .wolfteam import WolfTeam


class Wolf(WolfTeam):
    role = 'wolf'
    commands = ['kill']

    def __init__(self, player):
        super().__init__(player)

        self._targets = []

    def sunset_reset(self):
        self._targets = []


    @property
    def targets(self):
        return self._targets
    