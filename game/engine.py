import asyncio, discord, json, os, random, textwrap, traceback
from collections import OrderedDict
from datetime import datetime, timedelta
from discord.ext import commands
from enum import auto, Enum

from .session import Session
from .roles.roles import roles as roles_list
from .roles.role import Template, Totems
from .stasis import Stasis

from config import *
from settings import *


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


class GameEngine:
    def __init__(self, bot):
        self.bot = bot

        self.setup()
        self.rolelists()
        self.declaration()

        self.stasis = Stasis()

        self.lang = self.load_language(MESSAGE_LANGUAGE)
        self.gamemodes = self.load_gamemodes()

    def setup(self):
        self.roles_list = roles_list

    def declaration(self):
        self.dwarn = DAY_WARNING
        self.dtmout = DAY_TIMEOUT
        self.nwarn = NIGHT_WARNING
        self.ntmout = NIGHT_TIMEOUT

        # self.COMMANDS_FOR_ROLE = {
        #     'see' : ['seer'],
        #     'kill' : ['wolf'],
        # }

    def rolelists(self):
        # VILLAGE_ROLES_ORDERED = ['seer', 'oracle', 'shaman', 'harlot', 'hunter', 'augur', 'detective', 'matchmaker', 'guardian angel', 'bodyguard', 'priest', 'village drunk', 'mystic', 'mad scientist', 'time lord', 'villager']
        # WOLF_ROLES_ORDERED = ['wolf', 'werecrow', 'doomsayer', 'wolf cub', 'werekitten', 'wolf shaman', 'wolf mystic', 'traitor', 'hag', 'sorcerer', 'warlock', 'minion', 'cultist']
        # NEUTRAL_ROLES_ORDERED = ['jester', 'crazed shaman', 'monster', 'piper', 'amnesiac', 'fool', 'vengeful ghost', 'succubus', 'clone', 'lycan', 'turncoat', 'serial killer', 'executioner', 'hot potato']
        # TEMPLATES_ORDERED = ['cursed', 'blessed villager', 'gunner', 'sharpshooter', 'mayor', 'assassin', 'bishop']

        self.templates = Template.templates

        # self.seen_wolf = ['wolf', 'cursed']  # [x for x in self.roles if x._seen_role == 'wolf'] + [n for n, r in Template.seen.items() if r == 'wolf']

    def roles(self, team=None):
        if team is None: return list(self.roles_list.keys())
        elif team == 'wolfchat': return [n for n, o in self.roles_list.items() if o.team == 'wolf' and o.wolfchat]
        else: return [n for n, o in self.roles_list.items() if o.team == team]


    def load_language(self, language):
        file = 'lang/{}.json'.format(language)
        
        if not os.path.isfile(file):
            file = 'lang/en.json'
            print("Could not find language file {}.json, fallback on en.json".format(language))
        
        with open(file, 'r', encoding='utf-8') as f:
            langfile = json.load(f)

        if file != 'lang/en.json':
            with open('lang/en.json', 'r', encoding='utf-8') as f:
                enlang = json.load(f)

            for head in ['phrases', 'teams', 'roles', 'totmems']:
                if head in langfile:
                    for key, value in langfile[head].items():
                        if key not in enlang[head]:
                            print(f'Could not find key "{key}" ({head})')
                            continue
                        enlang[head][key] = value

            for head in ['plurals']:
                if head in langfile:
                    for key, value in langfile[head].items():
                        enlang[head][key] = value

            if 'game_name' in langfile: enlang['game_name'] = langfile['game_name']

            langfile = enlang

        return langfile

    def load_gamemodes(self):
        file = 'game/gamemodes.json'
        with open(file, 'r', encoding='utf-8') as f:
            return json.load(f)


    async def log(self, level, prefix, *args, **kwargs):
        try:
            try: ownermention = self.bot.owner.mention
            except AttributeError: ownermention = "@owner"
            levelmsg = {
                0: '[DEBUG]',
                1: '[INFO]',
                2: '**[WARNING]**',
                3: f'**[ERROR]** {ownermention}'
            }

            msg = ' '.join([str(x) for x in [levelmsg[level], prefix.upper(), ' '.join(args), ' '.join([f"{k.strip('_').replace('_', ' ').upper()}: {v}" for k, v in kwargs.items()])] if x])

            # localfile

            if level >= MIN_LOG_LEVEL:
                width = 2000 - 50
                msglist = [x.replace('%n','\n') for x in textwrap.wrap(msg.replace('\n','%n'), width)]

                logmsg = await self.bot.LOG_CHANNEL.send(msglist[0])

                for x in msglist[1:]:
                    logmsg = await logmsg.reply("[CONTINUED] " + x, mention_author=False)
        except Exception as e:
            traceback.print_exc()

    ulog = lambda self, u: f"<{u.name} | {u.id}>"  # user log format
    slog = lambda self, s: f"s:{s.id}"             # session log format


    def session_setup(self, channel):
        session = Session(channel)
        session.phase = GameState.LOBBY

        self.bot.sessions[session.id] = session
        self.bot.sessiontasks[session.id] = {
            'wait_bucket': None,
            'wait_timer': None,
            'game_start_timeout_loop': None,
            'session_start_loop': None,
            'idle': {}
        }
        self.bot.sessionlock[session.id] = asyncio.Lock()

        return session


    async def lock_lobby(self, session, users=[], role=None, *, bulk=False):
        channel = session.channel

        overwrites = dict()

        for user in users:
            overwrite = await self.player_give_perms(channel, user, role, bulk=True)
            if overwrite: overwrites[user] = overwrite

        default_role = channel.guild.default_role
        default_perms = channel.overwrites_for(default_role)
        default_perms.send_messages = False
        if not bulk:
            overwrites[default_role] = default_perms
            await channel.edit(overwrites=self.update_overwrites(channel.overwrites, overwrites))
        else: return default_perms

    async def unlock_lobby(self, session=None, users=[], role=None, *, bulk=False, channel=None):
        if session is not None:
            channel = session.channel
        
        overwrites = dict()

        for user in users:
            overwrite = await self.player_remove_perms(channel, user, role, bulk=True)
            if overwrite: overwrites[user] = None

        default_role = channel.guild.default_role
        default_perms = channel.overwrites_for(default_role)
        default_perms.send_messages = None
        if not bulk:
            overwrites[default_role] = default_perms
            await channel.edit(overwrites=self.update_overwrites(channel.overwrites, overwrites))
        else: return default_perms

    async def player_give_perms(self, channel, user, role, *, bulk=False):
        if role is not None:
            member = channel.guild.get_member(user.id)
            if member is not None:
                if role not in member.roles:
                    await member.add_roles(role)

        perms = channel.overwrites_for(user)
        perms.read_messages = True
        perms.send_messages = True
        if not bulk: await channel.edit(overwrites=self.update_overwrites(channel.overwrites, {user: perms}))
        else: return perms
        return None

    async def player_remove_perms(self, channel, user, role=None, *, bulk=False):
        if role is not None:
            member = channel.guild.get_member(user.id)
            if member is not None:
                try:
                    await member.remove_roles(role)
                except Exception as e:
                    print(e)
            else: print('huh?')

        perms = channel.overwrites
        if user not in perms: return None
        else:
            if not bulk: await channel.edit(overwrites=self.update_overwrites(perms, {user: None}))
            else: return True
        return None

    def update_overwrites(self, old, new):
        for k, v in new.items():
            if v is not None:
                old[k] = v
            elif k in old: del old[k]
        return old




    async def run_game(self, session):
        session.phase = GameState.GAME_SETUP

        # lobby perms

        session.in_session = True
        session.set_night()

        player_count = session.player_count

        if not session.gamemode:
            vote_dict = dict()
            for player in session.preplayers:
                if player.vote.gamemode in vote_dict.keys():
                    vote_dict[player.vote] += 1
                elif player.vote.gamemode:
                    vote_dict[player.vote] = 1
            topvote = [g for g, n in vote_dict.items() if n >= session.player_count // 2 + 1]
            if topvote: session.gamemode(self.gamemodes[random.choice(topvote)])


        if not session.gamemode:
            gamemode_list = []

            for name, gamemode in self.gamemodes.items():
                chance = gamemode['chance']
                if player_count < gamemode['min_players'] or player_count > gamemode['max_players']:
                    chance = 0
                elif name in vote_dict.keys():
                    chance += int(round((vote_dict[name] / player_count * 200)))
                gamemode_list += [name] * chance

            gamemode = random.choice(gamemode_list)
            if not gamemode: gamemode = 'default'
            session.gamemode = self.gamemodes[gamemode]

        reveal_votes = [player.vote.reveal for player in session.preplayers if player.vote.reveal is not None]
        session.reveal = reveal_votes.count(True) >= reveal_votes.count(False) * 1.00001


        await self.lock_lobby(session, users=[x.user for x in session.preplayers if x.real], role=self.bot.PLAYERS_ROLE)


        session.in_session = True
        session = await self.session_update('push', session)

        if player_count < session.gamemode['min_players'] or player_count > session.gamemode['max_players']:
            session.gamemode = self.gamemodes['default']

        
        async with self.stasis.connection(self.bot.stasis_name) as conn:
            stasisised = await self.stasis.get_all_dict(
                lock=self.bot.stasis_lock, name=self.bot.stasis_name, conn=conn)

            for user, count in stasisised.items():
                await self.stasis.update(user, -1,
                    lock=self.bot.stasis_lock, name=self.bot.stasis_name, conn=conn)


        # await session.send(self.lg('welcome',
        #     listing=' '.join([x.mention for x in self.sort_players(session.preplayers, False)]),
        #     gamemode=session.gamemode['name'],
        #     count=session.player_count,
        #     prefix=BOT_PREFIX
        # ))

        await session.send(
            ' '.join([x.mention for x in self.sort_players(session.preplayers, False)]),
            embed=await self.wwembed(
                title=self.lg('welcome_1'),
                fields={self.lg('welcome_2', gamemode=session.gamemode['name'], count=session.player_count): self.lg('welcome_3')}))

        for i in range(RETRY_RUN_GAME):
            try:
                gamemode = session.gamemode['name']
                session = await self.assign_roles(session, gamemode)
                break
            except Exception as e:
                traceback.print_exc()
                print('----')
                await self.log(2, 'error', self.slog(session), "Role attribution failed with error: {e}. See console for more details.")
                pass
        else:
            msg = await session.send(self.lg('role_attribution_fail',
                listing=' '.join([x.mention for x in self.sort_players(session.preplayers, False)]),
                count=RETRY_RUN_GAME
            ))
            
            session.in_session = False
            session = await self.session_update('push', session, ['in_session'])
            session = self.session_setup(session.channel)
            return

        await self.session_update('push', session)

        for i in range(RETRY_RUN_GAME):
            try:
                if i == 0: session = await self.game_loop(session)
                else: session = await self.game_loop(session, True)
                break
            except Exception as e:
                await session.send(self.lg('game_loop_break',
                    listing=' '.join([x.mention for x in self.sort_players(session.players)]))
                )
                traceback.print_exc()
                print('----')
                await self.log(3, 'error', self.slog(session), f"Game loop broke with error: {e}. See console for more details.")
                error = traceback.format_exc()
                width = 2000 - 50
                for msg in [x.replace('%n','\n') for x in textwrap.wrap(error.replace('\n','%n'), width)]:
                    await self.bot.owner.send(f"```py\n{msg}```")
        else:
            msg = await session.send(self.lg('game_loop_fail',
                listing=' '.join([x.mention for x in self.sort_players(session.players)]),
                count=RETRY_RUN_GAME
            ))
            
            await self.end_game(session=session, reason="The game has been stopped forcefully.", end_stats=self.end_game_stats(session))
            session.in_session = False
            session = await self.session_update('push', session, ['in_session'])
            session = self.session_setup(session.channel)
            return



    async def game_loop(self, session=None, retry=False):
        # PRE-GAME
        await self.log(1, 'game start', self.slog(session))

        # GAME START
        session.phase = GameState.SUNSET2
        session.in_session = True
        while session.in_session and not self.win_condition(session):
            if session.phase == GameState.SUNSET:
                session = await self.sunset(session, 'post-day')
                session.phase = GameState.SUNSET2
                session = await self.session_update('push', session)

            elif session.phase == GameState.SUNSET2: # NIGHT
                # SUNSET
                session.phase = GameState.SUNSET
                session = await self.sunset(session, 'pre-night')
                session = await self.session_update('push', session)

                # NIGHT
                session.phase = GameState.NIGHT
                session = await self.night(session)
                session.phase = GameState.SUNRISE
                session = await self.session_update('push', session)

            elif session.phase == GameState.SUNRISE: # SUNRISE
                session = await self.sunrise(session)
                session.phase = GameState.DAY
                session = await self.session_update('push', session)

            elif session.phase == GameState.DAY: # DAY
                session = await self.day(session)
                session.phase = GameState.SUNSET
                session = await self.session_update('push', session)

        # GAME END
        if session.in_session:
            session = await self.session_update('pull', session)
            win_team, win_lore, winners = self.win_condition(session)
            end_stats = self.end_game_stats(session)

            session = await self.end_game(
                session=session,
                win_team=win_team,
                reason=win_lore,
                winners=winners,
                end_stats=end_stats
            )

        session = self.session_setup(session.channel)


    async def sunset(self, session, when):
        if when == 'pre-night':
            session.set_night()
            session.night_start = datetime.utcnow()
            session.num_kills = 1

            for player in session.players:
                # totems

                role_msg, info_msg, role = self.send_role_info(session, player)
                if role_msg:
                    try:
                        embed = await self.format_role_info(*self.send_role_info(session, player), send_role=session.night_count == 0)
                        if embed.fields: 
                            await player.send(embed=embed)
                    except discord.Forbidden:
                        await session.send(self.lg('role_dm_off', mention=player.mention))

            session = await self.session_update('push', session)

            # log - totem

        elif when == 'post-day':
            session.set_night()
            session.day_count = 1
            session = await self.session_update('push', session)

            if self.in_session(session):
                await session.send(embed=await self.wwembed(user=self.bot.user, title=self.lg('sunset'), description=self.lg('day_summary', time=self.timedelta_to_str(session.latest_day_elapsed))))

                # entranced stuff

                for player in session.players:
                    # clear totems

                    player.vote = None

                    # if player.team == 'wolf' and 'kill' in player.commands:
                    #     player.targets = []
                    player.sunset_reset()

                    # amnesia stuff

                    session = await self.player_update(session, player)

        # traitor!
        return session

    async def night(self, session):
        session.night_start = datetime.utcnow()
        await session.send(embed=await self.wwembed(title=self.lg('now_nighttime'), footer=False))

        warn = False
        end = None
        # NIGHT LOOP
        while self.in_session(session) and session.night:
            session = await self.session_update('pull', session)
            session, warn, end = await self.night_loop(session, warn, end)
            session = await self.session_update('push', session, ['_daynight', 'day_start', 'num_wolf_kills'])
            await asyncio.sleep(0.1)

        session.latest_night_elapsed = datetime.utcnow() - session.night_start
        session.night_elapsed = session.latest_night_elapsed
        session.night_count = 1

        session.day_start = datetime.utcnow()

        return session

    async def night_loop(self, session, warn, end):
        wolf_kill_dict = {}
        num_wolves = 0

        end_night = True
        for player in session.players:
            if player.alive:
                if player.team == 'wolf' and 'kill' in player.commands:
                    num_wolves += 1
                    num_kills = 1
                    for t in player.targets:
                        try:
                            wolf_kill_dict[t] += 1
                        except KeyError:
                            wolf_kill_dict[t] = 1

                end_night = end_night and player.night_check()
        if num_wolves > 0:
            end_night = end_night and len(wolf_kill_dict) == num_kills and not any([t != num_wolves for t in wolf_kill_dict.values()])

        end_night = end_night or (datetime.utcnow() - session.night_start).total_seconds() > self.ntmout

        if not warn and (datetime.utcnow() - session.night_start).total_seconds() > self.nwarn:
            warn = True
            await session.send(self.lg('almost_day'))

        if not end_night:
            end = None

        if end_night:
            if end is None: end = datetime.utcnow() + timedelta(seconds=random.choice(range(NIGHT_BUFFER[0], NIGHT_BUFFER[1] + 1)))
            if datetime.utcnow() >= end:
                session.set_day()
                session.day_start = datetime.utcnow()

        session.num_wolf_kills = num_kills

        return session, warn, end

    async def sunrise(self, session):
        session.night_count = 1

        log_msg = {}

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


        wolf_deaths, killed_dict = self.wolf_kill(session, alive_players, killed_dict)
        

        for player, v in killed_dict.items():
            if v > 0: killed_players.append(player)

        killed_players = self.sort_players(killed_players)

        killed_temp = killed_players[:]

        
        log_msg['deaths_from_wolf'] = ', '.join(self.ulog(x) for x in wolf_deaths)
        log_msg['killed_players'] = ', '.join(self.ulog(x) for x in killed_players)

        log_msg = {k: f"{v}\n" for k, v in log_msg.items()}

        await self.log(1, 'sunrise log', self.slog(session), '\n', **log_msg)

        if len(killed_players) == 0:
            if True: # stuff
                killed_msg.append(self.lg('no_kills'))
        else:
            l = len(killed_players)
            dead_bodies = [f"**{p.mention}**{f', a **{self.lgr(p.death_role)}**' if session.reveal else ''}" for p in killed_players]  # may need lang fix
            killed_msg.append(self.lg("dead_body", 
                pl=self.pl(l),
                listing=self.listing(dead_bodies, session.reveal)
            ))

        if session.in_session and not self.win_condition(session):
            # killed_msg_final = '\n'.join(killed_msg)
            # await session.send(
            #     self.lg('night_summary', time=self.timedelta_to_str(session.latest_night_elapsed))
            #     + f"\n\n{killed_msg_final}"
            # )
            await session.send(embed=await self.wwembed(
                title=self.lg('sunrise'),
                description=self.lg('night_summary', time=self.timedelta_to_str(session.latest_night_elapsed)),
                fields={self.lg('summary'): '\n'.join(killed_msg)}
            ))

            for player in session.players: # more totem stuff - 'angry'
                pass

        for player in killed_temp:
            kill_team = "wolf" if player not in [] and (player in wolf_deaths) else "village"
            session = await self.player_death(session, player, "night kill", kill_team)

        for player in session.players:
            player.vote = None

        # traitor!!!!
        return session

    async def day(self, session):
        session.day_start = datetime.utcnow()

        if session.in_session and not self.win_condition(session):
            for player in session.players: # more totem stuff
                pass

            await session.send(embed=await self.wwembed(title=self.lg('now_daytime'), description=self.lg('now_daytime_2', time=self.timestr_to_text(DISCUSSION_LENGTH)), footer=False))

        for player in session.players: # blindness, illness, doomsayer
            pass

        lynched_player = None
        warn = False
        vote_start = False

        # DAY LOOP
        while self.in_session(session) and not lynched_player and session.day:
            session = await self.session_update('pull', session)
            session, lynched_player, totem_dict, vote_start, warn = await self.day_loop(session, lynched_player, vote_start, warn)
            session = await self.session_update('push', session, ['night_start', '_daynight'])
            await asyncio.sleep(0.1)

        if not lynched_player and self.in_session(session):
            vote_dict, totem_dict, able_players = self.get_votes(session)
            max_votes = max(vote_dict.values())
            max_voted = [p for p, c in vote_dict.items() if c == max_votes and c != 'abstain']

            if len(max_voted) == 1:
                lynched_player = max_voted[0]

        if session.in_session:
            session.night_start = datetime.utcnow()
        session.latest_day_elapsed = datetime.utcnow() - session.day_start
        session.day_elapsed = session.latest_day_elapsed
        session.day_count = 1


        lynched_msg = []

        if lynched_player and self.in_session(session):
            if lynched_player == 'abstain':
                for player in [x for x in totem_dict if x.alive and totem_dict[x] < 0]:
                    lynched_msg.append(self.lg('meekly_vote', voter=self.get_name(player)))
                lynched_msg.append(self.lg('abstain'))

                await session.send(embed=await self.wwembed(description='\n'.join(lynched_msg), footer=False))

            else:
                lynched_player = self.find_player(session, lynched_player)
                lynched_name = self.get_name(lynched_player)

                for player in [x for x in totem_dict if x.alive and totem_dict[x] > 0 and x.id != lynched_player]:
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
                                lynched=lynched_player.mention,
                                role=lynched_player.death_role
                            ))
                        else:
                            lynched_msg.append(self.lg('lynched_no_reveal',
                                lynched=lynched_player.mention
                            ))

                        await session.send(embed=await self.wwembed(description='\n'.join(lynched_msg), footer=False))

                        # if lynched_player.role == 'jester':
                        #     lynched_player.lynched = True

                        # for player in [x for x in session.players if x.alive]:
                        #     if player.role == 'executioner' and not player.win:
                        #         if player.target == lynched_player:
                        #             player.template.win = True
                        #             session = await self.player_update(session, player)
                        #             await player.send("ergoaheolgrhaui you win")


                    lynchers_team = [x.team for x in session.players if x.alive and x.vote == lynched_player]
                    session = await self.player_death(session, lynched_player, 'lynch', 'wolf' if lynchers_team.count('wolf') > lynchers_team.count('village') else 'village')

                # fool stuff

        elif not lynched_player and self.in_session(session):
            await session.send(embed=await self.wwembed(description=self.lg('not_enough_votes'), footer=False))

        return session

    async def day_loop(self, session, lynched_player, vote_start, warn):
        vote_dict, totem_dict, able_players = self.get_votes(session)

        if vote_dict['abstain'] >= len(able_players) / 2:  # even split or majority
            lynched_player = 'abstain'

        max_votes = max(vote_dict.values())
        if max_votes >= len(able_players) // 2 + 1:  # majority
            max_voted = [p for p, c in vote_dict.items() if c == max_votes]
            lynched_player = random.choice(max_voted)

        if (datetime.utcnow() - session.day_start).total_seconds() > self.dtmout:
            session.night_start = datetime.utcnow()
            session.set_night()

        if not vote_start and (datetime.utcnow() - session.day_start).total_seconds() > DISCUSSION_LENGTH:
            vote_start = True
            await session.send(embed=await self.wwembed(description=self.lg('voting_start'), footer=False))

        if not warn and (datetime.utcnow() - session.day_start).total_seconds() > self.dwarn:
            warn = True
            await session.send(embed=await self.wwembed(description=self.lg('almost_night'), footer=False))

        return session, lynched_player, totem_dict, vote_start, warn

    async def end_game(self, *, session, win_team=None, reason=None, winners=[], end_stats=None):
        if not session.in_session: return

        session.in_session = False

        if session.day:
            if session.day_start:
                session.day_elapsed = datetime.utcnow() - session.day_start
        else:
            if session.night_start:
                session.night_elapsed = datetime.utcnow() - session.night_start

        embed = await self.wwembed(
            title=self.lg('end_game_1'),
            description=self.lg('end_game_2',
                night_length = self.timedelta_to_str(session.night_elapsed),
                day_length = self.timedelta_to_str(session.day_elapsed),
                game_length = self.timedelta_to_str(session.day_elapsed + session.night_elapsed)),
            fields={
                self.lg('summary'): reason,
                self.lg('end_game_reveal'): end_stats
            }
        )

        if winners:
            # crazed shaman stuff

            winners = self.sort_players(list(set(winners)))
            if len(winners) == 0:
                embed.add_field(name=self.lg('end_game_result'), value=self.lg('end_game_no_winners'), inline=False)
            else:
                embed.add_field(
                    name=self.lg('results'), 
                    value=self.lg('end_game_winners',
                        s=self.s(len(winners)),
                        pl=self.pl(len(winners)),
                        listing=self.listing([x.mention for x in winners])
                    ), 
                    inline=False)

        else:
            embed.add_field(name=self.lg('results'), value=self.lg('end_game_no_winners'), inline=False)

        await session.send(' '.join([x.mention for x in self.sort_players(session.players)]), embed=embed)
        await self.log(1, 'game win', self.slog(session), winners=' '.join(str(x.id) for x in winners))

        sessionusers = [x.user for x in session.players if x.real]

        for player in session.players:
            session = await self.player_death(session, player, 'game end', 'bot')


        await self.unlock_lobby(session, users=sessionusers, role=self.bot.PLAYERS_ROLE)

        return session


    def win_condition(self, session):
        teams = {'village' : 0, 'wolf' : 0, 'neutral' : 0}

        # injured stuff

        for player in session.players:
            if player.alive:
                teams[player.team] += 1

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
        elif teams['village'] + teams['neutral'] - (1 if session.night else 0) <= teams['wolf']:
            win_team = 'wolf'
            win_lore = self.lg('wolf_win')

        # Village wins
        elif len([y for y in [x for x in session.players if x.alive and x.team == 'wolf'] if y.actual_wolf]) == 0:  # or y.role == 'traitor'
            win_team = 'village'
            win_lore = self.lg('village_win')

        else: return False

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
        role_msg = []
        role_dict = {}
        for player in session.players:
            role_dict[player.role] = []

        for player in session.players:
            role_dict[player.role].append(player)

        for key in self.sort_roles(role_dict):
            value = self.sort_players(role_dict[key])

            if len(value) == 0:
                pass

            role_msg.append(self.lg('end_role_reveal', 
                role=key,
                pl=self.pl(len(value)),
                listing=self.listing([f"**{self.get_name(x)}**" for x in value])
            ))

        # lover stuff

        return '\n'.join(role_msg)



    async def assign_roles(self, session, gamemode):
        massive_role_list = []
        roles_gamemode_template_list = []
        gm = self.gamemodes[gamemode]
        session.players = []
        player_count = session.player_count

        gamemode_roles = self.get_roles(gm, player_count)

        for role, count in gamemode_roles.items():
            if role in self.roles():
                if role in self.templates:
                    roles_gamemode_template_list += [role] * count
                else:
                    massive_role_list += [role] * count

        massive_role_list, debug_message = self.balance_roles(massive_role_list, num_players=session.player_count)
        if debug_message:
            await self.log(2, 'balance roles', self.slog(session), msg=debug_message)

        session.original_roles_amount = gamemode_roles


        random.shuffle(massive_role_list)

        for player in session.preplayers:
            role = massive_role_list.pop()
            player.role = role
            newplayer = self.roles_list[role](player)

            session.players.append(newplayer)

        for i in range(gamemode_roles['cursed'] if 'cursed' in gamemode_roles.keys() else 0):
            cursed_choices = [x for x in session.players if x.role not in self.roles('wolf') + ['seer'] and x.seen_role != 'wolf' and not x.template.cursed]
            if cursed_choices:
                cursed = random.choice(cursed_choices)
                cursed.template.cursed = True
                session = await self.player_update(session, cursed)

        # mayor
        # gunner
        # sharpshooter
        # assassin
        # blessed vill
        # bishop

        return session

    def balance_roles(self, massive_role_list, *, default_role='villager', num_players=-1):
        extra_players = num_players - len(massive_role_list)
        if extra_players > 0:
            massive_role_list += [default_role] * extra_players
            return (massive_role_list, self.lg('not_enough_roles', extra=extra_players, default=default_role))
        elif extra_players < 0:
            random.shuffle(massive_role_list)
            removed_roles = []
            team_roles = [0, 0, 0]
            for role in massive_role_list:
                if role in self.roles('wolf'):
                    team_roles[0] += 1
                elif role in self.roles('village'):
                    team_roles[1] += 1
                elif role in self.roles('neutral'):
                    team_roles[2] += 1
            for i in range(-1 * extra_players):
                team_fractions = list(x / len(massive_role_list) for x in team_roles)
                roles_to_remove = set()
                if team_fractions[0] > 0.35:
                    roles_to_remove |= set(self.roles('wolf'))
                if team_fractions[1] > 0.7:
                    roles_to_remove |= set(self.roles('village'))
                if team_fractions[2] > 0.15:
                    roles_to_remove |= set(self.roles('neutral'))
                if len(roles_to_remove) == 0:
                    roles_to_remove = set(roles)
                    if team_fractions[0] < 0.25:
                        roles_to_remove -= set(self.roles('wolf'))
                    if team_fractions[1] < 0.5:
                        roles_to_remove -= set(self.roles('village'))
                    if team_fractions[2] < 0.05:
                        roles_to_remove -= set(self.roles('neutral'))
                    if len(roles_to_remove) == 0:
                        roles_to_remove = set(roles)
                for role in massive_role_list[:]:
                    if role in roles_to_remove:
                        massive_role_list.remove(role)
                        removed_roles.append(role)
                        break
            return (massive_role_list, self.lg('too_many_roles', roles=', '.join(self.sort_roles(removed_roles))))
        return (massive_role_list, '')

    def get_roles(self, gm, players):
        key = players - gm['min_players']
        if gm['min_players'] <= players <= gm['max_players']:
            gamemode_roles = {r: x[key] for r, x in gm['roles'].items() if x[key] > 0}

        return gamemode_roles



    async def session_update(self, action, session, spec = []):
        async with self.bot.sessionlock[session.id]:
            if action == 'pull':
                return self.bot.sessions[session.id]
            elif action == 'push':
                if not spec:
                    self.bot.sessions[session.id] = session
                else:
                    if not isinstance(spec, list): spec = [spec]
                    for i in spec:
                        setattr(self.bot.sessions[session.id], i, getattr(session, i))

                return self.bot.sessions[session.id]

    async def player_update(self, session, player):
        session = await self.session_update('pull', session)
        sp = session.players
        sp[sp.index([x for x in sp if x.id == player.id][0])] = player
        session = await self.session_update('push', session, ['players'])
        return session

    async def preplayer_update(self, session, player):
        session = await self.session_update('pull', session)
        sp = session.preplayers
        sp[sp.index([x for x in sp if x.id == player.id][0])] = player
        session = await self.session_update('push', session, ['preplayers'])
        return session


 
    async def player_death(self, session, player, reason, kill_team):
        ingame = 'IN GAME'
        if session.in_session and reason != 'game cancel' or reason == 'game end':
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
            session.preplayers.pop(session.preplayers.index(player))
            session.player_ids.pop(session.player_ids.index(player.id))

        if player.real:
            await self.player_remove_perms(session.channel, player.user, self.bot.PLAYERS_ROLE)

        if session.in_session and kill_team != 'bot':
            # wolf cub stuff
            pass

        # more assassin stuff

        await self.log(0, 'player death', self.slog(session), self.ulog(player.user), gamestate=ingame, reason=reason)

        if session.in_session and reason != 'game cancel':
            session = await self.player_update(session, player)
        return session

    def wolf_kill(self, session, alive_players, killed_dict):
        wolf_votes = {}
        wolf_killed = []
        wolf_deaths = []

        for player in alive_players:
            if player.team == 'wolf' and 'kill' in player.commands:
                for t in player.targets:
                    if t in wolf_votes:
                        wolf_votes[t] += 1
                    elif t:
                        wolf_votes[t] = 1

        if wolf_votes:
            sorted_votes = sorted(wolf_votes, key=lambda x: wolf_votes[x], reverse=True)
            wolf_killed = self.sort_players([self.find_player(session, x) for x in sorted_votes[:session.num_wolf_kills]])
            for k in wolf_killed:
                if False: pass # harlot, moster, serial killer, etc
                else:
                    killed_dict[k] += 1
                    wolf_deaths.append(k)

        return wolf_deaths, killed_dict


    def send_role_info(self, session, player):
        if player.alive:
            rolename = player.role if player.role not in [] else 'villager'
            # role = self.roles_list[rolename]
            role = player if player.role not in [] else self.roles_list['villager']
            templates = player.template

            role_msg = self.lgr(rolename, 'desc')

            msg = []
            living_players = [x for x in session.players if x.alive]
            living_players_string = [f"{self.get_name(x)} ({x.id})" for x in living_players]

            if player.team == 'wolf':
                living_players_string = []
                for p in living_players:
                    role_string = []

                    if p.template.cursed:
                        role_string.append(self.lgr('cursed'))
                    if p.team == 'wolf' and p.role not in []:
                        role_string.append(self.lgr(p.role))

                    rs = f' ({" ".join(role_string)})' if role_string else ''
                    living_players_string.append(f"{self.get_name(p)} ({p.id}){rs}")

            # succubus
            # piper
            # executioner
            # shaman
            # clone

            if player.commands:
                lps = '\n'.join(living_players_string)
                # msg.append(f"Living players:\n```basic\n{lps}```")
                msg.append(f"```basic\n{lps}```")

            # mystic
            # wolf mystic
            # turncoat
            # gunner
            # sharpshooter
            # assassin
            # matchmaker
            # minion

            return role_msg, '\n'.join(msg), self.lgr(rolename)

        elif False:
            return '', '', ''  # vengeful ghost
        else:
            return '', '', ''

    async def format_role_info(self, role_msg, info_msg, role, *, ctx=None, send_role=True):
        msg = {}
        if send_role: msg[self.lg('role_msg_header', a=self.a(role), role=role.capitalize())] = role_msg
        if info_msg: msg[self.lg('info_msg_header')] = info_msg
        context = {'ctx': ctx} if ctx is not None else {'footer': False}
        embed = await self.wwembed(**context, fields=msg, title=self.lg('your_role_header', role=role))
        return embed

    def get_votes(self, session):
        totem_dict = {}
        for player in session.players:
            # totem_dict[player] = player.totems.impatience - player.totems.pacifism
            totem_dict[player] = 0

        voteable_players = [x for x in session.players if x.alive]
        # able_players = [x for x in voteable_players if 'injured' not in x.template]
        able_players = [x for x in voteable_players if True]

        vote_dict = {'abstain' : 0}
        for player in voteable_players:
            vote_dict[player.id] = 0

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

    in_session = lambda self, s: s.in_session and not self.win_condition(s)

    listloop = lambda self, x, n: n + x if x < 0 else n - x if x > n else x
   

    def lg(self, key, *args, **kwargs): # phrase translator
        ref = self.lang
        choices = ref['phrases'][key]
        text = random.choice(choices)


        kwargs['villagers'] = self.lgr('villager', 'pl')
        kwargs['wolves'] = self.lgr('wolf', 'pl')
        kwargs['prefix'] = BOT_PREFIX
        kwargs['game_name'] = ref['game_name']

        text = text.format(*args, **{k:v for k, v in kwargs.items() if f'{{{k}}}' in text})

        for role in ref['roles'].values():
            for indicator, word in role.items():
                text = text.replace(f'<{role["sg"]}|{indicator}>', word)

        for sg, pl in ref['plurals'].items():
            text = text.replace(f'<{sg}|sg>', sg)
            text = text.replace(f'<{sg}|pl>', pl)

        return text

    def lgr(self, role, pl='sg'): # role translator
        return self.lang['roles'][role][pl]

    def lgt(self, team): # team translator
        return self.lang['teams'][team]


    pl = lambda self, n: 'sg' if n == 1 else 'pl'
    s = lambda self, n: '' if n == 1 else 's'
    _s = lambda self, n: 's' if n == 1 else ''
    a = lambda self, x: 'an' if any(x.lower().startswith(y) for y in ['a', 'e', 'i', 'o', 'u']) else 'a'

    listing = lambda self, x, c=False: ' and '.join([y for y in [', '.join(x[:-1]) + (',' if len(x[:-1]) > 1 else '')] + [x[-1]] if y]) + (',' if c else '')

    timedelta_to_str = lambda self, x: "{0:02d}:{1:02d}".format(x.seconds // 60, x.seconds % 60)


    def sort_roles(self, role_list):
        role_list = list(role_list)
        result = []
        for role in self.roles('wolf') + self.roles('village') + self.roles('neutral') + self.templates:
            result += [role] * role_list.count(role)
        return result

    def sort_roles_dict(self, role_list):
        return {x: role_list[x] for x in self.sort_roles(role_list)}

    def sort_players(self, players, pre=False):
        real = []
        fake = []
        for player in players:
            if pre:
                if player.real: real.append(player)
                else: fake.append(player)
            else:
                if player.player.real: real.append(player)
                else: fake.append(player)
        return sorted(real, key=self.get_name) + sorted(fake, key=lambda x: x.name)

    def get_name(self, player):
        escape = lambda x: discord.utils.escape_mentions(discord.utils.escape_markdown(str(x)))
        member = player.player.user
        if member: return escape(member.display_name)
        else: return escape(player)

    def find_player(self, session, player_id):
        try: return session.players[[x.id for x in session.players].index(player_id)]
        except ValueError: None

    def split_time(self, x):
        hours, remainder = divmod(int(round(x, 2)), 3600)
        minutes, seconds = divmod(remainder, 60)
        return hours, minutes, seconds

    def timestr_to_text(self, x):
        x = self.split_time(x)
        return ', '.join([f"**{t}** {n}{self.s(t)}" for t, n in zip(x, ['hour', 'minute', 'second']) if t])


    async def wwembed(self, c=None, *, ctx=None, user=None, msg=None, fields={}, inline=False, footer=True, **kwargs):
        embed = discord.Embed(**kwargs, colour=EMBED_COLOUR)
        if footer: embed.timestamp = datetime.utcnow()

        if ctx is not None:
            if user is None:
                user = ctx.author
            if msg is None:
                msg = ctx.message.content
        if user is None:
            user = self.bot.user

        footertxt = []
        if msg is not None:
            footertxt.append(str(msg))
        if user is not None:
            if user.id != self.bot.user.id:
                footertxt.append(str(user))
            else:
                footertxt.append(user.name)


        if footertxt and footer: 
            if user is not None:
                embed.set_footer(text=' | '.join(footertxt), icon_url=user.avatar_url)
            else:
                embed.set_footer(text=' | '.join(footertxt))

        for k, v in fields.items(): embed.add_field(name=k, value=v, inline=inline)

        if c is not None: await c.reply(embed=embed)

        return embed