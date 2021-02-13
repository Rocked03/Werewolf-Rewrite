class Role:
    role = None
    team = None

    _death_role = None
    _seen_role = None

    def __init__(self, _player):
        self.player = _player
        self.send = self.player.send
        self.mention = self.player.mention
        self.id = self.player.id
        self.name = self.player.name
        self.nickname = self.player.nickname
        self.discriminator = self.player.discriminator

        self.template = Template()
        self.totems = Totems()
        self.items = Items()
        self.alive = True
        self.vote = None

        self._targets = None
    
    @property
    def death_role(self):
        return self._death_role or self.role

    @property
    def seen_role(self):
        if self.template.cursed:
            return self.template.seen['cursed']
        return self._seen_role or self.role


    # def __eq__(self, other):
    #     if isinstance(other, Role) or issubclass(other, Role):
    #         return self.name == other.name
    #     elif isinstance(other, str):
    #         return self.name == other
    #     return False


    def night_check(self):
        return True

    def sunset_reset(self):
        pass


    @property
    def targets(self):
        return self._targets

    @targets.setter
    def targets(self, value):
        self._targets = value



class Template:
    cursed = False
    target = {

    }
    seen = {
        'cursed': 'wolf'
    }
    templates = [
        'cursed'
    ]

class Totems:
    placeholder = 0

class Items:
    placeholder = 0