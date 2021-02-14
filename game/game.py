import asyncio, copy, difflib, discord, typing
from datetime import datetime, timedelta
from discord.ext import commands
from wonderwords import RandomWord # pip wonderwords

from .engine import GameEngine, GameState
from .roles.player import Player, Bot

from config import *
from settings import *


# TODO MUST
# 
# ---- other non priority
# time
# logs
# roles/perms
# command descriptions
# multi lang support
# more roles
# notify
# stasis
# clean up command checks (single function?)
# nicer message formats (eg embeds and stuff)
# command aliases
# info




class Game(commands.Cog, name="Game"):
    def __init__(self, bot):
        self.bot = bot
        self.engine = GameEngine(bot)

        self.lg = self.engine.lg
        self.lgr = self.engine.lgr
        self.lgt = self.engine.lgt
        self.session_update = self.engine.session_update
        self.player_update = self.engine.player_update
        self.preplayer_update = self.engine.preplayer_update

        self.pl = self.engine.pl
        self.s = self.engine.s
        self.a = self.engine.a
        self.get_name = self.engine.get_name
        self.listing = self.engine.listing

        self.player_death = self.engine.player_death

        self.roles_list = self.engine.roles_list
        self.roles = self.engine.roles
        self.templates = self.engine.templates
        self.gamemodes = self.engine.gamemodes

        self.bot.sessions = {}
        self.bot.sessiontasks = {}
        self.bot.sessionlock = {}
        # self.wait_timer = datetime.utcnow()
        # self.wait_bucket = WAIT_BUCKET_INIT

        self.reveal_aliases = {
            'reveal': ['reveal', 'rev', 'rvl' 'r'],
            'noreveal': ['noreveal', 'norev', 'norvl', 'nrvl', 'nr']
        }

        self.bot.pseudousers = []

        self.rw = RandomWord()
        self.randomword = lambda: (' '.join(self.rw.word(include_parts_of_speech=[x], word_max_length=8) for x in ['adjectives', 'nouns'])).title()


        self.bot.loop.create_task(self.init_sessions(GAME_CHANNEL_ID))


    def admin():
        def predicate(ctx):
            if ctx.guild and ctx.guild.id == WEREWOLF_SERVER_ID:
                roles = ctx.author.roles
            else:
                roles = ctx.bot.get_guild(WEREWOLF_SERVER_ID).get_member(ctx.author.id).roles
            return any(x.id in ADMINS_ROLE_ID for x in roles)
        return commands.check(predicate)

    @admin()
    @commands.command(aliases=['initsession'])
    async def initiatesession(self, ctx, channel: commands.TextChannelConverter = None):
        """Initiates a new session in the current or specified channel."""
        if channel is None: channel = ctx.channel
        session = self.find_session_channel(channel.id)
        if session: return await ctx.reply(self.lg('session_already_exists'))

        self.engine.session_setup(channel)
        await ctx.reply(self.lg('session_init', channel=channel.mention, channelid=channel.id))
        # log

    async def init_sessions(self, channels):
        await self.bot.wait_until_ready()
        for c in channels:
            channel = self.bot.get_channel(c)
            self.engine.session_setup(channel)
        # log

    @admin()
    @commands.command()
    async def destroysession(self, ctx, channel: commands.TextChannelConverter = None):
        """Destroys the session in the current or specified channel. Likely to break things if done in-game."""
        if channel is None: channel = ctx.channel
        session = self.find_session_channel(channel.id)
        if session is None: return await ctx.reply(self.lg('no_session_channel'))

        message = await ctx.reply(self.lg('session_destroy_check'))
        check = lambda m: m.author.id == ctx.author.id and m.channel.id == ctx.channel.id
        try: msg = await self.bot.wait_for('message', timeout=5, check=check)
        except asyncio.TimeoutError: msg = None
        else: msg = None if msg.content.lower() not in self.engine.lang['phrases']['confirm_yes'] else msg

        if msg is None:
            return await message.edit(content=self.lg('timed_out'))

        self.bot.sessions.pop(session.id)

        await ctx.reply(self.lg('session_destroyed', sessionid=channel.id))
        # log


    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.id == self.bot.user.id: return

        if not message.guild:
            # log

            if message.content.startswith(BOT_PREFIX):
                return await self.bot.process_commands(message)

            if message.content.split(' ')[0] in [x for c in self.bot.commands for x in [c.name] + c.aliases]:
                newmsg = copy.copy(message)
                newmsg.content = BOT_PREFIX + newmsg.content
                return await self.bot.process_commands(newmsg)

            session = self.find_session_player(message.author.id)
            if not session: return await self.bot.process_commands(message)

            player = self.get_player(session, message.author.id)
            if session.in_session and player.alive and player.role in self.roles('wolfchat'):
                if not message.content.startswith(BOT_PREFIX): await self.wolfchat(session, message)
            else: return await self.bot.process_commands(message)

        # await self.bot.process_commands(message)


    @commands.command(aliases=['j'])
    async def join(self, ctx, gamemode = None):
        """Joins the game if it has not started yet. Votes for gamemode if given."""
        
        if not ctx.guild: return await ctx.reply(self.lg('use_in_channel_2', command=ctx.invoked_with))
        session = self.find_session_channel(ctx.channel.id)
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        # STASIS

        successful, msg = await self.player_join(session, ctx.author)
        await ctx.reply(msg)

        if not successful:
            return

        if gamemode:
            session = await self.session_update('pull', session)
            self.vote_gamemode(session, gamemode)


    async def player_join(self, session, user, real=True):
        newplayer = Player(user.id, user, real=real)
        session = await self.session_update('pull', session)

        # multiple session support

        if user.id in [p.id for p in session.preplayers]:
            return False, self.lg('already_in')
        if session.player_count >= MAX_PLAYERS:
            return False, self.lg('max_players', max_players=MAX_PLAYERS)

        session.preplayers.append(newplayer)
        session.player_ids.append(user.id)

        session = await self.session_update('push', session, ['preplayers', 'player_ids'])

        if session.player_count == 1:
            sessiontasks = self.bot.sessiontasks[session.id]
            sessiontasks['wait_bucket'] = WAIT_BUCKET_INIT
            sessiontasks['wait_timer'] = datetime.utcnow() + timedelta(seconds=WAIT_AFTER_JOIN)
            sessiontasks['game_start_timeout_loop'] = self.bot.loop.create_task(self.game_start_timeout_loop(session))
            sessiontasks['wait_timer_loop'] = self.bot.loop.create_task(self.wait_timer_loop(session))
            # lobby status waiting to start
            msg = self.lg('first_join', name=user.display_name)

        else:
            msg = self.lg('joined_game', name=user.display_name, count=session.player_count)

        # add role

        sessiontasks = self.bot.sessiontasks[session.id]
        sessiontasks['wait_timer'] = datetime.utcnow() + timedelta(seconds=WAIT_AFTER_JOIN)
        sessiontasks['idle'][user.id] = self.bot.loop.create_task(self.player_idle(session, user.id))

        # log

        return True, msg

    @admin()
    @commands.command(aliases=['bj', 'pseudouser', 'pseudousers', 'pu'])
    async def botjoin(self, ctx, count=1):
        """Joins pseudouser bots to the session."""
        session = self.find_session_channel(ctx.channel.id)
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        bot_dm_channel = self.bot.get_channel(PSEUDOUSER_MSG_CHANNEL_ID)

        for i in range(count):
            pseudouser = Bot(
                _id=ctx.message.id + i,
                _name=self.randomword(),
                _discriminator='0000',
                _channel=bot_dm_channel
            )

            successful, msg = await self.player_join(session, pseudouser, False)
            if successful:
                msg += f"\n\nAdded pseudouser: {pseudouser.mention} ({pseudouser.id})"
                self.bot.pseudousers.append(pseudouser)
            await ctx.reply(msg)

    class PseudouserConverter(commands.Converter):
        async def convert(self, ctx, argument: int):
            return [p for p in ctx.bot.pseudousers if int(argument) == p.id][0]


    @commands.command(aliases=['quit', 'q'])
    async def leave(self, ctx, force=None):
        """Leaves the current game. If you need to leave, please do it before the game starts."""
        session = self.find_session_player(ctx.author.id)
        if not session: return await ctx.reply(self.lg('no_session_user'))

        if session.in_session:
            player = self.get_player(session, ctx.author.id)
            if not player.alive: return

            if force != '-force':
                message = await ctx.reply(self.lg('leave_confirm', count='something'))

                check = lambda m: m.author.id == ctx.author.id and m.channel.id == ctx.channel.id
                try: msg = await self.bot.wait_for('message', timeout=5, check=check)
                except asyncio.TimeoutError: msg = None
                else: msg = None if msg.content.lower() not in self.engine.lang['phrases']['confirm_yes'] else msg

                if msg is None:
                    return await message.edit(content=self.lg('timed_out'))

            else: msg = ctx.message

            session = await self.session_update('pull', session)
            session, msg2 = await self.player_leave(session, self.find_player(session, ctx.author.id))
            session = await self.player_update(session, self.find_player(session, ctx.author.id))

            return await msg.reply(msg2)

        else:
            session = await self.session_update('pull', session)
            session, msg = await self.preplayer_leave(session, self.find_preplayer(session, ctx.author.id))
            session = await self.session_update('push', session, ['preplayers'])

            return await ctx.reply(msg)

    async def player_leave(self, session, player, reason='leave'):
        if reason == 'leave': msgtype = 'leave_death'
        elif reason == 'fleave': msgtype = 'guild_leave_death'

        if session.reveal:
            msg = self.lg(msgtype, name=self.get_name(player), role=player.death_role)
        else:
            msg = self.lg(msgtype + '_no_reveal', name=self.get_name(player))

        session = await self.player_death(session, player, reason, 'bot')

        # STASIS
        # TRAITOR
        # log

        return session, msg

    async def preplayer_leave(self, session, player, reason='leave'):
        session = await self.player_death(session, player, 'leave', 'bot')
        msgtype = 'leave_lobby' if reason == 'leave' else 'guild_leave_lobby'
        msg = self.lg('leave_lobby', name=player.nickname, count=session.player_count, s=self.s(session.player_count))
        # log
        return session, msg

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        session = self.find_session_player(member.id)
        if not session: return

        if session.in_session:
            player = self.find_player(session, member.id)
            if player.alive:
                session, msg = self.player_leave(session, player, 'fleave')

        else:
            player = self.find_preplayer(session, member.id)
            session, msg = self.preplayer_leave(session, player, 'fleave')

        return await session.send(msg)


    @commands.command(aliases=['v'])
    async def vote(self, ctx, *, target = None):
        """Casts a vote. Gamemode or Reveal if pre-game, Player if in-game."""
        session = self.find_session_player(ctx.author.id)
        if not session:
            return await ctx.reply(self.lg('no_session_user'))

        if session.in_session:
            cmd = self.bot.get_command("lynch")
            await cmd(ctx, target=target)
            return

        if ctx.channel.id != session.id:
            return use_in_channel(session, ctx)

        if target is None:
            cmd = self.bot.get_command("votes")
            await cmd(ctx)
            return

        if target.lower().replace(' ', '') in sum(list(self.reveal_aliases.values()), []):
            msg = await self.vote_reveal(session, ctx.author.id, target)
        else:
            msg = await self.vote_gamemode(session, ctx.author.id, target)
        await ctx.reply(msg)

    async def vote_gamemode(self, session, player_id, gamemode):
        if session.gamemode:
            return self.lg('admin_set_gamemode')

        player = self.find_preplayer(session, player_id)

        choice, close = self._autocomplete(gamemode, self.gamemodes.keys())
        if len(choice) == 1:
            player.vote.gamemode = choice[0]
            session = await self.preplayer_update(session, player)
            return self.lg('gamemode_voted', gamemode=choice[0])
        else:
            msg = [self.lg('multiple_options',
                count=len(choice),
                pl=self.s(len(choice)),
                options=', '.join([f"`{x}`" for x in choice])
            )]
            if close:
                msg.append(self.lg('multiple_options_2',
                    pl=self.s(len(choice)),
                    options=', '.join([f"`{x}`" for x in choice])
                ))
            return '\n'.join(msg)

    async def vote_reveal(self, session, player_id, choice):
        if session.reveal is not None:
            return self.lg('admin_set_reveal')

        player = self.find_preplayer(session, player_id)

        if choice in self.reveal_aliases['reveal']:
            player.vote.reveal = True
        else:
            player.vote.reveal = False

        session = await self.preplayer_update(session, player)

        return self.lg('reveal_voted', _not='' if player.vote.reveal else 'not ')

    @commands.command(aliases=['vs'])
    async def votes(self, ctx):
        """Displays list of current votes."""
        session = self.find_session_player(ctx.author.id)
        if not session:
            session = self.find_session_channel(ctx.channel.id)
            if not session:
                return await ctx.reply(self.lg('no_session_user'))

        if not session.in_session:
            vote_dict = {'start': [], 'reveal': [], 'no reveal': []}

            for player in session.preplayers:
                if player.vote.start: vote_dict['start'].append(player)
                
                if player.vote.gamemode is not None:
                    gm = player.vote.gamemode
                    if gm in vote_dict.keys():
                        vote_dict[gm].append(player)
                    else: vote_dict[gm] = [player]

                if player.vote.reveal is not None:
                    rv = 'reveal' if player.vote.reveal else 'no reveal'
                    vote_dict[rv].append(player)

            pc = session.player_count
            gmmaj = session.player_count // 2 + 1  # gamemode majority required
            stmaj = max(2, min(session.player_count // 4 + 1, 4))
            msg = [self.lg('start_vote_count',
                player_count = pc,
                s_pc = self.s(pc),
                gamemode_count = gmmaj,
                s_gm = self.s(gmmaj),
                start_count = stmaj
            )]

            pllist = lambda x: ', '.join(p.user.display_name for p in x)

            for gamemode, plist in vote_dict.items():
                if gamemode in ['start', 'reveal', 'no reveal']: continue
                msg.append(f"{gamemode} ({len(plist)} vote{self.s(len(plist))}: {pllist(plist)})")

            if vote_dict['reveal']:
                x = vote_dict['reveal']
                msg.append(f"{len(x)} vote{self.s(len(x))} to reveal roles: {pllist(x)}")

            if vote_dict['no reveal']:
                x = vote_dict['no reveal']
                msg.append(f"{len(x)} vote{self.s(len(x))} not to reveal roles: {pllist(x)}")

            x = vote_dict['start']
            msg.append(f'{len(x)} vote{self.s(len(x))} to start: {pllist(x)}')

            return await ctx.reply('\n'.join(msg))

        elif session.in_session and session.day:
            vote_dict = {'abstain': []}
            alive_players = [x for x in session.players if x.alive]
            able_voters = [x for x in alive_players if True]

            for player in able_voters:
                if player.vote in vote_dict: vote_dict[player.vote].append(player)
                elif player.vote is not None: vote_dict[player.vote] = [player]

            abstainers = vote_dict['abstain']

            msg = [self.lg('lynch_vote_count',
                player_count = len(alive_players),
                vote_count = len(able_voters) // 2 + 1,
                voteable_count = len(able_voters),
                abstain_count = len(abstainers),
                s_abs = self.s(len(abstainers))
            )]

            if len(vote_dict) == 1 and not vote_dict['abstain']:
                msg.append(self.lg('no_votes_yet', channel = session.mention))

            else:
                msg.append("Current votes: ```\n")
                for voted, voters in {x: c for x, c in vote_dict.items() if x != 'abstain'}.items():
                    votee = self.find_player(session, voted)
                    msg.append(f"{self.get_name(votee)} ({votee.id}) ({len(voters)} vote{self.s(len(voters))}): {', '.join(self.get_name(x) for x in voters)}")
                msg.append(f"{len(abstainers)} vote{self.s(len(abstainers))} to abstain: {', '.join(self.get_name(x) for x in vote_dict['abstain'])}")
                msg.append('```')

            return await ctx.reply('\n'.join(msg))

        else:
            return await ctx.reply(self.lg('night_votes'))

    @commands.command(aliases=['s'])
    async def stats(self, ctx):
        """Lists all players, and game info if in-game."""
        session = self.find_session_player(ctx.author.id)
        if not session:
            session = self.find_session_channel(ctx.channel.id)
            if not session:
                return await ctx.reply(self.lg('no_session_channel'))

        if not session.in_session:
            if session.player_count == 0:
                return await ctx.reply(self.lg('no_session_channel_join'))
            else:
                return await ctx.reply(self.lg('lobby_count',
                    count=session.player_count,
                    s=self.s(session.player_count),
                    players='\n'.join(f"{p.name} ({p.id})" for p in session.preplayers)
                ))

        else:
            msg = self.stats_msg(session)
            return await ctx.reply(msg)

    def stats_msg(self, session):
        msg = [self.lg('stats_info', daynight='day' if session.day else 'night', gamemode=session.gamemode['name'])]
        msg.append(self.lg('stats_player_count',
            count=session.player_count,
            alive=len([x for x in session.players if x.alive]),
            dead=len([x for x in session.players if not x.alive])
        ))
        msg.append(self.lg('stats_players',
            alive='\n'.join(f"{self.get_name(x)} ({x.id})" for x in session.players if x.alive),
            dead='\n'.join(f"{self.get_name(x)} ({x.id})" for x in session.players if not x.alive)
        ))


        orig_roles = self.engine.sort_roles_dict(session.original_roles_amount)
        # traitor stuff

        role_dict = self.engine.sort_roles_dict({x: [0, 0] for x in self.roles_list})
        for player in session.players:
            role = player.role
            role_dict[role][0] += 1
            role_dict[role][1] += 1

        msg.append("Total roles: " + ', '.join(f"{self.lgr(name, 'pl')}: {count}" for name, count in orig_roles.items()))

        if session.reveal:
            for role in list(role_dict):
                if role in self.templates:
                    del role_dict[role]

            # traitor

            for player in session.players:
                if not player.alive:
                    reveal = player.death_role
                    role_dict[reveal][0] = max(0, role_dict[reveal][0] - 1)
                    role_dict[reveal][1] = max(0, role_dict[reveal][1] - 1)

            # clone
            # amnesiac and executioner

            for template in self.templates:
                if template in orig_roles:
                    del orig_roles[template]

            msg.append("Current roles: " + ', '.join(f"{self.lgr(role, 'pl')}: {count[0] if count[0] == count[1] else f'{count[0]}-{count[1]}'}" for role, count in role_dict.items()))

        msg.append('```')

        return '\n'.join(msg)


    @commands.command()
    async def start(self, ctx):
        """Votes to start the game, if the minimum player count is met."""
        session = self.find_session_player(ctx.author.id)
        if not session: return await ctx.reply(self.lg('no_session_user'))

        if ctx.channel.id != session.id:
            return await use_in_channel(session, ctx)

        if session.in_session:
            return await ctx.reply(self.lg('already_in_session'))

        if session.player_count < MIN_PLAYERS:
            return await ctx.reply(self.lg('min_player_start', min=MIN_PLAYERS))

        player = self.find_preplayer(session, ctx.author.id)
        if player.vote.start:
            return await ctx.reply(self.lg('already_voted'))

        sessiontasks = self.bot.sessiontasks[session.id]
        if datetime.utcnow() < sessiontasks['wait_timer']:
            seconds = int(round((sessiontasks['wait_timer'] - datetime.utcnow()).total_seconds()))
            return await ctx.reply(self.lg('wait_start',
                seconds=seconds,
                pl=self.s(seconds)
            ))

        player.vote.start = True
        session = await self.preplayer_update(session, player)

        votes = len([x for x in session.preplayers if x.vote.start])
        votes_needed = max(2, min(session.player_count // 4 + 1, 4))

        if votes < votes_needed:
            await ctx.reply(self.lg('start_vote',
                name = self.get_name(player),
                count = votes_needed - votes,
                pl = self.s(votes_needed - votes)
            ))

        else:
            await self.engine.run_game(session)

        if votes == 1:
            sessiontasks = self.bot.sessiontasks[session.id]
            sessiontasks['start_votes_loop'] = self.bot.loop.create_task(self.start_votes_loop(session, player))


    async def wolfchat(self, session, msg, bot=False):
        if not bot:
            player = self.find_player(session, msg.author.id)
            msg = msg.content
        for wolf in [x for x in session.players if x.team == 'wolf' and x.wolfchat]:
            if not bot:
                if wolf.id == player.id: continue
                prefix = f"[{self.lg('wolfchat').capitalize()}] {self.get_name(player)}"
            else:
                prefix = f"[{self.lg('wolfchat').capitalize()}] <System>"

            try:
                await wolf.send(f"**{prefix}**: {msg}")
            except discord.Forbidden:
                await player.send(self.lg('wolfchat_no_dm', player=self.get_name(wolf), wolfchat=self.lg('wolfchat')))


    @commands.command()
    async def myrole(self, ctx):
        """Tells you your role in DMs."""
        session = self.find_session_player(ctx.author.id)
        if not session: return await ctx.reply(self.lg('no_session_user'))

        if ctx.channel.id != session.id and ctx.guild:
            return

        player = self.find_player(session, ctx.author.id)

        role_msg, info_msg = self.engine.send_role_info(session, player)
        try:
            await player.send(role_msg)
            if info_msg:
                await player.send(info_msg)
            await ctx.message.add_reaction('👍')
        except discord.Forbidden:
            await session.send(self.lg('role_dm_off', mention=player.mention))

    @commands.command(aliases=['roles'])
    async def role(self, ctx, *, param = None):
        """Displays list of roles if none specified, otherwise gives info on role. If gamemode specified, displays role table for gamemode."""
        session = self.find_session_player(ctx.author.id)
        if param is not None: param = param.lower()

        msgtype = None
        if param is None:
            if session is None: msgtype = 'list'
            elif not session.in_session: msgtype = 'list'
            else: msgtype = 'gmroles'
        elif param == 'list': msgtype = 'list'
        elif len(self._autocomplete(param, self.roles())[0]) == 1: msgtype = 'role'
        else: msgtype = 'gamemode'

        if msgtype == 'list':
            return await ctx.reply(self.role_list())

        if msgtype == 'role':
            return await ctx.reply(self.role_info(param))

        if msgtype in ['gamemode', 'gmroles']:
            if msgtype == 'gmroles':
                gamemode = session.gamemode
            else:
                gamemode = 'default'
                params = param.split(' ')

                choices, _ = self._autocomplete(params[0], list(self.gamemodes.keys()))
                if len(choices) == 1: gamemode = choices[0]

            num_players = -1

            if params[0].isdigit():
                num_players = int(params[0])
            if len(params) == 2 and params[1].isdigit():
                num_players = params[1]

            if num_players == -1:
                return await ctx.reply(self.role_table(gamemode))

            game_roles = self.get_roles(gamemode, num_players)

            if game_roles is None: return await ctx.reply(self.lg('gamemode_boundaries'))

            msg = [f'Roles for **{num_players}** in gamemode **{gamemode}**']
            msg.append('```py')
            msg += [f"{role}: {count}" for role, count in game_roles.items()]
            msg.append('```')

            return await ctx.reply('\n'.join(msg))

    def role_list(self):
        msg = []
        msg.append("```ini")
        msg.append(f"[{self.lgt('village').capitalize()}] {', '.join(self.roles('village'))}")
        msg.append(f"[{self.lgt('wolf').capitalize()}] {', '.join(self.roles('wolf'))}")
        # msg.append(f"[self.lgt('neutral').capitalize()] {', '.join(self.roles('neutral'))}")
        msg.append(f"[Templates] {', '.join(self.templates)}")
        msg.append("```")
        return '\n'.join(msg)

    def role_info(self, string):
        role = self._autocomplete(string, self.roles())[0][0]
        roleobj = self.roles_list[role]
        msg = []
        msg.append(f"**Role name**: {self.lgr(role)}")
        msg.append(f"**Team**: {self.lgt(roleobj.team)}")
        msg.append(f"**Description**: {self.lgr(role, 'desc')}")
        return '\n'.join(msg)

    def role_table(self, gamemode):
        WIDTH = 10
        role_dict = dict()
        for role, count in self.gamemodes[gamemode]['roles'].items():
            if max(count): role_dict[role] = count

        gmobj = self.gamemodes[gamemode]

        msg = [f'Role table for gamemode **{gamemode}**']
        msg.append('```py')
        msg.append(' ' * (WIDTH + 2) + ', '.join(f"{x:02d}" for x in range(gmobj['min_players'], gmobj['max_players'] + 1)))
        for role in self.engine.sort_roles(role_dict):
            ns = role_dict[role]
            msg.append(f"{role}{' ' * (WIDTH - len(role))}: {', '.join((f'''{' ' if n < 10 else ''}{n}''') for n in ns)}")
        msg.append('```')

        return '\n'.join(msg)


    @commands.command()
    async def lynch(self, ctx, *, target = None):
        """Votes to lynch a player during the day."""
        session = self.find_session_player(ctx.author.id)
        if not session: return

        if not ctx.guild:
            return await self.use_in_channel(session, ctx)
        if session.channel.id != ctx.channel.id: return

        player = self.get_player(session, ctx.author.id)
        if not player.alive: return

        if session.night: return
        if (datetime.utcnow() - session.day_start).total_seconds() <= 2: return

        if target is None:
            cmd = self.bot.get_command("votes")
            await cmd(ctx)
            return

        to_lynch = self.get_player(session, target.split(' ')[0])
        if not to_lynch:
            to_lynch = self.get_player(session, target)

        if not to_lynch:
            return await ctx.reply(self.lg('no_player_found', query=target))

        if not to_lynch.alive:
            return await ctx.reply(self.lg('player_dead', name=self.get_name(to_lynch)))

        session = await self.session_update('pull', session)
        player = self.find_player(session, ctx.author.id)
        player.vote = to_lynch.id
        session = await self.player_update(session, player)

        await ctx.reply(self.lg('lynch_vote', name=self.get_name(to_lynch)))
        # vote numbers or something?

        # log

    @commands.command(aliases=['r'])
    async def retract(self, ctx):
        """Retracts vote to lynch or kill, if any."""
        session = self.find_session_player(ctx.author.id)
        if not session: return

        if not session.in_session:
            player = self.find_preplayer(session, ctx.author.id)

            if not ctx.guild:
                return await ctx.reply(self.lg('use_in_channel', command=ctx.invoked_with, channel=session.mention))
            if session.channel.id != ctx.channel.id: return

            if not player.vote.gamemode and player.vote.reveal is None:
                return await ctx.reply(self.lg('no_vote'))

            votecount = [bool(player.vote.gamemode), player.vote.reveal is not None].count(True)

            player.vote.gamemode = None
            player.vote.reveal = None
            session = await self.player_update(session, player)

            return await ctx.reply(self.lg('retract_vote', pl=self.s(votecount)))

        elif session.in_session:
            player = self.find_player(session, ctx.author.id)
            if not player.alive: return

            if session.day:
                if not ctx.guild:
                    return await self.use_in_channel(session, ctx)

                if player.vote is None:
                    return await ctx.reply(self.lg('no_vote'))

                player.vote = None
                session = await self.player_update(session, player)

                return await ctx.reply(self.lg('retract_vote', pl=''))

            else:
                if 'kill' in player.commands:
                    if ctx.guild:
                        try:
                            return await player.user.send(self.lg('use_in_dm', command=ctx.invoked_with))
                        except discord.Forbidden:
                            return

                    former_targets = player.targets[:]
                    player.targets = []
                    session = await self.player_update(session, player)

                    await ctx.reply('retract_kill', pl=self.s(len(former_targets)))

                    # wolfchat
                    # log

    @commands.command(aliases=['abs'])
    async def abstain(self, ctx):
        """Votes to abstain during the day."""
        session = self.find_session_player(ctx.author.id)
        if not session: return

        if not session.in_session or session.night: return

        player = self.get_player(session, ctx.author.id)
        if not player.alive: return

        if not ctx.guild:
            return await self.use_in_channel(session, ctx)
        if session.channel.id != ctx.channel.id: return

        # evil village

        if session.day_start == timedelta(0):
            return await ctx.reply(self.lg('abstain_first_day'))

        # injured

        player.vote = 'abstain'
        session = await self.player_update(session, player)
        # log

        return await ctx.reply(self.lg('abstain', name=self.get_name(player)))


    @commands.command(hidden=True)
    async def kill(self, ctx, *, target=None):
        """If able, casts your vote to kill a player overnight."""
        session = self.find_session_player(ctx.author.id)
        if not session: return
        player = self.find_player(session, ctx.author.id)
        if not player.alive: return
        if 'kill' not in player.commands: return

        if session.day:
            return ctx.reply(self.lg('kill_day'))

        if target is None:
            targets = [f"`{self.get_name(self.find_player(session, x))}`" for x in player.targets]
            return ctx.reply('\n'.join([
                self.lgr(player.role, 'desc'), 
                self.lg('kill_target',
                    listing=': ' + ', '.join(targets) if targets else 'nobody'
                )
            ]))

        if player.role == 'wolf':
            num_kills = 1
            targets = target.lower().split(' and ')
            actual_targets = []
            for target in targets:
                targetpl = self.get_player(session, target)
                if targetpl is None:
                    return await ctx.reply(self.lg('no_player_found', query=target))
                actual_targets.append(targetpl)
            actual_targets = set(actual_targets)

            valid_targets = []
            if len(actual_targets) > num_kills:
                return await ctx.reply(self.lg('kill_too_many', count=num_kills, s=self.s(num_kills)))

            for target in actual_targets:
                if target.id == player.id:
                    return await ctx.reply(self.lg('kill_suicide'))
                elif target.team == 'wolf' and target.role not in []:
                    return await ctx.reply(self.lg('kill_teamkill'))
                elif not target.alive:
                    return await ctx.reply(self.lg('player_dead', name=self.get_name(target)))

                valid_targets.append(target)

            # misdirection

            session = await self.session_update('pull', session)
            player = self.find_player(session, player.id)
            player.targets = [x.id for x in valid_targets]
            session = await self.player_update(session, player)

            listing = self.listing([f"**{self.get_name(x)}**" for x in valid_targets])

            await ctx.reply(self.lg('kill_voted', listing=listing))
            await self.wolfchat(session, self.lg('kill_voted_wolfchat', name=self.get_name(player), listing=listing), bot=True)

            # log

    @commands.command(hidden=True)
    async def see(self, ctx, *, target=None):
        """If able, detects and seemingly reveals a player's role."""
        session = self.find_session_player(ctx.author.id)
        if not session: return
        player = self.find_player(session, ctx.author.id)
        if not player.alive: return
        if 'see' not in player.commands: return

        if session.day:
            return await ctx.reply(self.lg('see_day'))

        if player._target is not None:
            return await ctx.reply(self.lg('see_already_used'))

        if target is None:
            return await ctx.reply(self.lgr(player.role, 'desc'))

        targetpl = self.get_player(session, target)
        if targetpl is None:
            return await ctx.reply(self.lg('no_player_found', query=target))

        if targetpl.id == player.id:
            return await ctx.reply(self.lg('see_self'))
        elif not targetpl.alive:
            return await ctx.reply(self.lg('player_dead', name=self.get_name(target)))

        if player.role == 'seer':
            seen_role = targetpl.seen_role
            # deceit totem
            msg = self.lg('see_is', a=self.a(seen_role), role=seen_role)

        await ctx.reply(self.lg('see_result', name=self.get_name(targetpl), msg=msg))

        session = await self.session_update('pull', session)
        player = self.find_player(session, player.id)
        player._target = targetpl.id
        session = await self.player_update(session, player)

        # log


    @admin()
    @commands.command()
    async def revealroles(self, ctx, session: int = None):
        """Reveals the roles and state of players in-game."""
        if session is not None: session = self.find_session_channel(session)
        else: session = self.find_session_channel(ctx.channel.id)
        if not session: 
            session = self.find_session_player(ctx.author.id)
            if not session:
                return await ctx.reply(self.lg('no_session_channel'))

        if not session.in_session: return await ctx.reply(self.lg('not_in_session'))

        msg = [f"**Gamemode**: {session.gamemode['name']}\n```diff"]
        for player in session.players:
            msg.append(f"{'+' if player.alive else '-'} {self.get_name(player)} ({player.id}): {player.role}; template: {str(', '.join(str(x) for x in self.templates if getattr(player.template, x)))}; action: {str(player.targets)}")
        msg.append("```")

        # await ctx.reply('\n'.join(msg))
        try:
            await ctx.author.send('\n'.join(msg))
        except discord.Forbidden:
            await ctx.send(self.lg('dm_off', mention=player.mention))

        # log



    @admin()
    @commands.group(aliases=['f'])
    async def force(self, ctx):
        """Force commands, used to debug or force a state."""
        if not ctx.invoked_subcommand:
            await ctx.send_help(ctx.command)

    @force.command(name='join', aliases=['j'])
    async def fjoin(self, ctx, session: typing.Union[commands.TextChannelConverter, commands.UserConverter, PseudouserConverter], target: commands.Greedy[typing.Union[commands.UserConverter, PseudouserConverter]] = None):
        """Joins players to game."""
        if isinstance(session, int): session = self.find_session_channel(session)
        else:
            a = session
            session = self.find_session_channel(ctx.channel.id)
            if target is not None: target = [a] + target
            else: target = [a]
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        for t in target:
            success, msg = await self.player_join(session, t)
            await ctx.reply(f"**[{t.mention} ({t.id})]**: {msg}")

    @force.command(name='leave', aliases=['l'])
    async def fleave(self, ctx, session: typing.Union[commands.TextChannelConverter, commands.UserConverter, PseudouserConverter], target: commands.Greedy[typing.Union[commands.UserConverter, PseudouserConverter]] = None):
        """Kicks players from game."""
        if isinstance(session, discord.TextChannel): session = self.find_session_channel(session.id)
        else:
            a = session
            session = self.find_session_channel(ctx.channel.id)
            if target is not None: target = [a] + target
            else: target = [a]
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        if not session.in_session:
            for t in target:
                name = f"{t.mention} ({t.id})"

                player = self.find_preplayer(session, t.id)
                if player is None:
                    await ctx.reply(f"**{name}** is not in-game!")
                    continue

                session = await self.session_update('pull', session)
                session, msg = await self.preplayer_leave(session, self.find_preplayer(session, t.id))

                await ctx.reply(f"**[{name}]**: {msg}")
        else:
            for t in target:
                name = f"{t.mention} ({t.id})"
                player = self.find_player(session, t.id)
                if player is None:
                    await ctx.reply(f"**{name}** is not in-game!")
                    continue
                if not player.alive:
                    await ctx.reply(f"**{name}** is already dead!")
                    continue

                session = await self.session_update('pull', session)
                session, msg = await self.player_leave(session, self.get_player(session, t.id))
                session = await self.player_update(session, self.get_player(session, t.id))

                await ctx.reply(f"**[{name}]**: {msg}")

    @force.command(name='start')
    async def fstart(self, ctx, session: int = None):
        """Starts the game."""
        if session is not None: session = self.find_session_channel(session)
        else: session = self.find_session_channel(ctx.channel.id)
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        if session.in_session: return await ctx.reply("The game is already in session!")

        await ctx.reply(f"Starting...")
        await self.engine.run_game(session)
        # log

    @force.command(name='stop')
    async def fstop(self, ctx, session: int = None):
        """Stops the game."""
        if session is not None: session = self.find_session_channel(session)
        else: session = self.find_session_channel(ctx.channel.id)
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        if not session.in_session: return await ctx.reply("The game not yet in session!")

        session.in_session = False
        session = await self.session_update('push', session, ['in_session'])
        await ctx.reply(f"Stopping...")
        # log

    @force.command(name='timeset', aliases=['settime', 'ts'])
    async def ftimeset(self, ctx, session: typing.Union[int, str], *, time = None):
        """Sets the time to day or night."""
        if isinstance(session, int): session = self.find_session_channel(session)
        else:
            a = session
            session = self.find_session_channel(ctx.channel.id)
            if time is not None: time = ' '.join([a, time])
            else: time = a
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        time = time.lower()
        if time not in ['day', 'night']:
            return await ctx.reply("Please specify either `day` or `night`")

        if time == 'day':
            session.set_day()
        elif time == 'night':
            session.set_night()

        session = await self.session_update('push', session, ['_daynight'])

        await ctx.reply(f"Setting time to {time}")
        # log

    @force.command(name='day', hidden=True)
    async def fday(self, ctx):
        """Sets the time to day."""
        cmd = self.bot.get_command("force timeset")
        await cmd(ctx, 'day')

    @force.command(name='night', hidden=False)
    async def fnight(self, ctx):
        """Sets the time to night."""
        cmd = self.bot.get_command("force timeset")
        await cmd(ctx, 'night')

    @force.command(name='target')
    async def ftarget(self, ctx, session: typing.Union[commands.TextChannelConverter, commands.UserConverter, PseudouserConverter], target: commands.Greedy[typing.Union[commands.UserConverter, PseudouserConverter, str]] = None):
        """Sets a player's target."""
        if isinstance(session, int): session = self.find_session_channel(session)
        else:
            a = session
            session = self.find_session_channel(ctx.channel.id)
            if target is not None: target = [a] + target
            else: target = [a]
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        if len(target) < 2: return await ctx.reply("Please specify at least two users - the targeter and the targeted player(s).")

        targeter = self.find_player(session, target[0].id)
        if targeter is None: return await ctx.reply(f"**{target[0].display_name} ({target[0].id})** is not in-game!")

        targeted = []
        for t in target[1:]:
            td = self.find_player(session, t.id)
            if td is None: return await ctx.reply(f"**{t} ({t.id})** is not in-game!")
            targeted.append(td)

        if not any(x in ['kill'] for x in targeter.commands):
            return await ctx.reply(f"**{self.get_name(targeter)} ({targeter.id})** can't target anything!")

        session = await self.session_update('pull', session)
        player = self.find_player(session, targeter.id)
        player.targets = [x.id for x in targeted]
        session = await self.player_update(session, player)

        listing = self.listing([f"{x.mention} ({x.id})" for x in targeted])

        await ctx.reply(f"Set {targeter.mention}'s ({targeter.id}) target(s) to {listing}")
        # log

    @force.command(name='vote')
    async def fvote(self, ctx, session: typing.Union[commands.TextChannelConverter, commands.UserConverter, PseudouserConverter], target: commands.Greedy[typing.Union[commands.UserConverter, PseudouserConverter, str]] = None):
        """Sets a player's vote."""
        if isinstance(session, int): session = self.find_session_channel(session)
        else:
            a = session
            session = self.find_session_channel(ctx.channel.id)
            if target is not None: target = [a] + target
            else: target = [a]
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        if not session.in_session and not session.day: return await ctx.reply("You can only force votes during day in-game.")

        if len(target) != 2: return await ctx.reply("Please specify two users - the targeter and the targeted player.")

        targeter = self.find_player(session, target[0].id)
        if targeter is None: return await ctx.reply(f"**{target[0].display_name} ({target[0].id})** is not in-game!")

        targeted = target[1]
        td = self.find_player(session, targeted.id)
        if td is None: return await ctx.reply(f"**{targeted.display_name} ({targeted.id})** is not in-game!")
        targeted = td

        session = await self.session_update('pull', session)
        player = self.find_player(session, targeter.id)
        player.vote = targeted.id
        session = await self.player_update(session, player)

        await ctx.reply(f"Set {targeter.mention}'s ({targeter.id}) vote to {targeted.mention} ({targeted.id})")
        # log

    @force.command(name='role')
    async def frole(self, ctx, session: typing.Union[commands.TextChannelConverter, commands.UserConverter, PseudouserConverter], target: commands.Greedy[typing.Union[commands.UserConverter, PseudouserConverter, str]] = None):
        """Sets a player's role."""
        if isinstance(session, int): session = self.find_session_channel(session)
        else:
            a = session
            session = self.find_session_channel(ctx.channel.id)
            if target is not None: target = [a] + target
            else: target = [a]
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        if not session.in_session: return await ctx.reply("Please wait until the game is in session")

        player = self.find_player(session, target[0].id)
        if player is None: return await ctx.reply(f"**{target[0].display_name} ({target[0].id})** is not in-game!")

        role = ' '.join(target[1:]).lower()
        if role not in self.roles():
            return await ctx.reply(f"Cannot find role named `{role}`")

        newrole = self.roles_list[role](player.player)
        newrole.template = player.template
        newrole.totems = player.totems
        newrole.items = player.items
        newrole.alive = player.alive
        newrole.vote = player.vote

        session = await self.session_update('pull', session)
        session = await self.player_update(session, newrole)

        await ctx.reply(f"Set {player.mention}'s ({player.id}) role to `{role}`")
        # log

        role_msg, info_msg = self.engine.send_role_info(session, newrole)
        try:
            await newrole.send(role_msg)
            if info_msg:
                await newrole.send(info_msg)
        except discord.Forbidden:
            await session.send(self.lg('role_dm_off', mention=newrole.mention))

    @force.command(name='template')
    async def ftemplate(self, ctx, session: typing.Union[commands.TextChannelConverter, commands.UserConverter, PseudouserConverter], target: commands.Greedy[typing.Union[commands.UserConverter, PseudouserConverter, str]] = None):
        """Toggles a player's template."""
        if isinstance(session, int): session = self.find_session_channel(session)
        else:
            a = session
            session = self.find_session_channel(ctx.channel.id)
            if target is not None: target = [a] + target
            else: target = [a]
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        if not session.in_session: return await ctx.reply("Please wait until the game is in session")

        player = self.find_player(session, target[0].id)
        if player is None: return await ctx.reply(f"**{target[0].display_name} ({target[0].id})** is not in-game!")

        template = ' '.join(target[1:]).lower()
        if template not in self.templates:
            return await ctx.reply(f"Cannot find template named `{template}`")

        session = await self.session_update('pull', session)
        player = self.find_player(session, target[0].id)
        current = getattr(player.template, template)
        setattr(player.template, template, not current)
        session = await self.player_update(session, player)

        await ctx.reply(f"Toggled {player.mention}'s' ({player.id}) `{template}` template to `{not current}`")
        # log

    @force.command(name='gamemode', aliases=['gm'])
    async def fgamemode(self, ctx, session: typing.Union[commands.TextChannelConverter, str], *, mode: str = None):
        """Sets and locks the gamemode."""
        if isinstance(session, int): session = self.find_session_channel(session)
        else:
            a = session
            session = self.find_session_channel(ctx.channel.id)
            if mode is not None: mode = ' '.join([a] + mode)
            else: mode = a
        if not session: return await ctx.reply(self.lg('no_session_channel'))


        if session.in_session: return await ctx.reply("The game is already in session!")

        mode = mode.lower()
        # if mode not in self.gamemodes.keys():
        #     return await ctx.reply(f"Cannot find gamemode named `{mode}`")

        choice, close = self._autocomplete(mode, self.gamemodes.keys())
        if len(choice) != 1:
            msg = [self.lg('multiple_options',
                count=len(choice),
                pl=self.s(len(choice)),
                options=', '.join([f"`{x}`" for x in choice])
            )]
            if close:
                msg.append(self.lg('multiple_options_2',
                    pl=self.s(len(choice)),
                    options=', '.join([f"`{x}`" for x in choice])
                ))
            await ctx.reply('\n'.join(msg))
            # log
            return

        mode = choice[0]
        session.gamemode = mode
        session = await self.session_update('push', session, ['gamemode'])

        await ctx.reply(f"Set the gamemode to `{mode}`")
        # log

    @force.command(name='reveal', aliases=['rv'])
    async def freveal(self, ctx, session: typing.Union[commands.TextChannelConverter, str], *, mode: str = None):
        """Sets and locks the reveal state."""
        if isinstance(session, int): session = self.find_session_channel(session)
        else:
            a = session
            session = self.find_session_channel(ctx.channel.id)
            if mode is not None: mode = ' '.join([a] + mode)
            else: mode = a
        if not session: return await ctx.reply(self.lg('no_session_channel'))

        if session.in_session: return await ctx.reply("The game is already in session!")

        mode = mode.lower()
        if mode not in sum(list(self.reveal_aliases.values()), []):
            return await ctx.reply(f"Please specify either `reveal` or `noreveal`")

        session.reveal = True if mode in self.reveal_aliases['reveal'] else False
        session = await self.session_update('push', session, ['reveal'])

        await ctx.reply(f"Set the reveal to `{mode}`")
        # log



    def find_session_player(self, player_id):
        try:
            session = next((s for k, s in self.bot.sessions.items() if player_id in s.player_ids))
        except StopIteration:
            session = None
        return session

    def find_session_channel(self, channel_id):
        try:
            session = self.bot.sessions[channel_id]
        except KeyError:
            session = None
        return session

    def session_channel(self, session, message):
        return session.id == message.channel.id

    def find_player(self, session, player_id):
        try: return session.players[[x.id for x in session.players].index(player_id)]
        except ValueError: None

    def find_preplayer(self, session, player_id):
        try: return session.preplayers[[x.id for x in session.preplayers].index(player_id)]
        except ValueError: None

    def get_player(self, session, string):
        string = str(string)
        string = string.lower().replace(' ', '')
        string = string.strip('<@!>')
        name_dict = {
            'name': [],
            'discriminator': [],
            'nick': [],
            'name_contains': [],
            'nick_contains': []
        }

        for player in session.players:
            if string in [player.name.lower(), player.nickname.lower(), str(player.id)]:
                return player
            if player.name.lower().replace(' ', '').startswith(string):
                name_dict['name'].append(player)
            if string.strip('#') == player.discriminator:
                name_dict['discriminator'].append(player)
            if player.nickname.lower().replace(' ', '').startswith(string):
                name_dict['nick'].append(player)
            if string in player.name.lower().replace(' ', ''):
                name_dict['name_contains'].append(player)
            if string in player.nickname.lower().replace(' ', ''):
                name_dict['nick_contains'].append(player)

        for k, v in name_dict.items():
            if len(v) == 1: return v[0]
        return None


    def get_roles(self, gamemode, playercount):
        if gamemode in self.gamemodes.keys():
            gmobj = self.gamemodes[gamemode]

            if playercount not in range(gmobj['min_players'], gmobj['max_players'] + 1):
                return None

            gamemode_roles = {}
            for role in self.roles_list:
                if role in gmobj['roles'].keys() and gmobj['roles'][role][playercount - MIN_PLAYERS] > 0:
                    gamemode_roles[role] = gmobj['roles'][role][playercount - MIN_PLAYERS]
            return gamemode_roles

    async def player_idle(self, session, player):
        while player in [x.id for x in session.preplayers] and not session.in_session:
            await asyncio.sleep(1)
            session = await self.session_update('pull', session)

        if not session.in_session:
            return

        session = await self.session_update('pull', session)
        while session.phase == GameState.GAME_SETUP:
            await asyncio.sleep(1)
            session = await self.session_update('pull', session)


        session = await self.session_update('pull', session)
        player = self.find_player(session, player)
        while player.id in [x.id for x in session.players] and session.in_session and self.find_player(session, player.id) and player.alive and player.player.real:
            check = lambda m: m.author.id == player.id and m.channel.id == session.id
            try: msg = await self.bot.wait_for('message', timeout=PLAYER_TIMEOUT, check=check)
            except asyncio.TimeoutError: msg = None

            session = await self.session_update('pull', session)
            if msg is None and self.find_player(session, player.id) and self.find_player(session, player.id).alive and session.in_session:
                player = self.find_player(session, player.id)
                if player.alive:
                    await session.send(self.lg('idle_lobby', mention=player.mention))
                    await player.send(self.lg('idle_dm', channel=session.mention))

                    try: msg = await self.bot.wait_for('message', timeout=PLAYER_TIMEOUT2, check=check)
                    except asyncio.TimeoutError: msg = None

                    session = await self.session_update('pull', session)
                    if msg is None and self.find_player(session, player.id) and session.in_session:
                        player = self.find_player(session, player.id)
                        if player.alive:
                            name = self.get_name(player)
                            if session.reveal:
                                await session.send(self.lg('idle', name=name, role=player.death_role))
                            else:
                                await session.send(self.lg('idle_no_reveal', name=name))

                            # STASIS

                            session = await self.session_update('pull', session)
                            session = await self.engine.player_death(session, player, 'idle', 'bot')

                            # TRAITOR

                            # log

                            session = await self.session_update('push', session, ['players'])

    async def game_start_timeout_loop(self, session):
        session.first_join = datetime.utcnow()
        session = await self.session_update('push', session, ['first_join'])

        while not session.in_session and session.player_count and (datetime.utcnow() - session.first_join).total_seconds() < GAME_START_TIMEOUT:
            await asyncio.sleep(0.1)
            session = await self.session_update('pull', session)

        if not session.in_session and session.player_count:
            # lobby thingy
            await session.send(self.lg('start_timeout', players=' '.join([x.mention for x in session.preplayers])))
            # unlock lobby

            for player in session.preplayers:
                session = await self.engine.player_death(session, player, 'game cancel', 'bot')

            # session = await self.session_update('push', session)

            self.engine.session_setup(session.channel)

    async def wait_timer_loop(self, session):
        timer = datetime.utcnow()
        while not session.in_session and session.player_count:
            if datetime.utcnow() - timer > timedelta(seconds=WAIT_BUCKET_DELAY):
                timer = datetime.utcnow()
                self.bot.sessiontasks[session.id]['wait_bucket'] = min(self.bot.sessiontasks[session.id]['wait_bucket'] + 1, WAIT_BUCKET_MAX)
            await asyncio.sleep(0.5)
            session = await self.session_update('pull', session)

    async def start_votes_loop(self, session, player):
        start = datetime.utcnow()
        while (datetime.utcnow() - start).total_seconds() < 60:
            session = await self.session_update('pull', session)
            votes_needed = max(2, min(session.player_count // 4 + 1, 4))
            votes = len([x for x in session.preplayers if x.vote.start])
            if votes >= votes_needed or session.in_session or votes == 0:
                break
            await asyncio.sleep(0.1)
        else:
            session = await self.session_update('pull', session)
            for player in session.preplayers:
                player.vote.start = False
                session = await self.preplayer_update(session, player)
            await session.send(self.lg('start_idle'))




    async def use_in_channel(self, session, ctx):
        return await ctx.reply(self.lg('use_in_channel', command=ctx.invoked_with, channel=session.mention))

    def _autocomplete(self, string, lst):
        if string.lower() in [x.lower() for x in lst]:
            return [string], []
        else:
            choices = []
            for item in lst:
                if item.lower().startswith(string.lower()):
                    choices.append(item)
            close = difflib.get_close_matches(string.lower(), [x.lower() for x in lst], cutoff=0.5)
            close = [lst[[z.lower() for z in lst].index(x)] for x in close if x not in [y.lower() for y in choices]]
            return choices, close

    async def command_session_check(self, ctx, *, no_session=False, in_session=None, day=False, night=False, alive=None, in_dm=None, in_channel=False):
        session = self.find_session_player(ctx.author.id)
        if not session: return False, self.lg('no_session_user') if no_session else ''

        if in_session is not None:
            if session.in_session != in_session: return False, ''

        if day:
            if not session.day: return False, ''
        if night:
            if not session.night: return False, ''

        if alive is not None:
            player = self.get_player(session, ctx.author.id)
            if player.alive != alive: return False, ''

        if in_dm is not None:
            if not ctx.guild != in_dm: return (False, await self.use_in_channel(session, ctx))

        if in_channel:
            if session.channel.id != ctx.channel.id: return False, ''



def setup(bot):
    bot.add_cog(Game(bot))