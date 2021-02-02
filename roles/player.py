from .roles import Role


class Player:
    def __init__(self, bot, _id, role, user=None, real=True):
        self.bot = bot
        self.id = _id  # player id
        self._user = user
        self._roles = [role]  # list of all roles player had
        self._role = None
        self._name = None
        self._nickname = None
        self._discriminator = None

        self._real = real

        send = self._user.send
        mention = self._user.mention

    def __str__(self):
        if self._nickname: return self._nickname
        elif self._name: return self._name
        else: return self.id

    @property
    def role(self):
        return self._roles[-1]  # most recent role

    @property
    def orig_role(self):
        return self._roles[0]  # original role

    @property
    def name(self):
        return self._name or f'player with id {self.id}'

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def nickname(self):
        return self._nickname or self.name

    @nickname.setter
    def nickname(self, value):
        self._nickname = value

    @property
    def discriminator(self):
        return self._discriminator or '0000'

    @discriminator.setter
    def discriminator(self, value):
        self._discriminator = value

    @property
    def real(self):
        return self._real
    
