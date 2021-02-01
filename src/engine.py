import discord, random
from collections import OrderedDict
from datetime import datetime, timedelta
from discord.ext import commands
from enum import auto, Enum

from session import Session


class GameState(Enum):
    INIT = 'init'
    LOBBY = 'lobby'
    GAME_SETUP = 'game_setup'
    SUNSET = 'sunset'  # day -> sunset transition
    SUNSET2 = 'sunset2' # sunset -> night transition
    NIGHT = 'night'
    SUNRISE = 'sunrise'  # night -> day transition
    DAY = 'day'
    GAME_TEARDOWN = 'game_teardown'

class EventType(Enum):
    LOBBY_JOIN = 'lobby_join'
    LOBBY_LEAVE = 'lobby_leave'
    GAME_SETUP = 'game_setup'
    GAME_TEARDOWN = 'game_teardown'
    SUNSET_TRANSITION = 'sunset_transition'
    SUNRISE_TRANSITION = 'sunrise_transition'
    PLAYER_DEATH = 'player_death'
    PLAYER_IDLE = 'player_idle'
    PLAYER_LYNCH = 'player_lynch'  # not to be confused by PLAYER_DEATH with DeathType LYNCH
    PLAYER_ABSTAIN = 'player_abstain'


class GameEngine:
    def __init__(self, bot):
        self.bot = bot

        self.setup()
        self.declaration()

        self.lang = self.load_language(LANGUAGE)

    def setup(self):
        self.sessions = {}

    def declaration(self):
        self.dwarn = DAY_WARNING
        self.dtmout = DAY_TIMEOUT
        self.nwarn = NIGHT_WARNING
        self.ntmout = NIGHT_TIMEOUT

        self.COMMANDS_FOR_ROLE = {
            'see' : ['seer'],
            'kill' : ['wolf'],
        }


    def load_language(self, language):
        file = 'lang/{}.json'.format(language)
        if not os.path.isfile(file):
            file = 'lang/en.json'
            print("Could not find language file {}.json, fallback on en.json".format(language))
        with open(file, 'r', encoding='utf-8') as f:
            return json.load(f)


    def sessionsetup(self):
        session = Session()
        session.phase = GameState.LOBBY
        # session.gamemode = 'default'

        return session

    def teardown(self, session):  # can probably just make a new session?
        session.phase = GameState.GAME_TEARDOWN
        # queue removing player roles
        session.players.clear()
        session.nights = 0
        session.days = 0
        # wait for player roles to be removed
        session.phase = GameState.LOBBY

        return session

    # def start(self):
    #     self.phase = GameState.GAME_SETUP
    #     self.dispatch(EventType.GAME_SETUP, {'gamemode': self.gamemode})

    # def add_event_listener(self, event_type: EventType, callback):
    #     # callback takes arguments (engine, event_type, data?)
    #     try:
    #         self.events[event_type].append(callback)
    #     except KeyError:
    #         self.events[event_type] = [callback]

    # def dispatch(self, event_type, data):
    #     for callback in self.events[event_type]:
    #         callback(self, event_type, data)


    def win_condition(self, session):
        return False


    async def game_loop(self, session=None):
        # PRE-GAME
        # send welcome

        # GAME START
        while session.in_session and not self.win_condition(session):
            elif session.phase == GameState.SUNSET:
                session = await self.sunset(session, 'post-day')
                session.phase = GameState.SUNSET

            elif session.phase == GameState.SUNSET2: # NIGHT
                # SUNSET
                session.phase = GameState.SUNSET
                session = await self.sunset(session, 'pre-night')

                # NIGHT
                session.phase = GameState.NIGHT
                session = await self.night(session)

                session.phase = GameState.SUNRISE

            elif session.phase == GameState.SUNRISE: # SUNRISE
                session = await self.sunrise(session)
                session.phase = GameState.DAY

            elif session.phase == GameState.DAY: # DAY
                session = await self.day(session)
                session.phase = GameState.SUNSET

        # GAME END
        if session.in_session:
            win_team, win_lore, winners = self.win_condition(session)
            end_stats = self.end_game_stats(session)

            session = await self.end_game(
                win_team=win_team,
                reason=win_lore,
                winners=winners,
                end_stats=end_stats
            )


    async def sunset(self, session, when):
        if when == 'pre-night':
            session.night_start = datetime.utcnow()

            session.num_kills = 1
            for player in session.players: # Totems
                pass

        elif when == 'post-day':
            session.set_night()
            session.day_count += 1

            if self.in_session(session):
                await session.send(self.lg('day_summary', time=self.timedeltatostr(session.latest_day_elapsed)))

                # entranced stuff

                for player in session.players:
                    # clear totems

                    player.vote = None

                    # amnesia stuff

                    session = self.playerupdate(session, player)

        # traitor!


        return session

    async def night(self, session):
        session.night_start = datetime.utcnow()
        session.send(self.lg('now_nighttime'))

        warn = False
        # NIGHT LOOP
        while self.win_condition(session) is None and session.night and session.in_session
            session = await self.night_loop(session, warn)
            await asyncio.sleep(0.1)

        session.latest_night_elapsed = datetime.utcnow() - session.night_start
        session.night_elapsed += session.latest_night_elapsed

        session.day_start = datetime.utcnow()

        return session

    async def night_loop(self, session, warn):
        wolf_kill_dict = {}
        num_wolves = 0

        end_night = True
        for player in session.players:
            if player.alive:
                action = player.night_check
                if player.team == 'wolf' and player.role in self.COMMANDS_FOR_ROLE['kill']:
                    num_wolves += 1
                    num_kills = 1
                    for t in player.targets:
                        try:
                            wolf_kill_dict[t] += 1
                        except KeyError:
                            wolf_kill_dict[t] = 1

                else:
                    end_night = end_night and player.role.nightcheck()

        if num_wolves > 0:
            end_night = end_night and len(wolf_kill_dict) == num_kills and not any([t != num_wolves for t in wolf_kill_dict.values()])

        end_night = end_night or (datetime.utcnow() - session.night_start).total_seconds() > self.ntmout

        if not warn and (datetime.utcnow() - session.night_start).total_seconds() > self.nwarn:
            warn = True
            await session.send(self.lg('almost_day'))

        if end_night:
            session.set_day()
            session.day_start = datetime.utcnow()

        session.num_wolf_kills = num_kills

        return session

    async def sunrise(self, session):
        session.night_count += 1

        killed_msg = []
        killed_dict = {p: 0 for p in session.players}
        # for player in session[1]:
        #     if "blessed" in get_role(player, 'templates'):
        #         killed_dict[player] = -1
        #     else:
        #         killed_dict[player] = 0
        killed_players = []
        alive_players = [x for x in session.players if x.alive] #or (x.role == "vengeful ghost" and [a for a in session[1][x][4] if a.startswith("vengeance:")]))]
        
        if session.in_session: # Totems
            for player in alive_players:
                pass


        wolf_deaths = self.wolf_kill(session, alive_players)
        

        for player, v in killed_dict.items():
            if v > 0: killed_players.append(player)

        killed_players = self.sort_players(killed_players)

        killed_temp = killed_players[:]


        if len(killed_players) == 0:
            if True: # stuff
                killed_msg.append(self.lg('no_kills'))
        else:
            l = len(killed_players)
            dead_bodies = [f"**{self.get_name(p)}**{f", a **{self.lgr(p.role)}**" if session.reveal}" for p in killed_players]  # may need lang fix
            killed_msg.append(self.lg("dead_body", 
                pl=self.pl(l),
                listing={self.listing(dead_bodies, session.reveal)}
            ))

        if session.in_session and not self.win_condition(session):
            await session.send(
                self.lg('night_summary', time=self.timedeltatostr(session.latest_night_elapsed), self.lgr('villager', 'pl'))
                + f'\n\n{'\n'join(killed_msg)}'
            )

            for player in session.players: # more totem stuff - 'angry'
                pass

        killed_dict = {}
        for player in killed_temp:
            kill_team = "wolf" if player not in [] and (player in wolf_deaths) else "village"
            killed_dict[player] = ("night kill", kill_team)


        for player in session.players:
            player.vote = None

        # traitor!!!!

        return session

    async def day(self, session):
        session.day_start = datetime.utcnow()

        if session.in_session and not self.win_condition(session):
            for player in session.players: # more totem stuff
                pass

            await session.send(self.lg('now_daytime', prefix=BOT_PREFIX))

        for player in session.players: # blindness, illness, doomsayer
            pass

        lynched_player = None
        warn = False

        # DAY LOOP
        while self.in_session(session) and not lynched_player and session.day:
            session, lynched_player, totem_dict, warn = await day_loop(session, lynched_player, warn)
            await asyncio.sleep(0.1)

        if not lynched_player and self.in_session(session):
            vote_dict = self.get_votes(session)
            max_votes = max(vote_dict.values())
            max_voted = [p for p, c in vote_dict if c == max_votes and c != 'abstain']

            if len(max_voted) == 1:
                lynched_player = max_voted[0]

        if session.in_session:
            session.night_start = datetime.utcnow()
            session.latest_day_elapsed = datetime.utcnow() - session.day_start
            session.day_elapsed += session.latest_day_elapsed


        lynched_msg = []

        if lynched_player and self.in_session(session):
            if lynched_player == 'abstain':
                for player in [x for x in totem_dict if x.alive and totem_dict[x] < 0]:
                    lynched_msg.append(self.lg('meekly_vote', voter=self.get_name(player)))
                lynched_msg.append(self.lg('abstain'))

                session.send('\n'.join(lynched_msg))

            else:
                lynched_name = self.get_name(lynched_player)

                for player in [x for x in totem_dict if x.alive and totem_dict[x] > 0 and x != lynched_player]:
                    lynched_msg.append(self.lg('impatient_vote',
                        voter=self.get_name(player),
                        votee=lynched_name
                    ))

                if lynched_player in session.players:
                    # if lynched_player.totems.revealing:
                    #     pass # revealing totem stuff

                    # if lynched_player.template.mayor and not lynched_player.revealed:
                    #     pass # mayor stuff

                    if False: pass
                    else:
                        # if lynched_player.totems.luck:
                        #     lynched_player = self.misdirect(session, lynched_player)

                        if session.reveal:
                            lynched_msg.append(self.lg('lynched',
                                lynched=lynched_name,
                                role=lynched_player.role
                            ))
                        else:
                            lynched_msg.append(self.lg('lynched_no_reveal',
                                lynched=lynched_name
                            ))

                        session.send('\n'.join(lynched_msg))

                        # if lynched_player.role == 'jester':
                        #     lynched_player.lynched = True

                        # for player in [x for x in session.players if x.alive]:
                        #     if player.role == 'executioner' and not player.win:
                        #         if player.target == lynched_player:
                        #             player.template.win = True
                        #             session = self.playerupdate(session, player)
                        #             await player.send("ergoaheolgrhaui you win")


                    lynchers_team = [x.team for x in session.players if x.alive and x.vote == lynched_player]
                    session = player_death(lynched_player, 'lynch', 'wolf' if lynchers_team.count('wolf') > lynchers_team.count('village') else 'village')

                # fool stuff

        elif not lynched_player and self.in_session(session):
            session.send(self.lg('not_enough_votes'))


        return session

    async def day_loop(self, session, lynched_player, warn):
        vote_dict, totem_dict, able_players = self.get_votes(session)

        if vote_dict[abstain] >= len(able_players) / 2:  # even split or majority
            lynched_player = 'abstain'

        max_votes = max(vote_dict.values())
        if max_votes >= len(able_players) // 2 + 1:  # majority
            max_voted = [p for p, c in vote_dict if c == max_votes]
            lynched_player = random.choice(max_voted)

        if (datetime.utcnow() - session.day_start).total_seconds() > self.dtmout:
            session.night_start = datetime.utcnow()
            session.set_night()

        if not warn and (datetime.utcnow() - session.day_start).total_seconds() > self.dwarn:
            warn = True
            session.send(self.lg('almost_night'))


        return session, lynched_player, totem_dict, warn

    async def end_game(self, *, win_team=None, reason=None, winners=None, end_stats=None):
        if not session.in_session: return

        session.in_session = False

        if session.day:
            if session.day_start:
                session.day_elapsed += datetime.utcnow() - session.day_start
        else:
            if session.night_start:
                session.night_elapsed += datetime.utcnow() - session.night_start

        msg = [self.lg('end_game',
            mentions = ' '.join(self.sort_players([x.mention for x in session.players])),
            night_length = self.timedeltatostr(session.night_elapsed),
            day_length = self.timedeltatostr(session.day_elapsed),
            game_length = self.timedeltatostr(session.day_elapsed + session.night_elapsed)
        )]

        if winners:
            # crazed shaman stuff

            winners = self.sort_players(list(set(winners)))
            if len(winners) == 0:
                msg.append(self.lg('end_game_no_winners'))
            else:
                msg.append(self.lg('end_game_winners',
                    pl=self.pl(len(winners)),
                    listing=self.listing(winners)
                ))

        else:
            msg.append(self.lg('end_game_no_winners'))

        await session.send('\n'.join(msg))

        for player in session.players:
            await self.player_death(session, player, 'game end', 'bot')


        # unlock lobby

        return session


    def win_condition(self, session):
        teams = {'village' : 0, 'wolf' : 0, 'neutral' : 0}

        # injured stuff

        for player in session.players:
            if player.alive:
                teams[player.role] += 1

        winners = []
        win_team = None
        win_lore = ''
        win_msg = ''

        # lovers stuff

        # Nobody wins
        if not len([x for x in session.players if x.alive]):
            win_team = 'no win'
            win_lore = self.lg('no_win')

        # Wolves win
        elif teams['village'] + teams['neutral'] <= teams['wolf']:
            win_team = 'wolf'
            win_lore = self.lg('wolf_win')

        # Village wins
        elif len([y for y in [x for x in session.players if x.alive and x.team == 'wolf'] if y.role.actual_wolf]) == 0:  # or y.role == 'traitor'
            win_team = 'village'
            win_lore = self.lg('village_win')

        else: return None

        for player in session.players:
            # lover
            # piper
            # succubus/entranced

            if player.team == win_team:
                winners.append(player)

            # vengeful ghost
            # amnesiac
            # jester
            # monster
            # clone
            # lycan
            # turncoat
            # serial killer
            # executioner
            # more succubi/entranced stuff

        return win_team, win_lore, winners

    def end_game_stats(self, session):
        role_msg = ''
        role_dict = {}
        for player in session.players:
            role_dict[player.role] = []

        for player in session.players:
            role_dict[player.role].append(player)

        for key in self.sort_roles(role_dict):
            value = self.sort_players(role_dict[key])

            if len(value) == 0:
                pass

            role_msg += self.lg('end_role_reveal', 
                role=key,
                pl=self.pl(len(value)),
                listing=self.listing([f"**{self.get_name(x)}**" for x in value])
            )

        # lover stuff

        return role_msg


    def playerupdate(self, session, player):
        sp = session.players
        sp[sp.index([x for x in sp if x.id == player.id][0])] = player
        return session

    def player_death(self, session, player, reason, kill_team):
        ingame = 'IN GAME'
        if session.in_session and reason != 'game cancel':
            player.alive = False

            # lover stuff
            # assassin stuff

            if session.in_session:
                # more assassin stuff
                # more lover stuff

                # mad scientist stuff

                # desperation totem stuff

                # clone stuff
                # succubus stuff
                # vengeful ghost stuff
                # piper stuff
                # executioner stuff
                # time lord stuff

                pass

        else:
            ingame = 'NOT IN GAME'
            # remove from list?

        # REMOVE PLAYER ROLE

        if session.in_session and kill_team != 'bot':
            # wolf cub stuff
            pass

        # more assassin stuff

        session = self.playerupdate(session, player)
        return session

    def wolf_kill(self, session, alive_players):
        wolf_votes = {}
        wolf_killed = []
        wolf_deaths = []

        for player in alive_players:
            if player.team == 'wolf' and player.role in self.COMMANDS_FOR_ROLE['kill']:
                for t in player.targets
                    if t in wolf_votes:
                        wolf_votes[t] += 1
                    elif t:
                        wolf_votes[t] = 1

        if wolf_votes:
            sorted_votes = sorted(wolf_votes, key=lambda x: wolf_votes[x], reverse=True)
            wolf_killed = sort_players(sorted_votes[:session.num_wolf_kills])
            for k in wolf_killed:
                if False: pass # harlot, moster, serial killer, etc
                else:
                    killed_dict[k] += 1
                    wolf_deaths.append(k)

        return wolf_deaths

    def get_votes(self, session):
        totem_dict = {}
        # for player in session.players:
        #     totem_dict[player] = player.totems.impatience - player.totems.pacifism

        voteable_players = [x for x in session.players if x.alive]
        # able_players = [x for x in voteable_players if 'injured' not in x.template]
        able_players = [x for x in voteable_players if True]

        vote_dict = {'abstain' : 0}
        for player in voteable_players:
            vote_dict[player] = 0

        able_voters = [x for x in able_players if totem_dict[x] == 0]

        for player in able_voters:
            if player.vote in vote_dict:
                # count = 2 if player.totems.influence and player.vote != 'abstain' else 1
                count = 1
                vote_dict[player.vote] += count

        # for player in [x for x in able_players if totem_dict[x] != 0]:
        #     if totem_dict[player] < 0:
        #         vote_dict['abstain'] += 1
        #     else:
        #         for p in [x for x in voteable_players if x != player]:
        #             vote_dict[p] += 1

        return vote_dict, totem_dict, able_players

    def misdirect(self, session, player):
        alive_players = [x for x in session.players if x.alive]
        index = alive_players.index(player)
        return random.choice([alive_players[self.listloop(index - 1)], alive_players[self.listloop(index + 1)]])

    in_session = lambda s: return s.in_session and self.win_condition(s)

    listloop = lambda x, n: return n + x if x < 0 else n - x if x > n else x

    def lg(self, key, *, args, **kwargs): # phrase translator
        ref = self.lang
        choices = ref['phrases'][key]
        text = random.choice(choices)


        kwargs['villagers'] = self.lgr('villager', 'pl')
        kwargs['wolves'] = self.lgr('wolf', 'pl')

        text = text.format(*args, **kwargs)

        for role in ref['roles'].values():
            for indicator, word in role.items():
                text.replace(f'<{role['sg']}|{indicator}>', word)

        for sg, pl in ref['plurals']:
            text.replace(f'<{sg}|sg>', sg)
            text.replace(f'<{sg}|pl>', pl)

        return text

    def lgr(self, role, pl='sg'): # role translator
        return self.lang['roles'][role][pl]

    def lgt(self, team): # team translator
        return self.lang['teams'][team]


    pl = lambda x: 'sg' if n == 1 else 'pl'

    listing = lambda x, c=False: ' and '.join([y for y in [', '.join(x[:-1]) + (',' if len(x[:-1]) > 1 else '')] + [x[-1]] if y]) + ',' if c else ''

    def timedeltatostr(self, x):
        return "{0:02d}:{1:02d}".format(x.seconds // 60, x.seconds % 60)

    def sort_roles(role_list):
        # VILLAGE_ROLES_ORDERED = ['seer', 'oracle', 'shaman', 'harlot', 'hunter', 'augur', 'detective', 'matchmaker', 'guardian angel', 'bodyguard', 'priest', 'village drunk', 'mystic', 'mad scientist', 'time lord', 'villager']
        # WOLF_ROLES_ORDERED = ['wolf', 'werecrow', 'doomsayer', 'wolf cub', 'werekitten', 'wolf shaman', 'wolf mystic', 'traitor', 'hag', 'sorcerer', 'warlock', 'minion', 'cultist']
        # NEUTRAL_ROLES_ORDERED = ['jester', 'crazed shaman', 'monster', 'piper', 'amnesiac', 'fool', 'vengeful ghost', 'succubus', 'clone', 'lycan', 'turncoat', 'serial killer', 'executioner', 'hot potato']
        # TEMPLATES_ORDERED = ['cursed villager', 'blessed villager', 'gunner', 'sharpshooter', 'mayor', 'assassin', 'bishop']

        VILLAGE_ROLES_ORDERED = ['seer', 'villager']
        WOLF_ROLES_ORDERED = ['wolf']
        NEUTRAL_ROLES_ORDERED = []
        TEMPLATES_ORDERED = []

        role_list = list(role_list)
        result = []
        for role in WOLF_ROLES_ORDERED + VILLAGE_ROLES_ORDERED + NEUTRAL_ROLES_ORDERED + TEMPLATES_ORDERED:
            result += [role] * role_list.count(role)
        return result

    def sort_players(self, players):
        real = []
        fake = []
        for player in players:
            if player.player.real: real.append(player)
            else: fake.append(player)
        return sorted(real, key=get_name) + sorted(fake, key=lambda x: x.id)

    def get_name(self, player):
        member = player.player.user
        if member: return str(member.display_name)
        else: return str(player)


class WinState(Enum):
    NO_WIN = auto()
    VILLAGE_WIN = auto()
    WOLF_WIN = auto()


class DeathType(Enum):
    WOLF_KILL = auto()
    LYNCH = auto()
    IDLE = auto()
