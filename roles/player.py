from .roles import Role


class Player:
    def __init__(self, _id, user, *, real=True):
        self.id = _id  # player id
        self._user = user
        self._name = None
        self._nickname = None
        self._discriminator = None

        self._role = None

        self._real = real

        send = self._user.send
        mention = self._user.mention

        class Voting:
            start = False
            gamemode = None
            reveal = None
        self._vote = Voting


    def __str__(self):
        if self._nickname: return self._nickname
        elif self._name: return self._name
        else: return self.id

    @property
    def name(self):
        return self._name or self._user.name or f'player with id {self.id}'

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def nickname(self):
        return self._nickname or self.name or self._user.display_name

    @nickname.setter
    def nickname(self, value):
        self._nickname = value

    @property
    def discriminator(self):
        return self._discriminator or self._user.discriminator or '0000'

    @discriminator.setter
    def discriminator(self, value):
        self._discriminator = value

    @property
    def role(self):
        return self._role
    
    @role.setter
    def role(self, value):
        return self._role

    @property
    def real(self):
        return self._real
    
    @property
    def vote(self):
        return self._vote
    
    @vote.setter
    def vote(self, value):
        self._vote = value