from .role import Role
from .villageteam import VillageTeam


class Villager(VillageTeam):
    role = 'villager'
    commands = []

    def __init__(self, player):
        super().__init__(player)