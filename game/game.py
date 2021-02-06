import difflib
from datetime import datetime, timedelta
from discord.ext import commands

from engine import GameEngine
from .roles.player import Player

from .config import *


# TODO MUST
# votes
# kill
# see
# leave
# leave server
# retract and abstain
# wolfchat
# roles
# my role
# SEND ROLE INFO IMPORTANT
# force commands (stop, day/night, join, start, target, role, gamemode)
# temp testing bots
# command descriptions
# ---- other non priority
# more roles
# notify
# stasis
# clean up command checks (single function?)



class Game(commands.Cog, name="Game"):
    def __init__(self, bot):
        self.bot = bot
        self.engine = GameEngine(bot)

        self.lg = self.engine.lg
        self.session_update = self.engine.session_update
        self.player_update = self.engine.player_update
        self.preplayer_update = self.engine.preplayer_update

        self.pl = self.engine.pl
        self.s = self.engine.s
        self.get_name = self.engine.get_name

        self.bot.sessions = {}
        self.bot.sessiontasks = {}
        # self.wait_timer = datetime.utcnow()
        # self.wait_bucket = WAIT_BUCKET_INIT

        self.reveal_aliases = {
            'reveal': ['reveal', 'rev', 'rvl' 'r'],
            'noreveal': ['noreveal', 'norev', 'norvl', 'nrvl', 'nr']
        }


    @commands.command(aliases=['j'])
    async def join(self, ctx, gamemode = None):
        """Joins the game if it has not started yet. Votes for [<gamemode>] if it is given."""
        session = self.find_session_channel(ctx.channel.id)
        if not session: return await ctx.reply(self.lg('no_session'))

        # STASIS

        msg = self.player_join(session, ctx.author)
        await ctx.reply(msg)

        if gamemode:
            session = self.session_update('pull', session)
            self.vote_gamemode(session, gamemode)

    def player_join(self, session, user):
        newplayer = Player(user.id, user)
        session = self.session_update('pull', session)

        # multiple session support

        if user.id in [p.id for p in session.preplayers]:
            return self.lg('already_in')
        if session.player_count >= MAX_PLAYERS:
            return self.lg('max_players', max_players=MAX_PLAYERS)

        session.preplayers.append(newplayer)
        session.player_ids.append(user.id)

        session = self.session_update('push', session, ['_preplayers', 'player_ids'])

        if session.player_count == 1:
            sessiontasks = self.bot.sessiontasks[session.id]
            sessiontasks['wait_bucket'] = WAIT_BUCKET_INIT
            sessiontasks['wait_timer'] = datetime.utcnow() + timedelta(seconds=WAIT_AFTER_JOIN)
            sessiontasks['game_start_timeout_loop'] = self.bot.loop.create_task(self.game_start_timeout_loop(session))
            sessiontasks['wait_timer_loop'] = self.bot.loop.create_task(self.wait_timeout_loop(session))
            # lobby status waiting to start
            return self.lg('first_join', name=user.display_name, prefix=BOT_PREFIX)

        else:
            return self.lg('joined_game', name=user.display_name, count=session.player_count)

        # add role

        sessiontasks = self.bot.sessiontasks[session.id]
        sessiontasks['wait_timer'] = datetime.utcnow() + timedelta(seconds=WAIT_AFTER_JOIN)
        sessiontasks['idle'][user.id] = self.bot.loop.create_task(self.player_idle(session, user.id))


    @commands.command(aliases=['v'])
    async def vote(self, ctx, *, target = None):
        session = self.find_session_player(ctx.author.id)
        if not session: return await ctx.reply(self.lg('no_session_user'), prefix=BOT_PREFIX)

        if session.in_session:
            cmd = bot.get_command("lynch")
            await cmd(ctx, target)
            return

        if ctx.channel.id != session.id:
            return use_in_channel(session, ctx)

        if target is None:
            cmd = bot.get_command("votes")
            await cmd(ctx)

        if target.lower().replace(' ', '') in sum(list(self.reveal_aliases.values()), []):
            msg = self.vote_reveal(session, ctx.author.id, target)
        else:
            msg = self.vote_gamemode(session, ctx.author.id, target)
        await ctx.reply(msg)


    def vote_gamemode(self, session, player_id, gamemode):
        if session.gamemode:
            return self.lg('admin_set_gamemode')

        player = self.find_preplayer(session, player_id)

        choice, close = _autocomplete(gamemode, self.bot.gamemodes.keys())
        if len(choice) == 1:
            player.vote.gamemode = choice
            session = self.preplayer_update(session, player)
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

    def vote_reveal(self, session, player_id, choice):
        if session.reveal is not None:
            return self.lg('admin_set_reveal')

        player = self.find_preplayer(session, player_id)

        if choice in self.reveal_aliases['reveal']:
            player.vote.reveal = True
        else:
            player.vote.reveal = False

        session = self.preplayer_update(session, player)

        return self.lg('reveal_voted', _not='' if player.vote.reveal else 'not ')



    @commands.command()
    async def start(self, ctx):
        session = self.find_session_player(ctx.author.id)
        if not session: return await ctx.reply(self.lg('no_session_user', prefix=BOT_PREFIX))

        if ctx.channel.id != session.id:
            return await use_in_channel(session, ctx)

        if session.player_count < MIN_PLAYERS:
            return await ctx.reply(self.lg('min_player_start', min=MIN_PLAYERS))

        player = self.find_preplayer(session, ctx.author.id)
        if player.vote.start:
            return await ctx.reply(self.lg('already_voted'))

        if datetime.utcnow() < session.wait_timer:
            seconds = int(round(session.wait_timer - datetime.utcnow().total_seconds()))
            return await ctx.reply(self.lg('wait_start',
                seconds=seconds,
                pl=self.s(seconds)
            ))

        player.vote.start = True
        session = self.preplayer_update(session, player)

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
            sessiontasks['start_votes_loop'] = self.bot.loop.create_task(self.start_votes_loop(session))



    @commands.command()
    async def lynch(self, ctx, *, target = None):
        session = self.find_session_player(ctx.author.id)
        if not session: return

        if ctx.channel.is_private:
            return await self.use_in_channel(session, ctx)
        if session.channel.id != ctx.channel.id: return

        if session.night: return
        if (datetime.utcnow() - session.day_start).total_seconds <= 2: return

        if target is None:
            cmd = bot.get_command("votes")
            await cmd(ctx)
            return

        to_lynch = self.get_player(session, target.split(' ')[0])
        if not to_lynch:
            to_lynch = self.get_player(session, target)

        if not to_lynch:
            return await ctx.reply(self.lg('no_player_found', query=target))

        if not to_lynch.alive:
            return await ctx.reply(self.lg('player_dead', name=self.get_name(to_lynch)))

        session = self.session_update('pull', session)
        player = self.find_player(session, ctx.author.id)
        player.vote = to_lynch.id
        session = self.player_update(session, player)

        await ctx.reply(self.lg('lynch_vote', name=self.get_name(to_lynch)))
        # vote numbers or something?

        # log

    @commands.command(aliases=['r'])
    async def retract(self, ctx):
        session = self.find_session_player(ctx.author.id)
        if not session: return

        if not session.in_session:
            player = self.find_preplayer(session, ctx.author.id)

            if ctx.channel.is_private:
                return await ctx.reply(self.lg('use_in_channel', command=ctx.invoked_with, channel=session.mention))
            if session.channel.id != ctx.channel.id: return

            if not player.vote.gamemode and player.vote.reveal is None:
                return await ctx.reply(self.lg('no_vote', prefix=BOT_PREFIX))

            votecount = [bool(player.vote.gamemode), player.vote.reveal is not None].count(True)

            player.vote.gamemode = None
            player.vote.reveal = None
            session = self.player_update(session, player)

            return await ctx.reply(self.lg('retract_vote', pl=self.s(votecount)))

        elif session.in_session:
            player = self.find_player(session, ctx.author.id)

            if not player.alive: return

            if session.day:
                if ctx.channel.is_private:
                    return await self.use_in_channel(session, ctx)

                player.vote = None
                session = self.player_update(session, player)

                return await ctx.reply(self.lg('retract_vote', pl=''))

            else:
                if player.role in self.COMMANDS_FOR_ROLE['kill']:
                    if not ctx.channel.is_private:
                        try:
                            return await player.user.send(self.lg('use_in_dm', command=ctx.invoked_with))
                        except discord.Forbidden:
                            return

                    former_targets = player.targets[:]
                    player.targets = []
                    session = self.player_update(session, player)

                    await ctx.reply('retract_kill', pl=self.s(len(former_targets)))

                    # wolfchat
                    # log

    @commands.command(aliases=['abs'])
    async def abstain(self, ctx):
        session = self.find_session_player(ctx.author.id)
        if not session: return

        if not session.in_session or session.night: return

        player = self.get_player(session, ctx.author.id)
        if not player.alive: return

        if ctx.channel.is_private:
            return await self.use_in_channel(session, ctx)
        if session.channel.id != ctx.channel.id: return

        # evil village

        if session.day_start == timedelta(0):
            return ctx.reply(self.lg('abstain_first_day'))

        # injured

        player.vote = 'abstain'
        session = self.player_update(session, player)
        # log

        return ctx.reply(self.lg('abstain', name=self.get_name(player)))




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
        string = string.lower().replace(' ', '')
        string = string.strip('<@!>')
        name_dict = {
            'name': [],
            'discriminiator': [],
            'nick': [],
            'name_contains': [],
            'nick_contains': []
        }

        for player in session.players:
            if string in [player.name.lower(), player.nickname.lower(), str(player.id)]:
                return player
            if player.name.lower().replace(' ', '').startswith(string):
                name_dict['name'].append(player)
            if string.strip('#') == player.discriminiator:
                name_dict['discriminiator'].append(player)
            if player.nickname.lower().replace(' ', '').startswith(string):
                name_dict['nick'].append(player)
            if string in player.name.lower().replace(' ', ''):
                name_dict['name_contains'].append(player)
            if string in player.nickname.lower().replace(' ', ''):
                name_dict['nick_contains'].append(player)

        for k, v in name_dict.items():
            if len(v) == 1: return v[0]
        return None



    async def player_idle(self, session, player):
        while player in [x.id for x in session.preplayers] and not session.in_session:
            await asyncio.sleep(1)
            session = self.session_update('pull', session)

        while player in [x.id for x in session.preplayers] and session.in_session and self.find_player(session, player):
            if not self.find_player(session, player).alive: break

            def check(m):
                if m.author.id == player and m.channel.id == session.id:
                    return True
                session = self.session_update('pull', session)
                player = self.find_player(session, player)
                alive = player.alive if player else None
                return any([session.in_session, not alive, not player])

            try: msg = await self.bot.wait_for('on_message', timeout=PLAYER_TIMEOUT, check=check)
            except asyncio.TimeoutError: msg = None
            else: msg = None if msg.author.id != player or msg.channel.id != session.id else msg

            session = self.session_update('pull', session)
            if msg is None and self.find_player(session, player) and sesssion.in_session:
                p = self.find_player(session, player)
                if p.alive:
                    await session.send(self.lg('idle_lobby', mention=f"<@{player}>"))
                    await p.send(self.lg('idle_dm', channel=session.mention))

                    try: msg = await self.bot.wait_for('on_message', timeout=PLAYER_TIMEOUT2, check=check)
                    except asyncio.TimeoutError: msg = None
                    else: msg = None if msg.author.id != player or msg.channel.id != session.id else msg

                    session = self.session_update('pull', session)
                    if msg is None and self.find_player(session, player) and sesssion.in_session:
                        p = self.find_player(session, player)
                        if p.alive:
                            name = self.get_name(p)
                            if session.reveal:
                                await session.send(self.lg('idle', name=name, role=p.death_role))
                            else:
                                await session.send(self.lg('idle_no_reveal', name=name))

                            # STASIS

                            session = self.session_update('pull', session)
                            session = self.engine.player_death(session, p, 'idle', 'bot')

                            # TRAITOR

                            # log

                            session = self.session_update('push', session, ['players'])






    async def game_start_timeout_loop(self, session):
        session.first_join(datetime.utcnow())
        session = self.session_update('push', session, '_first_join')

        while not session.in_session and session.player_count and session.first_join < timedelta(seconds=GAME_START_TIMEOUT):
            await asyncio.sleep(0.1)
            session = self.session_update('pull', session)

        if not session.in_session and session.player_count:
            # lobby thingy
            await session.send(self.lg('start_timeout', players=' '.join([x.mention for x in session.preplayers]), prefix=BOT_PREFIX))
            # unlock lobby

            for player in session.preplayers:
                session = self.engine.player_death(session, player, 'game cancel', 'bot')

            session = self.session_update('push', session)

            self.engine.session_setup(session.channel)


    async def wait_timer_loop(self, session):
        timer = datetime.utcnow()
        while not session.in_session and session.player_count:
            if datetime.utcnow() - timer > timedelta(seconds=WAIT_BUCKET_DELAY):
                timer = datetime.utcnow()
                self.bot.sessiontasks[session.id]['wait_bucket'] = min(self.bot.sessiontasks[session.id]['wait_bucket'] + 1, WAIT_BUCKET_MAX)
            await asyncio.sleep(0.5)
            session = self.session_update('pull', session)



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




bot.add_cog(Game(bot))