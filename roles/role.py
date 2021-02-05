class Role:
    def __init__(self, player):
        self._player = player
        self.send = self._player.send
        self.mention = self._player.mention
        self.id = self._player.id

        self._role = None
        self._description = None
        self._team = None

        self._death_role = None
        self._seen_role = None

        self._template = Template()
        self._totems = Totems()
        self._items = Items()
        self._alive = True
        self._vote = None
        self._revealed = False

    @property
    def player(self):
        return self._player
    

    @property
    def role(self):
        return self._role

    @property
    def plural(self):
        return self._plural
    
    @property
    def description(self):
        return self._description

    @property
    def team(self):
        return self._team
    

    @property
    def death_role(self):
        return self._death_role

    @property
    def seen_role(self):
        if self._template.cursed:
            return self._template.seen['cursed']
        return self._seen_role


    @property
    def template(self):
        return self._template

    @property
    def totems(self):
        return self._totems

    @property
    def alive(self):
        return self._alive

    @property
    def vote(self):
        return self._vote

    @property
    def revealed(self):
        return self._revealed
    
    


    def __eq__(self, other):
        if isinstance(other, Role) or issubclass(other, Role):
            return self.name == other.name
        elif isinstance(other, str):
            return self.name == other
        return False


class Template:
    cursed = False
    target = {
        
    }
    seen = {
        'cursed': 'wolf'
    }

class Totems:
    placeholder = 0

class Items:
    placeholder = 0