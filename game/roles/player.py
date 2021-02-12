from .role import Role


class Player:
    def __init__(self, _id, _user, *, real=True):
        self.id = _id  # player id
        self.user = _user
        self._name = None
        self._nickname = None
        self._discriminator = None

        self._role = None

        self._real = real

        self.send = self.user.send
        self.mention = self.user.mention

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
        return self._name or self.user.name or f'player with id {self.id}'

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def nickname(self):
        return self._nickname or self.name or self.user.display_name

    @nickname.setter
    def nickname(self, value):
        self._nickname = value

    @property
    def discriminator(self):
        return self._discriminator or self.user.discriminator or '0000'

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


class Bot:
    def __init__(self, _id, _name, _discriminator, _channel):
        self.id = _id
        self.name = _name
        self.display_name = _name
        self.discriminator = _discriminator

        self.mention = f"@{_name}#{_discriminator}\\🤖"

        self.channel = _channel

    async def send(self, *args, **kwargs):
        args = list(args)
        args[0] = f"**[DM to {self.name}]**: {args[0]}"
        await self.channel.send(*args, **kwargs)