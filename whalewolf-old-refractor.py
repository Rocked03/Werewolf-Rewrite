import discord
import asyncio
import aiohttp
import os
import random
import traceback
import sys
from datetime import datetime, timedelta
from io import BytesIO, StringIO
import config
# keep backwards compat for now
from config import *
from settings import *
import json
import urllib.request
from collections import OrderedDict
from itertools import chain

from adapters import DiscordAdapter
from constants import LobbyStatus

################## START INIT #####################
client = discord.Client()
# [playing?, {players dict}, day?, [night start, day start], [night elapsed, day elapsed], first join, gamemode, {original roles amount}]
session = [False, OrderedDict(), False, [0, 0], [timedelta(0), timedelta(0)], 0, '', {}]
first_notify = True
notify_previous = datetime.now()
ratelimit_dict = {}
pingif_dict = {}
notify_me = []
stasis = {}
commands = {}

wait_bucket = WAIT_BUCKET_INIT
wait_timer = datetime.now()
day_warning = DEFAULT_DAY_WARNING
day_timeout = DEFAULT_DAY_TIMEOUT
night_warning = DEFAULT_NIGHT_WARNING
night_timeout = DEFAULT_NIGHT_TIMEOUT

MAX_MESSAGE_LEN = 2000

faftergame = None
starttime = None

# External bot interface
adapter = DiscordAdapter(client, config)

with open(config.NOTIFY_FILE, 'a+') as notify_file:
    notify_file.seek(0)
    notify_me = notify_file.read().split(',')

if os.path.isfile(config.STASIS_FILE):
    with open(config.STASIS_FILE, 'r') as stasis_file:
        stasis = json.load(stasis_file)
else:
    with open(config.STASIS_FILE, 'a+') as stasis_file:
        stasis_file.write('{}')

random.seed(datetime.now())

def get_jsonparsed_data(url):
    try:
        response = urllib.request.urlopen(url)
    except urllib.error.HTTPError:
        return None, None # url does not exist
    data = response.read().decode("utf-8")
    return json.loads(data), data

def load_language(language):
    file = 'lang/{}.json'.format(language)
    if not os.path.isfile(file):
        file = 'lang/en.json'
        print("Could not find language file {}.json, fallback on en.json".format(language))
    with open(file, 'r', encoding='utf-8') as f:
        return json.load(f)

lang = load_language(config.MESSAGE_LANGUAGE)

def cmd(name, perms, description, *aliases):
    def real_decorator(func):
        commands[name] = [func, perms, description.format(config.BOT_PREFIX)]
        for alias in aliases:
            if alias not in commands:
                commands[alias] = [func, perms, "```\nAlias for {0}{1}.```".format(BOT_PREFIX, name)]
            else:
                print("ERROR: Cannot assign alias {0} to command {1} since it is already the name of a command!".format(alias, name))
        return func
    return real_decorator

################### END INIT ######################

@client.event
async def on_ready():
    global starttime
    print('Logged in as')
    print(client.user.name)
    print(client.user.id)
    print('------')
    await adapter.async_init()
    if starttime:
        await adapter.log(1, 'on_ready triggered again!')
    else:
        await adapter.log(1, 'on_ready triggered!')
    # [playing : True | False, players : {player id : [alive, role, action, template, other]}, day?, [datetime night, datetime day], [elapsed night, elapsed day], first join time, gamemode]
    starttime = datetime.now()

@client.event
async def on_resume():
    print("RESUMED")
    await adapter.log(1, "on_resume triggered!")

@client.event
async def on_message(message):
    if not starttime:
        return
    if message.author.id in [client.user.id] + IGNORE_LIST or not adapter.WEREWOLF_SERVER.get_member(message.author.id):
        if not (message.author.id in ADMINS or message.author.id == OWNER_ID):
            return
    if await rate_limit(message):
        return

    if message.channel.is_private:
        await adapter.log(0, 'pm from ' + message.author.name + ' (' + message.author.id + '): ' + message.content)
        if session[0] and message.author.id in session[1]:
            if session[1][message.author.id][1] in WOLFCHAT_ROLES and session[1][message.author.id][0]:
                if not message.content.strip().startswith(BOT_PREFIX):
                    await wolfchat(message)

    if message.content.strip().startswith(BOT_PREFIX):
        # command
        command = message.content.strip()[len(BOT_PREFIX):].lower().split(' ')[0]
        parameters = ' '.join(message.content.strip().lower().split(' ')[1:])
        if has_privileges(1, message) or message.channel.id == GAME_CHANNEL or message.channel.is_private:
            await parse_command(command, message, parameters)
    elif message.channel.is_private:
        command = message.content.strip().lower().split(' ')[0]
        parameters = ' '.join(message.content.strip().lower().split(' ')[1:])
        await parse_command(command, message, parameters)

@client.event
async def on_member_remove(member):
    member_id = member.id
    member_name = member.name
    if member_id in session[1]:
        leave_msg = ""
        if session[0] and session[1][member_id][0]:
            await player_deaths({member_id: ('fleave', "bot")})
            if session[6] == 'noreveal':
                leave_msg += "**" + member_name + "** left the server. Farewell.\n"
            else:
                leave_msg += "**" + member_name + "** left the server. Farewell **" + get_role(member_id, 'death') + "**.\n"
            if member_id in stasis:
                stasis[member_id] += QUIT_GAME_STASIS
            else:
                stasis[member_id] = QUIT_GAME_STASIS
            await adapter.send_lobby(leave_msg)
            await adapter.log(2, "{} ({}) was FLEAVED for leaving the server IN GAME".format(member_name, member_id))
            if win_condition() == None:
                await check_traitor()                
        elif not session[0]:
            await player_deaths({member_id: ('fleave', "bot")})
            leave_msg += "**" + member_name + "** left the server. Farewell.\nNew player count: **{}**".format(len(session[1]))
            await adapter.send_lobby(leave_msg)
            await adapter.log(2, "{} ({}) was FLEAVED for leaving the server OUT OF GAME".format(member_name, member_id))
        if len(session[1]) == 0:
            await adapter.set_lobby_status(LobbyStatus.READY)

############# COMMANDS #############
@cmd('shutdown', [2, 2], "```\n{0}shutdown takes no arguments\n\nShuts down the bot. Owner-only.```")
async def cmd_shutdown(message, parameters):
    if parameters.startswith("-fstop"):
        await cmd_fstop(message, "-force")
    elif parameters.startswith("-stop"):
        await cmd_fstop(message, parameters[len("-stop"):])
    elif parameters.startswith("-fleave"):
        await cmd_fleave(message, 'all')
    await adapter.reply(message, "Shutting down...", mentionauthor=True)
    await client.logout()

@cmd('ping', [0, 0], "```\n{0}ping takes no arguments\n\nTests the bot\'s responsiveness.```")
async def cmd_ping(message, parameters):
    msg = random.choice(lang['ping']).format(
        bot_nick=adapter.BOT_NAME, author=message.author.name, p=BOT_PREFIX)
    await adapter.reply(message, msg, mentionauthor=True)

@cmd('eval', [2, 2], "```\n{0}eval <evaluation string>\n\nEvaluates <evaluation string> using Python\'s eval() function and returns a result. Owner-only.```")
async def cmd_eval(message, parameters):
    output = None
    parameters = ' '.join(message.content.split(' ')[1:])
    if parameters == '':
        await adapter.reply(message, commands['eval'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    try:
        output = eval(parameters)
    except:
        await adapter.reply(message, '```\n' + str(traceback.format_exc()) + '\n```', mentionauthor=True)
        traceback.print_exc()
        return
    if asyncio.iscoroutine(output):
        output = await output
    await adapter.reply(message, '```py\n' + str(output) + '\n```', cleanmessage=False, mentionauthor=True)

@cmd('exec', [2, 2], "```\n{0}exec <exec string>\n\nExecutes <exec string> using Python\'s exec() function. Owner-only.```")
async def cmd_exec(message, parameters):
    parameters = ' '.join(message.content.split(' ')[1:])
    if parameters == '':
        await adapter.reply(message, commands['exec'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    old_stdout = sys.stdout
    redirected_output = sys.stdout = StringIO()
    try:
        exec(parameters)
    except Exception:
        await adapter.reply(message, '```py\n{}\n```'.format(traceback.format_exc()), mentionauthor=True)
        return
    finally:
        sys.stdout = old_stdout
    output = str(redirected_output.getvalue())
    if output == '':
        output = ":thumbsup:"
    await adapter.reply(message, output)

@cmd('async', [2, 2], "```\n{0}async <code>\n\nExecutes <code> as a coroutine.```")
async def cmd_async(message, parameters, recursion=0):
    if parameters == '':
        await adapter.reply(message, commands['async'][2].format(PREFIX), mentionauthor=True)
        return
    env = {'message' : message,
           'parameters' : parameters,
           'recursion' : recursion,
           'client' : client,
           'channel' : message.channel,
           'author' : message.author,
           'server' : message.server}
    env.update(globals())
    old_stdout = sys.stdout
    redirected_output = sys.stdout = StringIO()
    result = None
    exec_string = "async def _temp_exec():\n"
    exec_string += '\n'.join(' ' * 4 + line for line in parameters.split('\n'))
    try:
        exec(exec_string, env)
    except Exception:
        traceback.print_exc()
        result = traceback.format_exc()
    else:
        _temp_exec = env['_temp_exec']
        try:
            returnval = await _temp_exec()
            value = redirected_output.getvalue()
            if returnval == None:
                result = value
            else:
                result = value + '\n' + str(returnval)
        except Exception:
            traceback.print_exc()
            result = traceback.format_exc()
    finally:
        sys.stdout = old_stdout
    await adapter.reply(message, "```py\n{}\n```".format(result))

@cmd('help', [0, 0], "```\n{0}help <command>\n\nReturns hopefully helpful information on <command>. Try {0}list for a listing of commands.```")
async def cmd_help(message, parameters):
    if parameters == '':
        parameters = 'help'
    if parameters in commands:
        await adapter.reply(message, commands[parameters][2].format(BOT_PREFIX), mentionauthor=True)
    else:
        await adapter.reply(message, 'No help found for command ' + parameters, mentionauthor=True)

@cmd('list', [0, 0], "```\n{0}list takes no arguments\n\nDisplays a listing of commands. Try {0}help <command> for help regarding a specific command.```")
async def cmd_list(message, parameters):
    cmdlist = []
    for key in commands:
        if message.channel.is_private:
            if has_privileges(commands[key][1][1], message):
                cmdlist.append(key)
        else:
            if has_privileges(commands[key][1][0], message):
                cmdlist.append(key)
    await adapter.reply(message, "Available commands: {}".format(", ".join(sorted(cmdlist))), mentionauthor=True)

@cmd('join', [0, 1], "```\n{0}join [<gamemode>]\n\nJoins the game if it has not started yet. Votes for [<gamemode>] if it is given.```", 'j')
async def cmd_join(message, parameters):
    global wait_timer # ugh globals
    global wait_bucket
    if session[0]:
        return
    if message.author.id in stasis and stasis[message.author.id] > 0:
        await adapter.reply(message, "You are in stasis for **{}** game{}. Please do not break rules, idle out or use !leave during a game.".format(
                                stasis[message.author.id], '' if stasis[message.author.id] == 1 else 's'), mentionauthor=True)
        return
    if len(session[1]) >= MAX_PLAYERS:
        await adapter.reply(message, random.choice(lang['maxplayers']).format(MAX_PLAYERS), mentionauthor=True)
        return
    if message.author.id in session[1]:
        await adapter.reply(message, random.choice(lang['alreadyin']).format(message.author.name), mentionauthor=True)
    else:
        session[1][message.author.id] = [True, '', '', [], []]
        # alive, ?, ?, blessed/cursed, totems
        if len(session[1]) == 1:
            wait_bucket = WAIT_BUCKET_INIT
            wait_timer = datetime.now() + timedelta(seconds=WAIT_AFTER_JOIN)
            client.loop.create_task(game_start_timeout_loop())
            client.loop.create_task(wait_timer_loop())
            await adapter.set_lobby_status(LobbyStatus.WAITING_TO_START)
            await adapter.send_lobby(random.choice(lang['gamestart']).format(
                                            message.author.name, p=BOT_PREFIX))
        else:
            await adapter.reply(message, "**{}** joined the game and raised the number of players to **{}**.".format(
                                                        message.author.name, len(session[1])))
        if parameters:
            await cmd_vote(message, parameters)
        #                            alive, role, action, [templates], [other]
        await adapter.add_player_role(message.author.id)
        wait_timer = datetime.now() + timedelta(seconds=WAIT_AFTER_JOIN)
        client.loop.create_task(player_idle(message))

@cmd('leave', [0, 1], "```\n{0}leave takes no arguments\n\nLeaves the current game. If you need to leave, please do it before the game starts.```", 'q')
async def cmd_leave(message, parameters):
    if session[0] and message.author.id in session[1] and session[1][message.author.id][0]:
        if parameters != '-force':
            msg = await adapter.reply(message, "Are you sure you want to quit during game? Doing "
                                                "so will result in {} games of stasis. You may bypass "
                                                "this confirmation by using `{}leave -force`.".format(
                                                    QUIT_GAME_STASIS, BOT_PREFIX))
            def check(m):
                c = m.content.lower()
                return c in ['yes', 'y', 'no', 'n']
            response = await adapter.wait_for_message(author=message.author, channel=message.channel, timeout=5, check=check)
            await adapter.delete_message(msg)
            if not response or response.content.lower() not in ['yes', 'y']:
                return
        if not session[1][message.author.id][0]:
            # prevent race condition where user runs this command multiple times and then says "yes"
            return
        if session[6] == 'noreveal':
            await adapter.send_lobby(random.choice(lang['leavedeathnoreveal']).format(message.author.name))       
        else:
            await adapter.send_lobby(random.choice(lang['leavedeath']).format(
                message.author.name, get_role(message.author.id, 'death')))
        await player_deaths({message.author.id : ('leave', "bot")})
        if message.author.id in stasis:
            stasis[message.author.id] += QUIT_GAME_STASIS
        else:
            stasis[message.author.id] = QUIT_GAME_STASIS
        if session[0] and win_condition() == None:
            await check_traitor()
        await adapter.log(1, "{} ({}) QUIT DURING GAME".format(message.author.display_name, message.author.id))
    else:
        if message.author.id in session[1]:
            if session[0]:
                await adapter.reply(message, "wot?", mentionauthor=True)
                return
            await player_deaths({message.author.id : ('leave', "bot")})
            await adapter.send_lobby(random.choice(lang['leavelobby']).format(message.author.name, len(session[1])))
            if len(session[1]) == 0:
                await adapter.set_lobby_status(LobbyStatus.READY)
        else:
            await adapter.reply(message, random.choice(lang['notplayingleave']), mentionauthor=True)

@cmd('wait', [0, 1], "```\n{0}wait takes no arguments\n\nIncreases the wait time until {0}start may be used.```", 'w')
async def cmd_wait(message, parameters):
    global wait_bucket
    global wait_timer
    if session[0] or message.author.id not in session[1]:
        return
    if wait_bucket <= 0:
        wait_bucket = 0
        await adapter.reply(message, "That command is ratelimited.", mentionauthor=True)
    else:
        wait_timer = max(datetime.now() + timedelta(seconds=EXTRA_WAIT), wait_timer + timedelta(seconds=EXTRA_WAIT))
        wait_bucket -= 1
        await adapter.send_lobby("**{}** increased the wait time by {} seconds.".format(message.author.name, EXTRA_WAIT))

@cmd('fjoin', [1, 1], "```\n{0}fjoin <mentions of users>\n\nForces each <mention> to join the game.```")
async def cmd_fjoin(message, parameters):
    if session[0]:
        return
    if parameters == '':
        await adapter.reply(message, commands['fjoin'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    raw_members = parameters.split(' ')
    join_list = []
    for member in raw_members:
        if member.strip('<!@>').isdigit():
            join_list.append(member.strip('<!@>'))
        elif '-' in member:
            left = member.split('-')[0]
            right = member.split('-')[1]
            if left.isdigit() and right.isdigit():
                join_list += list(map(str, range(int(left), int(right) + 1)))
    if join_list == []:
        await adapter.reply(message, "ERROR: no valid mentions found", mentionauthor=True)
        return
    join_msg = ""
    for member in sort_players(join_list):
        session[1][member] = [True, '', '', [], []]
        join_msg += "**" + get_name(member) + "** was forced to join the game.\n"
        await adapter.add_player_role(member)
    join_msg += "New player count: **{}**".format(len(session[1]))
    if len(session[1]) > 0:
        await adapter.set_lobby_status(LobbyStatus.WAITING_TO_START)
    await adapter.reply(message, join_msg)
    await adapter.log(2, "{0} ({1}) used FJOIN {2}".format(message.author.name, message.author.id, parameters))

@cmd('fleave', [1, 1], "```\n{0}fleave <mentions of users | all>\n\nForces each <mention> to leave the game. If the parameter is all, removes all players from the game.```")
async def cmd_fleave(message, parameters):
    if parameters == '':
        await adapter.reply(message, commands['fleave'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    raw_members = parameters.split(' ')
    leave_list = []
    if parameters == 'all':
        leave_list = list(session[1])
    else:
        for member in raw_members:
            if member.strip('<!@>').isdigit():
                leave_list.append(member.strip('<!@>'))
            elif '-' in member:
                left = member.split('-')[0]
                right = member.split('-')[1]
                if left.isdigit() and right.isdigit():
                    leave_list += list(map(str, range(int(left), int(right) + 1)))
    if leave_list == []:
        await adapter.reply(message, "ERROR: no valid mentions found", mentionauthor=True)
        return
    leave_msg = ""

    for member in sort_players(leave_list):
        if member in list(session[1]):
            if session[0]:
                if session[6] == 'noreveal':
                    leave_msg += "**" + get_name(member) + "** was forcibly shoved into a fire. The air smells of freshly burnt flesh.\n"
                else:
                    leave_msg += "**" + get_name(member) + "** was forcibly shoved into a fire. The air smells of freshly burnt **" + get_role(member, 'death') + "**.\n"
            else:
                leave_msg += "**" + get_name(member) + "** was forced to leave the game.\n"
    leave_dict = {}
    for p in [x for x in sort_players(leave_list) if x in session[1]]:
        leave_dict[p] = ('fleave', "bot")
    await player_deaths(leave_dict)
    if not session[0]:
        leave_msg += "New player count: **{}**".format(len(session[1]))
    await adapter.send_lobby(leave_msg)
    await adapter.log(2, "{0} ({1}) used FLEAVE {2}".format(message.author.name, message.author.id, parameters))
    if session[0] and win_condition() == None:
        await check_traitor()
    if len(session[1]) == 0:
        await adapter.set_lobby_status(LobbyStatus.READY)

@cmd('refresh', [1, 1], "```\n{0}refresh [<language file>]\n\nRefreshes the current language's language file from GitHub. Admin only.```")
async def cmd_refresh(message, parameters):
    global lang
    if parameters == '':
        parameters = MESSAGE_LANGUAGE
    url = "https://raw.githubusercontent.com/belguawhale/Discord-Werewolf/master/lang/{}.json".format(parameters)
    codeset = parameters
    temp_lang, temp_str = get_jsonparsed_data(url)
    if not temp_lang:
        await adapter.reply(message, "Could not refresh language {} from Github.".format(parameters), mentionauthor=True)
        return
    with open('lang/{}.json'.format(parameters), 'w', encoding='utf-8') as f:
        f.write(temp_str)
    lang = temp_lang
    await adapter.reply(message, 'The messages with language code `' + codeset + '` have been refreshed from GitHub.', mentionauthor=True)

@cmd('start', [0, 1], "```\n{0}start takes no arguments\n\nVotes to start the game. A game needs at least " +\
                      str(MIN_PLAYERS) + " players to start.```")
async def cmd_start(message, parameters):
    if session[0]:
        return
    if message.author.id not in session[1]:
        await adapter.reply(message, random.choice(lang['notplayingstart']).format(p=BOT_PREFIX), mentionauthor=True)
        return
    if len(session[1]) < MIN_PLAYERS:
        await adapter.reply(message, random.choice(lang['minplayers']).format(MIN_PLAYERS), mentionauthor=True)
        return
    if session[1][message.author.id][1]:
        return
    if datetime.now() < wait_timer:
        await adapter.reply(message, "Please wait at least {} more second{}.".format(
            int((wait_timer - datetime.now()).total_seconds()), '' if int((wait_timer - datetime.now()).total_seconds()) == 1 else 's'), mentionauthor=True)
        return
    session[1][message.author.id][1] = 'start'
    votes = len([x for x in session[1] if session[1][x][1] == 'start'])
    votes_needed = max(2, min(len(session[1]) // 4 + 1, 4))
    if votes < votes_needed:
        await adapter.send_lobby("**{}** has voted to start the game. **{}** more vote{} needed.".format(
            message.author.display_name, votes_needed - votes, '' if (votes_needed - votes == 1) else 's'))
    else:
        await run_game()
    if votes == 1:
        await start_votes(message.author.id)

@cmd('fstart', [1, 2], "```\n{0}fstart takes no arguments\n\nForces game to start.```")
async def cmd_fstart(message, parameters):
    if session[0]:
        return
    if len(session[1]) < MIN_PLAYERS:
        await adapter.reply(message, random.choice(lang['minplayers']).format(MIN_PLAYERS), mentionauthor=True)
    else:
        await adapter.send_lobby("**" + message.author.name + "** forced the game to start.")
        await adapter.log(2, "{0} ({1}) FSTART".format(message.author.name, message.author.id))
        await run_game()

@cmd('fstop', [1, 1], "```\n{0}fstop [<-force|reason>]\n\nForcibly stops the current game with an optional [<reason>]. Use {0}fstop -force if "
                      "bot errors.```")
async def cmd_fstop(message, parameters):
    msg = "Game forcibly stopped by **" + message.author.name + "**"
    if parameters == "":
        msg += "."
    elif parameters == "-force":
        if not session[0]:
            await adapter.reply(message, "There is no currently running game!", mentionauthor=True)
            return
        msg += ". Here is some debugging info:\n```py\n{0}\n```".format(str(session))
        session[0] = False
        await adapter.unlock_lobby()
        session[3] = [datetime.now(), datetime.now()]
        session[4] = [timedelta(0), timedelta(0)]
        session[6] = ''
        session[7] = {}
        await adapter.send_lobby(msg)
        player_dict = {}
        for player in list(session[1]):
            player_dict[player] = ('fstop', "bot")
        await player_deaths(player_dict)
        return
    else:
        msg += " for reason: `" + parameters + "`."

    if not session[0]:
        await adapter.reply(message, "There is no currently running game!", mentionauthor=True)
        return
    else:
        await adapter.log(2, "{0} ({1}) FSTOP {2}".format(message.author.name, message.author.id, parameters))
    await end_game(msg + '\n\n' + end_game_stats())

@cmd('sync', [1, 1], "```\n{0}sync takes no arguments\n\nSynchronizes all player roles and channel permissions with session.```")
async def cmd_sync(message, parameters):
    for member in adapter.WEREWOLF_SERVER.members:
        should_have_role = member.id in session[1] and session[1][member.id][0]
        if await adapter.has_player_role(member.id) and not should_have_role:
            await adapter.remove_player_role(member.id)
        elif not await adapter.has_player_role(member.id) and should_have_role:
            await adapter.add_player_role(member.id)
    if session[0]:
        await adapter.lock_lobby()
    else:
        await adapter.unlock_lobby()
    await adapter.log(2, "{0} ({1}) SYNC".format(message.author.name, message.author.id))
    await adapter.reply(message, "Sync successful.", mentionauthor=True)

@cmd('op', [1, 1], "```\n{0}op takes no arguments\n\nOps yourself if you are an admin```")
async def cmd_op(message, parameters):
    await adapter.log(2, "{0} ({1}) OP {2}".format(message.author.name, message.author.id, parameters))
    if parameters == "":
        await adapter.add_admin_role(message.author.id)
        await adapter.reply(message, ":thumbsup:", mentionauthor=True)
    else:
        member = adapter.WEREWOLF_SERVER.get_member(parameters.strip("<!@>"))
        if member and member.id in ADMINS:
            await adapter.add_admin_role(member.id)
            await adapter.reply(message, ":thumbsup:", mentionauthor=True)

@cmd('deop', [1, 1], "```\n{0}deop takes no arguments\n\nDeops yourself so you can play with the players ;)```")
async def cmd_deop(message, parameters):
    await adapter.log(2, "{0} ({1}) DEOP {2}".format(message.author.name, message.author.id, parameters))
    if parameters == "":
        await adapter.remove_admin_role(message.author.id)
        await adapter.reply(message, ":thumbsup:", mentionauthor=True)
    else:
        member = adapter.WEREWOLF_SERVER.get_member(parameters.strip("<!@>"))
        if member and member.id in ADMINS:
            await adapter.remove_admin_role(member.id)
            await adapter.reply(message, ":thumbsup:", mentionauthor=True)

@cmd('role', [0, 0], "```\n{0}role [<role | number of players | gamemode>] [<number of players>]\n\nIf a <role> is given, "
                     "displays a description of <role>. If a <number of players> is given, displays the quantity of each "
                     "role for the specified <number of players> for the specified <gamemode>, defaulting to default. If "
                     "only a <gamemode> is given, displays a role guide for <gamemode>. "
                     "If left blank, displays a list of roles.```", 'roles')
async def cmd_role(message, parameters):
    if parameters == "" and not session[0] or parameters == 'list':
        roles_message = ''
        roles_message += "\n```ini\n[Village Team] " + ", ".join(sort_roles(VILLAGE_ROLES_ORDERED))
        roles_message += "\n[Wolf Team] " + ", ".join(sort_roles(WOLF_ROLES_ORDERED))
        roles_message += "\n[Neutrals] " + ", ".join(sort_roles(NEUTRAL_ROLES_ORDERED))
        roles_message += "\n[Templates] " + ", ".join(sort_roles(TEMPLATES_ORDERED)) + "```"
        await adapter.reply(message, roles_message, mentionauthor=True)
        return
    elif parameters == "" and session[0]:
        msg = "**{}** players playing **{}** gamemode:```\n".format(len(session[1]),
        'roles' if session[6].startswith('roles') else session[6])
        if session[6] in ('random',):
            msg += "!role is disabled for the {} gamemode.\n```".format(session[6])
            await adapter.reply(message, msg, mentionauthor=True)
            return

        game_roles = dict(session[7])

        msg += '\n'.join(["{}: {}".format(x, game_roles[x]) for x in sort_roles(game_roles)])
        msg += '```'
        await adapter.reply(message, msg, mentionauthor=True)
        return
    elif _autocomplete(parameters, roles)[1] == 1:
        role = _autocomplete(parameters, roles)[0]
        await adapter.reply(message, "<https://werewolf.miraheze.org/wiki/{}>\n```\nRole name: {}\nTeam: {}\nDescription: {}\n```".format(
            role + "_(role)" if role == "lycan" else role.replace(' ', '_'), role, roles[role][0], roles[role][2]), mentionauthor=True)
        return
    params = parameters.split(' ')
    gamemode = 'default'
    num_players = -1
    choice, num = _autocomplete(params[0], gamemodes)
    if num == 1:
        gamemode = choice

    if params[0].isdigit():
        num_players = params[0]
    elif len(params) == 2 and params[1].isdigit():
        num_players = params[1]
    if num_players == -1:
        if len(params) == 2:
            if params[1] == 'table':
                # generate role table
                WIDTH = 20
                role_dict = dict()
                for role in gamemodes[gamemode]['roles']:
                    if max(gamemodes[gamemode]['roles'][role]):
                        role_dict.update({role : gamemodes[gamemode]['roles'][role]})
                role_guide = "Role table for gamemode **{}**:\n".format(gamemode)
                role_guide += "```\n" + " " * (WIDTH + 2)
                role_guide += ','.join("{}{}".format(' ' * (2 - len(str(x))), x) for x in range(gamemodes[gamemode]['min_players'], gamemodes[gamemode]['max_players'] + 1)) + '\n'
                role_guide += '\n'.join(role + ' ' * (WIDTH - len(role)) + ": " + repr(
                    role_dict[role][gamemodes[gamemode]['min_players'] - MIN_PLAYERS:gamemodes[gamemode]['max_players']]) for role in sort_roles(role_dict))
                role_guide += "\n```"
            elif params[1] == 'guide':
                # generate role guide
                role_dict = gamemodes[gamemode]['roles']
                prev_dict = dict((x, 0) for x in roles if x != 'villager')
                role_guide = 'Role guide for gamemode **{}**:\n'.format(gamemode)
                for i in range(gamemodes[gamemode]['max_players'] - MIN_PLAYERS + 1):
                    current_dict = {}
                    for role in sort_roles(roles):
                        if role == 'villager':
                            continue
                        if role in role_dict:
                            current_dict[role] = role_dict[role][i]
                        else:
                            current_dict[role] = 0
                    # compare previous and current
                    if current_dict == prev_dict:
                        # same
                        continue
                    role_guide += '**[{}]** '.format(i + MIN_PLAYERS)
                    for role in sort_roles(roles):
                        if role == 'villager':
                            continue
                        if current_dict[role] == 0 and prev_dict[role] == 0:
                            # role not in gamemode
                            continue
                        if current_dict[role] > prev_dict[role]:
                            # role increased
                            role_guide += role
                            if current_dict[role] > 1:
                                role_guide += " ({})".format(current_dict[role])
                            role_guide += ', '
                        elif prev_dict[role] > current_dict[role]:
                            role_guide += '~~{}'.format(role)
                            if prev_dict[role] > 1:
                                role_guide += " ({})".format(prev_dict[role])
                            role_guide += '~~, '
                    role_guide = role_guide.rstrip(', ') + '\n'
                    # makes a copy
                    prev_dict = dict(current_dict)
            else:
                role_guide = "Please choose one of the following: " + ', '.join(['guide', 'table'])
        else:
            role_guide = "Please choose one of the following for the third parameter: {}".format(', '.join(['guide', 'table']))
        await adapter.reply(message, role_guide, mentionauthor=True)
    else:
        num_players = int(num_players)
        if num_players in range(gamemodes[gamemode]['min_players'], gamemodes[gamemode]['max_players'] + 1):
            if gamemode in ('random',):
                msg = "!role is disabled for the **{}** gamemode.".format(gamemode)
            else:
                msg = "Roles for **{}** players in gamemode **{}**:```\n".format(num_players, gamemode)
                game_roles = get_roles(gamemode, num_players)
                msg += '\n'.join("{}: {}".format(x, game_roles[x]) for x in sort_roles(game_roles))
                msg += '```'
            await adapter.reply(message, msg, mentionauthor=True)
        else:
            await adapter.reply(message, "Please choose a number of players between " + str(gamemodes[gamemode]['min_players'], mentionauthor=True) +\
            " and " + str(gamemodes[gamemode]['max_players']) + ".")

async def _send_role_info(player, sendrole=True):
    if session[0] and player in session[1]:
        if session[1][player][0]:
            role = get_role(player, 'role') if get_role(player, 'role') not in ['amnesiac', 'vengeful ghost', 'time lord'] else "villager"
            templates = get_role(player, 'templates')
            try:
                if sendrole:
                    await adapter.send_user(player, "Your role is **{0}**. {1}\n".format(role, roles[role][2]), Raise=True)
                msg = []
                living_players = [x for x in session[1] if session[1][x][0]]
                living_players_string = ['{} ({})'.format(get_name(x), x) for x in living_players]
                if role in COMMANDS_FOR_ROLE['kill'] and roles[role][0] == 'wolf':
                    if 'angry' in session[1][player][4]:
                        num_kills = session[1][player][4].count('angry')
                        msg.append("You are **angry** tonight, and may kill {} targets by using `kill {}`.\n".format(
                            num_kills + 1, ' AND '.join('player' + str(x + 1) for x in range(num_kills + 1))))
                if roles[role][0] == 'wolf' and role != 'cultist' and (role != 'minion' or str(session[4][1]) == "0:00:00") or role == 'minion' and sendrole:
                    living_players_string = []
                    for plr in living_players:
                        temprole = get_role(plr, 'role')
                        temptemplates = get_role(plr, 'templates')
                        role_string = []
                        if 'cursed' in temptemplates and role != 'minion':
                            role_string.append('cursed')
                        if roles[temprole][0] == 'wolf' and temprole not in ['minion', 'cultist']:
                            role_string.append(temprole)
                        living_players_string.append("{} ({}){}".format(get_name(plr), plr,
                        ' ({})'.format(' '.join(role_string)) if role_string else ''))
                if role == 'succubus':
                    living_players_string = []
                    for plr in living_players:
                        temprole = get_role(plr, 'role')
                        role_string = []
                        if 'entranced' in session[1][plr][4]:
                            role_string.append('entranced')
                        if temprole == 'succubus':
                            role_string.append(temprole)
                        living_players_string.append("{} ({}){}".format(get_name(plr), plr,
                        ' ({})'.format(' '.join(role_string)) if role_string else ''))
                if role == 'piper':
                    living_players_string = []
                    for plr in living_players:
                        temprole = get_role(plr, 'role')
                        role_string = []
                        if 'charmed' in session[1][plr][4]:
                            role_string.append('charmed')
                        if temprole == 'piper':
                            role_string.append(temprole)
                        living_players_string.append("{} ({}){}".format(get_name(plr), plr,
                        ' ({})'.format(' '.join(role_string)) if role_string else ''))
                if role == 'executioner' and sendrole:
                    if [x for x in session[1][player][4] if x.startswith('execute:')]:
                        exe_target = [x for x in session[1][player][4] if x.startswith('execute:')][0].strip('execute:')
                        if 'win' in session[1][player][4]:
                            msg.append('Your target was **{}**. This player was lynched, so you won.'.format(get_name(exe_target)))
                        else:
                            msg.append('Your target for lynch is **{}**.'.format(get_name(exe_target)))
                    else:
                        if [x for x in living_players if get_role(x, 'actualteam') == 'village']:
                            exe_target = random.choice([x for x in living_players if get_role(x, 'actualteam') == 'village'])
                            session[1][player][4].append('execute:{}'.format(exe_target))
                            msg.append('Your target for lynch is **{}**.'.format(get_name(exe_target)))
                        else:
                            session[1][player][1] = 'jester'
                            session[1][player][4].append('executioner')
                            await adapter.send_user(player, 'There are no available targets. You have now become a **jester**.\nYour role is **jester**. ' + roles['jester'][2] + '\n')
                if role in ['shaman', 'wolf shaman']:
                    totem = ''
                    if session[1][player][2] in totems:
                        totem = session[1][player][2]
                    elif [x for x in session[1][player][4] if x.startswith("totem:")]:
                        totem = [x.split(':')[1] for x in session[1][player][4] if x.startswith("totem:")].pop()
                    if totem:
                        msg.append("You have the **{}**. {}\n".format(totem.replace('_', ' '), totems[totem]))
                if role == 'clone' and sendrole:
                    if [x for x in session[1][player][4] if x.startswith('clone:')]:
                        msg.append('You are cloning **{}**.'.format(get_name([x.split(':')[1] for x in session[1][player][4] if x.startswith('clone:')].pop())))
                    else:
                        msg.append("Living players: ```basic\n" + '\n'.join(living_players_string) + '\n```')
                if role in ['wolf', 'werecrow', 'doomsayer', 'wolf cub', 'werekitten', 'wolf shaman', 'wolf mystic', 'traitor', 'sorcerer', 'seer',
                            'oracle', 'shaman', 'harlot', 'hunter', 'augur', 'detective', 'guardian angel',
                            'crazed shaman', 'succubus', 'hag', 'piper', 'bodyguard', 'warlock', 'serial killer', 'hot potato']:
                    msg.append("Living players: ```basic\n" + '\n'.join(living_players_string) + '\n```')
                #mystic stuff/wolf mystic stuff
                if role == 'mystic':
                    wolfcount = 0
                    for player in session[1]:
                        if get_role(player, 'actualteam') == 'wolf' and session[1][player][0]:
                            wolfcount += 1
                    if "silence_totem2" in session[1][player][4]:
                        await adapter.send_user(player, "You are silenced and unable to sense anything of significance.".format(wolfcount))
                    else:
                        await adapter.send_user(player, "You sense that there are **{}** wolves.".format(wolfcount))
                if role == 'wolf mystic':
                    vilcount = 0
                    for player in session[1]:
                        if ((get_role(player, 'actualteam') == 'village' and get_role(player, 'role') != 'villager') or get_role(player, 'role') in ['fool', 'monster', 'succubus', 'piper', 'demoniac', 'serial killer']) and session[1][player][0]:
                            vilcount += 1
                    if "silence_totem2" in session[1][player][4]:
                        await adapter.send_user(player, "You are silenced and unable to sense anything of significance.".format(wolfcount))
                    else:
                        await adapter.send_user(player, "You sense that there are **{}** villagers.".format(vilcount))
                #turncoat being told when they can turn
                if role == 'turncoat' and 'sided2' not in session[1][player][4]:
                    await adapter.send_user(player, "You can switch sides tonight.")
                if 'gunner' in templates and (sendrole or session[1][player][4].count('bullet') > 0 or 'gunnotify' in session[1][player][4]):
                    msg.append("You have a gun and **{}** bullet{}. Use the command "
                               "`{}role gunner` for more information.".format(
                        session[1][player][4].count('bullet'), '' if session[1][player][4].count('bullet') == 1 else 's',
                        BOT_PREFIX))
                    if 'gunnotify' in session[1][player][4]:
                        session[1][player][4].remove('gunnotify')
                if 'sharpshooter' in templates and (sendrole or session[1][player][4].count('bullet') > 0):
                    msg.append("You have a gun and **{}** bullet{}. Use the command "
                               "`{}role sharpshooter` for more information.".format(
                        session[1][player][4].count('bullet'), '' if session[1][player][4].count('bullet') == 1 else 's',
                        BOT_PREFIX))
                if 'assassin' in templates and sendrole:
                    target = ""
                    for o in session[1][player][4]:
                        if o.startswith("assassinate:"):
                            target = o.split(":")[1]
                    if target:
                        if role == 'village drunk':
                            msg.append("In your drunken stupor you have selected **{0}** as your target. Use the command `{1}role assassin` for more information.".format(get_name(target), BOT_PREFIX))
                        else:
                            msg.append("Your target is **{0}**. Use the command `{1}role assassin` "
                               "for more information.".format(get_name(target), BOT_PREFIX))
                    else:
                        msg.append("You are an **assassin**, and wish to spread chaos. Type `target <player>` to make them your target. If you die, you take them with you, but if they die, you may choose another target.\nLiving players: ```basic\n" + '\n'.join(living_players_string) + '\n```')
                if role == 'matchmaker' and sendrole:
                    msg.append("Living players: ```basic\n" + '\n'.join(living_players_string) + '\n```')
                if role == 'minion' and (str(session[4][1]) == "0:00:00" or sendrole):
                    msg.append("Living players: ```basic\n" + '\n'.join(living_players_string) + '\n```')
                if msg:
                    await adapter.send_user(player, '\n'.join(msg), Raise=True)
            except discord.Forbidden:
                await adapter.send_lobby("<@{0}>, Please make sure you have enabled `Allow direct messages from server members` under Privacy Settings for this server so I can pm your role. "
                                         "After that, you may use the command `{1}myrole` to resend your role.".format(player, config.BOT_PREFIX))
        elif get_role(player, 'role') == 'vengeful ghost' and [x for x in session[1][player][4] if x.startswith("vengeance:")]:
            against = 'wolf'
            if [x for x in session[1][player][4] if x.startswith("vengeance:")]:
                against = [x.split(':')[1] for x in session[1][player][4] if x.startswith('vengeance:')].pop()
            await adapter.send_user(player, "You are a **vengeful ghost**, sworn to take revenge on the {0} that you believe killed you. You must kill one of them with `kill <player>` tonight. If you do not, one of them will be selected at random.".format('wolves' if against == 'wolf' else 'villagers'))
            living_players = [x for x in session[1] if session[1][x][0] if roles[get_role(x, "role")][0] == against]
            living_players_string = ['{} ({})'.format(get_name(x), x) for x in living_players]
            await adapter.send_user(player, "Living players: ```basic\n" + '\n'.join(living_players_string) + '\n```')
        
@cmd('myrole', [0, 0], "```\n{0}myrole takes no arguments\n\nTells you your role in pm.```")
async def cmd_myrole(message, parameters):
    await _send_role_info(message.author.id)

@cmd('stats', [0, 0], "```\n{0}stats takes no arguments\n\nLists current players in the lobby during the join phase, and lists game information in-game.```")
async def cmd_stats(message, parameters):
    #TODO: rewrite
    if session[0]:
        reply_msg = "It is now **" + ("day" if session[2] else "night") + "time**. Using the **{}** gamemode.".format(
            'roles' if session[6].startswith('roles') else session[6])
        reply_msg += "\n**" + str(len(session[1])) + "** players playing: **" + str(len([x for x in session[1] if session[1][x][0]])) + "** alive, "
        reply_msg += "**" + str(len([x for x in session[1] if not session[1][x][0]])) + "** dead\n"
        reply_msg += "```basic\nLiving players:\n" + "\n".join(get_name(x) + ' (' + x + ')' for x in session[1] if session[1][x][0]) + '\n'
        reply_msg += "Dead players:\n" + "\n".join(get_name(x) + ' (' + x + ')' for x in session[1] if not session[1][x][0]) + '\n'

        if session[6] in ('random',):
            reply_msg += '\n!stats is disabled for the {} gamemode.```'.format(session[6])
            await adapter.reply(message, reply_msg, mentionauthor=True)
            return
        orig_roles = dict(session[7])
        # make a copy
        role_dict = {}
        traitorvill = 0
        traitor_turned = False
        for other in [session[1][x][4] for x in session[1]]:
            if 'traitor' in other:
                traitor_turned = True
                break
        for role in roles: # Fixes !stats crashing with !frole of roles not in game
            role_dict[role] = [0, 0]
            # [min, max] for traitor and similar roles
        for player in session[1]:
            # Get maximum numbers for all roles
            role_dict[get_role(player, 'role') if not [x for x in session[1][player][4] if x.startswith('turned:')] else [x for x in session[1][player][4] if x.startswith('turned:')].pop().split(':')[1]][0] += 1
            role_dict[get_role(player, 'role') if not [x for x in session[1][player][4] if x.startswith('turned:')] else [x for x in session[1][player][4] if x.startswith('turned:')].pop().split(':')[1]][1] += 1
            if get_role(player, 'role') in ['villager', 'traitor'] or 'turned:villager' in session[1][player][4]:
                traitorvill += 1
                

        #reply_msg += "Total roles: " + ", ".join(sorted([x + ": " + str(roles[x][3][len(session[1]) - MIN_PLAYERS]) for x in roles if roles[x][3][len(session[1]) - MIN_PLAYERS] > 0])).rstrip(", ") + '\n'
        # ^ saved this beast for posterity

        reply_msg += "Total roles: "
        total_roles = dict(orig_roles)
        reply_msg += ', '.join("{}: {}".format(x, total_roles[x]) for x in sort_roles(total_roles))
        
        if session[6] == 'noreveal':
            reply_msg += "```"
            await adapter.reply(message, reply_msg, mentionauthor=True)
            return

        for role in list(role_dict):
            # list is used to make a copy
            if role in TEMPLATES_ORDERED:
                del role_dict[role]

        if traitor_turned:
            role_dict['wolf'][0] += role_dict['traitor'][0]
            role_dict['wolf'][1] += role_dict['traitor'][1]
            role_dict['traitor'] = [0, 0]

        for player in session[1]:
            role = get_role(player, 'role')
            # Subtract dead players
            if not session[1][player][0]:
                reveal = get_role(player, 'deathstats')

                if role == 'traitor' and traitor_turned:
                    # player died as traitor but traitor turn message played, so subtract from wolves
                    reveal = 'wolf'

                if reveal == 'villager':
                    traitorvill -= 1
                    # could be traitor or villager
                    if 'traitor' in role_dict:
                        role_dict['traitor'][0] = max(0, role_dict['traitor'][0] - 1)
                        if role_dict['traitor'][1] > traitorvill:
                            role_dict['traitor'][1] = traitorvill

                    role_dict['villager'][0] = max(0, role_dict['villager'][0] - 1)
                    if role_dict['villager'][1] > traitorvill:
                        role_dict['villager'][1] = traitorvill
                else:
                    # player died is definitely that role
                    role_dict[reveal][0] = max(0, role_dict[reveal][0] - 1)
                    role_dict[reveal][1] = max(0, role_dict[reveal][1] - 1)
            
        for clone in session[1]:
            if [x for x in session[1][clone][4] if x.startswith('clone:')]:
                role = get_role(clone, 'role')
                if (not session[1][clone][0] and role != 'clone' and orig_roles[role] > 1 and role_dict[role] != 0) or (session[1][clone][0] and role != 'clone'):
                    #first part - if the clone's dead but whether or not the corpse is them or a real their role, call them alive
                    #and the second part is if they are alive and have cloned, call them a clone instead
                    role_dict['clone'][0] += 1
                    role_dict['clone'][1] += 1
                    if role != 'traitor':
                        role_dict[role][0] -= 1
                        role_dict[role][1] -= 1
        
        # after turning, amnesiac/executioner is shown instead of current role
        for player in [x for x in session[1] if session[1][x][0]]:
            if "amnesiac" in session[1][player][4] or "executioner" in session[1][player][4]:
                role = get_role(player, 'role')
                role_dict[role][0] -= 1
                role_dict[role][1] -= 1
                if "amnesiac" in session[1][player][4]:
                    role_dict["amnesiac"][0] += 1
                    role_dict["amnesiac"][1] += 1
                else:
                    role_dict["executioner"][0] += 1
                    role_dict["executioner"][1] += 1

        reply_msg += "\nCurrent roles: "
        for template in TEMPLATES_ORDERED:
            if template in orig_roles:
                del orig_roles[template]
        for role in sort_roles(list(set(roles) - set(TEMPLATES_ORDERED))):
            if role in orig_roles or role_dict[role][0]:
                if role_dict[role][0] == role_dict[role][1]:
                    if role_dict[role][0] == 1:
                        reply_msg += role
                    else:
                        reply_msg += roles[role][1]
                    reply_msg += ": " + str(role_dict[role][0])
                else:
                    reply_msg += roles[role][1] + ": {}-{}".format(role_dict[role][0], role_dict[role][1])
                reply_msg += ", "
        reply_msg = reply_msg.rstrip(", ") + "```"
        await adapter.reply(message, reply_msg, mentionauthor=True)
    else:
        players = ["{} ({})".format(get_name(x), x) for x in session[1]]
        num_players = len(session[1])
        if num_players == 0:
            await adapter.reply(message, "There is currently no active game. Try {}join to start a new game!".format(BOT_PREFIX))
        else:
            await adapter.reply(message, "{} players in lobby: ```\n{}\n```".format(num_players, '\n'.join(players)))

@cmd('revealroles', [1, 1], "```\n{0}revealroles takes no arguments\n\nDisplays what each user's roles are and sends it in pm.```", 'rr')
async def cmd_revealroles(message, parameters):
    msg = ["**Gamemode**: {}```diff".format(session[6])]
    for player in session[1]:
        msg.append("{} {} ({}): {}; action: {}; other: {}".format(
            '+' if session[1][player][0] else '-', get_name(player), player, get_role(player, 'actual'),
            session[1][player][2], ' '.join(session[1][player][4])))
    msg.append("```")
    await adapter.reply(message, '\n'.join(msg))
    await adapter.log(2, "{0} ({1}) REVEALROLES".format(message.author.name, message.author.id))

@cmd('see', [2, 0], "```\n{0}see <player>\n\nIf you are a seer, uses your power to detect <player>'s role. If you are a doomsayer, dooms <player> with either sickness, lycanthropy or death.```")
async def cmd_see(message, parameters):
    if not session[0] or message.author.id not in session[1] or not session[1][message.author.id][0]:
        return
    role = get_role(message.author.id, 'role')
    if role not in COMMANDS_FOR_ROLE['see']:
        return
    if session[2]:
        await adapter.reply(message, "You may only see during the night.", mentionauthor=True)
        return
    if "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    if (session[1][message.author.id][2] and role != 'doomsayer') or (role == 'doomsayer' and not [x for x in session[1][message.author.id][4] if x.startswith('doom:')]):
        await adapter.reply(message, "You have already used your power.", mentionauthor=True)
        return
    else:
        if parameters == "":
            await adapter.reply(message, roles[role][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if role != 'doomsayer':
                    if player == message.author.id:
                        await adapter.reply(message, "Using your power on yourself would be a waste.", mentionauthor=True)
                    elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                        await adapter.reply(message, "You may not see a succubus.", mentionauthor=True)
                    elif not session[1][player][0]:
                        await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                    else:
                        session[1][message.author.id][2] = player
                        if 'misdirection_totem2' in session[1][message.author.id][4]:
                            player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                        elif 'luck_totem2' in session[1][player][4]:
                            player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                        if role == 'seer':
                            seen_role = get_role(player, 'seen')
                            if (session[1][player][4].count('deceit_totem2') +\
                                session[1][message.author.id][4].count('deceit_totem2')) % 2 == 1:
                                if seen_role == 'wolf':
                                    seen_role = 'villager'
                                else:
                                    seen_role = 'wolf'
                            reply_msg = "is a **{}**".format(seen_role)
                        elif role == 'oracle':
                            seen_role = get_role(player, 'seenoracle')
                            if (session[1][player][4].count('deceit_totem2') +\
                                session[1][message.author.id][4].count('deceit_totem2')) % 2 == 1:
                                # getting team will return either village or wolf team
                                if seen_role == 'wolf':
                                    seen_role = 'villager'
                                else:
                                    seen_role = 'wolf'
                            reply_msg = "is {}a **wolf**".format('**not** ' if seen_role == 'villager' else '')
                        elif role == 'augur':
                            seen_role = get_role(player, 'actualteam')
                            if get_role(player, 'role') == 'amnesiac':
                                seen_role = roles[[x.split(':')[1].replace("_", " ") for x in session[1][player][4] if x.startswith("role:")].pop()][1]
                            reply_msg = "exudes a **{}** aura".format(
                                'red' if seen_role == 'wolf' else 'blue' if seen_role == 'village' else 'grey')
                        await adapter.reply(message, "You have a vision... in your vision you see that **{}** {}!".format(
                            get_name(player), reply_msg), mentionauthor=True)
                        await adapter.log(1, "{0} ({1}) SEE {2} ({3}) AS {4}".format(get_name(message.author.id), message.author.id, get_name(player), player, seen_role))
                else:
                    if player == message.author.id:
                        await adapter.reply(message, "Seeing yourself would be a waste.", mentionauthor=True)
                        return
                    elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                        await adapter.reply(message, "You may not see a succubus.", mentionauthor=True)
                        return
                    elif not session[1][player][0]:
                        await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                        return
                    elif get_role(player, 'role') in WOLFCHAT_ROLES:
                        await adapter.reply(message, "Seeing another wolf would be a waste.", mentionauthor=True)
                        return
                    else:
                        if 'misdirection_totem2' in session[1][message.author.id][4]:
                            player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, 'role') not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                        elif 'luck_totem2' in session[1][player][4]:
                            player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id and get_role(x, 'role') not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    doom = 'lycan'
                    if [x for x in session[1][message.author.id][4] if x.startswith('doom:')]:
                        doom = [x.split(':')[1] for x in session[1][message.author.id][4] if x.startswith('doom:')].pop()
                    if doom == 'lycan':
                        await adapter.reply(message, "You have a vision that **{0}** is transforming into a savage beast tomorrow night.".format(get_name(player)), mentionauthor=True)
                        session[1][player][4].append('lycanthropy')
                    elif doom == 'death':
                        await adapter.reply(message, "You have a vision that **{0}** will meet an untimely end tonight.".format(get_name(player)), mentionauthor=True)
                        session[1][message.author.id][4].append('doomdeath:{}'.format(player))
                    elif doom == 'sick':
                        await adapter.reply(message, "You have a vision that **{0}** will become incredibly ill tomorrow and unable to do anything.".format(get_name(player)), mentionauthor=True)
                        session[1][player][4].append('sick')
                    try:
                        session[1][message.author.id][4].remove('doom:{}'.format(doom))
                    except ValueError as e:
                        await adapter.log(2, "```py\n{}\n```".format(traceback.format_exc()))
                    await adapter.log(1, "{} ({}) {} DOOM {} ({})".format(get_name(message.author.id), message.author.id, doom,
                        get_name(player), player))
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)

@cmd('bless', [2, 0], "```\n{0}bless <player>\n\nIf you are a priest, gives a blessing to <player>```")
async def cmd_bless(message, parameters):
    if not session[0] or message.author.id not in session[1] or not session[1][message.author.id][0]:
        return
    role = get_role(message.author.id, 'role')
    if role not in COMMANDS_FOR_ROLE['bless']:
        return
    if not session[2]:
        await adapter.reply(message, "You may only bless during the day.", mentionauthor=True)
        return
    if 'bless' not in session[1][message.author.id][4]:
        await adapter.reply(message, "You have already blessed someone this game.", mentionauthor=True)
        return
    if "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    else:
        if parameters == "":
            await adapter.reply(message, roles[role][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if player == message.author.id:
                    await adapter.reply(message, "You may not bless yourself.", mentionauthor=True)
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                else:
                    session[1][message.author.id][4].remove('bless')
                    if 'misdirection_totem2' in session[1][message.author.id][4]:
                        player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    elif 'luck_totem2' in session[1][player][4]:
                        player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    await adapter.reply(message, "You have given a blessing to **{0}**.".format(get_name(player)), mentionauthor=True)
                    session[1][player][3].append('blessed')
                    await adapter.send_user(player, "You suddenly feel very safe.")
                    await adapter.log(1, "{} ({}) BLESS {} ({})".format(get_name(message.author.id), message.author.id,
                        get_name(player), player))

@cmd('consecrate', [2, 0], "```\n{0}consecrate <player>\n\nIf you are a priest, prevents <player> if they are a vengeful ghost from killing the following night, doing so removes your ability to participate in the vote that day```")
async def cmd_consecrate(message, parameters):
    if not session[0] or message.author.id not in session[1] or not session[1][message.author.id][0]:
        return
    role = get_role(message.author.id, 'role')
    if role not in COMMANDS_FOR_ROLE['consecrate']:
        return
    if not session[2]:
        return
    if 'consecrated' in session[1][message.author.id][4]:
        return
    if "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    else:
        if parameters == "":
            await adapter.reply(message, roles[role][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if session[1][player][0]:
                    await adapter.reply(message, "**{0}** is not dead.".format(get_name(player)), mentionauthor=True)
                else:
                    if 'misdirection_totem2' in session[1][message.author.id][4]:
                        player = misdirect(message.author.id, alive_players=[x for x in session[1] if not session[1][x][0]])
                    elif 'luck_totem2' in session[1][player][4]:
                        player = misdirect(player, alive_players=[x for x in session[1] if not session[1][x][0]])
                    await adapter.reply(message, "You have consecrated the body of **{0}**.".format(get_name(player)), mentionauthor=True)
                    session[1][player][4].append('consecrated')
                    session[1][message.author.id][4].append('consecrated')
                    session[1][message.author.id][4].append('injured')
                    session[1][message.author.id][2] = ''
                    await adapter.log(1, "{} ({}) CONSECRATE {} ({})".format(get_name(message.author.id), message.author.id,
                        get_name(player), player))

@cmd('hex', [2, 0], "```\n{0}hex <player>\n\nIf you are a hag, hexes <player>```")
async def cmd_hex(message, parameters):
    if not session[0] or message.author.id not in session[1] or not session[1][message.author.id][0]:
        return
    if get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['hex']:
        return
    if session[2]:
        return
    if session[1][message.author.id][2]:
        await adapter.reply(message, "You have already hexed someone tonight.", mentionauthor=True)
        return
    if "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    else:
        if parameters == "":
            await adapter.reply(message, roles[role][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if not session[1][player][0]:
                    await adapter.reply(message, "**{0}** is dead.".format(get_name(player)), mentionauthor=True)
                elif 'lasttarget:{}'.format(player) in session[1][message.author.id][4]:
                    await adapter.reply(message, "You hexed **{0}** last night. You cannot hex the same person two nights in a row.".format(get_name(player)), mentionauthor=True)
                elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                    await adapter.reply(message, "You may not hex a succubus.", mentionauthor=True)
                elif get_role(player, 'role') in WOLFCHAT_ROLES:
                    await adapter.reply(message, "Hexing a wolf would be a waste.", mentionauthor=True)
                else:
                    if 'misdirection_totem2' in session[1][message.author.id][4]:
                        player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, 'role') not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    elif 'luck_totem2' in session[1][player][4]:
                        player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, 'role') not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    await adapter.reply(message, "You have cast a hex on **{0}**.".format(get_name(player)), mentionauthor=True)
                    await wolfchat("**{0}** has cast a hex on **{1}**.".format(get_name(message.author.id), get_name(player)))
                    session[1][message.author.id][2] = player
                    session[1][message.author.id][4] = [x for x in session[1][message.author.id][4] if not x.startswith('lasttarget:')] + ['lasttarget:{}'.format(player)]
                    await adapter.log(1, "{} ({}) HEX {} ({})".format(get_name(message.author.id), message.author.id,
                        get_name(player), player))

@cmd('choose', [2, 0], "```\n{0}choose <player1> and <player2>\n\nIf you are a matchmaker, Selects two players to fall in love. You may select yourself as one of the lovers.```", 'match')
async def cmd_choose(message, parameters):
    if not session[0] or get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['choose'] or not session[1][message.author.id][0] or not message.channel.is_private:
        return
    if parameters == "":
        await adapter.reply(message, roles[session[1][message.author.id][1]][2].format(BOT_PREFIX), mentionauthor=True)
    else:
        if get_role(message.author.id, 'role') == 'matchmaker':
            if 'match' not in session[1][message.author.id][4]:
                await adapter.reply(message, "You have already chosen lovers.", mentionauthor=True)
                return
            targets = parameters.split(' and ')
            if len(targets) == 2:
                actual_targets = []
                for target in targets:
                    player = get_player(target)
                    if not player:
                        await adapter.reply(message, "Could not find player " + target, mentionauthor=True)
                        return
                    actual_targets.append(player)
                actual_targets = set(actual_targets)
                valid_targets = []
                if len(actual_targets) != 2:
                    await adapter.reply(message, "You may only choose **2** unique players to match.", mentionauthor=True)
                    return
                for player in actual_targets:
                    if not session[1][player][0]:
                        await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                        return
                    else:
                        valid_targets.append(player)
                redirected_targets = []
                for player in valid_targets:
                    if 'misdirection_totem2' in session[1][message.author.id][4]:
                        new_target = misdirect(message.author.id)
                        while new_target in redirected_targets:
                            new_target = misdirect(message.author.id)
                        redirected_targets.append(new_target)
                    elif 'luck_totem2' in session[1][player][4]:
                        new_target = misdirect(player)
                        while new_target in redirected_targets:
                            new_target = misdirect(message.author.id)
                        redirected_targets.append(new_target)
                    else:
                        redirected_targets.append(player)
                await adapter.reply(message, "You have selected **{}** and **{}** to be lovers.".format(*map(get_name, redirected_targets)), mentionauthor=True)
                session[1][message.author.id][4].remove('match')
                player1 = redirected_targets[0]
                player2 = redirected_targets[1]
                if "lover:" + player2 not in session[1][player1][4]:
                    session[1][player1][4].append("lover:" + player2)
                if "lover:" + player1 not in session[1][player2][4]:
                    session[1][player2][4].append("lover:" + player1)
                await adapter.log(1, "{} ({}) CHOOSE {} ({}) AND {} ({})".format(get_name(message.author.id), message.author.id,
                    get_name(player1), player1, get_name(player2), player2))
                love_msg = "You are in love with **{}**. If that player dies for any reason, the pain will be too much for you to bear and you will commit suicide."
                await adapter.send_user(player1, love_msg.format(get_name(player2)))
                await adapter.send_user(player2, love_msg.format(get_name(player1)))
            else:
                await adapter.reply(message, "You must choose two different players.", mentionauthor=True)
        elif get_role(message.author.id, 'role') == 'hot potato':
            if session[1][message.author.id][2]:
                await adapter.reply(message, "You have already chosen to swap with **{}**.".format(get_name(session[1][message.author.id][2])), mentionauthor=True)
                return
            if "silence_totem2" in session[1][message.author.id][4]:
                await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
                return
            player = get_player(parameters)
            if player:
                if 'misdirection_totem2' in session[1][message.author.id][4]:
                    player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                elif 'luck_totem2' in session[1][player][4]:
                    player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])                
                if player == message.author.id:
                    await adapter.reply(message, "You may not choose yourself.", mentionauthor=True)
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                else:
                    session[1][message.author.id][2] = player
                    await adapter.reply(message, "You have chosen to swap with **" + get_name(player) + "**.", mentionauthor=True)
                    await adapter.log(1, "{0} ({1}) POTATOCHOOSE {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)
            
@cmd('kill', [2, 0], "```\n{0}kill <player>\n\nIf you are a wolf, casts your vote to target <player>. If you are a "
                     "hunter, vengeful ghost, or serial killer, <player> will die the following night.```")
async def cmd_kill(message, parameters):
    if not session[0] or message.author.id not in session[1] or get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['kill']:
        return
    if get_role(message.author.id, 'role') == "vengeful ghost":
        if session[1][message.author.id][0] or not [x for x in session[1][message.author.id][4] if x.startswith("vengeance:")]:
            return
    if session[2]:
        await adapter.reply(message, "You may only kill someone during the night.", mentionauthor=True)
        return
    if parameters == "":
        await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
    else:
        if get_role(message.author.id, 'role') == 'serial killer':
            if session[1][message.author.id][2] == message.author.id:
                await adapter.reply(message, "You have already chosen to not kill tonight.".format(get_name(session[1][message.author.id][2])), mentionauthor=True)
                return
            if "silence_totem2" in session[1][message.author.id][4]:
                await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
                return
            player = get_player(parameters)
            if player:
                if 'luck_totem2' in session[1][player][4]:
                    player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                if player == message.author.id:
                    await adapter.reply(message, "Suicide is bad for you.", mentionauthor=True)
                elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                    await adapter.reply(message, "You may not kill a succubus.", mentionauthor=True)
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                else:
                    session[1][message.author.id][2] = player
                    await adapter.reply(message, "You have chosen to kill **" + get_name(player) + "** tonight.", mentionauthor=True)
                    await adapter.log(1, "{0} ({1}) SERIALKILL {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)
        elif get_role(message.author.id, 'role') == 'hunter':
            if 'hunterbullet' not in session[1][message.author.id][4]:
                await adapter.reply(message, "You have already killed someone this game.", mentionauthor=True)
                return
            elif session[1][message.author.id][2]:
                if session[1][message.author.id][2] == message.author.id:
                    await adapter.reply(message, "You have already chosen to not kill tonight.".format(get_name(session[1][message.author.id][2])), mentionauthor=True)
                else:
                    await adapter.reply(message, "You have already chosen to kill **{}**.".format(get_name(session[1][message.author.id][2])), mentionauthor=True)
                return
            if "silence_totem2" in session[1][message.author.id][4]:
                await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
                return
            player = get_player(parameters)
            if player:
                if 'misdirection_totem2' in session[1][message.author.id][4]:
                    player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                elif 'luck_totem2' in session[1][player][4]:
                    player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])                
                if player == message.author.id:
                    await adapter.reply(message, "Suicide is bad for you.", mentionauthor=True)
                elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                    await adapter.reply(message, "You may not kill a succubus.", mentionauthor=True)
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                else:
                    session[1][message.author.id][2] = player
                    await adapter.reply(message, "You have chosen to kill **" + get_name(player) + "** tonight.", mentionauthor=True)
                    await adapter.log(1, "{0} ({1}) HUNTERKILL {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)
        elif roles[get_role(message.author.id, 'role')][0] == 'wolf':
            num_kills = session[1][message.author.id][4].count('angry') + 1
            targets = parameters.split(' and ')
            actual_targets = []
            for target in targets:
                player = get_player(target)
                if not player:
                    await adapter.reply(message, "Could not find player " + target, mentionauthor=True)
                    return
                actual_targets.append(player)
            actual_targets = set(actual_targets)
            valid_targets = []
            if len(actual_targets) > num_kills:
                await adapter.reply(message, "You may only kill **{}** targets.".format(num_kills), mentionauthor=True)
                return
            for player in actual_targets:
                if player == message.author.id:
                    await adapter.reply(message, "Suicide is bad for you.", mentionauthor=True)
                    return
                elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                    await adapter.reply(message, "You may not kill a succubus.", mentionauthor=True)
                    return
                elif get_role(message.author.id, 'actualteam') == 'wolf' and \
                get_role(player, 'actualteam') == 'wolf' and get_role(player, 'role') not in ['minion', 'cultist']:
                    await adapter.reply(message, "You can't kill another wolf.", mentionauthor=True)
                    return
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                    return
                else:
                    if "silence_totem2" in session[1][message.author.id][4]:
                        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
                        return
                    elif "ill_wolf" in session[1][message.author.id][4]:
                        await adapter.reply(message, "You are feeling ill tonight, and are unable to kill anyone.", mentionauthor=True)
                        return
                    valid_targets.append(player)
            redirected_targets = []
            for player in valid_targets:
                if 'misdirection_totem2' in session[1][message.author.id][4]:
                    new_target = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, "role") not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    while new_target in redirected_targets:
                        new_target = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, "role") not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    redirected_targets.append(new_target)
                elif 'luck_totem2' in session[1][player][4]:
                    new_target = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, "role") not in WOLFCHAT_ROLES])
                    while new_target in redirected_targets:
                        new_target = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, "role") not in WOLFCHAT_ROLES])
                    redirected_targets.append(new_target)
                else:
                    redirected_targets.append(player)
            session[1][message.author.id][2] = ','.join(redirected_targets)
            await adapter.reply(message, "You have voted to kill **{}**.".format('** and **'.join(
                map(get_name, valid_targets))), mentionauthor=True)
            await wolfchat("**{}** has voted to kill **{}**.".format(get_name(message.author.id), '** and **'.join(
                map(get_name, valid_targets))))
            await adapter.log(1, "{0} ({1}) KILL {2} ({3})".format(get_name(message.author.id), message.author.id,
            ' and '.join(map(get_name, valid_targets)), ','.join(valid_targets)))
        elif get_role(message.author.id, 'role') == 'vengeful ghost':
            if 'consecrated' not in session[1][message.author.id][4] and 'driven' not in session[1][message.author.id][4]:
                if session[1][message.author.id][2] != '':
                    await adapter.reply(message, "You have already chosen to kill **{}**.".format(get_name(session[1][message.author.id][2])), mentionauthor=True)
                    return
                player = get_player(parameters)
                against = 'wolf'
                if [x for x in session[1][message.author.id][4] if x.startswith("vengeance:")]:
                    against = [x.split(":")[1] for x in session[1][message.author.id][4] if x.startswith("vengeance:")].pop()
                if player:
                    if player == message.author.id:
                        return
                    elif not session[1][player][0]:
                        await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                    elif get_role(player, 'actualteam') != against:
                        await adapter.reply(message, "You must target a {}.".format('villager' if against == 'village' else 'wolf'), mentionauthor=True)
                    else:
                        session[1][message.author.id][2] = player
                        await adapter.reply(message, "You have chosen to kill **" + get_name(player) + "** tonight.", mentionauthor=True)
                        await adapter.log(1, "{0} ({1}) VENGEFUL KILL {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
                else:
                    await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)
            elif 'consecrated' in session[1][message.author.id][4]:
                await adapter.reply(message, "You have been consecrated, so you cannot kill tonight.", mentionauthor=True)

@cmd('vote', [0, 0], "```\n{0}vote [<gamemode | player>]\n\nVotes for <gamemode> during the join phase or votes to lynch <player> during the day. If no arguments "
                     "are given, replies with a list of current votes.```", 'v')
async def cmd_vote(message, parameters):
    if session[0]:
        await cmd_lynch(message, parameters)
    else:
        if message.channel.is_private:
            await adapter.reply(message, "Please use vote in channel.", mentionauthor=True)
            return
        if parameters == "":
            await cmd_votes(message, parameters)
        else:
            if session[6]:
                await adapter.reply(message, "An admin has already set a gamemode.", mentionauthor=True)
                return
            if message.author.id in session[1]:
                choice, num = _autocomplete(parameters, gamemodes)
                if num == 0:
                    await adapter.reply(message, "Could not find gamemode {}".format(parameters), mentionauthor=True)
                elif num == 1:
                    session[1][message.author.id][2] = choice
                    await adapter.reply(message, "You have voted for the **{}** gamemode.".format(choice), mentionauthor=True)
                else:
                    await adapter.reply(message, "Multiple options: {}".format(', '.join(sorted(choice))), mentionauthor=True)
            else:
                await adapter.reply(message, "You cannot vote for a gamemode if you are not playing!", mentionauthor=True)

@cmd('lynch', [0, 0], "```\n{0}lynch [<player>]\n\nVotes to lynch [<player>] during the day. If no arguments are given, replies with a list of current votes.```")
async def cmd_lynch(message, parameters):
    if not session[0] or not (session[2] and (datetime.now() - session[3][1]).total_seconds() > 2):
        return
    if parameters == "":
        await cmd_votes(message, parameters)
    else:
        if message.author.id not in session[1]:
            return
        if message.channel.is_private:
            await adapter.reply(message, "Please use lynch in channel.", mentionauthor=True)
            return
        if 'illness' in session[1][message.author.id][4]:
            try:
                await adapter.send_user(message.author.id, "You are staying home due to your illness and cannot participate in the vote.")
            except discord.Forbidden:
                pass
            return
        if 'injured' in session[1][message.author.id][4]:
            await adapter.reply(message, "You are injured and unable to vote.", mentionauthor=True)
            return
        to_lynch = get_player(parameters.split(' ')[0])
        if not to_lynch:
            to_lynch = get_player(parameters)
        if to_lynch:
            if not session[1][to_lynch][0]:
                await adapter.reply(message, "Player **" + get_name(to_lynch) + "** is dead!", mentionauthor=True)
            else:
                session[1][message.author.id][2] = to_lynch
                await adapter.reply(message, "You have voted to lynch **" + get_name(to_lynch) + "**.", mentionauthor=True)
                vote_list = list(chain.from_iterable([[int(i.split(':')[1]) for i in session[1][x][4] if i.startswith("vote:")] for x in session[1]]))
                if len(vote_list) == 0:
                    session[1][message.author.id][4].append("vote:1")
                else:
                    session[1][message.author.id][4] = [x for x in session[1][message.author.id][4] if not x.startswith('vote:')]
                    session[1][message.author.id][4].append("vote:{}".format(max(vote_list) + 1))
                await adapter.log(1, "{0} ({1}) LYNCH {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(to_lynch), to_lynch))
        else:
            await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)

@cmd('votes', [0, 0], "```\n{0}votes takes no arguments\n\nDisplays votes for gamemodes during the join phase or current votes to lynch during the day.```")
async def cmd_votes(message, parameters):
    if not session[0]:
        vote_dict = {'start' : []}
        for player in session[1]:
            if session[1][player][2] in vote_dict:
                vote_dict[session[1][player][2]].append(player)
            elif session[1][player][2] != '':
                vote_dict[session[1][player][2]] = [player]
            if session[1][player][1] == 'start':
                vote_dict['start'].append(player)
        reply_msg = "**{}** player{} in the lobby, **{}** vote{} required to choose a gamemode, **{}** votes needed to start.```\n".format(
            len(session[1]), '' if len(session[1]) == 1 else 's', len(session[1]) // 2 + 1, '' if len(session[1]) // 2 + 1 == 1 else 's',
            max(2, min(len(session[1]) // 4 + 1, 4)))
        for gamemode in vote_dict:
            if gamemode == 'start':
                continue
            reply_msg += "{} ({} vote{}): {}\n".format(gamemode, len(vote_dict[gamemode]), '' if len(vote_dict[gamemode]) == 1 else 's',
                                                     ', '.join(map(get_name, vote_dict[gamemode])))
        reply_msg += "{} vote{} to start: {}\n```".format(len(vote_dict['start']), '' if len(vote_dict['start']) == 1 else 's',
                                                       ', '.join(map(get_name, vote_dict['start'])))
        await adapter.reply(message, reply_msg, mentionauthor=True)
    elif session[0] and session[2]:
        vote_dict = {'abstain': []}
        alive_players = [x for x in session[1] if session[1][x][0]]
        able_voters = [x for x in alive_players if 'injured' not in session[1][x][4]]
        for player in able_voters:
            if session[1][player][2] in vote_dict:
                vote_dict[session[1][player][2]].append(player)
            elif session[1][player][2] != '':
                vote_dict[session[1][player][2]] = [player]
        abstainers = vote_dict['abstain']
        reply_msg = "**{}** living players, **{}** votes required to lynch, **{}** players available to vote, **{}** player{} refrained from voting.\n".format(
            len(alive_players), len(able_voters) // 2 + 1, len(able_voters), len(abstainers), '' if len(abstainers) == 1 else 's')

        if len(vote_dict) == 1 and vote_dict['abstain'] == []:
            reply_msg += "No one has cast a vote yet. Do `{}lynch <player>` in #{} to lynch <player>. ".format(config.BOT_PREFIX, adapter.GAME_CHANNEL.name)
        else:
            reply_msg += "Current votes: ```\n"
            for voted in [x for x in vote_dict if x != 'abstain']:
                reply_msg += "{} ({}) ({} vote{}): {}\n".format(
                    get_name(voted), voted, len(vote_dict[voted]), '' if len(vote_dict[voted]) == 1 else 's', ', '.join(['{} ({})'.format(get_name(x), x) for x in vote_dict[voted]]))
            reply_msg += "{} vote{} to abstain: {}\n".format(
                len(vote_dict['abstain']), '' if len(vote_dict['abstain']) == 1 else 's', ', '.join(['{} ({})'.format(get_name(x), x) for x in vote_dict['abstain']]))
            reply_msg += "```"
        await adapter.reply(message, reply_msg, mentionauthor=True)

@cmd('retract', [0, 0], "```\n{0}retract takes no arguments\n\nRetracts your gamemode and vote to start during the join phase, "
                        "or retracts your vote to lynch or kill during the game.```", 'r')
async def cmd_retract(message, parameters):
    if message.author.id not in session[1]:
        # not playing
        return
    if not session[0] and session[1][message.author.id][2] == '' and session[1][message.author.id][1] == '':
        # no vote to start nor vote for gamemode
        return
    if session[0] and session[1][message.author.id][2] == '':
        # no target
        return
    if not session[0]:
        if message.channel.is_private:
            await adapter.reply(message, "Please use retract in channel.", mentionauthor=True)
            return
        session[1][message.author.id][2] = ''
        session[1][message.author.id][1] = ''
        await adapter.reply(message, "You retracted your vote.", mentionauthor=True)
        session[1][message.author.id][4] = [x for x in session[1][message.author.id][4] if not x.startswith("vote:")]
    elif session[0] and session[1][message.author.id][0]:
        if session[2]:
            if message.channel.is_private:
                await adapter.reply(message, "Please use retract in channel.", mentionauthor=True)
                return
            session[1][message.author.id][2] = ''
            await adapter.reply(message, "You retracted your vote.", mentionauthor=True)
            await adapter.log(1, "{0} ({1}) RETRACT VOTE".format(get_name(message.author.id), message.author.id))
        else:
            # We have a killing role
            if session[1][message.author.id][1] in COMMANDS_FOR_ROLE['kill']:
                # All killing roles can retract except hunter and vg
                if session[1][message.author.id][1] not in ['hunter', 'vengeful ghost']:
                    # Sent in public channel 
                    if not message.channel.is_private:
                        try:
                            await adapter.send_user(message.author.id, "Please use retract in pm.")
                        except discord.Forbidden:
                            pass
                        return
                    # Reset target
                    session[1][message.author.id][2] = ''
                    await adapter.reply(message, "You retracted your kill.", mentionauthor=True)
                    # If this killing role is also a wolf chat role, then teammates will be alerted
                    if session[1][message.author.id][1] in ACTUAL_WOLVES:
                        await wolfchat("**{}** has retracted their kill.".format(get_name(message.author.id)))
                    await adapter.log(1, "{0} ({1}) RETRACT KILL".format(get_name(message.author.id), message.author.id))

@cmd('abstain', [0, 2], "```\n{0}abstain takes no arguments\n\nRefrain from voting someone today.```", 'abs', 'nl')
async def cmd_abstain(message, parameters):
    if not session[0] or not session[2] or not message.author.id in session[1] or not session[1][message.author.id][0]:
        return
    if session[6] == 'evilvillage':
        await adapter.send_lobby("The evilvillage cannot abstain.")
        return
    if session[4][1] == timedelta(0):
        await adapter.send_lobby("The village may not abstain on the first day.")
        return
    if 'injured' in session[1][message.author.id][4]:
        await adapter.reply(message, "You are injured and unable to vote.", mentionauthor=True)
        return
    session[1][message.author.id][2] = 'abstain'
    await adapter.log(1, "{0} ({1}) ABSTAIN".format(get_name(message.author.id), message.author.id))
    await adapter.send_lobby("**{}** votes to not lynch anyone today.".format(get_name(message.author.id)))

@cmd('coin', [0, 0], "```\n{0}coin takes no arguments\n\nFlips a coin. Don't use this for decision-making, especially not for life or death situations.```")
async def cmd_coin(message, parameters):
    value = random.randint(1,100)
    reply_msg = ''
    if value == 1:
        reply_msg = 'its side'
    elif value == 100:
        reply_msg = adapter.BOT_NAME
    elif value < 50:
        reply_msg = 'heads'
    else:
        reply_msg = 'tails'
    await adapter.reply(message, 'The coin landed on **' + reply_msg + '**!', mentionauthor=True)

@cmd('admins', [0, 0], "```\n{0}admins takes no arguments\n\nLists online/idle admins if used in pm, and **alerts** online/idle admins if used in channel (**USE ONLY WHEN NEEDED**).```")
async def cmd_admins(message, parameters):
    await adapter.reply(message, 'Available admins: ' + ', '.join('<@{}>'.format(x) for x in ADMINS if is_online(x)), cleanmessage=False, mentionauthor=True)

@cmd('fday', [1, 2], "```\n{0}fday takes no arguments\n\nForces night to end.```")
async def cmd_fday(message, parameters):
    if session[0] and not session[2]:
        session[2] = True
        await adapter.reply(message, ":thumbsup:", mentionauthor=True)
        await adapter.log(2, "{0} ({1}) FDAY".format(message.author.name, message.author.id))

@cmd('fnight', [1, 2], "```\n{0}fnight takes no arguments\n\nForces day to end.```")
async def cmd_fnight(message, parameters):
    if session[0] and session[2]:
        session[2] = False
        await adapter.reply(message, ":thumbsup:", mentionauthor=True)
        await adapter.log(2, "{0} ({1}) FNIGHT".format(message.author.name, message.author.id))

@cmd('frole', [1, 2], "```\n{0}frole <player> <role>\n\nSets <player>'s role to <role>.```")
async def cmd_frole(message, parameters):
    if parameters == '':
        return
    player = parameters.split(' ')[0]
    role = parameters.split(' ', 1)[1]
    temp_player = get_player(player)
    if temp_player:
        if session[0]:
            if role in roles or role in ['cursed']:
                if role not in ['cursed'] + TEMPLATES_ORDERED:
                    session[1][temp_player][1] = role
                if role == 'cursed villager':
                    session[1][temp_player][1] = 'villager'
                    for i in range(session[1][temp_player][3].count('cursed')):
                        session[1][temp_player][3].remove('cursed')
                    session[1][temp_player][3].append('cursed')
                elif role == 'cursed':
                    for i in range(session[1][temp_player][3].count('cursed')):
                        session[1][temp_player][3].remove('cursed')
                    session[1][temp_player][3].append('cursed')
                elif role in TEMPLATES_ORDERED:
                    for i in range(session[1][temp_player][3].count(role)):
                        session[1][temp_player][3].remove(role)
                    session[1][temp_player][3].append(role)
                await adapter.reply(message, "Successfully set **{}**'s role to **{}**.".format(get_name(temp_player), role), mentionauthor=True)
            else:
                await adapter.reply(message, "Cannot find role named **" + role + "**", mentionauthor=True)
        else:
            session[1][temp_player][1] = role
    else:
        await adapter.reply(message, "Cannot find player named **" + player + "**", mentionauthor=True)
    await adapter.log(2, "{0} ({1}) FROLE {2}".format(message.author.name, message.author.id, parameters))

@cmd('force', [1, 2], "```\n{0}force <player> <target>\n\nSets <player>'s target flag (session[1][player][2]) to <target>.```")
async def cmd_force(message, parameters):
    if parameters == '':
        await adapter.reply(message, commands['force'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    player = parameters.split(' ')[0]
    target = ' '.join(parameters.split(' ')[1:])
    temp_player = get_player(player)
    if temp_player:
        session[1][temp_player][2] = target
        await adapter.reply(message, "Successfully set **{}**'s target to **{}**.".format(get_name(temp_player), target), mentionauthor=True)
    else:
        await adapter.reply(message, "Cannot find player named **" + player + "**", mentionauthor=True)
    await adapter.log(2, "{0} ({1}) FORCE {2}".format(message.author.name, message.author.id, parameters))

@cmd('session', [1, 1], "```\n{0}session takes no arguments\n\nReplies with the contents of the session variable in pm for debugging purposes. Admin only.```")
async def cmd_session(message, parameters):
    await adapter.send_user(message.author.id, "```py\n{}\n```".format(str(session)))
    await adapter.log(2, "{0} ({1}) SESSION".format(message.author.name, message.author.id))

@cmd('time', [0, 0], "```\n{0}time takes no arguments\n\nChecks in-game time.```", 't')
async def cmd_time(message, parameters):
    if session[0]:
        seconds = 0
        timeofday = ''
        sunstate = ''
        if session[2]:
            seconds = day_timeout - (datetime.now() - session[3][1]).seconds
            timeofday = 'daytime'
            sunstate = 'sunset'
        else:
            seconds = night_timeout - (datetime.now() - session[3][0]).seconds
            timeofday = 'nighttime'
            sunstate = 'sunrise'
        await adapter.reply(message, "It is now **{0}**. There is **{1:02d}:{2:02d}** until {3}.".format(timeofday, seconds // 60, seconds % 60, sunstate), mentionauthor=True)
    else:
        if len(session[1]) > 0:
            timeleft = GAME_START_TIMEOUT - (datetime.now() - session[5]).seconds
            await adapter.reply(message, "There is **{0:02d}:{1:02d}** left to start the game until it will be automatically cancelled. "
                                 "GAME_START_TIMEOUT is currently set to **{2:02d}:{3:02d}**.".format(
                                     timeleft // 60, timeleft % 60, GAME_START_TIMEOUT // 60, GAME_START_TIMEOUT % 60), mentionauthor=True)

@cmd('give', [2, 0], "```\n{0}give <player>\n\nIf you are a shaman or wolf shaman, gives your totem to <player>. You can see your totem by using `myrole` in pm.```")
async def cmd_give(message, parameters):
    if not session[0] or message.author.id not in session[1] or session[1][message.author.id][1] not in ['shaman', 'crazed shaman', 'wolf shaman'] or not session[1][message.author.id][0]:
        return
    if session[2]:
        await adapter.reply(message, "You may only give totems during the night.", mentionauthor=True)
        return
    if "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    if session[1][message.author.id][2] not in totems and session[1][message.author.id][1] != 'wolf shaman':
        await adapter.reply(message, "You have already given your totem to **" + get_name(session[1][message.author.id][2]) + "**.", mentionauthor=True)
        return
    elif session[1][message.author.id][1] == 'wolf shaman' and not [x for x in session[1][message.author.id][4] if x.startswith('totem:')]:
        given_to = [x.split(":")[1] for x in session[1][message.author.id][4] if x.startswith('lasttarget:')]
        if given_to:
            await adapter.reply(message, "You have already given your totem to **{}**.".format(get_name(given_to[0])), mentionauthor=True)
            return
    else:
        if parameters == "":
            await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                else:
                    if 'lasttarget:{}'.format(player) in session[1][message.author.id][4]:
                        await adapter.reply(message, "You gave your totem to **{}** last time, you must choose someone else.".format(get_name(player)), mentionauthor=True)
                        return
                    if session[1][message.author.id][1] in ["shaman", "crazed shaman"]:
                        totem = session[1][message.author.id][2]
                    else:
                        totem = [x for x in session[1][message.author.id][4] if x.startswith("totem:")][0].split(':')[1]
                    if 'misdirection_totem2' in session[1][message.author.id][4]:
                        player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    elif 'luck_totem2' in session[1][player][4]:
                        player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    session[1][player][4].append(totem)
                    if session[1][message.author.id][1] == "wolf shaman":
                        session[1][message.author.id][4] = [x for x in session[1][message.author.id][4] if x != "totem:{}".format(totem)]

                    else:
                        if 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus' and totem not in ["protection_totem", "revealing_totem", "desperation_totem", "influence_totem", "luck_totem", "pestilence_totem", "retribution_totem"]:
                            await adapter.reply(message, "You may not give a succubus.", mentionauthor=True)
                            return
                        session[1][message.author.id][2] = player
                        session[1][message.author.id][4] = [x for x in session[1][message.author.id][4] if not x.startswith('lasttarget:')] + ['lasttarget:{}'.format(player)]
                    await adapter.reply(message, "You have given your totem to **" + get_name(player) + "**.", mentionauthor=True)
                    if session[1][message.author.id][1] == 'wolf shaman':
                        await wolfchat("**{0}** has given a totem to **{1}**.".format(get_name(message.author.id), get_name(player)))
                    session[1][message.author.id][4].append('given:{}'.format(totem))
                    await adapter.log(1, "{0} ({1}) GAVE {2} ({3}) {4}".format(get_name(message.author.id), message.author.id, get_name(player), player, totem))
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)

@cmd('info', [0, 0], "```\n{0}info takes no arguments\n\nGives information on how the game works.```")
async def cmd_info(message, parameters):
    msg = "In Werewolf, there are two teams, village and wolves. The villagers try to get rid of all of the wolves, and the wolves try to kill all of the villagers.\n"
    msg += "There are two phases, night and day. During night, the wolf/wolves choose a target to kill, and some special village roles like seer perform their actions. "
    msg += "During day, the village discusses everything and chooses someone to lynch. "
    msg += "Once you die, you can't talk in the lobby channel but you can discuss the game with the spectators in #spectator-chat.\n\n"
    msg += "To join a game, use `{0}join`. If you cannot chat in #lobby, then either a game is ongoing or you are dead.\n"
    msg += "For a list of roles, use the command `{0}roles`. For information on a particular role, use `{0}role role`. For statistics on the current game, use `{0}stats`. "
    msg += "For a list of commands, use `{0}list`. For help on a command, use `{0}help command`. To see the in-game time, use `{0}time`.\n\n"
    msg += "Please let belungawhale know about any bugs you might find."
    await adapter.reply(message, msg.format(BOT_PREFIX), mentionauthor=True)

@cmd('notify_role', [0, 0], "```\n{0}notify_role [<true|false>]\n\nGives or take the " + config.WEREWOLF_NOTIFY_ROLE_NAME + " role.```")
async def cmd_notify_role(message, parameters):
    if not adapter.WEREWOLF_NOTIFY_ROLE:
        await adapter.reply(message, "Error: A " + config.WEREWOLF_NOTIFY_ROLE_NAME + " role does not exist. Please let an admin know.", mentionauthor=True)
        return
    # TODO: how to refactor to use adapter?
    member = await adapter.get_user_destination(message.author.id)
    if not member:
        await adapter.reply(message, "You are not in the server!", mentionauthor=True)
    has_role = adapter.WEREWOLF_NOTIFY_ROLE in member.roles
    if parameters == '':
        has_role = not has_role
    elif parameters in ['true', '+', 'yes']:
        has_role = True
    elif parameters in ['false', '-', 'no']:
        has_role = False
    else:
        await adapter.reply(message, commands['notify_role'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    if has_role:
        await adapter.add_notify_role(member.id)
        await adapter.reply(message, "You will be notified by @" + adapter.WEREWOLF_NOTIFY_ROLE.name + ".", mentionauthor=True)
    else:
        await adapter.remove_notify_role(member.id)
        await adapter.reply(message, "You will not be notified by @" + adapter.WEREWOLF_NOTIFY_ROLE.name + ".", mentionauthor=True)

@cmd('ignore', [1, 1], "```\n{0}ignore <add|remove|list> <user>\n\nAdds or removes <user> from the ignore list, or outputs the ignore list.```")
async def cmd_ignore(message, parameters):
    parameters = ' '.join(message.content.strip().split(' ')[1:])
    parameters = parameters.strip()
    global IGNORE_LIST
    if parameters == '':
        await adapter.reply(message, commands['ignore'][2].format(BOT_PREFIX), mentionauthor=True)
    else:
        action = parameters.split(' ')[0].lower()
        target = ' '.join(parameters.split(' ')[1:])
        member_by_id = adapter.WEREWOLF_SERVER.get_member(target.strip('<@!>'))
        member_by_name = adapter.WEREWOLF_SERVER.get_member_named(target)
        member = None
        if member_by_id:
            member = member_by_id
        elif member_by_name:
            member = member_by_name
        if action not in ['+', 'add', '-', 'remove', 'list']:
            await adapter.reply(message, "Error: invalid flag `" + action + "`. Supported flags are add, remove, list", mentionauthor=True)
            return
        if not member and action != 'list':
            await adapter.reply(message, "Error: could not find target " + target, mentionauthor=True)
            return
        if action in ['+', 'add']:
            if member.id in IGNORE_LIST:
                await adapter.reply(message, member.name + " is already in the ignore list!", mentionauthor=True)
            else:
                IGNORE_LIST.append(member.id)
                await adapter.reply(message, member.name + " was added to the ignore list.", mentionauthor=True)
        elif action in ['-', 'remove']:
            if member.id in IGNORE_LIST:
                IGNORE_LIST.remove(member.id)
                await adapter.reply(message, member.name + " was removed from the ignore list.", mentionauthor=True)
            else:
                await adapter.reply(message, member.name + " is not in the ignore list!", mentionauthor=True)
        elif action == 'list':
            if len(IGNORE_LIST) == 0:
                await adapter.reply(message, "The ignore list is empty.", mentionauthor=True)
            else:
                msg_dict = {}
                for ignored in IGNORE_LIST:
                    member = adapter.WEREWOLF_SERVER.get_member(ignored)
                    msg_dict[ignored] = member.name if member else "<user not in server with id " + ignored + ">"
                await adapter.reply(message, str(len(IGNORE_LIST)) + " ignored users:\n```\n" + '\n'.join([x + " (" + msg_dict[x] + ")" for x in msg_dict]) + "```", mentionauthor=True)
        else:
            await adapter.reply(message, commands['ignore'][2].format(BOT_PREFIX), mentionauthor=True)
        await adapter.log(2, "{0} ({1}) IGNORE {2}".format(message.author.name, message.author.id, parameters))

# TODO
async def cmd_pingif(message, parameters):
    global pingif_dict
    if parameters == '':
        if message.author.id in pingif_dict:
            await adapter.reply(message, "You will be notified when there are at least **{}** players.".format(pingif_dict[message.author.id]), mentionauthor=True)
        else:
            await adapter.reply(message, "You have not set a pingif yet. `{}pingif <number of players>`".format(BOT_PREFIX), mentionauthor=True)
    elif parameters.isdigit():
        num = int(parameters)
        if num in range(MIN_PLAYERS, MAX_PLAYERS + 1):
            pingif_dict[message.author.id] = num
            await adapter.reply(message, "You will be notified when there are at least **{}** players.".format(pingif_dict[message.author.id]), mentionauthor=True)
        else:
            await adapter.reply(message, "Please enter a number between {} and {} players.".format(MIN_PLAYERS, MAX_PLAYERS), mentionauthor=True)
    else:
        await adapter.reply(message, "Please enter a valid number of players to be notified at.", mentionauthor=True)

@cmd('online', [1, 1], "```\n{0}online takes no arguments\n\nNotifies all online users.```")
async def cmd_online(message, parameters):
    members = [x.id for x in message.server.members]
    online = ["<@{}>".format(x) for x in members if is_online(x)]
    await adapter.reply(message, "PING! {}".format(''.join(online)), cleanmessage=False, mentionauthor=True)

@cmd('notify', [0, 0], "```\n{0}notify [<true|false>]\n\nNotifies all online users who want to be notified, or adds/removes you from the notify list.```")
async def cmd_notify(message, parameters):
    global first_notify
    global notify_previous
    if session[0]:
        return
    notify = message.author.id in notify_me
    if parameters == '':
        if message.author.id in stasis and stasis[message.author.id] == 0 or message.author.id not in stasis:
            online = ["<@{}>".format(x) for x in notify_me if x != message.author.id and is_online(x) and x not in session[1] and (x in stasis and stasis[x] == 0 or x not in stasis)]
            if first_notify:
                first_notify = False
                notify_previous = datetime.now()
                await adapter.reply(message, "PING! {}".format(''.join(online)), cleanmessage=False, mentionauthor=True)
            else:
                notify_current = datetime.now()
                notify_difference = (notify_current - notify_previous).total_seconds()
                if notify_difference >= NOTIFY_COOLDOWN:
                    notify_previous = notify_current
                    await adapter.reply(message, "PING! {}".format(''.join(online)), cleanmessage=False, mentionauthor=True)
                else:
                    time_remaining = int(NOTIFY_COOLDOWN - notify_difference)
                    if time_remaining < 1:
                        time_remaining = 1
                    minutes, seconds = divmod(time_remaining, 60)
                    await adapter.reply(message, "Please wait at least another {}.".format(quantified_items_grammatical_list(OrderedDict([("minute", minutes), ("second", seconds)]))), mentionauthor=True)
        else:
            await adapter.reply(message, "You have stasis, so you cannot notify others.", mentionauthor=True)
    elif parameters in ['true', '+', 'yes']:
        if notify:
            await adapter.reply(message, "You are already in the notify list.", mentionauthor=True)
            return
        notify_me.append(message.author.id)
        await adapter.reply(message, "You will be notified by `{}notify`.".format(BOT_PREFIX), mentionauthor=True)
    elif parameters in ['false', '-', 'no']:
        if not notify:
            await adapter.reply(message, "You are not in the notify list.", mentionauthor=True)
            return
        notify_me.remove(message.author.id)
        await adapter.reply(message, "You will not be notified by `{}notify`.".format(BOT_PREFIX), mentionauthor=True)
    else:
        await adapter.reply(message, commands['notify'][2].format(BOT_PREFIX), mentionauthor=True)

@cmd('getrole', [1, 1], "```\n{0}getrole <player> <revealtype>\n\nTests get_role command.```")
async def cmd_getrole(message, parameters):
    if not session[0] or parameters == '':
        await adapter.reply(message, commands['getrole'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    player = parameters.split(' ')[0]
    revealtype = ' '.join(parameters.split(' ')[1:])
    temp_player = get_player(player)
    if temp_player:
        role = get_role(temp_player, revealtype)
        await adapter.reply(message, "**{}** is a **{}** using revealtype **{}**".format(get_name(temp_player), role, revealtype), mentionauthor=True)
    else:
        await adapter.reply(message, "Cannot find player named **" + player + "**", mentionauthor=True)

@cmd('entrance', [2, 0], "```\n{0}entrance <player>\n\nIf you are a succubus, entrances <player>. You will die if you visit the victim of the wolves.```")
async def cmd_entrance(message, parameters):
    if not session[0] or message.author.id not in session[1] or get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['entrance'] or not session[1][message.author.id][0]:
        return
    if session[2]:
        await adapter.reply(message, "You may only entrance during the night.", mentionauthor=True)
        return
    if session[1][message.author.id][2]:
        if session[1][message.author.id][2] == message.author.id:
            await adapter.reply(message, "Your entrance was unsuccessful.", mentionauthor=True)
            return
        else:
            await adapter.reply(message, "You are already entrancing **{}** tonight.".format(get_name(session[1][message.author.id][2])), mentionauthor=True)
            return
    if "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    else:
        if parameters == "":
            await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if player == message.author.id:
                    await adapter.reply(message, "You may not entrance yourself. Use `pass` to not entrance anyone tonight.", mentionauthor=True)
                if get_role(player, 'role') == 'succubus':
                    await adapter.reply(message, "You cannot entrance another succubus.", mentionauthor=True)
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                else:
                    if 'misdirection_totem2' in session[1][message.author.id][4]:
                        player = misdirect(message.author.id)
                    elif 'luck_totem2' in session[1][player][4]:
                        player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id])
                    if 'entranced' not in session[1][player][4]:
                        if 'bishop' in session[1][player][3]:
                            await adapter.reply(message, "The holiness of **{}** deters you from approaching. Your entrance is unsuccessful.".format(get_name(player)), mentionauthor=True)
                            session[1][message.author.id][2] = message.author.id
                            await adapter.send_user(player, "You smell the strange scent of a succubus for a fleeting moment. The succubus came near you, but it left you untouched.")
                            await adapter.log(1, "{0} ({1}) FAILED TO ENTRANCE {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
                        else:
                            await adapter.reply(message, "You are entrancing **{}** tonight.".format(get_name(player)), mentionauthor=True)
                            session[1][message.author.id][2] = player
                            role = get_role(player, 'role')
                            templates = get_role(player, 'templates')
                            session[1][player][4].append('entranced')
                            succubus_message = "You have become entranced by **{0}**. From this point on, you must vote along with them or risk dying. You **cannot win with your own team**, but you will win should all alive players become entranced."
                            if role in COMMANDS_FOR_ROLE['kill'] and message.author.id in session[1][player][2]:
                                session[1][player][2] = ''
                                succubus_message += " You discover that **{0}** is a succubus and have retracted your kill as a result.\n".format(get_name(message.author.id))
                            if 'assassin' in templates and 'assassinate:{}'.format(message.author.id) in session[1][player][4]:
                                session[1][player][4].remove('assassinate:{}'.format(message.author.id))
                                succubus_message += " You discover that **{0}** is a succubus and must now target someone else.\n".format(get_name(message.author.id))
                            if role == 'hag' and session[1][player][2] == message.author.id:
                                succubus_message += " You discover that **{0}** is a succubus and have retracted your hex as a result.\n".format(get_name(message.author.id))
                                session[1][player][2] = ''
                                session[1][player][4].remove('lasttarget:{}'.format(message.author.id))
                            if role == 'piper' and ('tocharm' in session[1][message.author.id][4] or 'charmed' in session[1][message.author.id][4]):
                                succubus_message += " You discover that **{0}** is a succubus and have retracted your charm as a result.\n".format(get_name(message.author.id))
                                session[1][message.author.id][4] = [x for x in session[1][message.author.id][4] if x not in ['charmed', 'tocharm']]
                                session[1][player][4].append('charm')
                            if role in COMMANDS_FOR_ROLE['give']:
                                totem = ''
                                if role == 'wolf shaman' and not [x for x in session[1][player][4] if x.startswith('totem:')] and 'lasttarget:{}'.format(message.author.id) in session[1][player][4]:
                                    totem = [x.split(':')[1] for x in session[1][player][4] if x.startswith('given:')].pop()
                                elif message.author.id == session[1][player][2]:
                                    totem = [x.split(':')[1] for x in session[1][player][4] if x.startswith('given:')].pop()
                                if totem not in ["protection_totem", "revealing_totem", "desperation_totem", "influence_totem", "luck_totem", "pestilence_totem", "retribution_totem", '']:
                                    succubus_message += " You discover that **{0}** is a succubus and have retracted your totem as a result."
                                    session[1][message.author.id][4].remove(totem)
                                    session[1][player][4].remove('given:{}'.format(totem))
                                    session[1][player][4].remove('lasttarget:{}'.format(message.author.id))
                                    if role == 'wolf shaman':
                                        session[1][player][4].append('totem:{}'.format(totem))
                                    else:
                                        session[1][player][2] == totem
                            await adapter.send_user(player, succubus_message.format(get_name(message.author.id)))
                            await adapter.log(1, "{0} ({1}) ENTRANCE {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
                    else:
                        await adapter.reply(message, "**{}** is already entranced.".format(get_name(player)), mentionauthor=True)
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)

@cmd('curse', [2, 0], "```\n{0}curse <player>\n\nIf you are a warlock, curses <player>. Be fast though as the curse takes effect as soon as you use the command.```")
async def cmd_curse(message, parameters):
    if not session[0] or message.author.id not in session[1] or get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['curse'] or not session[1][message.author.id][0]:
        return
    if session[2]:
        await adapter.reply(message, "You may only curse during the night.", mentionauthor=True)
        return
    if session[1][message.author.id][2]:
        await adapter.reply(message, "You have already cursed someone tonight.", mentionauthor=True)
        return
    if "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    else:
        if parameters == "":
            await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if player == message.author.id:
                    await adapter.reply(message, "That would be a waste.", mentionauthor=True)
                elif player in [x for x in session[1] if roles[get_role(x, 'role')][0] == 'wolf' and get_role(x, 'role') not in ['minion', 'cultist']]:
                    await adapter.reply(message, "Cursing a fellow wolf would be a waste.", mentionauthor=True)
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                    await adapter.reply(message, "You may not curse a succubus.", mentionauthor=True)
                else:
                    if 'misdirection_totem2' in session[1][message.author.id][4]:
                        player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, 'role') not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    elif 'luck_totem2' in session[1][player][4]:
                        player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, 'role') not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    templates = get_role(player, 'templates')
                    if 'cursed' not in templates:
                        await adapter.reply(message, "You have cast a curse on **{}**.".format(get_name(player)), mentionauthor=True)
                        await wolfchat("**{}** has cast a curse on **{}**.".format(get_name(message.author.id), get_name(player)))
                        session[1][message.author.id][2] = player
                        session[1][player][3].append('cursed')
                        await adapter.log(1, "{0} ({1}) CURSE {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
                    else:
                        await adapter.reply(message, "**{}** is already cursed.".format(get_name(player)), mentionauthor=True)
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)

@cmd('charm', [2, 0], "```\n{0}charm <player1> [and <player2>]\n\nIf you are a piper, charms <player1> and <player2>. You can choose to charm only one player.```")
async def cmd_charm(message, parameters):
    if not session[0] or get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['charm'] or not session[1][message.author.id][0] or not message.channel.is_private:
        return
    if parameters == "":
        await adapter.reply(message, roles[session[1][message.author.id][1]][2].format(BOT_PREFIX), mentionauthor=True)
    elif "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    else:
        if 'charm' not in session[1][message.author.id][4]:
            await adapter.reply(message, "You are already charming tonight.", mentionauthor=True)
            return
        targets = parameters.split(' and ')
        if len(targets) <= 2:
            actual_targets = []
            for target in targets:
                player = get_player(target)
                if not player:
                    await adapter.reply(message, "Could not find player " + target, mentionauthor=True)
                    return
                actual_targets.append(player)
            actual_targets = set(actual_targets)
            valid_targets = []
            for player in actual_targets:
                if not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                    return
                elif 'charmed' in session[1][player][4] or 'tocharm' in session[1][player][4]:
                    await adapter.reply(message, "**{}** is already charmed!".format(get_name(player)), mentionauthor=True)
                    return
                elif get_role(player, 'role') == 'piper':
                    await adapter.reply(message, "That would be a waste.", mentionauthor=True)
                    return
                elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                    await adapter.reply(message, "You may not charm a succubus.", mentionauthor=True)
                    return
                else:
                    valid_targets.append(player)
            redirected_targets = []
            for player in valid_targets:
                if 'misdirection_totem2' in session[1][message.author.id][4]:
                    new_target = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, 'role') not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4]) and not ('charmed' in session[1][x][4] or 'tocharm' in session[1][x][4])])
                    redirected_targets.append(new_target)
                elif 'luck_totem2' in session[1][player][4]:
                    new_target = misdirect(player)
                    redirected_targets.append(new_targe, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, 'role') not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4]) and not 'charmed' in session[1][x][4] or 'tocharm' in session[1][x][4]])
                else:
                    redirected_targets.append(player)
            if len(valid_targets) == 2:
                await adapter.reply(message, "You have charmed **{}** and **{}**.".format(*map(get_name, redirected_targets)), mentionauthor=True)
                await adapter.log(1, "{} ({}) CHARM {} ({}) AND {} ({})".format(get_name(message.author.id), message.author.id, get_name(redirected_targets[0]), redirected_targets[0], get_name(redirected_targets[1]), redirected_targets[1]))
                for piper in [x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'piper' and x != message.author.id]:
                    await adapter.send_user(piper, "Another piper has charmed **{}** and **{}**!".format(*map(get_name, redirected_targets)))
            elif len(valid_targets) == 1:
                await adapter.reply(message, "You have charmed **{}**.".format(*map(get_name, redirected_targets)), mentionauthor=True)
                await adapter.log(1, "{} ({}) CHARM {} ({})".format(get_name(message.author.id), message.author.id, get_name(redirected_targets[0]), redirected_targets[0]))
                for piper in [x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'piper' and x != message.author.id]:
                    await adapter.send_user(piper, "Another piper has charmed **{}**!".format(*map(get_name, redirected_targets)))
            session[1][message.author.id][4].remove('charm')
            for charmed in redirected_targets:
                session[1][charmed][4].append('tocharm')
        else:
            await adapter.reply(message, "You must choose two different players.", mentionauthor=True)

@cmd('clone', [2, 0], "```\n{0}clone <player1>\n\n If you are a clone, makes <player> your cloning target.```")
async def cmd_clone(message, parameters):
    if not session[0] or message.author.id not in session[1] or get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['clone'] or not session[1][message.author.id][0]:
        return
    if 'clone' not in session[1][message.author.id][4]:
        await adapter.reply(message, "You have already chosen someone to clone.", mentionauthor=True)
        return
    if session[2]:
        await adapter.reply(message, "You can only clone during the night.", mentionauthor=True)
        return
    else:
        if parameters == "":
            await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if player == message.author.id:
                    await adapter.reply(message, "You can't clone yourself!", mentionauthor=True)
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                else:
                    session[1][message.author.id][4].append("clone:{}".format(player))
                    await adapter.reply(message, "You have chosen to clone **{}**. If they die you will take their role.".format(get_name(player)), mentionauthor=True)
                    await adapter.log(1, "{0} ({1}) CLONE TARGET {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
                    session[1][message.author.id][4].remove('clone')
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)
                
@cmd('side', [2, 0], "```\n{0}side <villagers>/<wolves>\n\nIf you are a turncoat, switches which team you are siding with.```")
async def cmd_side(message, parameters):
    if not session[0] or message.author.id not in session[1] or get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['side'] or not session[1][message.author.id][0]:
        return
    if session[2]:
        await adapter.reply(message, "You can only switch sides during the night.", mentionauthor=True)
        return
    if "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    if 'sided2' in session[1][message.author.id][4]:
        await adapter.reply(message, "You cannot switch sides again until tomorrow night.", mentionauthor=True)
        return
    else:
        if parameters == "":
            await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
        elif parameters in ["villagers", "villager", "village", "v", "vils", "vil", "vills", "vill"] :
            if 'side:villagers' not in session[1][message.author.id][4]:
                session[1][message.author.id][2] = 'villagers'
                await adapter.reply(message, "You are now siding with the village.", mentionauthor=True)
                return
            else:
                session[1][message.author.id][2] = 'pass'
                await adapter.reply(message, "You have decided not to change sides tonight.", mentionauthor=True)
        elif parameters in ["wolves", "wolf", "w", "woof", "werewolves", "werewolf"] :
            if 'side:wolves' not in session[1][message.author.id][4]:
                session[1][message.author.id][2] = 'wolves'
                await adapter.reply(message, "You are now siding with the wolves.", mentionauthor=True)
                return
            else:
                session[1][message.author.id][2] = 'pass'
                await adapter.reply(message, "You have decided not to change sides tonight.", mentionauthor=True)
        else:
            return
                

@cmd('visit', [2, 0], "```\n{0}visit <player>\n\nIf you are a harlot, visits <player>. You can stay home by visiting yourself. "
                      "You will die if you visit a wolf or the victim of the wolves. If you are a succubus, entrances <player>```")
async def cmd_visit(message, parameters):
    if not session[0] or message.author.id not in session[1] or get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['visit'] or not session[1][message.author.id][0]:
        return
    if get_role(message.author.id, 'role') == 'succubus':
        await cmd_entrance(message, parameters)
        return
    if session[2]:
        await adapter.reply(message, "You may only visit during the night.", mentionauthor=True)
        return
    if session[1][message.author.id][2]:
        if message.author.id == session[1][message.author.id][2]:
            await adapter.reply(message, "You are already spending the night at home.", mentionauthor=True)
        else:
            await adapter.reply(message, "You are already spending the night with **{}**.".format(get_name(session[1][message.author.id][2])), mentionauthor=True)
        return
    if "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    else:
        if parameters == "":
            await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if player == message.author.id:
                    await adapter.reply(message, "You have chosen to stay home tonight.", mentionauthor=True)
                    session[1][message.author.id][2] = message.author.id
                    await adapter.log(1, "{0} ({1}) STAY HOME".format(get_name(message.author.id), message.author.id))
                elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                    await adapter.reply(message, "You may not visit a succubus.", mentionauthor=True)
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                else:
                    if 'misdirection_totem2' in session[1][message.author.id][4]:
                        player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    elif 'luck_totem2' in session[1][player][4]:
                        player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    await adapter.reply(message, "You are spending the night with **{}**. Have a good time!".format(get_name(player)), mentionauthor=True)
                    session[1][message.author.id][2] = player
                    await adapter.send_user(player, "You are spending the night with **{}**. Have a good time!".format(get_name(message.author.id)))
                    await adapter.log(1, "{0} ({1}) VISIT {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)

@cmd('totem', [0, 0], "```\n{0}totem [<totem>]\n\nReturns information on a totem, or displays a list of totems.```", 'totems')
async def cmd_totem(message, parameters):
    if not parameters == '':
        reply_totems = []
        for totem in totems:
            if totem.startswith(parameters):
                reply_totems.append(totem)
        if _autocomplete(parameters, totems)[1] == 1:
            totem = _autocomplete(parameters, totems)[0]
            reply_msg = "```\n"
            reply_msg += totem[0].upper() + totem[1:].replace('_', ' ') + "\n\n"
            reply_msg += totems[totem] + "```"
            await adapter.reply(message, reply_msg, mentionauthor=True)
            return
    await adapter.reply(message, "Available totems: " + ", ".join(sorted([x.replace('_', ' ') for x in totems])), mentionauthor=True)

@cmd('fgame', [1, 2], "```\n{0}fgame [<gamemode>]\n\nForcibly sets or unsets [<gamemode>].```")
async def cmd_fgame(message, parameters):
    if session[0]:
        return
    if parameters == '':
        if session[6] != '':
            session[6] = ''
            await adapter.reply(message, "Successfully unset gamemode.", mentionauthor=True)
        else:
            await adapter.reply(message, "Gamemode has not been set.", mentionauthor=True)
    else:
        if parameters.startswith('roles'):
            role_string = ' '.join(parameters.split(' ')[1:])
            if role_string == '':
                await adapter.reply(message, "`{}fgame roles wolf:1,traitor:1,shaman:2,cursed villager:2,etc.`".format(BOT_PREFIX), mentionauthor=True)
            else:
                session[6] = parameters
                await adapter.reply(message, "Successfully set gamemode roles to `{}`".format(role_string), mentionauthor=True)
        else:
            choices, num = _autocomplete(parameters, gamemodes)
            if num == 1:
                session[6] = choices
                await adapter.reply(message, "Successfuly set gamemode to **{}**.".format(choices), mentionauthor=True)
            elif num > 1:
                await adapter.reply(message, "Multiple choices: {}".format(', '.join(sorted(choices))), mentionauthor=True)
            else:
                await adapter.reply(message, "Could not find gamemode {}".format(parameters), mentionauthor=True)
    await adapter.log(2, "{0} ({1}) FGAME {2}".format(message.author.name, message.author.id, parameters))

@cmd('github', [0, 0], "```\n{0}github takes no arguments\n\nReturns a link to the bot's Github repository.```")
async def cmd_github(message, parameters):
    await adapter.reply(message, "http://github.com/belguawhale/Discord-Werewolf", mentionauthor=True)

@cmd('ftemplate', [1, 2], "```\n{0}ftemplate <player> [<add|remove|set>] [<template1 [template2 ...]>]\n\nManipulates a player's templates.```")
async def cmd_ftemplate(message, parameters):
    if not session[0]:
        return
    if parameters == '':
        await adapter.reply(message, commands['ftemplate'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    params = parameters.split(' ')
    player = get_player(params[0])
    if len(params) > 1:
        action = parameters.split(' ')[1]
    else:
        action = ""
    if len(params) > 2:
        templates = parameters.split(' ')[2:]
    else:
        templates = []
    if player:
        reply_msg = "Successfully "
        if action in ['+', 'add', 'give']:
            session[1][player][3] += templates
            reply_msg += "added templates **{0}** to **{1}**."
        elif action in ['-', 'remove', 'del']:
            for template in templates[:]:
                if template in session[1][player][3]:
                    session[1][player][3].remove(template)
                else:
                    templates.remove(template)
            reply_msg += "removed templates **{0}** from **{1}**."
        elif action in ['=', 'set']:
            session[1][player][3] = templates
            reply_msg += "set **{1}**'s templates to **{0}**."
        else:
            reply_msg = "**{1}**'s templates: " + ', '.join(session[1][player][3])
    else:
        reply_msg = "Could not find player {1}."

    await adapter.reply(message, reply_msg.format(', '.join(templates), get_name(player)), mentionauthor=True)
    await adapter.log(2, "{0} ({1}) FTEMPLATE {2}".format(message.author.name, message.author.id, parameters))

@cmd('fother', [1, 2], "```\n{0}fother <player> [<add|remove|set>] [<other1 [other2 ...]>]\n\nManipulates a player's other flag (totems, traitor).```")
async def cmd_fother(message, parameters):
    if not session[0]:
        return
    if parameters == '':
        await adapter.reply(message, commands['fother'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    params = parameters.split(' ')
    player = get_player(params[0])
    if len(params) > 1:
        action = parameters.split(' ')[1]
    else:
        action = ""
    if len(params) > 2:
        others = parameters.split(' ')[2:]
    else:
        others = []
    if player:
        reply_msg = "Successfully "
        if action in ['+', 'add', 'give']:
            session[1][player][4] += others
            reply_msg += "added **{0}** to **{1}**'s other flag."
        elif action in ['-', 'remove', 'del']:
            for other in others[:]:
                if other in session[1][player][4]:
                    session[1][player][4].remove(other)
                else:
                    others.remove(other)
            reply_msg += "removed **{0}** from **{1}**'s other flag."
        elif action in ['=', 'set']:
            session[1][player][4] = others
            reply_msg += "set **{1}**'s other flag to **{0}**."
        else:
            reply_msg = "**{1}**'s other flag: " + ', '.join(session[1][player][4])
    else:
        reply_msg = "Could not find player {1}."

    await adapter.reply(message, reply_msg.format(', '.join(others), get_name(player)), mentionauthor=True)
    await adapter.log(2, "{0} ({1}) FOTHER {2}".format(message.author.name, message.author.id, parameters))

@cmd('faftergame', [2, 2], "```\n{0}faftergame <command> [<parameters>]\n\nSchedules <command> to run with [<parameters>] after the next game ends.```")
async def cmd_faftergame(message, parameters):
    if parameters == "":
        await adapter.reply(message, commands['faftergame'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    command = parameters.split(' ')[0]
    if command in commands:
        global faftergame
        faftergame = message
        await adapter.reply(message, "Command `{}` will run after the next game ends.".format(parameters), mentionauthor=True)
    else:
        await adapter.reply(message, "{} is not a valid command!".format(command), mentionauthor=True)

@cmd('uptime', [0, 0], "```\n{0}uptime takes no arguments\n\nChecks the bot's uptime.```")
async def cmd_uptime(message, parameters):
    delta = datetime.now() - starttime
    output = [[delta.days, 'day'],
              [delta.seconds // 3600, 'hour'],
              [delta.seconds // 60 % 60, 'minute'],
              [delta.seconds % 60, 'second']]
    for i in range(len(output)):
        if output[i][0] != 1:
            output[i][1] += 's'
    reply_msg = ''
    if output[0][0] != 0:
        reply_msg += "{} {} ".format(output[0][0], output[0][1])
    for i in range(1, len(output)):
        reply_msg += "{} {} ".format(output[i][0], output[i][1])
    reply_msg = reply_msg[:-1]
    await adapter.reply(message, "Uptime: **{}**".format(reply_msg), mentionauthor=True)

@cmd('fstasis', [1, 1], "```\n{0}fstasis <player> [<add|remove|set>] [<amount>]\n\nManipulates a player's stasis.```")
async def cmd_fstasis(message, parameters):
    if parameters == '':
        await adapter.reply(message, commands['fstasis'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    params = parameters.split(' ')
    player = params[0].strip('<!@>')
    member = adapter.WEREWOLF_SERVER.get_member(player)
    name = "user not in server with id " + player
    if member:
        name = member.display_name
    if len(params) > 1:
        action = parameters.split(' ')[1]
    else:
        action = ''
    if len(params) > 2:
        amount = parameters.split(' ')[2]
        if amount.isdigit():
            amount = int(amount)
        else:
            amount = -1
    else:
        amount = -2
    if player.isdigit():
        if action and amount >= -1:
            if amount >= 0:
                if player not in stasis:
                    stasis[player] = 0
                reply_msg = "Successfully "
                if action in ['+', 'add', 'give']:
                    stasis[player] += amount
                    reply_msg += "increased **{0}** ({1})'s stasis by **{2}**."
                elif action in ['-', 'remove', 'del']:
                    amount = min(amount, stasis[player])
                    stasis[player] -= amount
                    reply_msg += "decreased **{0}** ({1})'s stasis by **{2}**."
                elif action in ['=', 'set']:
                    stasis[player] = amount
                    reply_msg += "set **{0}** ({1})'s stasis to **{2}**."
                else:
                    if player not in stasis:
                        amount = 0
                    else:
                        amount = stasis[player]
                    reply_msg = "**{0}** ({1}) is in stasis for **{2}** game{3}."
            else:
                reply_msg = "Stasis must be a non-negative integer."
        else:
            if player not in stasis:
                amount = 0
            else:
                amount = stasis[player]
            reply_msg = "**{0}** ({1}) is in stasis for **{2}** game{3}."
    else:
        reply_msg = "Invalid mention/id: {0}."

    await adapter.reply(message, reply_msg.format(name, player, amount, '' if int(amount) == 1 else 's'), mentionauthor=True)
    await adapter.log(2, "{0} ({1}) FSTASIS {2}".format(message.author.name, message.author.id, parameters))

@cmd('gamemode', [0, 0], "```\n{0}gamemode [<gamemode>]\n\nDisplays information on [<gamemode>] or displays a "
                         "list of gamemodes.```", 'game', 'gamemodes')
async def cmd_gamemode(message, parameters):
    gamemode, num = _autocomplete(parameters, gamemodes)
    if num == 1 and parameters != '':
        await adapter.reply(message, "<https://werewolf.miraheze.org/wiki/{}>\n```\nGamemode: {}\nPlayers: {}\nDescription: {}\n\nUse the command "
                             "`!roles {} guide` to view roles for this gamemode.```".format(gamemode + "_(gamemode)" if gamemode == "lycan" else gamemode.replace(' ', '_'),
        gamemode, str(gamemodes[gamemode]['min_players']) + '-' + str(gamemodes[gamemode]['max_players']),
        gamemodes[gamemode]['description'], gamemode), mentionauthor=True)
    else:
        game_list = ""
        game_list += "\n```ini\n[Main Modes] " + ", ".join(sorted(x for x in (gamemodes) if gamemodes[x]['chance'] != 0))
        game_list += "\n[Majority Only] " + ", ".join(sorted(x for x in (gamemodes) if gamemodes[x]['chance'] == 0)) + "```"
        await adapter.reply(message, game_list, mentionauthor=True)

@cmd('verifygamemode', [1, 1], "```\n{0}verifygamemode [<gamemode>]\n\nChecks to make sure [<gamemode>] is valid.```", 'verifygamemodes')
async def cmd_verifygamemode(message, parameters):
    if parameters == '':
        await adapter.reply(message, "```\n{}\n```".format(verify_gamemodes()), mentionauthor=True)
    elif _autocomplete(parameters, gamemodes)[1] == 1:
        await adapter.reply(message, "```\n{}\n```".format(verify_gamemode(_autocomplete(parameters, gamemodes)[0])), mentionauthor=True)
    else:
        await adapter.reply(message, "Invalid gamemode: {}".format(parameters), mentionauthor=True)

@cmd('shoot', [0, 2], "```\n{0}shoot <player>\n\nIf you have a gun, shoots <player> during the day. You may only use this command in channel.```")
async def cmd_shoot(message, parameters):
    if not session[0] or message.author.id not in session[1] or not session[1][message.author.id][0]:
        return
    if ('gunner' not in get_role(message.author.id, 'templates') and 'sharpshooter' not in get_role(message.author.id, 'templates')):
        try:
            await adapter.send_user(message.author.id, "You don't have a gun.")
        except discord.Forbidden:
            pass
        return
    if not session[2]:
        try:
            await adapter.send_user(message.author.id, "You may only shoot players during the day.")
        except discord.Forbidden:
            pass
        return
    if "silence_totem2" in session[1][message.author.id][4]:
        try:
            await adapter.send_user(message.author.id, "You have been silenced, and are unable to use any special powers.")
        except discord.Forbidden:
            pass
        return
    msg = ''
    pm = False
    ded = None
    outcome = ''
    if session[1][message.author.id][4].count('bullet') < 1:
        msg = "You have no more bullets."
        pm = True
    else:
        if parameters == "":
            msg = commands['shoot'][2].format(BOT_PREFIX)
            pm = True
        else:
            target = get_player(parameters.split(' ')[0])
            if not target:
                target = get_player(parameters)
            if not target:
                msg = 'Could not find player {}'.format(parameters)
            elif target == message.author.id:
                msg = "You are holding it the wrong way."
            elif not session[1][target][0]:
                msg = "Player **{}** is dead!".format(get_name(target))
            else:
                if 'misdirection_totem2' in session[1][message.author.id][4]:
                    target = misdirect(message.author.id)
                elif 'luck_totem2' in session[1][target][4]:
                    target = misdirect(target, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id])
                if get_role(message.author.id, 'role') == 'village drunk':
                    SUICIDE = DRUNK_SUICIDE
                    MISS = DRUNK_MISS
                    HEADSHOT = DRUNK_HEADSHOT
                    INJURE = DRUNK_INJURE
                else:
                    SUICIDE = GUNNER_SUICIDE
                    MISS = GUNNER_MISS
                    HEADSHOT = GUNNER_HEADSHOT
                    INJURE = GUNNER_INJURE
                wolf = get_role(message.author.id, 'role') in WOLFCHAT_ROLES
                session[1][message.author.id][4].remove('bullet')
                if wolf:
                    if get_role(target, 'role') in WOLFCHAT_ROLES:
                        outcome = 'miss'
                else:
                    if get_role(target, 'role') in ACTUAL_WOLVES:
                        if get_role(target, 'role') in ['werekitten']:
                            outcome = random.choice((['suicide'] * SUICIDE + ['miss'] * (MISS + HEADSHOT + INJURE)) if 'sharpshooter' not in get_role(message.author.id, 'templates') else ['miss'])
                        else:
                            outcome = 'killwolf'
                    elif get_role(target, 'role') == 'succubus':
                        outcome = random.choice((['suicide'] * SUICIDE + ['miss'] * (MISS + HEADSHOT) + ['injure'] * INJURE) if 'sharpshooter' not in get_role(message.author.id, 'templates') else ['killvictim'])
                if outcome == '':
                    outcome = random.choice((['miss'] * MISS + ['suicide'] * SUICIDE \
                                             + ['killvictim'] * HEADSHOT + ['injure'] * INJURE)  if 'sharpshooter' not in get_role(message.author.id, 'templates') else ['killvictim'])
                if outcome in ['injure', 'killvictim', 'killwolf']:
                    msg = "**{}** shoots **{}** with a bullet!\n\n".format(get_name(message.author.id), get_name(target))
                if outcome == 'miss':
                    msg += "**{}** is a lousy shooter and missed!".format(get_name(message.author.id))
                elif outcome == 'killwolf':
                    if session[6] == 'noreveal':
                        msg += "**{}** is a wolf and is dying from the silver bullet!".format(get_name(target))
                    else:
                        msg += "**{}** is a **{}** and is dying from the silver bullet!".format(get_name(target),
                            get_role(target, 'death'))
                    ded = target
                elif outcome == 'suicide':
                    msg += "Oh no! **{}**'s gun was poorly maintained and has exploded! ".format(get_name(message.author.id))
                    if session[6] != 'noreveal':
                        msg += "The village mourns a **gunner-{}**.".format(get_role(message.author.id, 'death'))
                    ded = message.author.id
                elif outcome == 'killvictim':
                    if session[6] == 'noreveal':
                        msg += "**{}** is not a wolf but was fatally injured.".format(get_name(target))
                    else:
                        msg += "**{}** is not a wolf but was fatally injured. The village has sacrificed a **{}**.".format(
                            get_name(target), get_role(target, 'death'))
                    ded = target
                elif outcome == 'injure':
                    msg += "**{}** is a villager and was injured. Luckily the injury is minor and will heal after a day of rest.".format(
                            get_name(target))
                    session[1][target][4].append('injured')
                else:
                    msg += "wtf? (this is an error, please report to an admin)"

                await adapter.log(1, "{} ({}) SHOOT {} ({}) WITH OUTCOME {}".format(get_name(message.author.id), message.author.id,
                    get_name(target), target, outcome))

    if pm:
        target = message.author
    else:
        target = adapter.GAME_CHANNEL
    try:
        await adapter.send_user(target.id, msg)
    except discord.Forbidden:
        pass

    if ded:
        await player_deaths({ded : ('gunner ' + outcome, get_role(message.author.id, "actualteam"))})
        await check_traitor()
    elif outcome == 'injured':
        session[1][target][4].append('injured')

@cmd('target', [2, 0], "```\n{0}target <player>\n\nIf you are an assassin, makes <player> your target during the night.```", 'assassinate')
async def cmd_target(message, parameters):
    if not session[0] or message.author.id not in session[1] or COMMANDS_FOR_ROLE['target'][0] not in get_role(message.author.id, "templates") or not session[1][message.author.id][0]:
        return
    if session[2]:
        await adapter.reply(message, "You may only target a player during the night.", mentionauthor=True)
        return
    if [x for x in session[1][message.author.id][4] if x.startswith("assassinate:")]:
        await adapter.reply(message, "You have already targeted someone. You must wait until they die to target again.", mentionauthor=True)
    else:
        if parameters == "":
            await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if player == message.author.id:
                    await adapter.reply(message, "You can't target yourself!", mentionauthor=True)
                elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                    await adapter.reply(message, "You may not target a succubus.", mentionauthor=True)
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                else:
                    if 'misdirection_totem2' in session[1][message.author.id][4]:
                        player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    elif 'luck_totem2' in session[1][player][4]:
                        player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    session[1][message.author.id][4].append("assassinate:{}".format(player))
                    await adapter.reply(message, "You have chosen to target **{}**. They will be your target until they die.".format(
                        get_name(player)), mentionauthor=True)
                    await adapter.log(1, "{0} ({1}) TARGET {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)

@cmd('fsay', [1, 1], "```\n{0}fsay <message>\n\nSends <message> to the lobby channel.```")
async def cmd_fsay(message, parameters):
    if parameters:
        await adapter.send_lobby(parameters)
        await adapter.log(2, "{} ({}) FSAY {}".format(message.author.name, message.author.id, parameters))
    else:
        await adapter.reply(message, commands['fsay'][2].format(BOT_PREFIX), mentionauthor=True)

@cmd('observe', [2, 0], "```\n{0}observe <player>\n\nIf you are a werecrow, tells you if <player> was in their bed for the night. "
                        "If you are a sorcerer, tells you if <player> has supernatural powers (seer, etc.).```")
async def cmd_observe(message, parameters):
    if not session[0] or message.author.id not in session[1] or get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['observe'] or not session[1][message.author.id][0]:
        return
    if session[2]:
        await adapter.reply(message, "You may only observe during the night.", mentionauthor=True)
        return
    if get_role(message.author.id, 'role') == 'werecrow':
        if 'observe' in session[1][message.author.id][4]:
            await adapter.reply(message, "You are already observing someone!.", mentionauthor=True)
            return
        if "silence_totem2" in session[1][message.author.id][4]:
            await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
            return
        else:
            if parameters == "":
                await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
            else:
                player = get_player(parameters)
                if player:
                    if player == message.author.id:
                        await adapter.reply(message, "That would be a waste.", mentionauthor=True)
                    elif player in [x for x in session[1] if roles[get_role(x, 'role')][0] == 'wolf' and get_role(x, 'role') not in ['minion', 'cultist']]:
                        await adapter.reply(message, "Observing another wolf is a waste of time.", mentionauthor=True)
                    elif not session[1][player][0]:
                        await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                    elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                        await adapter.reply(message, "You may not observe a succubus.", mentionauthor=True)
                    else:
                        session[1][message.author.id][4].append('observe')
                        if 'misdirection_totem2' in session[1][message.author.id][4]:
                            player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                        elif 'luck_totem2' in session[1][player][4]:
                            player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, 'role') not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                        await adapter.reply(message, "You transform into a large crow and start your flight to **{0}'s** house. You will "
                                            "return after collecting your observations when day begins.".format(get_name(player)), mentionauthor=True)
                        await wolfchat("**{}** is observing **{}**.".format(get_name(message.author.id), get_name(player)))
                        await adapter.log(1, "{0} ({1}) OBSERVE {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
                        while not session[2] and win_condition() == None and session[0]:
                            await asyncio.sleep(0.1)
                        if 'observe' in session[1][message.author.id][4]:
                            session[1][message.author.id][4].remove('observe')
                        if get_role(player, 'role') in ['seer', 'oracle', 'harlot', 'hunter', 'augur', 'bodyguard', 'guardian angel', 'succubus', 'serial killer']\
                            and session[1][player][2] in set(session[1]) - set(player)\
                            or get_role(player, 'role') in ['shaman', 'crazed shaman', 'piper', ]\
                            and session[1][player][2] in session[1]:
                                msg = "not in bed all night"
                        else:
                                msg = "sleeping all night long"
                        try:
                            await adapter.send_user(message.author.id, "As the sun rises, you conclude that **{}** was {}, and you fly back to your house.".format(
                                get_name(player), msg))
                        except discord.Forbidden:
                            pass
                else:
                    await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)
    elif get_role(message.author.id, 'role') == 'sorcerer':
        if session[1][message.author.id][2]:
            await adapter.reply(message, "You have already used your power.", mentionauthor=True)
        elif parameters == "":
            await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if player == message.author.id:
                    await adapter.reply(message, "Using your power on yourself would be a waste.", mentionauthor=True)
                elif player in [x for x in session[1] if roles[get_role(x, 'role')][0] == 'wolf' and get_role(x, 'role') != 'cultist']:
                    await adapter.reply(message, "Observing another wolf is a waste of time.", mentionauthor=True)
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                elif 'entranced' in session[1][message.author.id][4] and get_role(player, 'role') == 'succubus':
                    await adapter.reply(message, "You may not observe a succubus.", mentionauthor=True)
                else:
                    if 'misdirection_totem2' in session[1][message.author.id][4]:
                        player = misdirect(message.author.id, alive_players=[x for x in session[1] if session[1][x][0] and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    elif 'luck_totem2' in session[1][player][4]:
                        player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and get_role(x, 'role') not in WOLFCHAT_ROLES and not (get_role(x, 'role') == 'succubus' and 'entranced' in session[1][message.author.id][4])])
                    session[1][message.author.id][2] = player
                    target_role = get_role(player, 'role')
                    if target_role == 'amnesiac':
                        target_role = [x.split(':')[1].replace("_", " ") for x in session[1][player][4] if x.startswith("role:")].pop()
                    if target_role in ['seer', 'oracle', 'augur']:
                        debug_msg = target_role
                        msg = "**{}** is a **{}**!".format(get_name(player), get_role(player, 'role'))
                    else:
                        debug_msg = "not paranormal"
                        msg = "**{}** does not have paranormal senses.".format(get_name(player))
                    await wolfchat("**{}** is observing **{}**.".format(get_name(message.author.id), get_name(player)))
                    await adapter.reply(message, "After casting your ritual, you determine that " + msg, mentionauthor=True)
                    await adapter.log(1, "{0} ({1}) OBSERVE {2} ({3}) AS {4}".format(get_name(message.author.id), message.author.id, get_name(player), player, debug_msg))
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)

@cmd('id', [2, 0], "```\n{0}id <player>\n\nIf you are a detective, investigates <player> during the day.```")
async def cmd_id(message, parameters):
    if not session[0] or message.author.id not in session[1] or get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['id'] or not session[1][message.author.id][0]:
        return
    if not session[2]:
        await adapter.reply(message, "You may only investigate during the day.", mentionauthor=True)
        return
    if 'investigate' in session[1][message.author.id][4]:
        await adapter.reply(message, "You have already investigated someone.", mentionauthor=True)
        return
    if "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    else:
        if parameters == "":
            await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if player == message.author.id:
                    await adapter.reply(message, "Investigating yourself would be a waste.", mentionauthor=True)
                elif not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                else:
                    session[1][message.author.id][4].append('investigate')
                    if 'misdirection_totem2' in session[1][message.author.id][4]:
                        player = misdirect(message.author.id)
                    elif 'luck_totem2' in session[1][player][4]:
                        player = misdirect(player, alive_players=[x for x in session[1] if session[1][x][0] and x != message.author.id])
                    await adapter.reply(message, "The results of your investigation have returned. **{}** is a **{}**!".format(
                        get_name(player), get_role(player, 'role') if not get_role(player, 'role') == 'amnesiac' else [x.split(':')[1].replace("_", " ") for x in session[1][player][4] if x.startswith("role:")].pop()), mentionauthor=True)
                    await adapter.log(1, "{0} ({1}) INVESTIGATE {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
                    if random.random() < DETECTIVE_REVEAL_CHANCE:
                        await wolfchat("Someone accidentally drops a paper. The paper reveals that **{}** ({}) is the detective!".format(
                            get_name(message.author.id), message.author.id))
                        await adapter.log(1, "{0} ({1}) DETECTIVE REVEAL".format(get_name(message.author.id), message.author.id))
                    while session[2] and win_condition() == None and session[0]:
                        await asyncio.sleep(0.1)
                    if 'investigate' in session[1][message.author.id][4]:
                        session[1][message.author.id][4].remove('investigate')
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)

@cmd('frevive', [1, 2], "```\n{0}frevive <player>\n\nRevives <player>. Used for debugging purposes.```")
async def cmd_frevive(message, parameters):
    if not session[0]:
        return
    if parameters == "":
        await adapter.reply(message, commands['frevive'][2].format(BOT_PREFIX), mentionauthor=True)
    else:
        player = get_player(parameters)
        if player:
            if session[1][player][0]:
                await adapter.reply(message, "Player **{}** is already alive!".format(player), mentionauthor=True)
            else:
                session[1][player][0] = True
                await adapter.reply(message, ":thumbsup:", mentionauthor=True)
        else:
            await adapter.reply(message, "Could not find player {}".format(parameters), mentionauthor=True)
    await adapter.log(2, "{} ({}) FREVIVE {}".format(message.author.name, message.author.id, parameters))

@cmd('pass', [2, 0], "```\n{0}pass takes no arguments\n\nChooses to not perform your action tonight.```")
async def cmd_pass(message, parameters):
    role = get_role(message.author.id, 'role')
    if not session[0] or message.author.id not in session[1] or role not in COMMANDS_FOR_ROLE['pass'] or not session[1][message.author.id][0]:
        return
    if session[2] and role in ('harlot', 'hunter', 'guardian angel', 'serial killer'):
        await adapter.reply(message, "You may only pass during the night.", mentionauthor=True)
        return
    if session[1][message.author.id][2] != '':
        return
    if role == 'harlot':
        session[1][message.author.id][2] = message.author.id
        await adapter.reply(message, "You have chosen to stay home tonight.", mentionauthor=True)
    elif role == 'succubus':
        session[1][message.author.id][2] = message.author.id
        await adapter.reply(message, "You have chosen to not entrance anyone tonight.", mentionauthor=True)
    elif role == 'warlock':
        session[1][message.author.id][2] = message.author.id
        await adapter.reply(message, "You have chosen not to curse anyone tonight.", mentionauthor=True)
        await wolfchat("**{}** has chosen not to curse anyone tonight.".format(get_name(message.author.id)))
    elif role in ['hunter', 'serial killer']:
        session[1][message.author.id][2] = message.author.id
        await adapter.reply(message, "You have chosen to not kill anyone tonight.", mentionauthor=True)
    elif role in ['guardian angel', 'bodyguard']:
        session[1][message.author.id][2] = 'pass'
        await adapter.reply(message, "You have chosen to not guard anyone tonight.", mentionauthor=True)
    elif role == 'piper':
        session[1][message.author.id][4] = [x for x in session[1][message.author.id][4] if x != 'charm']
        await adapter.reply(message, "You have chosen not to charm anyone tonight.", mentionauthor=True)
    elif role == 'turncoat':
        if 'sided2' in session[1][message.author.id][4]:
            return
        session[1][message.author.id][2] = 'pass'
        await adapter.reply(message, "You have chosen not to switch sides tonight.", mentionauthor=True)
    else:
        await adapter.reply(message, "wtf? (this is an error; please report to an admin", mentionauthor=True)
    await adapter.log(1, "{0} ({1}) PASS".format(get_name(message.author.id), message.author.id))

@cmd('cat', [0, 0], "```\n{0}cat takes no arguments\n\nFlips a cat.```")
async def cmd_cat(message, parameters):
    await adapter.reply(message, "The cat landed on **its feet**!", mentionauthor=True)

@cmd('fgoat', [1, 1], "```\n{0}fgoat <target>\n\nForcibly sends a goat to violently attack <target>.```")
async def cmd_fgoat(message, parameters):
    if parameters == '':
        await adapter.reply(message, commands['fgoat'][2].format(BOT_PREFIX), mentionauthor=True)
        return
    action = random.choice(['kicks', 'headbutts'])
    await adapter.send_lobby("**{}**'s goat walks by and {} **{}**.".format(message.author.name, action, parameters))

@cmd('guard', [2, 0], "```\n{0}guard <target>\n\nGuards <player>, preventing them from dying this night. Can guard yourself, however "
                      "cannot be used on the same target twice in a row.```", 'protect')
async def cmd_guard(message, parameters):
    if not session[0] or message.author.id not in session[1] or get_role(message.author.id, 'role') not in COMMANDS_FOR_ROLE['guard'] \
    or not session[1][message.author.id][0]:
        return
    if session[2]:
        await adapter.reply(message, "You may only guard players during the night.", mentionauthor=True)
        return
    if session[1][message.author.id][2]:
        if session[1][message.author.id][2] == 'pass':
            await adapter.reply(message, "You have already chosen to not guard anyone tonight.", mentionauthor=True)
        else:
            await adapter.reply(message, "You are already guarding **{}**.".format(get_name(session[1][message.author.id][2])), mentionauthor=True)
        return
    if "silence_totem2" in session[1][message.author.id][4]:
        await adapter.reply(message, "You have been silenced, and are unable to use any special powers.", mentionauthor=True)
        return
    else:
        if parameters == "":
            await adapter.reply(message, roles[session[1][message.author.id][1]][2], mentionauthor=True)
        else:
            player = get_player(parameters)
            if player:
                if 'lasttarget:' + player in session[1][message.author.id][4]: # so hacky but whaterver
                    await adapter.reply(message, "You already guarded **{}** last night. You may not guard the same player two nights in a row.".format(get_name(player)), mentionauthor=True)
                    return
                session[1][message.author.id][4][:] = [x for x in session[1][message.author.id][4] if not x.startswith('lasttarget:')]
                if not session[1][player][0]:
                    await adapter.reply(message, "Player **" + get_name(player) + "** is dead!", mentionauthor=True)
                    return
                if 'misdirection_totem2' in session[1][message.author.id][4]:
                    new_target = misdirect(message.author.id)
                    while 'lasttarget:' + new_target in session[1][message.author.id][4]:
                        new_target = misdirect(message.author.id)
                    player = new_target
                elif 'luck_totem2' in session[1][player][4]:
                    new_target = misdirect(player)
                    while 'lasttarget:' + new_target in session[1][message.author.id][4]:
                        new_target = misdirect(player)
                    player = new_target
                if get_role(message.author.id, 'role') == 'guardian angel':
                    session[1][message.author.id][4].append('lasttarget:' + player)
                if player == message.author.id:
                    if get_role(message.author.id, 'role') == 'guardian angel':
                        await adapter.reply(message, "You have chosen to guard yourself tonight.", mentionauthor=True)
                        session[1][message.author.id][2] = message.author.id
                        session[1][message.author.id][4].append("guarded")
                        await adapter.log(1, "{0} ({1}) GUARD SELF".format(get_name(message.author.id), message.author.id))
                    else:
                        await adapter.reply(message, "You cannot guard yourself. Use `pass` if you do not wish to guard anyone tonight.", mentionauthor=True)
                        return
                else:
                    await adapter.reply(message, "You have chosen to guard **{}**.".format(get_name(player)), mentionauthor=True)
                    session[1][message.author.id][2] = player
                    if get_role(message.author.id, 'role') == 'guardian angel':
                        session[1][player][4].append("guarded")
                    elif get_role(message.author.id, 'role') == 'bodyguard':
                        session[1][player][4].append("bodyguard:{}".format(message.author.id))
                    await adapter.send_user(player, "You can sleep well tonight, for you are being protected.")
                    await adapter.log(1, "{0} ({1}) GUARD {2} ({3})".format(get_name(message.author.id), message.author.id, get_name(player), player))
            else:
                await adapter.reply(message, "Could not find player " + parameters, mentionauthor=True)
                


######### END COMMANDS #############

def misdirect(player, alive_players=None):
    if not alive_players:
        alive_players = [x for x in session[1] if session[1][x][0]]
    return random.choice([alive_players[len(alive_players)-1] if alive_players.index(player) == 0 else alive_players[alive_players.index(player)-1], alive_players[0] if alive_players.index(player) == len(alive_players)-1 else alive_players[alive_players.index(player)+1]])

def has_privileges(level, message):
    if message.author.id == OWNER_ID:
        return True
    elif level == 1 and message.author.id in ADMINS:
        return True
    elif level == 0:
        return True
    else:
        return False

def pluralize(item, count): # can later be expanded to include irregular plurals (e.g. succubus -> succubi)
    if count <= 0:
        return ""
    elif count == 1:
        return "{} {}".format(count, item)
    else:
        return "{} {}s".format(count, item)

def quantified_items_grammatical_list(quantified_items_dict): # can later be expanded to include more than two items (there will be commas)
    quantified_items_list = []
    for item in quantified_items_dict:
        quantified_item = pluralize(item, quantified_items_dict[item])
        if quantified_item:
            quantified_items_list.append(quantified_item)
    if len(quantified_items_list) == 0:
        return ""
    elif len(quantified_items_list) == 1:
        return quantified_items_list[0]
    elif len(quantified_items_list) == 2:
        return quantified_items_list[0] + " and " + quantified_items_list[1]

async def parse_command(commandname, message, parameters):
    await adapter.log(0, 'Parsing command ' + commandname + ' with parameters `' + parameters + '` from ' + message.author.name + ' (' + message.author.id + ')')
    if commandname in commands:
        pm = 0
        if message.channel.is_private:
            pm = 1
        if has_privileges(commands[commandname][1][pm], message):
            try:
                await commands[commandname][0](message, parameters)
            except Exception:
                traceback.print_exc()
                print(session)
                msg = '```py\n{}\n```\n**session:**```py\n{}\n```'.format(traceback.format_exc(), session)
                await adapter.log(3, msg)
                await adapter.reply(message, "An error has occurred and has been logged.")
        elif has_privileges(commands[commandname][1][0], message):
            if session[0] and message.author.id in session[1] and session[1][message.author.id][0]:
                if commandname in COMMANDS_FOR_ROLE and (get_role(message.author.id, 'role') in COMMANDS_FOR_ROLE[commandname]\
                or not set(get_role(message.author.id, 'templates')).isdisjoint(set(COMMANDS_FOR_ROLE[commandname]))):
                    await adapter.reply(message, "Please use command " + commandname + " in channel.", mentionauthor=True)
        elif has_privileges(commands[commandname][1][1], message):
            if session[0] and message.author.id in session[1] and session[1][message.author.id][0]:
                if commandname in COMMANDS_FOR_ROLE and get_role(message.author.id, 'role') in COMMANDS_FOR_ROLE[commandname]:
                    try:
                        await adapter.send_user(message.author.id, "Please use command " + commandname + " in private message.")
                    except discord.Forbidden:
                        pass
            elif message.author.id in ADMINS:
                await adapter.reply(message, "Please use command " + commandname + " in private message.", mentionauthor=True)
        else:
            await adapter.log(2, 'User ' + message.author.name + ' (' + message.author.id + ') tried to use command ' + commandname + ' with parameters `' + parameters + '` without permissions!')

def balance_roles(massive_role_list, default_role='villager', num_players=-1):
    if num_players == -1:
        num_players = len(session[1])
    extra_players = num_players - len(massive_role_list)
    if extra_players > 0:
        massive_role_list += [default_role] * extra_players
        return (massive_role_list, "Not enough roles; added {} {} to role list".format(extra_players, default_role))
    elif extra_players < 0:
        random.shuffle(massive_role_list)
        removed_roles = []
        team_roles = [0, 0, 0]
        for role in massive_role_list:
            if role in WOLF_ROLES_ORDERED:
                team_roles[0] += 1
            elif role in VILLAGE_ROLES_ORDERED:
                team_roles[1] += 1
            elif role in NEUTRAL_ROLES_ORDERED:
                team_roles[2] += 1
        for i in range(-1 * extra_players):
            team_fractions = list(x / len(massive_role_list) for x in team_roles)
            roles_to_remove = set()
            if team_fractions[0] > 0.35:
                roles_to_remove |= set(WOLF_ROLES_ORDERED)
            if team_fractions[1] > 0.7:
                roles_to_remove |= set(VILLAGE_ROLES_ORDERED)
            if team_fractions[2] > 0.15:
                roles_to_remove |= set(NEUTRAL_ROLES_ORDERED)
            if len(roles_to_remove) == 0:
                roles_to_remove = set(roles)
                if team_fractions[0] < 0.25:
                    roles_to_remove -= set(WOLF_ROLES_ORDERED)
                if team_fractions[1] < 0.5:
                    roles_to_remove -= set(VILLAGE_ROLES_ORDERED)
                if team_fractions[2] < 0.05:
                    roles_to_remove -= set(NEUTRAL_ROLES_ORDERED)
                if len(roles_to_remove) == 0:
                    roles_to_remove = set(roles)
            for role in massive_role_list[:]:
                if role in roles_to_remove:
                    massive_role_list.remove(role)
                    removed_roles.append(role)
                    break
        return (massive_role_list, "Too many roles; removed {} from the role list".format(', '.join(sort_roles(removed_roles))))
    return (massive_role_list, '')

async def assign_roles(gamemode):
    massive_role_list = []
    roles_gamemode_template_list = []
    gamemode_roles = get_roles(gamemode, len(session[1]))

    if not gamemode_roles:
        # Second fallback just in case
        gamemode_roles = get_roles('default', len(session[1]))
        session[6] = 'default'

    # Generate list of roles

    for role in [x for x in gamemode_roles if x in roles]:
        if role in TEMPLATES_ORDERED and session[6].startswith('roles'):
            roles_gamemode_template_list += [role] * gamemode_roles[role]
        elif role not in TEMPLATES_ORDERED:
            massive_role_list += [role] * gamemode_roles[role]

    massive_role_list, debugmessage = balance_roles(massive_role_list)
    if debugmessage != '':
        await adapter.log(2, debugmessage)

    if session[6].startswith('roles'):
        session[7] = dict(dict((x, massive_role_list.count(x)) for x in roles if x in massive_role_list), **dict((y, roles_gamemode_template_list.count(y)) for y in TEMPLATES_ORDERED if y in roles_gamemode_template_list))
    else:
        session[7] = dict(gamemode_roles)

    random.shuffle(massive_role_list)
    for player in session[1]:
        role = massive_role_list.pop()
        session[1][player][1] = role
        if role == 'hunter':
            session[1][player][4].append('hunterbullet')
        elif role == 'matchmaker':
            session[1][player][4].append('match')
        elif role == 'amnesiac':
            session[1][player][4].append('role:{}'.format(random.choice(list(set(roles) - set(["minion", "matchmaker", "villager", "cultist", "amnesiac", "clone", "monster", "demoniac", "piper", "dullahan", "wild child"] + TEMPLATES_ORDERED)))))
            if 'role:hunter' in session[1][player][4]:
                session[1][player][4].append('hunterbullet')
            if 'role:priest' in session[1][player][4]:
                session[1][player][4].append('bless')
        elif role == 'priest':
            session[1][player][4].append('bless')
        elif role == 'clone':
            session[1][player][4].append('clone')

    for i in range(gamemode_roles['cursed villager'] if 'cursed villager' in gamemode_roles else 0):
        cursed_choices = [x for x in session[1] if get_role(x, 'role') not in\
        ACTUAL_WOLVES + ROLES_SEEN_WOLF + ['seer', 'oracle', 'jester', 'fool', 'executioner', 'hot potato'] and 'cursed' not in session[1][x][3]]
        if cursed_choices:
            cursed = random.choice(cursed_choices)
            session[1][cursed][3].append('cursed')
    for i in range(gamemode_roles['mayor'] if 'mayor' in gamemode_roles else 0):
        if gamemode == 'random':
            mayor_choices = [x for x in session[1] if 'mayor' not in session[1][x][3]]
        else:
            mayor_choices = [x for x in session[1] if get_role(x, 'role') not in\
        ['monster', 'jester', 'fool'] and 'mayor' not in session[1][x][3]]
        if mayor_choices:
            mayor = random.choice(mayor_choices)
            session[1][mayor][3].append('mayor')
            session[1][mayor][4].append('unrevealed')
    for i in range(gamemode_roles['gunner'] if 'gunner' in gamemode_roles else 0):
        if gamemode in ['chaos', 'random']:
            gunner_choices = [x for x in session[1] if ('gunner' not in session[1][x][3] and 'sharpshooter' not in session[1][x][3])]
        else:
            gunner_choices = [x for x in session[1] if get_role(x, 'role') not in \
            WOLF_ROLES_ORDERED + NEUTRAL_ROLES_ORDERED + ['priest'] and ('gunner' not in session[1][x][3] and 'sharpshooter' not in session[1][x][3])]
        if gunner_choices:
            pewpew = random.choice(gunner_choices)
            if get_role(pewpew, 'role') == 'village drunk':
                session[1][pewpew][3].append('gunner')
                if session[6] == 'mad':
                    session[1][pewpew][4] += ['bullet']
                else:
                    session[1][pewpew][4] += ['bullet'] * int(GUNNER_MULTIPLIER * len(session[1]) + 1) * DRUNK_MULTIPLIER
            elif random.random() > 0.2 or session[6] in ['aleatoire', 'bloodbath']:
                session[1][pewpew][3].append('gunner')
                if session[6] in ['mad', 'bloodbath']:
                    session[1][pewpew][4] += ['bullet']
                else:
                    session[1][pewpew][4] += ['bullet'] * int(GUNNER_MULTIPLIER * len(session[1]) + 1)
            else:
                session[1][pewpew][3].append('sharpshooter')
                if session[6] == 'mad':
                    session[1][pewpew][4] += ['bullet']
                else:
                    session[1][pewpew][4] += ['bullet'] * int(SHARPSHOOTER_MULTIPLIER * len(session[1]) + 1)
    gunners = [x for x in session[1] if 'gunner' in session[1][x][3]]
    for i in range(gamemode_roles['sharpshooter'] if 'sharpshooter' in gamemode_roles else 0):
        sharpshooter_choices = [x for x in gunners if 'sharpshooter' not in session[1][x][3]]
        if sharpshooter_choices:
            pewpew = random.choice(sharpshooter_choices)
            session[1][pewpew][3].remove('gunner')
            session[1][pewpew][4] = [x for x in session[1][pewpew][4] if x != 'bullet']
            session[1][pewpew][3].append('sharpshooter')
            session[1][pewpew][4] += ['bullet'] * int(SHARPSHOOTER_MULTIPLIER * len(session[1]) + 1)
    for i in range(gamemode_roles['assassin'] if 'assassin' in gamemode_roles else 0):
        if gamemode == 'random':
            assassin_choices = [x for x in session[1] if 'assassin' not in session[1][x][3]]
        else:
            assassin_choices = [x for x in session[1] if get_role(x, 'role') not in\
        ACTUAL_WOLVES + NEUTRAL_ROLES_ORDERED + ["traitor", "seer", "augur", "oracle", "harlot", "detective", "guardian angel"] and 'assassin' not in session[1][x][3]]
        if assassin_choices:
            assassin = random.choice(assassin_choices)
            session[1][assassin][3].append('assassin')
            if get_role(assassin, 'role') == 'village drunk':
                session[1][assassin][4].append('assassinate:{}'.format(random.choice([x for x in session[1] if x != assassin])))
    for i in range(gamemode_roles['blessed villager'] if 'blessed villager' in gamemode_roles else 0):
        if gamemode == 'random':
            blessed_choices = [x for x in session[1] if 'blessed' not in session[1][x][3]]
        else:
            blessed_choices = [x for x in session[1] if get_role(x, 'role') == 'villager' and not session[1][x][3]]
        if blessed_choices:
            blessed = random.choice(blessed_choices)
            session[1][blessed][3].append('blessed')
    for i in range(gamemode_roles['bishop'] if 'bishop' in gamemode_roles else 0):
        bishop_choices = [x for x in session[1] if get_role(x, 'role') != 'succubus' and 'bishop' not in session[1][x][3]]
        if bishop_choices:
            bishop = random.choice(bishop_choices)
            session[1][bishop][3].append('bishop')
    if gamemode == 'belunga':
        for player in session[1]:
            session[1][player][4].append('belunga_totem')

async def end_game(reason, winners=None):
    global faftergame
    await adapter.set_lobby_status(LobbyStatus.READY)
    if not session[0]:
        return
    session[0] = False
    if session[2]:
        if session[3][1]:
            session[4][1] += datetime.now() - session[3][1]
    else:
        if session[3][0]:
            session[4][0] += datetime.now() - session[3][0]
    msg = "<@{}> Game over! Night lasted **{:02d}:{:02d}**. Day lasted **{:02d}:{:02d}**. Game lasted **{:02d}:{:02d}**. \
          \n{}\n\n".format('> <@'.join(sort_players(session[1])), session[4][0].seconds // 60, session[4][0].seconds % 60,
          session[4][1].seconds // 60, session[4][1].seconds % 60, (session[4][0].seconds + session[4][1].seconds) // 60,
          (session[4][0].seconds + session[4][1].seconds) % 60, reason)
    if winners or session[6] == 'crazy':
        for player in session[1]:
            # ALTERNATE WIN CONDITIONS
            if session[1][player][0] and get_role(player, 'role') == 'crazed shaman':
                winners.append(player)
        winners = sort_players(set(winners)) # set ensures winners are unique
        if len(winners) == 0:
            msg += "No one wins!"
        elif len(winners) == 1:
            msg += "The winner is **{}**!".format(get_name(winners[0]))
        elif len(winners) == 2:
            msg += "The winners are **{}** and **{}**!".format(get_name(winners[0]), get_name(winners[1]))
        else:
            msg += "The winners are **{}**, and **{}**!".format('**, **'.join(map(get_name, winners[:-1])), get_name(winners[-1]))
    else:
        msg += "No one wins!"
    await adapter.send_lobby(msg)
    await adapter.log(1, "WINNERS: {}".format(winners))

    players = list(session[1])
    session[3] = [datetime.now(), datetime.now()]
    session[4] = [timedelta(0), timedelta(0)]
    session[6] = ''
    session[7] = {}
    
    global day_warning
    global day_timeout
    global night_warning
    global night_timeout
    day_warning = DEFAULT_DAY_WARNING
    day_timeout = DEFAULT_DAY_TIMEOUT
    night_warning = DEFAULT_NIGHT_WARNING
    night_timeout = DEFAULT_NIGHT_TIMEOUT

    player_dict = {}
    for player in players:
        player_dict[player] = ('game end', "bot")
    await player_deaths(player_dict)

    await adapter.unlock_lobby()

    if faftergame:
        # !faftergame <command> [<parameters>]
        # faftergame.content.split(' ')[0] is !faftergame
        command = faftergame.content.split(' ')[1]
        parameters = ' '.join(faftergame.content.split(' ')[2:])
        await commands[command][0](faftergame, parameters)
        faftergame = None

def win_condition():
    teams = {'village' : 0, 'wolf' : 0, 'neutral' : 0}
    injured_wolves = 0
    for player in session[1]:
        if session[1][player][0]:
            if 'injured' in session[1][player][4]:
                if get_role(player, 'actualteam') == 'wolf' and session[1][player][1] not in ['cultist', 'minion'] and 'entranced' not in session[1][player][4]:
                    injured_wolves += 1
            else:
                if session[1][player][1] in ['cultist', 'minion'] and session[6] != 'evilvillage':
                    teams['village'] += 1
                else:
                    teams[roles[session[1][player][1]][0]] += 1
    winners = []
    win_team = ''
    win_lore = ''
    win_msg = ''
    lovers = []
    players = session[1]
    for plr in players:
        for o in players[plr][4]:
            if o.startswith("lover:"):
                lvr = o.split(':')[1]
                if lvr in players:
                    if plr not in lovers and session[1][plr][0]:
                        lovers.append(plr)
                    if lvr not in lovers and session[1][lvr][0]:
                        lovers.append(lvr)
    if len([x for x in session[1] if session[1][x][0]]) == 0:
        win_team = 'no win'
        win_lore = 'Everyone died. The town sits abandoned, collecting dust.'
    elif len(lovers) == len([x for x in session[1] if session[1][x][0]]):
        win_team = 'lovers'
        win_lore = "Game over! The remaining villagers through their inseparable love for each other have agreed to stop all of this senseless violence and coexist in peace forever more. All remaining players win."
    elif len([x for x in session[1] if session[1][x][0] and (get_role(x, 'role') == 'succubus' or 'entranced' in session[1][x][4])]) == len([x for x in session[1] if session[1][x][0]]):
        win_team = 'succubi'
        win_lore = "Game over! The succub{} completely enthralled the village, making them officers in an ever-growing army set on spreading their control and influence throughout the entire world.".format('i' if len([x for x in session[1] if get_role(x, 'role') == 'succubus']) > 1 else 'us')
    elif len([x for x in session[1] if session[1][x][0] and (get_role(x, 'role') == 'piper' or 'charmed' in session[1][x][4])]) == len([x for x in session[1] if session[1][x][0]]):
        win_team = 'pipers'
        win_lore = "Game over! Everyone has fallen victim to the charms of the piper{0}. The piper{0} lead{1} the villagers away from the village, never to return...".format('' if len([x for x in session[1] if get_role(x, 'role') == 'piper']) < 2 else 's', 's' if len([x for x in session[1] if get_role(x, 'role') == 'piper']) < 2 else '')
    elif len([x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'serial killer']) >= len([x for x in session[1] if session[1][x][0]])/2:
        if [x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']:
            win_team = 'monster'
            win_lore = "Game over! The serial killer{0} stabbed all those in the village, except the monster{1}, causing the monster{1} to win.".format('s' if len([x for x in session[1] if get_role(x, 'role') == 'serial killer']) > 1 else '', 's' if len([x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']) > 1 else '')
        else:
            win_team = 'serial killers'
            win_lore = "Game over! The serial killer{0} stabbed all those in the village! The serial killer{0} walk{1} off, in the hope to successfully do the same at another location.".format('s' if len([x for x in session[1] if get_role(x, 'role') == 'serial killer']) > 1 else '', '' if len([x for x in session[1] if get_role(x, 'role') == 'serial killer']) > 1 else 's')
    elif teams['village'] + teams['neutral'] <= teams['wolf'] and not (session[6] == 'evilvillage' and teams['village']):
        if session[6] == 'evilvillage':
            if not teams['village']:
                if [x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']:
                    win_team = 'monster'
                    win_lore = "Game over! All the villagers are dead! As the cultists rejoice, they get destroyed by the monster{0}, causing the monster{0} to win.".format('s' if len([x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']) > 1 else '')
                elif not [x for x in session[1] if session[1][x][0] and get_role(x, 'role') not in ['cultist', 'minion']]:
                    win_team = 'no win'
                    win_lore = "Game over! All the villagers are dead, but the cult needed to sacrifice the wolves to accomplish that. The cult disperses shortly thereafter, and nobody wins."
                else:
                    win_team = 'wolf'
                    win_lore = "Game over! All the villagers are dead! The cultists rejoice with their wolf buddies and start plotting to take over the next village."
        elif [x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']:
            win_team = 'monster'
            win_lore = "Game over! The number of uninjured villagers is equal or less than the number of living wolves! The wolves overpower the villagers but then get destroyed by the monster{0}, causing the monster{0} to win.".format('s' if len([x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']) > 1 else '')
        else:
            win_team = 'wolf'
            win_lore = 'The number of uninjured villagers is equal or less than the number of living wolves! The wolves overpower the remaining villagers and devour them whole.'
    elif len([x for x in session[1] if session[1][x][0] and get_role(x, 'role') in ACTUAL_WOLVES + ['traitor']]) == 0 and len([x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'serial killer']) == 0:
        # old version: teams['wolf'] == 0 and injured_wolves == 0:
        if [x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']:
            win_team = 'monster'
            win_lore = "Game over! All the wolves are dead! As the villagers start preparing the BBQ, the monster{0} quickly kill{1} the remaining villagers, causing the monster{0} to win.".format('s' if len([x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']) > 1 else '', '' if len([x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']) > 1 else 's')
        else:
            if len([x for x in session[1] if session[1][x][0] and get_role(x, 'role') in ['cultist', 'minion']]) == teams['wolf'] and session[6] == 'evilvillage':
                win_team = 'village'
                win_lore = "Game over! All the wolves are dead! The villagers round up the remaining cultists, hang them, and live happily ever after."
            else:
                win_team = 'village'
                win_lore = 'All the wolves are dead! The surviving villagers gather the bodies of the dead wolves, roast them, and have a BBQ in celebration.'
    elif teams['village'] >= teams['wolf'] and session[6] == 'evilvillage':
        if [x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']:
            win_team = 'monster'
            win_lore = "Game over! The number of uninjured cultists is equal or less than the number of living villagers! as the villagers regain control over the village, the monster{0} quickly kill{1} the remaining villagers, causing the monster{0} to win.".format('s' if len([x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']) > 1 else '', '' if len([x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'monster']) > 1 else 's')
        elif not [x for x in session[1] if session[1][x][0] and get_role(x, 'role') in ['cultist', 'minion']]:
            win_team = 'village'
            win_lore = "Game over! All the cultists are dead! The now-exposed wolves are captured and killed by the remaining villagers. A BBQ party commences shortly thereafter."
        else:
            win_team = 'village'
            win_lore = "Game over! The number of uninjured cultists is equal or less than the number of living villagers! They manage to regain control of the village and dispose of the remaining cultists."
    else:
        return None
        
        
    for player in session[1]:
        lovers = []
        for n in session[1][player][4]:
            if n.startswith('lover:'):
                lovers.append(n.split(':')[1])
        role = get_role(player, 'role')
        templates = get_role(player, 'templates')
        if get_role(player, 'role') == 'piper' and win_team == 'pipers':
            winners.append(player)
        if (get_role(player, 'role') == 'succubus' or 'entranced' in session[1][player][4]) and win_team == 'succubi':
            winners.append(player)
        if get_role(player, 'actualteam') == win_team:
            winners.append(player)
        if [x for x in lovers if (session[1][x][0] and session[1][player][0])]:
            winners.append(player)
        if get_role(player, 'role') == 'vengeful ghost' and not session[1][player][0] and [x.split(':')[1] for x in session[1][player][4] if x.startswith("vengeance:")] and [x.split(':')[1] for x in session[1][player][4] if x.startswith("vengeance:")].pop() != win_team:
            winners.append(player)
        if (get_role(player, 'role') == 'amnesiac' or (get_role(player, 'role') == 'vengeful ghost' and session[1][player][0]) and win_team == 'village'):
            winners.append(player)
        if get_role(player, 'role') == 'jester' and 'lynched' in session[1][player][4]:
            winners.append(player)
        if get_role(player, 'role') == 'monster' and session[1][player][0] and win_team == 'monster':
            winners.append(player)
        if get_role(player, 'role') == 'clone' and session[1][player][0]:
            winners.append(player)
        if get_role(player, 'role') == 'lycan' and win_team == 'village':
            winners.append(player)
        if (get_role(player, 'role') == 'turncoat') and (('side:villagers' in session[1][player][4] and win_team == 'village') or ('side:wolves' in session[1][player][4] and win_team == 'wolf')):
            winners.append(player)
        if get_role(player, 'role') == 'serial killer' and session[1][player][0] and win_team == 'serial killers':
            winners.append(player)
        if get_role(player, 'role') == 'executioner' and 'win' in session[1][player][4]:
            winners.append(player)        
        if ((win_team != 'succubi' and 'entranced' in session[1][player][4]) or 'charmed' in session[1][player][4] or get_role(player, 'role') == 'hot potato') and player in winners:
            winners.remove(player)
    return [win_team, win_lore + '\n\n' + end_game_stats(), winners]

def end_game_stats():
    role_msg = ""
    role_dict = {}
    for role in roles:
        role_dict[role] = []
    for player in session[1]:
        if 'traitor' in session[1][player][4]:
            session[1][player][1] = 'traitor'
            session[1][player][4].remove('traitor')
        if 'wolf_cub' in session[1][player][4]:
            session[1][player][1] = 'wolf cub'
            session[1][player][4].remove('wolf_cub')
        role_dict[session[1][player][1]].append(player)
        if 'cursed' in session[1][player][3]:
            role_dict['cursed villager'].append(player)
        if 'gunner' in session[1][player][3]:
            role_dict['gunner'].append(player)
        if 'sharpshooter' in session[1][player][3]:
            role_dict['sharpshooter'].append(player)
        if 'assassin' in session[1][player][3]:
            role_dict['assassin'].append(player)
        if 'mayor' in session[1][player][3]:
            role_dict['mayor'].append(player)
        if 'bishop' in session[1][player][3]:
            role_dict['bishop'].append(player)
        if 'blessed' in session[1][player][3]:
            role_dict['blessed villager'].append(player)

    for key in sort_roles(role_dict):
        value = sort_players(role_dict[key])
        if len(value) == 0:
            pass
        elif len(value) == 1:
            role_msg += "The **{}** was **{}**. ".format(key, get_name(value[0]))
        elif len(value) == 2:
            role_msg += "The **{}** were **{}** and **{}**. ".format(roles[key][1], get_name(value[0]), get_name(value[1]))
        else:
            role_msg += "The **{}** were **{}**, and **{}**. ".format(roles[key][1], '**, **'.join(map(get_name, value[:-1])), get_name(value[-1]))

    lovers = []

    for player in session[1]:
        for o in session[1][player][4]:
            if o.startswith("lover:"):
                lover = o.split(':')[1]
                lovers.append(tuple(sort_players([player, lover])))
    lovers = list(set(lovers))
    # create a list of unique lover pairs
    sorted_second_lover = sort_players(x[1] for x in lovers)
    sorted_first_lover = sort_players(x[0] for x in lovers)
    # sort by second lover then first lover in the pair
    lovers_temp = []
    for l in sorted_second_lover:
        for pair in list(lovers):
            if pair[1] == l:
                lovers_temp.append(pair)
                lovers.remove(pair)
    lovers = list(lovers_temp)
    lovers_temp = []
    for l in sorted_first_lover:
        for pair in list(lovers):
            if pair[0] == l:
                lovers_temp.append(pair)
                lovers.remove(pair)
    lovers = list(lovers_temp)
    if len(lovers) == 0:
        pass
    elif len(lovers) == 1:
        # *map(get_name, lovers[0]) just applies get_name to each lover then unpacks the result into format
        role_msg += "The **lovers** were **{}/{}**. ".format(*map(get_name, lovers[0]))
    elif len(lovers) == 2:
        role_msg += "The **lovers** were **{}/{}** and **{}/{}**. ".format(*map(get_name, lovers[0] + lovers[1]))
    else:
        role_msg += "The **lovers** were {}, and **{}/{}**. ".format(
            ', '.join('**{}/{}**'.format(*map(get_name, x)) for x in lovers[:-1]), *map(get_name, lovers[-1]))
    return role_msg

def get_name(player):
    member = adapter.WEREWOLF_SERVER.get_member(player)
    if member:
        return str(member.display_name)
    else:
        return str(player)

def get_player(string):
    string = string.lower()
    users = []
    discriminators = []
    nicks = []
    users_contains = []
    nicks_contains = []
    for player in session[1]:
        if string == player.lower() or string.strip('<@!>') == player:
            return player
        member = adapter.WEREWOLF_SERVER.get_member(player)
        if member:
            if member.name.lower().startswith(string):
                users.append(player)
            if string.strip('#') == member.discriminator:
                discriminators.append(player)
            if member.display_name.lower().startswith(string):
                nicks.append(player)
            if string in member.name.lower():
                users_contains.append(player)
            if string in member.display_name.lower():
                nicks_contains.append(player)
        elif get_player(player).lower().startswith(string):
            users.append(player)
    if len(users) == 1:
        return users[0]
    if len(discriminators) == 1:
        return discriminators[0]
    if len(nicks) == 1:
        return nicks[0]
    if len(users_contains) == 1:
        return users_contains[0]
    if len(nicks_contains) == 1:
        return nicks_contains[0]
    return None

def sort_players(players):
    fake = []
    real = []
    for player in players:
        if adapter.WEREWOLF_SERVER.get_member(player):
            real.append(player)
        else:
            fake.append(player)
    return sorted(real, key=get_name) + sorted(fake, key=int)

def get_role(player, level):
    # level: {team: reveal team only; actualteam: actual team; seen: what the player is seen as; death: role taking into account cursed and cultist and traitor; actual: actual role}
    # (terminology: role = what you are, template = additional things that can be applied on top of your role)
    # cursed, gunner, blessed, mayor, assassin, and bishop are all templates
    # so you always have exactly 1 role, but can have 0 or more templates on top of that
    # revealing totem (and similar powers, like detective id) only reveal roles
    if player in session[1]:
        role = session[1][player][1]
        templates = session[1][player][3]
        if level == 'team':
            if roles[role][0] == 'wolf':
                if not role in ROLES_SEEN_VILLAGER:
                    return "wolf"
            return "village"
        elif level == 'actualteam':
            return roles[role][0]
        elif level == 'seen':
            seen_role = None
            if role in ROLES_SEEN_WOLF:
                seen_role = 'wolf'
            elif session[1][player][1] in ROLES_SEEN_VILLAGER:
                seen_role = 'villager'
            else:
                seen_role = role
            for template in templates:
                if template in ROLES_SEEN_WOLF:
                    seen_role = 'wolf'
                    break
                if template in ROLES_SEEN_VILLAGER:
                    seen_role = 'villager'
            return seen_role
        elif level == 'seenoracle':
            seen_role = get_role(player, 'seen')
            if seen_role != 'wolf':
                seen_role = 'villager'
            return seen_role
        elif level == 'death':
            returnstring = ''
            if role == 'traitor':
                returnstring += 'villager'
            else:
                returnstring += role
            return returnstring
        elif level == 'deathstats':
            returnstring = ''
            if role == 'traitor':
                returnstring += 'villager'
            else:
                returnstring += role
            return returnstring
        elif level == 'role':
            return role
        elif level == 'templates':
            return templates
        elif level == 'actual':
            return ' '.join(templates + [role])
    return None

def get_roles(gamemode, players):
    if gamemode.startswith('roles'):
        role_string = ' '.join(gamemode.split(' ')[1:])
        if role_string != '':
            gamemode_roles = {}
            separator = ','
            if ';' in role_string:
                separator = ';'
            for role_piece in role_string.split(separator):
                piece = role_piece.strip()
                if '=' in piece:
                    role, amount = piece.split('=')
                elif ':' in piece:
                    role, amount = piece.split(':')
                else:
                    return None
                amount = amount.strip()
                if amount.isdigit():
                    gamemode_roles[role.strip()] = int(amount)
            return gamemode_roles
    elif gamemode in gamemodes:
        if players in range(gamemodes[gamemode]['min_players'], gamemodes[gamemode]['max_players'] + 1):
            if gamemode == 'random':
                exit = False
                while not exit:
                    exit = True
                    available_roles = [x for x in roles if x not in TEMPLATES_ORDERED\
                                        and x not in ('villager', 'cultist')]
                    gamemode_roles = dict((x, 0) for x in available_roles)
                    gamemode_roles[random.choice([x for x in ACTUAL_WOLVES if x != 'wolf cub'])] += 1 # ensure at least 1 wolf that can kill
                    for i in range(players - 1):
                        gamemode_roles[random.choice(available_roles)] += 1
                    gamemode_roles['cursed villager'] = random.randrange(int(players/3))
                    gamemode_roles['gunner'] = random.randrange(int(players/4))
                    gamemode_roles['assassin'] = random.randrange(int(players/5))
                    gamemode_roles['mayor'] = random.randrange(int(players/7))
                    gamemode_roles['bishop'] = random.randrange(int(players/7))
                    gamemode_roles['blessed villager'] = random.randrange(int(players/8))
                    teams = {'village' : 0, 'wolf' : 0, 'neutral' : 0}
                    for role in gamemode_roles:
                        if role not in TEMPLATES_ORDERED:
                            teams[roles[role][0]] += gamemode_roles[role]
                    if teams['wolf'] >= 2/5*players:
                        exit = False
                for role in dict(gamemode_roles):
                    if gamemode_roles[role] == 0:
                        del gamemode_roles[role]
                return gamemode_roles
            else:
                gamemode_roles = {}
                for role in roles:
                    if role in gamemodes[gamemode]['roles'] and gamemodes[gamemode]['roles'][role][\
                    players - MIN_PLAYERS] > 0:
                        gamemode_roles[role] = gamemodes[gamemode]['roles'][role][players - MIN_PLAYERS]
                return gamemode_roles
    return None

def get_votes(totem_dict):
    voteable_players = [x for x in session[1] if session[1][x][0]]
    able_players = [x for x in voteable_players if 'injured' not in session[1][x][4]]
    vote_dict = {'abstain' : 0}
    for player in voteable_players:
        vote_dict[player] = 0
    able_voters = [x for x in able_players if totem_dict[x] == 0]
    for player in able_voters:
        if session[1][player][2] in vote_dict:
            vote_dict[session[1][player][2]] += 1
        if 'influence_totem' in session[1][player][4] and session[1][player][2] in vote_dict and session[1][player][2] != 'abstain':
            vote_dict[session[1][player][2]] += 1
    for player in [x for x in able_players if totem_dict[x] != 0]:
        if totem_dict[player] < 0:
            vote_dict['abstain'] += 1
        else:
            for p in [x for x in voteable_players if x != player]:
                vote_dict[p] += 1
    return vote_dict

def _autocomplete(string, lst):
    if string in lst:
        return (string, 1)
    else:
        choices = []
        for item in lst:
            if item.startswith(string):
                choices.append(item)
        if len(choices) == 1:
            return (choices[0], 1)
        else:
            return (choices, len(choices))

def verify_gamemode(gamemode, verbose=True):
    msg = ''
    good = True
    for i in range(gamemodes[gamemode]['max_players'] - gamemodes[gamemode]['min_players'] + 1):
        total = sum(gamemodes[gamemode]['roles'][role][i + gamemodes[gamemode]['min_players'] - MIN_PLAYERS] for role in gamemodes[gamemode]['roles']\
        if role not in TEMPLATES_ORDERED)
        msg += str(total)
        if total != i + gamemodes[gamemode]['min_players'] and total != 0:
            good = False
            msg += ' - should be ' + str(i + gamemodes[gamemode]['min_players'])
        msg += '\n'
    msg = msg[:-1]
    if verbose:
        return msg
    else:
        return good

def verify_gamemodes(verbose=True):
    msg = ''
    good = True
    for gamemode in sorted(gamemodes):
        msg += gamemode + '\n'
        result = verify_gamemode(gamemode)
        resultlist = result.split('\n')
        for i in range(len(resultlist)):
            if resultlist[i] != str(i + gamemodes[gamemode]['min_players']) and resultlist[i] != '0':
                msg += result
                good = False
                break
        else:
            msg += 'good'
        msg += '\n\n'
    if verbose:
        return msg
    else:
        return good

async def wolfchat(message, author=''):
    if isinstance(message, discord.Message):
        author = message.author.id
        msg = message.content
    else:
        msg = str(message)

    member = adapter.WEREWOLF_SERVER.get_member(author)
    if member:
        athr = member.display_name
    else:
        athr = author
    for wolf in [x for x in session[1] if x != author and session[1][x][0] and session[1][x][1] in WOLFCHAT_ROLES]:
        pfx = "**[Wolfchat]**"
        if athr != '':
            pfx += " message from **{}**".format(athr)
        await adapter.send_user(wolf, "{}: {}".format(pfx, msg))

async def player_idle(message):
    while message.author.id in session[1] and not session[0]:
        await asyncio.sleep(1)
    while message.author.id in session[1] and session[0] and session[1][message.author.id][0]:
        def check(msg):
            if not message.author.id in session[1] or not session[1][message.author.id][0] or not session[0]:
                return True
            if msg.author.id == message.author.id and msg.channel.id == adapter.GAME_CHANNEL.id:
                return True
            return False
        msg = await adapter.wait_for_message(author=message.author, channel=adapter.GAME_CHANNEL, timeout=PLAYER_TIMEOUT, check=check)
        if msg == None and message.author.id in session[1] and session[0] and session[1][message.author.id][0]:
            await adapter.send_lobby(message.author.mention + "**, you have been idling for a while. Please say something soon or you might be declared dead.**")
            await adapter.send_user(message.author.id, "**You have been idling in #" + adapter.GAME_CHANNEL.name + " for a while. Please say something soon or you might be declared dead.**")
            msg = await adapter.wait_for_message(author=message.author, channel=adapter.GAME_CHANNEL, timeout=PLAYER_TIMEOUT2, check=check)
            if msg == None and message.author.id in session[1] and session[0] and session[1][message.author.id][0]:
                if session[6] == 'noreveal':
                    await adapter.send_lobby("**" + get_name(message.author.id) + "** didn't get out of bed for a very long time and has been found dead.")
                else:
                    await adapter.send_lobby("**" + get_name(message.author.id) + "** didn't get out of bed for a very long time and has been found dead. "
                                          "The survivors bury the **" + get_role(message.author.id, 'death') + '**.')
                if message.author.id in stasis:
                    stasis[message.author.id] += QUIT_GAME_STASIS
                else:
                    stasis[message.author.id] = QUIT_GAME_STASIS
                await player_deaths({message.author.id : ('idle', "bot")})
                await check_traitor()
                await adapter.log(1, "{} ({}) IDLE OUT".format(message.author.display_name, message.author.id))

def is_online(user_id):
    member = adapter.WEREWOLF_SERVER.get_member(user_id)
    if member and member.status in [discord.Status.online, discord.Status.idle]:
        return True
    return False

async def player_deaths(players_dict): # players_dict = {dead : (reason, kill_team), ...}
    for player in players_dict:
        reason = players_dict[player][0]
        kill_team = players_dict[player][1]
        if player not in session[1]:
            return
        ingame = 'IN GAME'
        if session[0] and reason != 'game cancel':
            session[1][player][0] = False
            lovers = []
            for o in session[1][player][4]:
                if o.startswith('lover:'):
                    lovers.append(o.split(":")[1])
            assassin_target = ""
            for o in session[1][player][4]:
                if o.startswith('assassinate:') and "assassin" in get_role(player, "templates") and kill_team != "bot":
                    assassin_target = o.split(":")[1]
                    break
                    
            if session[0]:
                if assassin_target:
                    if session[1][assassin_target][0] and assassin_target not in players_dict and not ("protection_totem2" in session[1][assassin_target][4] or "guarded" in session[1][assassin_target][4]) and not 'blessed' in get_role(assassin_target, 'templates') and not [x for x in session[1][assassin_target][4] if x.startswith('bodyguard:')]:
                        await adapter.send_lobby("Before dying, **{0}** quickly slits **{1}**'s throat. The village mourns the loss of a{2} **{3}**.".format(get_name(player), get_name(assassin_target), "n" if get_role(assassin_target, "death").lower()[0] in ['a', 'e', 'i', 'o', 'u'] else "", get_role(assassin_target, "death")))
                        await player_deaths({assassin_target : ("assassination", get_role(player, 'actualteam'))})
                    elif 'blessed' in get_role(assassin_target, 'templates'):
                        try:
                            await adapter.send_user(player, "**{0}** seems to be blessed, causing your assassination attempt to fail.".format(get_name(assassin_target)))
                        except discord.Forbidden:
                            pass
                    elif "protection_totem2" in session[1][assassin_target][4]:
                        await adapter.send_lobby("Before dying, **{0}** quickly attempts to slit **{1}**'s throat; however, {1}'s totem emits a brilliant flash of light, causing the attempt to miss.".format(get_name(player), get_name(assassin_target)))
                    elif "guarded" in session[1][assassin_target][4]:
                        await adapter.send_lobby("Before dying, **{0}** quickly attempts to slit **{1}**'s throat; however, a guardian angel was on duty and able to foil the attempt.".format(get_name(player), get_name(assassin_target)))
                    elif [x for x in session[1][assassin_target][4] if x.startswith('bodyguard:')]:
                        await adapter.send_lobby("Sensing danger, **{2}** shoves **{1}** aside to save them from **{0}**.".format(get_name(player), get_name(assassin_target), get_name([x for x in session[1][assassin_target][4] if x.startswith('bodyguard:')].pop().split(':')[1])))
                for lover in lovers:
                    if session[1][lover][0] and kill_team != "bot" and lover not in players_dict:
                        await adapter.send_lobby("Saddened by the loss of their lover, **{0}**, a{1} **{2}**, commits suicide.".format(get_name(lover), "n" if get_role(lover, "death").lower()[0] in ['a', 'e', 'i', 'o', 'u'] else "", get_role(lover, "death")))
                        await player_deaths({lover : ("lover suicide", kill_team)})
                        
                #mad scientist target choosing
                mad_kills = []
                if get_role(player, 'role') == 'mad scientist' and kill_team != "bot":
                    players = [x for x in session[1]]
                    #mad scientist skips dead players if over 16p, random mode, rapidfire mode (or maelstrom)
                    skip_dead = False
                    if len(players) > 15 or session[6] in ['random', 'rapidfire']:
                        skip_dead = True
                    first = players.index(player)
                    #look for the first players not dead (or break after one loop)
                    while not session[1][players[first]][0]:
                        if first == 0:
                            first = len(players) - 1
                        else:
                            first -= 1
                        if not skip_dead:
                            break
                    if session[1][players[first]][0]:
                        mad_kills.append(players[first])
                    #the same on the other side
                    second = players.index(player)
                    while not session[1][players[second]][0]:
                        if second == len(players) - 1:
                            second = 0
                        else:
                            second += 1
                        if not skip_dead:
                            break
                    if session[1][players[second]][0]:
                        mad_kills.append(players[second])
                        
                #kill those next to the mad scientist if they aren't protected
                if mad_kills:
                    for mad_target in mad_kills:
                        if "blessed" in session[1][mad_target][4]:
                            mad_kills.remove(mad_target)
                        elif "protection_totem2" in session[1][mad_target][4]:
                            await adapter.send_lobby("Before the chemical can harm **{1}**, their totem flashes and they are teleported away from **{0}**.".format(get_name(player), get_name(mad_target)))
                            mad_kills.remove(mad_target)
                        elif "guarded" in session[1][mad_target][4]:
                            await adapter.send_lobby("Sensing danger, a guardian angel whisks **{1}** away from **{0}**.".format(get_name(player), get_name(mad_target)))
                            mad_kills.remove(mad_target)
                        for bodyguard in [x for x in session[1] if get_role(x, 'role') == "bodyguard" and session[1][x][2] == mad_target]:
                            if bodyguard and mad_target in mad_kills:
                                await adapter.send_lobby("Sensing danger, **{2}** shoves **{1}** aside to save them from **{0}**.".format(get_name(player), get_name(mad_target), get_name(bodyguard)))
                                mad_kills.remove(mad_target)
                                if bodyguard not in mad_kills:
                                    mad_kills.append(bodyguard)
                    if len(mad_kills) == 2:
                        await adapter.send_lobby("**{0}** throws a potent chemical into the crowd. **{1}**, a{2} **{3}**, and **{4}**, a{5} **{6}**, are hit and die.".format(get_name(player), get_name(mad_kills[0]), "n" if get_role(mad_kills[0], "death").lower()[0] in ['a', 'e', 'i', 'o', 'u'] else "", get_role(mad_kills[0], "death"), get_name(mad_kills[1]), "n" if get_role(mad_kills[1], "death").lower()[0] in ['a', 'e', 'i', 'o', 'u'] else "", get_role(mad_kills[1], "death")))
                        await player_deaths({mad_kills[0] : ("mad scientist", 'village'), mad_kills[1] : ("mad scientist", 'village')})
                    elif len(mad_kills) == 1:
                        await adapter.send_lobby("**{0}** throws a potent chemical into the crowd. **{1}**, a{2} **{3}**, is hit and dies.".format(get_name(player), get_name(mad_kills[0]), "n" if get_role(mad_kills[0], "death").lower()[0] in ['a', 'e', 'i', 'o', 'u'] else "", get_role(mad_kills[0], "death")))
                        await player_deaths({mad_kills[0] : ("mad scientist", 'village')})
                        
                if 'desperation_totem' in session[1][player][4] and reason == "lynch":
                    end_voter = ""
                    for x in session[1]:
                        if max(list(chain.from_iterable([[i for i in session[1][x][4] if i.startswith("vote:")] for x in session[1] if session[1][x][0]]))) in session[1][x][4] and session[1][x][2] == player:
                            end_voter = x
                    if end_voter and end_voter not in players_dict and get_role(player, 'role') != 'fool':
                        await adapter.send_lobby("As the noose is being fitted, **{0}**'s totem emits a brilliant flash of light. When the villagers are able to see again, they discover that **{1}**, a{2} **{3}**, has fallen over dead.".format(get_name(player), get_name(end_voter), "n" if get_role(end_voter, "death").lower()[0] in ['a', 'e', 'i', 'o', 'u'] else "", get_role(end_voter, "death")))
                        await player_deaths({end_voter : ("desperation", get_role(player, 'actualteam'))})
                
                #clone taking the dead's role
                for clone in [x for x in session[1] if (session[1][x][0] and get_role(x, 'role') == "clone" and "clone:{}".format(player) in session[1][x][4])]:
                    role = get_role(player, 'role')
                    cloning = player
                    #finding final target from who the clones were cloning
                    if role == "clone":
                        while role == 'clone' and not session[1][cloning][0]:
                            for new_target in [x for x in session[1][player][4] if x.startswith('clone:')]:
                                session[1][clone][4].append(new_target)
                                session[1][clone][4].remove("clone:{}".format(cloning))
                                cloning = (new_target.split(':')[1])
                                role == get_role(cloning, 'role')
                        await adapter.send_user(clone, "Your target was a clone and you are now cloning their target, **{0}**.".format(get_name(cloning)))
                                
                    #if the clone target is dead (in case we cloned a clone but their target is alive)
                    if not session[1][cloning][0]:
                        if role == "amnesiac":
                            role = [x.split(':')[1].replace("_", " ") for x in session[1][player][4] if x.startswith("role:")].pop()
                        if role == "priest" and "bless" in session[1][player][4]:
                            session[1][clone][4].append("bless")
                        elif role == "hunter" and "hunterbullet" in session[1][player][4]:
                            session[1][clone][4].append("hunterbullet")
                        elif role == "piper"  and "charmed" in session[1][clone][4]:
                            session[1][clone][4].remove("charmed")
                        elif role == "succubus"  and "entranced" in session[1][clone][4]:
                            session[1][clone][4].remove("entranced")
                        elif role == "executioner":
                            if [x for x in session[1][player][4] if x.startswith('execute:')]:
                                session[1][clone][4].append([x for x in session[1][player][4] if x.startswith('execute:')][0])
                                if 'win' in session[1][player][4]:
                                    session[1][clone][4].append('win')
                            else:
                                if [x for x in [y for y in session[1] if session[1][y][0]] if get_role(x, 'actualteam') == 'village']:
                                    session[1][clone][4].append('execute:{}'.format(random.choice([x for x in [y for y in session[1] if session[1][y][0]] if get_role(x, 'actualteam') == 'village'])))
                                else:
                                    role = 'jester'
                                    session[1][clone][4].append('executioner')
                        elif role == "jester" and "executioner" in session[1][player][4]:
                            session[1][clone][4].append("executioner")
                        session[1][clone][1] = role
                        await adapter.send_user(clone, "You have cloned your target and are now a **{0}**.\nYour role is **{0}**. {1}\n".format(role, roles[role][2]))
                        if role == 'executioner':
                            exe_target = [x for x in session[1][clone][4] if x.startswith('execute:')][0].strip('execute:')
                            if 'win' in session[1][clone][4]:
                                await adapter.send_user(clone, 'Your target was **{}**. This player was lynched, so you won.'.format(get_name(exe_target)))
                            else:
                                await adapter.send_user(clone, 'Your target for lynch is **{}**.'.format(get_name(exe_target)))
                        elif role == "minion":
                            living_players_string = []
                            for plr in [x for x in session[1] if session[1][x][0]]:
                                temprole = get_role(plr, 'role')
                                role_string = []
                                if roles[temprole][0] == 'wolf' and temprole not in ['minion', 'cultist']:
                                    role_string.append(temprole)
                                living_players_string.append("{} ({}){}".format(get_name(plr), plr,
                                ' ({})'.format(' '.join(role_string)) if role_string else ''))
                            await adapter.send_user(clone, 'Living players: ```basic\n' + '\n'.join(living_players_string) + '\n```')
                        if role in WOLFCHAT_ROLES:
                            await wolfchat("{0} is now a **{1}**!".format(get_name(clone), role))

                if get_role(player, 'role') ==  'succubus' and not [x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'succubus']:
                    if kill_team != 'bot':
                        foul_dict = {}
                        foul_message = ''
                        entranced_list = [x for x in session[1] if session[1][x][0] and "entranced" in session[1][x][4] and x not in players_dict]
                        for entranced in entranced_list:
                            foul_dict[entranced] = ('foul curse', kill_team)
                        if len(entranced_list) == 1:
                            foul_message = "As the last remaining succubus dies, a foul curse causes **{}**, a{} **{}** to wither away and die in front of the astonished village.\n".format(get_name(entranced), "n" if get_role(entranced, "death").lower()[0] in ["a", "e", "i", "o", "u"] else "", get_role(entranced, "death"))
                        elif len(entranced_list) > 1:
                            foul_message = "As the last remaining succubus dies, a foul curse causes **{}**, and **{}**, a{} **{}** to wither away and die in front of the astonished village.\n".format("**, **".join(get_name(x) + "**, a{} **".format("n" if get_role(x, "death").lower()[0] in ["a", "e", "i", "o", "u"] else "") + get_role(x, 'death') for x in entranced_list[:-1]), get_name(entranced_list[-1]), "n" if get_role(entranced_list[-1], "death").lower()[0] in ["a", "e", "i", "o", "u"] else "", get_role(entranced_list[-1], "death"))
                        if foul_message:
                            await adapter.send_lobby(foul_message)
                            await player_deaths(foul_dict)
                    else:
                        for entranced in [x for x in session[1] if session[1][x][0] and 'entranced' in session[1][x][4] and x not in players_dict]:
                            session[1][entranced][4].remove('entranced')
                            await adapter.send_user(entranced, "You are no longer entranced. **Your win conditions have reset to normal.**")
                if get_role(player, 'role') == "vengeful ghost" and (kill_team != "bot" and not reason == 'gunner suicide'):
                    session[1][player][4].append("vengeance:{}".format(kill_team))
                    await adapter.send_user(player, "OOOooooOOOOooo! You are the **vengeful ghost**. It is now your job to exact your revenge on the **{0}** that killed you.".format('villagers' if kill_team == 'village' else 'wolves'))
                if get_role(player, 'role') == 'piper' and not [x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'piper']:
                    for player_ in session[1]:
                        session[1][player_][4] = [x for x in session[1][player_][4] if not x in ['charmed', 'tocharm']]
                for plr in [x for x in session[1] if session[1][x][0]]:
                    if get_role(plr, 'role') == 'executioner' and 'win' not in session[1][plr][4] and [x for x in session[1][plr][4] if x.startswith('execute:')]:
                        if [x for x in session[1][plr][4] if x.startswith('execute:')][0].strip('execute:') == player:
                            session[1][plr][1] = 'jester'
                            session[1][plr][4].append('executioner')
                            session[1][plr][4].remove([x for x in session[1][plr][4] if x.startswith('execute:')][0])
                            await adapter.send_user(plr, 'Your target has died, not via lynch. You have now become a **jester**.\nYour role is **jester**. ' + roles['jester'][2] + '\n')
                #timelord stuff
                global day_warning
                global day_timeout
                global night_warning
                global night_timeout
                if get_role(player, 'role') == 'time lord' and (kill_team != 'bot') and day_warning != 45:
                    day_warning = 45
                    day_timeout = 60
                    night_warning = 20
                    night_timeout = 30
                    await adapter.send_lobby("The time lord has died. Night will now only last **{0}** seconds, and day **{1}** seconds. Better be speedy!".format(night_timeout, day_timeout))
                    #reset day timer?
                    
                
        else:
            ingame = 'NOT IN GAME'
            del session[1][player]
        await adapter.remove_player_role(player)
        if session[0] and kill_team != "bot":
            if get_role(player, 'role') == 'wolf cub':
                for p in session[1]:
                    if session[1][p][0] and get_role(p, 'role') in ACTUAL_WOLVES + ['traitor']:
                        session[1][p][4].append('angry')
        for p in [x for x in session[1] if "assassin" in get_role(x, "templates") and "assassinate:{}".format(player) in session[1][x][4]]:
            session[1][p][4] = [x for x in session[1][p][4] if x != "assassinate:{}".format(player)]
            if get_role(p, 'role') == 'village drunk':
                session[1][p][4].append('assassinate:{}'.format(random.choice([x for x in session[1] if x != p])))
                
        await adapter.log(0, "{} ({}) PLAYER DEATH {} FOR {}".format(get_name(player), player, ingame, reason))

async def check_traitor():
    if not session[0] and win_condition() == None:
        return
    wolf_cub_turned = False
    for other in [session[1][x][4] for x in session[1]]:
        if 'traitor' in other:
            # traitor already turned
            return
    wolf_team_alive = [x for x in session[1] if session[1][x][0] and get_role(x, 'role') in [
        'traitor'] + ACTUAL_WOLVES]
    if len(wolf_team_alive) == 0:
        # no wolves alive; don't play traitor turn message
        return
    wolf_team_no_traitors = [x for x in wolf_team_alive if get_role(x, 'role') != 'traitor']
    wolf_team_no_cubs = [x for x in wolf_team_no_traitors if get_role(x, 'role') != 'wolf cub']
    if len(wolf_team_no_cubs) == 0:
        cubs = [x for x in wolf_team_alive if get_role(x, 'role') == 'wolf cub']
        if cubs:
            await adapter.log(1, ', '.join(cubs) + " grew up into wolf")
            for cub in cubs:
                session[1][cub][4].append('wolf_cub')
                session[1][cub][1] = 'wolf'
                await adapter.send_user(cub, "You have grown up into a wolf and vowed to take revenge for your dead parents!")
                await adapter.send_lobby("**The villagers listen horrified as they hear growling deepen in pitch. The wolf will do whatever it takes to avenge their parents!**")
    if len(wolf_team_no_traitors) == 0:
        traitors = [x for x in wolf_team_alive if get_role(x, 'role') == 'traitor']
        await adapter.log(1, ', '.join(traitors) + " turned into wolf")
        for traitor in traitors:
            session[1][traitor][4].append('traitor')
            session[1][traitor][1] = 'wolf'
            await adapter.send_user(traitor, "HOOOOOOOOOWL. You have become... a wolf!\nIt is up to you to avenge your fallen leaders!")
        if session[6] != 'noreveal':
            await adapter.send_lobby("**The villagers, during their celebrations, are frightened as they hear a loud howl. The wolves are not gone!**")

def sort_roles(role_list):
    role_list = list(role_list)
    result = []
    for role in WOLF_ROLES_ORDERED + VILLAGE_ROLES_ORDERED + NEUTRAL_ROLES_ORDERED + TEMPLATES_ORDERED:
        result += [role] * role_list.count(role)
    return result

async def run_game():
    await adapter.set_lobby_status(LobbyStatus.IN_GAME)
    session[0] = True
    session[2] = False
    if session[6] == '':
        vote_dict = {}
        for player in session[1]:
            vote = session[1][player][2]
            if vote in vote_dict:
                vote_dict[vote] += 1
            elif vote != '':
                vote_dict[vote] = 1
        for gamemode in vote_dict:
            if vote_dict[gamemode] >= len(session[1]) // 2 + 1:
                session[6] = gamemode
        if not session[6]:
            #setting the mode taking votes and chances into consideration for probabilities
            #aleatoire, charming, default, evilvillage, foolish, lycan, mad, mudkip, and noreveal
            ALEATOIRE = gamemodes['aleatoire']['chance'] 
            if len(session[1]) < gamemodes['aleatoire']['min_players'] or len(session[1]) > gamemodes['aleatoire']['max_players']:
                ALEATOIRE = 0
            elif 'aleatoire' in vote_dict:
                ALEATOIRE += int((vote_dict['aleatoire']/len(session[1])) * 200)
            CHARMING = gamemodes['charming']['chance'] 
            if len(session[1]) < gamemodes['charming']['min_players'] or len(session[1]) > gamemodes['charming']['max_players']:
                CHARMING = 0
            elif 'charming' in vote_dict:
                CHARMING += int((vote_dict['charming']/len(session[1])) * 200)
            DEFAULT = gamemodes['default']['chance'] 
            if len(session[1]) < gamemodes['default']['min_players'] or len(session[1]) > gamemodes['default']['max_players']:
                DEFAULT = 0
            elif 'default' in vote_dict:
                DEFAULT += int((vote_dict['default']/len(session[1])) * 200)
            EVIL = gamemodes['evilvillage']['chance'] 
            if len(session[1]) < gamemodes['evilvillage']['min_players'] or len(session[1]) > gamemodes['evilvillage']['max_players']:
                EVIL = 0
            elif 'evilvillage' in vote_dict:
                EVIL += int((vote_dict['evilvillage']/len(session[1])) * 200)
            FOOLISH = gamemodes['foolish']['chance'] 
            if len(session[1]) < gamemodes['foolish']['min_players'] or len(session[1]) > gamemodes['foolish']['max_players']:
                FOOLISH = 0
            elif 'foolish' in vote_dict:
                FOOLISH += int((vote_dict['foolish']/len(session[1])) * 200)
            LYCAN = gamemodes['lycan']['chance'] 
            if len(session[1]) < gamemodes['lycan']['min_players'] or len(session[1]) > gamemodes['lycan']['max_players']:
                LYCAN = 0
            elif 'lycan' in vote_dict:
                LYCAN += int((vote_dict['lycan']/len(session[1])) * 200)
            MAD = gamemodes['mad']['chance'] 
            if len(session[1]) < gamemodes['mad']['min_players'] or len(session[1]) > gamemodes['mad']['max_players']:
                MAD = 0
            elif 'mad' in vote_dict:
                MAD += int((vote_dict['mad']/len(session[1])) * 200)
            MUDKIP = gamemodes['mudkip']['chance'] 
            if len(session[1]) < gamemodes['mudkip']['min_players'] or len(session[1]) > gamemodes['mudkip']['max_players']:
                MUDKIP = 0
            elif 'mudkip' in vote_dict:
                MUDKIP += int((vote_dict['mudkip']/len(session[1])) * 200)
            NOREVEAL = gamemodes['noreveal']['chance'] 
            if len(session[1]) < gamemodes['noreveal']['min_players'] or len(session[1]) > gamemodes['noreveal']['max_players']:
                NOREVEAL = 0
            elif 'noreveal' in vote_dict:
                NOREVEAL += int((vote_dict['noreveal']/len(session[1])) * 200)
            mode = (random.choice((['aleatoire'] * ALEATOIRE + ['charming'] * CHARMING + ['default'] * DEFAULT + ['evilvillage'] * EVIL\
            + ['foolish'] * FOOLISH + ['lycan'] * LYCAN + ['mad'] * MAD + ['mudkip'] * MUDKIP + ['noreveal'] * NOREVEAL)))
            session[6] = mode
    for player in session[1]:
        session[1][player][1] = ''
        session[1][player][2] = ''
    await adapter.lock_lobby()
    if not get_roles(session[6], len(session[1])):
        session[6] = 'default' # Fallback if invalid number of players for gamemode or invalid gamemode somehow

    for stasised in [x for x in stasis if stasis[x] > 0]:
        stasis[stasised] -= 1
    await adapter.send_lobby("<@{}>, Welcome to Werewolf, the popular detective/social party game (a theme of Mafia). "
                              "Using the **{}** game mode with **{}** players.\nAll players check for PMs from me for instructions. "
                              "If you did not receive a pm, please try `{}myrole` to resend your role.".format('> <@'.join(sort_players(session[1])),
                              'roles' if session[6].startswith('roles') else session[6], len(session[1]), config.BOT_PREFIX))
    for i in range(RETRY_RUN_GAME):
        try:
            if datetime.now().date() == __import__('datetime').date(2018, 4, 1):
                gamemode = "mudkip" if session[6] == 'default' else ('default' if session[6] == 'mudkip' else session[6])
            else:
                gamemode = session[6]
            await assign_roles(gamemode)
            break
        except:
            await adapter.log(2, "Role attribution failed with error: ```py\n{}\n```".format(traceback.format_exc()))
    else:
        msg = await adapter.send_lobby("<@{}>, role attribution failed 3 times. Cancelling game. "
                                                                          "Here is some debugging info:```py\n{}\n```".format(
                  '> <@'.join(sort_players(session[1])), session))
        await cmd_fstop(msg, '-force')
        return

    for i in range(RETRY_RUN_GAME):
        try:
            if i == 0:
                await game_loop()
            else:
                await game_loop(session)
            break
        except:
            await adapter.send_lobby("<@{}>, game loop broke. Attempting to resume game...".format(
                '> <@'.join(sort_players(session[1])), session))
            await adapter.log(3, "Game loop broke with error: ```py\n{}\n```".format(traceback.format_exc()))
    else:
        msg = await adapter.send_lobby("<@{}>, game loop broke 3 times. Cancelling game.".format(
                  '> <@'.join(sort_players(session[1])), session))
        await cmd_fstop(msg, '-force')

















# GAME LOOP

async def game_loop(ses=None):
    if ses:
        await adapter.send_lobby("<@{}>, Welcome to Werewolf, the popular detective/social party game (a theme of Mafia). "
                              "Using the **{}** game mode with **{}** players.\nAll players check for PMs from me for instructions. "
                              "If you did not receive a pm, please try `{}myrole` to resend your role.".format('> <@'.join(sort_players(session[1])),
                              'roles' if session[6].startswith('roles') else session[6], len(session[1]), config.BOT_PREFIX))
        globals()['session'] = ses
    await adapter.log(1, "Game object: ```py\n{}\n```".format(session))
    night = 1
    global day_warning
    global day_timeout
    global night_warning
    global night_timeout
    # GAME START
    while win_condition() == None and session[0]:
        if not session[2]: # NIGHT
            session[3][0] = datetime.now()
            log_msg = ['SUNSET LOG:']
            num_kills = 1
            for player in session[1]:
                role = get_role(player, 'role')
                if "silence_totem2" not in session[1][player][4]:
                    if role in ['crazed shaman', 'wolf shaman'] and session[1][player][0]:
                        if role == 'wolf shaman':
                            if session[6] == "mudkip":
                                session[1][player][4].append("totem:{}".format(random.choice(["protection_totem", "misdirection_totem"])))
                            else:
                                session[1][player][4].append("totem:{}".format(random.choice(WOLF_SHAMAN_TOTEMS)))
                        elif role == 'crazed shaman':
                            session[1][player][2] = random.choice(list(totems))
                        log_msg.append("{} ({}) HAS {}".format(get_name(player), player, (session[1][player][2] if role != "wolf shaman" else [x.split(":")[1] for x in session[1][player][4] if x.startswith("totem:")].pop())))
                    elif role == 'doomsayer':
                        session[1][player][4].append('doom:{}'.format(random.choice(['sick', 'lycan', 'death'])))
                    elif role == 'piper':
                        session[1][player][4].append('charm')
                else:
                    if role in ['crazed shaman', 'piper'] and session[1][player][0]:
                        session[1][player][2] = player
                if role == 'shaman' and session[1][player][0]:
                    if session[6] == "mudkip":
                        session[1][player][2] = random.choice(
                            ["pestilence_totem", "death_totem"]) if not night == 1 else "death_totem"
                    elif session[6] == 'aleatoire':
                        # protection (40%), death (20%), retribution (20%), silence (10%), desperation (5%), pestilence (5%).
                        session[1][player][2] = random.choice(
                            ["protection_totem"] * 8 + ["death_totem"] * 4 + ["retribution_totem"] * 4 + [
                                "silence_totem"] * 2 + ["desperation_totem"] + ["pestilence_totem"])
                    else:
                        session[1][player][2] = random.choice(SHAMAN_TOTEMS)
                    log_msg.append("{} ({}) HAS {}".format(get_name(player), player, session[1][player][2]))
                elif role == 'hunter' and session[1][player][0] and 'hunterbullet' not in session[1][player][4]:
                    session[1][player][2] = player
                if night == 1:
                    await _send_role_info(player)
                else:
                    await _send_role_info(player, sendrole=False)
            await adapter.log(1, '\n'.join(log_msg))
            
            session[3][0] = datetime.now()
            await adapter.send_lobby("It is now **nighttime**.")
            warn = False
            # NIGHT LOOP
            while win_condition() == None and not session[2] and session[0]:
                end_night = True
                wolf_kill_dict = {}
                num_wolves = 0
                for player in session[1]:
                    role = get_role(player, 'role')
                    templates = get_role(player, 'templates')
                    if session[1][player][0]:
                        if role in ['wolf', 'werecrow', 'doomsayer', 'werekitten', 'wolf shaman', 'wolf mystic', 'sorcerer',
                                    'seer', 'oracle', 'harlot', 'hunter', 'augur',
                                    'guardian angel', 'succubus', 'hag', 'warlock', 'bodyguard', 'turncoat', 'serial killer', 'hot potato'] and 'silence_totem2' not in session[1][player][4]:
                            end_night = end_night and (session[1][player][2] != '')
                            if role == 'werecrow':
                                end_night = end_night and 'observe' in session[1][player][4]
                        if role in ['shaman', 'crazed shaman'] and 'silence_totem2' not in session[1][player][4]:
                            end_night = end_night and (session[1][player][2] in session[1])
                        if role == "wolf shaman":
                            end_night = end_night and not [x for x in session[1][player][4] if x.startswith("totem:")]
                        if role == 'matchmaker':
                            end_night = end_night and 'match' not in session[1][player][4]
                        if role == 'clone':
                            end_night = end_night and 'clone' not in session[1][player][4]
                        if role == 'piper':
                            end_night = end_night and 'charm' not in session[1][player][4]
                        if "assassin" in templates:
                            end_night = end_night and [x for x in session[1][player][4] if x.startswith("assassinate:")]
                        if role == 'doomsayer':
                            end_night = end_night and not [x for x in session[1][player][4] if x.startswith("doom:")]
                        if roles[role][0] == 'wolf' and role in COMMANDS_FOR_ROLE['kill']:
                            num_wolves += 1
                            num_wolves -=  len([x for x in [y for y in session[1] if session[1][y][0] and roles[get_role(y, 'role')][0] == 'wolf' and get_role(y, 'role') in COMMANDS_FOR_ROLE['kill']] if 'silence_totem2' in session[1][x][4]])
                            num_kills = session[1][player][4].count('angry') + 1
                            t = session[1][player][2]
                            # if no target then t == '' and that will be a key in wolf_kill_dict
                            targets = t.split(',')
                            for target in targets:
                                try:
                                    wolf_kill_dict[target] += 1
                                except KeyError:
                                    wolf_kill_dict[target] = 1
                    if role == "vengeful ghost" and [x for x in session[1][player][4] if x.startswith("vengeance:")] and not session[1][player][0]:
                        end_night = end_night and session[1][player][2] != ''
                if num_wolves > 0:
                    end_night = end_night and len(wolf_kill_dict) == num_kills
                    for t in wolf_kill_dict:
                        end_night = end_night and wolf_kill_dict[t] == num_wolves
                        # night will only end if all wolves select same target(s)
                end_night = end_night or (datetime.now() - session[3][0]).total_seconds() > night_timeout
                if end_night:
                    session[2] = True
                    session[3][1] = -datetime.now() # attempted fix for using !time right as night ends
                if (datetime.now() - session[3][0]).total_seconds() > night_warning and warn == False:
                    warn = True
                    await adapter.send_lobby("**A few villagers awake early and notice it is still dark outside. "
                                            "The night is almost over and there are still whispers heard in the village.**")
                await asyncio.sleep(0.1)
            night_elapsed = datetime.now() - session[3][0]
            session[4][0] += night_elapsed

            # BETWEEN NIGHT AND DAY
            session[3][1] = datetime.now() # fixes using !time screwing stuff up
            killed_msg = ''
            killed_dict = {}
            for player in session[1]:
                if "blessed" in get_role(player, 'templates'):
                    killed_dict[player] = -1
                else:
                    killed_dict[player] = 0
            killed_players = []
            alive_players = [x for x in session[1] if (session[1][x][0] or (get_role(x, 'role') == "vengeful ghost" and [a for a in session[1][x][4] if a.startswith("vengeance:")]))]
            log_msg = ["SUNRISE LOG:"]
            if session[0]:
                for player in alive_players:
                    role = get_role(player, 'role')
                    templates = get_role(player, 'templates')
                    if "silence_totem2" in session[1][player][4] and (role != 'matchmaker'):
                        if "assassin" in templates and not [x for x in session[1][player][4] if x.startswith("assassinate:")]:
                            if "misdirection_totem2" in session[1][player][4]:
                                target = misdirect(player)
                            else:
                                target = random.choice([x for x in alive_players if x != player and "luck_totem2" not in session[1][x][4]])
                            session[1][player][4].append("assassinate:{}".format(target))
                            log_msg.append("{0} ({1}) TARGET RANDOMLY {2} ({3})".format(get_name(player), player, get_name(target), target))

                            await adapter.send_user(player, "Because you forgot to select a target at night, you are now targeting **{0}**.".format(get_name(target)))
                        continue
                    if role == 'doomsayer':
                        session[1][player][4] = [x for x in session[1][player][4] if not x.startswith('doom:')]
                    if role == 'piper':
                        session[1][player][4] = [x for x in session[1][player][4] if not x == 'charm']
                    if (role in ['shaman', 'crazed shaman'] and session[1][player][2] in totems) or (role == "wolf shaman" and [x for x in session[1][player][4] if x.startswith("totem:")]):
                        if "misdirection_totem2" in session[1][player][4]:
                            totem_target = misdirect(player)
                        else:
                            totem_target = random.choice([x for x in alive_players if x != player and ("luck_totem2" not in session[1][x][4]) and ("lasttarget:{}".format(x) not in [y for y in session[1][player][4]])])
                        if role in ['shaman', 'crazed shaman']:
                            totem = session[1][player][2]
                        else:
                            totem = [x for x in session[1][player][4] if x.startswith("totem:")][0].split(":")[1]
                        session[1][totem_target][4].append(totem)
                        if role in ['shaman', 'crazed shaman']:
                            session[1][player][2] = totem_target
                        else:
                            session[1][player][4] = [x for x in session[1][player][4] if not x.startswith("totem:")]
                        session[1][player][4] = [x for x in session[1][player][4] if not x.startswith("lasttarget")] + ["lasttarget:{}".format(totem_target)]
                        log_msg.append(player + '\'s ' + totem + ' given to ' + totem_target)

                        random_given = "wtf? this is a bug; pls report to admins"
                        if role in ['shaman', 'wolf shaman']:
                            random_given = "Because you forgot to give your totem out at night, your **{0}** was randomly given to **{1}**.".format(
                                totem.replace('_', ' '), get_name(totem_target))
                        elif role == 'crazed shaman':
                            random_given = "Because you forgot to give your totem out at night, your totem was randomly given to **{0}**.".format(get_name(totem_target))
                        await adapter.send_user(player, random_given)
                    elif role == 'matchmaker' and 'match' in session[1][player][4] and str(session[4][1]) == "0:00:00":
                        trycount = 0
                        alreadytried = []
                        while True:
                            player1 = random.choice([x for x in session[1] if session[1][x][0]])
                            player2 = random.choice([x for x in session[1] if session[1][x][0] and x != player1])
                            if not ("lover:" + player2 in session[1][player1][4] or "lover:" + player1 in session[1][player2][4]):
                                session[1][player][4].remove('match')
                                session[1][player1][4].append('lover:' + player2)
                                session[1][player2][4].append('lover:' + player1)
                                await adapter.send_user(player1,
                                                        "You are in love with **{0}**. If that player dies for any reason, the pain will be too much for you to bear and you will commit suicide.".format(
                                                            get_name(player2)))
                                await adapter.send_user(player2,
                                                        "You are in love with **{0}**. If that player dies for any reason, the pain will be too much for you to bear and you will commit suicide.".format(
                                                            get_name(player1)))
                                await adapter.log(1, "{0} ({1}) MATCH {2} ({3}) AND {4} ({5})".format(get_name(player), player, get_name(player1), player1, get_name(player2), player2))
                                break
                            elif [player1 + player2] not in alreadytried:
                                trycount += 1
                                alreadytried.append([player1 + player2])
                            if trycount >= (len([x for x in session[1] if session[1][x][0]])*(len([x for x in session[1] if session[1][x][0]]) - 1)): #all possible lover sets are done
                                break
                        await adapter.send_user(player,
                                                "Because you forgot to choose lovers at night, two lovers have been selected for you.")
                    elif role == 'harlot' and session[1][player][2] == '':
                        session[1][player][2] = player
                        log_msg.append("{0} ({1}) STAY HOME".format(get_name(player), player))
                        await adapter.send_user(player, "You will stay home tonight.")
                    elif role == 'succubus' and session[1][player][2] == '':
                        session[1][player][2] = player
                        log_msg.append("{0} ({1}) STAY HOME".format(get_name(player), player))
                        await adapter.send_user(player, "You have chosen to not entrance anyone tonight.")
                    elif role == 'hunter' and session[1][player][2] == '':
                        session[1][player][2] = player
                        log_msg.append("{0} ({1}) PASS".format(get_name(player), player))
                        await adapter.send_user(player, "You have chosen to not kill anyone tonight.")
                    elif role == 'serial killer' and session[1][player][2] == '':
                        session[1][player][2] = player
                        log_msg.append("{0} ({1}) PASS".format(get_name(player), player))
                        await adapter.send_user(player, "You have chosen to not kill anyone tonight.")
                    elif role == 'guardian angel' and session[1][player][2] in ['pass', '']:
                        session[1][player][2] = ''
                        session[1][player][4][:] = [x for x in session[1][player][4] if not x.startswith('lasttarget:')]
                        # clear previous target since no target selected
                        log_msg.append("{0} ({1}) NO GUARD".format(get_name(player), player))
                        await adapter.send_user(player, "You have chosen to not guard anyone tonight.")
                    elif role == 'vengeful ghost' and [x for x in session[1][player][4] if x.startswith('vengeance:')] and not session[1][player][0] and session[1][player][2] == '' and 'consecrated' not in session[1][player][4] and 'driven' not in session[1][player][4]:
                        against = 'wolf'
                        if [x for x in session[1][player][4] if x.startswith("vengeance:")]:
                            against = [x.split(':')[1] for x in session[1][player][4] if x.startswith('vengeance:')].pop()
                        if "misdirection_totem2" in session[1][player][4]:
                            target = misdirect(player, alive_players=[x for x in alive_players if x != player and get_role(x, 'actualteam') == against and 'luck_totem2' not in session[1][x][4]])
                        else:
                            target = random.choice([x for x in alive_players if x != player and "luck_totem2" not in session[1][x][4] and get_role(x, 'actualteam') == against])
                        session[1][player][2] = target
                        log_msg.append("{0} ({1}) VENGEFUL KILL {2} ({3})".format(get_name(player), player, get_name(target), target))
                    # randomly choose clone targets if unchosen
                    elif role == 'clone' and 'clone' in session[1][player][4]:
                        target = random.choice([x for x in alive_players if x != player])
                        session[1][player][4].append("clone:{}".format(target))
                        await adapter.send_user(player, "Because you did not choose someone to clone, you are cloning **{}**. If they die you will take their role.".format(get_name(target)))
                        session[1][player][4].remove('clone')
                        await adapter.log(1, "{0} ({1}) CLONE TARGET {2} ({3})".format(get_name(player), player, get_name(target), target))
                    # turncoat siding
                    elif role == 'turncoat' and session[1][player][2]:
                        if session[1][player][2] == 'wolves':
                            session[1][player][4].append('sided')
                            session[1][player][4].append('side:wolves')
                            if 'side:villagers' in session[1][player][4]:
                                session[1][player][4].remove('side:villagers')
                        elif session[1][player][2] == 'villagers':
                            session[1][player][4].append('sided')
                            session[1][player][4].append('side:villagers')
                            if 'side:wolves' in session[1][player][4]:
                                session[1][player][4].remove('side:wolves')
                    if "assassin" in templates and not [x for x in session[1][player][4] if x.startswith("assassinate:")]:
                        if "misdirection_totem2" in session[1][player][4]:
                            target = misdirect(player)
                        else:
                            target = random.choice([x for x in alive_players if x != player and "luck_totem2" not in session[1][x][4]])
                        session[1][player][4].append("assassinate:{}".format(target))
                        log_msg.append("{0} ({1}) TARGET RANDOMLY {2} ({3})".format(get_name(player), player, get_name(target), target))
                        await adapter.send_user(player, "Because you forgot to select a target at night, you are now targeting **{0}**.".format(get_name(target)))

            # BELUNGA
            for player in [x for x in session[1] if session[1][x][0]]:
                for i in range(session[1][player][4].count('belunga_totem')):
                    session[1][player][4].append(random.choice(list(totems) + ['belunga_totem', 'bullet']))
                    if random.random() < 0.1 and 'gunner' not in get_role(player, 'templates') and 'sharpshooter' not in get_role(player, 'templates'):
                        session[1][player][3].append('gunner')
                        session[1][player][4].append('gunnotify')

            # Wolf kill
            wolf_votes = {}
            wolf_killed = []
            gunner_revenge = []
            wolf_deaths = []
            wolf_turn = []

            for player in alive_players:
                if roles[get_role(player, 'role')][0] == 'wolf' and get_role(player, 'role') in COMMANDS_FOR_ROLE['kill']:
                    for t in session[1][player][2].split(','):
                        if t in wolf_votes:
                            wolf_votes[t] += 1
                        elif t != "":
                            wolf_votes[t] = 1
            if wolf_votes != {}:
                sorted_votes = sorted(wolf_votes, key=lambda x: wolf_votes[x], reverse=True)
                wolf_killed = sort_players(sorted_votes[:num_kills])
                log_msg.append("WOLFKILL: " + ', '.join('{} ({})'.format(get_name(x), x) for x in wolf_killed))
                for k in wolf_killed:
                    if get_role(k, 'role') == 'harlot' and session[1][k][2] != k:
                        killed_msg += "The wolves' selected victim was not at home last night, and avoided the attack.\n"
                    elif get_role(k, 'role') in ['monster', 'serial killer']:
                        pass
                    else:
                        killed_dict[k] += 1
                        wolf_deaths.append(k)

            # Guardian Angel stuff
            guarded = []
            guardeded = [] # like protect_totemed

            for angel in [x for x in alive_players if get_role(x, 'role') == 'guardian angel']:
                target = session[1][angel][2]
                if target: # GA makes more sense working on target even if they are harlot not at home
                    killed_dict[target] -= 50
                    guarded.append(target)

            # Harlot stuff
            for harlot in [x for x in alive_players if get_role(x, 'role') == 'harlot']:
                visited = session[1][harlot][2]
                if visited != harlot:
                    if visited in wolf_killed and not ('protection_totem' in session[1][visited][4] or 'blessed' in session[1][visited][4] or harlot in guarded):
                        killed_dict[harlot] += 1
                        killed_msg += "**{}**, a **harlot**, made the unfortunate mistake of visiting the victim's house last night and is now dead.\n".format(get_name(harlot))
                        wolf_deaths.append(harlot)
                    elif get_role(visited, 'role') in ACTUAL_WOLVES and harlot not in guarded:
                        killed_dict[harlot] += 1
                        killed_msg += "**{}**, a **harlot**, made the unfortunate mistake of visiting a wolf's house last night and is now dead.\n".format(get_name(harlot))
                        wolf_deaths.append(harlot)

            # Succubus stuff
            for succubus in [x for x in alive_players if get_role(x, 'role') == 'succubus']:
                visited = session[1][succubus][2]
                if visited != succubus:
                    if visited in wolf_killed and not ('protection_totem' in session[1][visited][4] or 'blessed' in session[1][visited][4] or succubus in guarded):
                        killed_dict[succubus] += 1
                        killed_msg += "**{}**, a **succubus**, made the unfortunate mistake of visiting the victim's house last night and is now dead.\n".format(get_name(succubus))
                        wolf_deaths.append(succubus)
            for disobeyer in [x for x in alive_players if 'disobey' in session[1][x][4]]:
                if random.random() < 0.5:
                    killed_dict[disobeyer] += 100 # this is what happens to bad bois

            # Hag stuff
            for hag in [x for x in alive_players if get_role(x, 'role') == 'hag']:
                hexed = session[1][hag][2]
                if hexed:
                    session[1][hexed][4].append('hex')

            # Doomsayer stuff
            doom_deaths = []

            for doomsayer in [x for x in session[1] if get_role(x, 'role') == 'doomsayer' and [a for a in session[1][x][4] if a.startswith('doomdeath:')]]:
                target = [a.split(':')[1] for a in session[1][doomsayer][4] if a.startswith('doomdeath:')].pop()
                killed_dict[target] += 1
                doom_deaths.append(target)
                session[1][doomsayer][4] = [a for a in session[1][doomsayer][4] if not a.startswith('doomdeath:')]

            # Hunter stuff
            for hunter in [x for x in session[1] if get_role(x, 'role') == 'hunter']:
                target = session[1][hunter][2]
                if target not in [hunter, '']:
                    if 'hunterbullet' in session[1][hunter][4]:
                        session[1][hunter][4].remove('hunterbullet')
                        killed_dict[target] += 100
                        
            # Serial killer stuff
            sk_deaths = []
            
            for sk in [x for x in session[1] if get_role(x, 'role') == 'serial killer']:
                target = session[1][sk][2]
                if target not in [sk, '']:
                    killed_dict[target] += 1
                    sk_deaths.append(target)

            # Vengeful ghost stuff
            for ghost in [x for x in session[1] if get_role(x, 'role') == 'vengeful ghost' and not session[1][x][0] and [a for a in session[1][x][4] if a.startswith('vengeance:')]]:
                target = session[1][ghost][2]
                if target:
                    killed_dict[target] += 1
                    session[1][target][4].append('vg_target')
                    if 'retribution_totem2' in session[1][target][4]:
                        session[1][ghost][4].append('driven')
                        killed_msg += "**{0}**'s totem emitted a brilliant flash of light last night. It appears that **{1}**'s spirit was driven away by the flash.\n".format(get_name(target), get_name(ghost))

            # Bodyguard stuff
            for bodyguard in [x for x in alive_players if get_role(x, 'role') == 'bodyguard']:
                target = session[1][bodyguard][2]
                if target in session[1] and (target in wolf_deaths or target in sk_deaths or 'vg_target' in session[1][target][4]) and not ('protection_totem' in session[1][target][4] or 'blessed' in session[1][target][4] or bodyguard in guarded):
                    killed_dict[bodyguard] += 1
                    killed_dict[target] -= 1
                    if 'protection_totem' not in session[1][bodyguard][4]:
                        killed_msg += "**{}** sacrificed their life to guard that of another.\n".format(get_name(bodyguard))
                    if target in wolf_deaths:
                        wolf_deaths.append(bodyguard)
                        wolf_deaths.remove(target)
                    #elif get_role(target, 'role') in ACTUAL_WOLVES:
                    #    killed_dict[bodyguard] += 1
                    #    killed_msg += "**{}**, a **bodyguard**, made the unfortunate mistake of guarding a wolf last night and is now dead.\n".format(get_name(bodyguard))
                    #    wolf_deaths.append(bodyguard)
            for player in [x for x in session[1] if 'vg_target' in session[1][x][4]]:
                session[1][player][4].remove('vg_target')

            # Totem stuff
            protect_totemed = []
            death_totemed = []
            ill_wolves = []
            revengekill = ""

            for player in sort_players(session[1]):
                prot_tots = 0
                death_tots = 0
                death_tots += session[1][player][4].count('death_totem')
                killed_dict[player] += death_tots
                if get_role(player, 'role') != 'harlot' or session[1][player][2] == player:
                    # fix for harlot with protect
                    prot_tots = session[1][player][4].count('protection_totem')
                    killed_dict[player] -= prot_tots
                if player in wolf_killed and killed_dict[player] < 1 and not (get_role(player, 'role') == 'harlot' and session[1][player][2] != player):
                    # if player was targeted by wolves but did not die and was not harlot avoiding attack
                    if player in guarded:
                        guardeded.append(player)
                    elif 'protection_totem' in session[1][player][4]:
                        protect_totemed.append(player)
                if 'death_totem' in session[1][player][4] and killed_dict[player] > 0 and death_tots - prot_tots - guarded.count(player) > 0:
                    death_totemed.append(player)

                if 'cursed_totem' in session[1][player][4]:
                    if 'cursed' not in get_role(player, 'templates'):
                        session[1][player][3].append('cursed')

                if player in wolf_deaths and killed_dict[player] > 0 and player not in death_totemed:
                    # player was targeted and killed by wolves
                    if session[1][player][4].count('lycanthropy_totem2') > 0 or get_role(player, 'role') == 'lycan' or 'lycanthropy2' in session[1][player][4]:
                        killed_dict[player] -= 1
                        if killed_dict[player] == 0:
                            wolf_turn.append(player)
                            await wolfchat("{} is now a **wolf**!".format(get_name(player)))
                            if get_role(player, 'role') == 'lycan':
                                lycan_message = "HOOOOOOOOOWL. You have become... a wolf!"
                            elif 'lycanthropy2' in session[1][player][4]:
                                lycan_message = "You awake to a sharp pain, and realize you are being attacked by a werewolf! You suddenly feel the weight of fate upon you, and find yourself turning into a werewolf!"
                            else:
                                lycan_message = "You awake to a sharp pain, and realize you are being attacked by a werewolf! Your totem emits a bright flash of light, and you find yourself turning into a werewolf!"
                            await adapter.send_user(player, lycan_message)
                    elif "pestilence_totem2" in session[1][player][4]:
                        for p in session[1]:
                            if roles[get_role(p, 'role')][0] == 'wolf' and get_role(p, 'role') in COMMANDS_FOR_ROLE['kill']:
                                ill_wolves.append(p)
                    if session[1][player][4].count('retribution_totem') > 0 and player not in wolf_turn:
                        revenge_targets = [x for x in session[1] if session[1][x][0] and get_role(x, 'role') in [
                            'wolf', 'doomsayer', 'werecrow', 'werekitten', 'wolf shaman', 'wolf mystic']]
                        if get_role(player, 'role') == 'harlot' and get_role(session[1][player][2], 'role') in [
                            'wolf', 'doomsayer', 'werecrow', 'wolf cub', 'werekitten', 'wolf shaman', 'wolf mystic']:
                            revenge_targets[:] = [session[1][player][2]]
                        else:
                            revenge_targets[:] = [x for x in revenge_targets if player in session[1][x][2].split(',')]
                        if revenge_targets:
                            revengekill = random.choice(revenge_targets)
                            killed_dict[revengekill] += 100
                            if killed_dict[revengekill] > 0:
                                killed_msg += "While being attacked last night, **{}**'s totem emitted a bright flash of light. The dead body of **{}**".format(
                                                get_name(player), get_name(revengekill))
                                killed_msg += ", a **{}**, was found at the scene.\n".format(get_role(revengekill, 'role'))

            for player in session[1]:
                session[1][player][4] = [x for x in session[1][player][4] if x != "ill_wolf"]
            for wolf in ill_wolves:
                session[1][wolf][4].append("ill_wolf")

            gun_rev = {}
            
            for player in sort_players(wolf_deaths):
                if ('gunner' in get_role(player, 'templates') or 'sharpshooter' in get_role(player, 'templates')) and \
                session[1][player][4].count('bullet') > 0 and killed_dict[player] > 0:
                    target = ""
                    if random.random() < GUNNER_REVENGE_WOLF:
                        revenge_targets = [x for x in session[1] if session[1][x][0] and get_role(x, 'role') in [
                            'wolf', 'doomsayer', 'werecrow', 'werekitten', 'wolf shaman', 'wolf mystic']]
                        if get_role(player, 'role') == 'harlot' and get_role(session[1][player][2], 'role') in [
                            'wolf', 'doomsayer', 'werecrow', 'wolf cub', 'werekitten', 'wolf shaman', 'wolf mystic']:
                            revenge_targets[:] = [session[1][player][2]]
                        else:
                            revenge_targets[:] = [x for x in revenge_targets if session[1][x][2] in wolf_killed]
                        revenge_targets[:] = [x for x in revenge_targets if x not in gunner_revenge]
                        if revenge_targets:
                            target = random.choice(revenge_targets)
                            gunner_revenge.append(target)
                            session[1][player][4].remove('bullet')
                            killed_dict[target] += 100
                            if killed_dict[target] > 0:
                                gun_rev[player] = target
                    if session[1][player][4].count('bullet') > 0:
                        give_gun_targets = [x for x in session[1] if session[1][x][0] and get_role(x, 'role') in WOLFCHAT_ROLES and x != target]
                        if len(give_gun_targets) > 0:
                            give_gun = random.choice(give_gun_targets)
                            if not 'gunner' in get_role(give_gun, 'templates'):
                                session[1][give_gun][3].append('gunner')
                            session[1][give_gun][4].append('bullet')
                            await adapter.send_user(give_gun, "While searching through **{}**'s belongings, you discover a gun loaded with 1 "
                                "silver bullet! You may only use it during the day. If you shoot at a wolf, you will intentionally miss. If you "
                                "shoot a villager, it is likely that they will be injured.".format(get_name(player)))

            for player in killed_dict:
                if killed_dict[player] > 0:
                    killed_players.append(player)

            killed_players = sort_players(killed_players)

            killed_temp = killed_players[:]

            log_msg.append("PROTECT_TOTEMED: " + ", ".join("{} ({})".format(get_name(x), x) for x in protect_totemed))
            if guarded:
                log_msg.append("GUARDED: " + ", ".join("{} ({})".format(get_name(x), x) for x in guarded))
            if guardeded:
                log_msg.append("ACTUALLY GUARDED: " + ", ".join("{} ({})".format(get_name(x), x) for x in guardeded))
            log_msg.append("DEATH_TOTEMED: " + ", ".join("{} ({})".format(get_name(x), x) for x in death_totemed))
            log_msg.append("PLAYERS TURNED WOLF: " + ", ".join("{} ({})".format(get_name(x), x) for x in wolf_turn))
            if revengekill:
                log_msg.append("RETRIBUTED: " + "{} ({})".format(get_name(revengekill), revengekill))
            if gunner_revenge:
                log_msg.append("GUNNER_REVENGE: " + ", ".join("{} ({})".format(get_name(x), x) for x in gunner_revenge))
            log_msg.append("DEATHS FROM WOLF: " + ", ".join("{} ({})".format(get_name(x), x) for x in wolf_deaths))
            log_msg.append("KILLED PLAYERS: " + ", ".join("{} ({})".format(get_name(x), x) for x in killed_players))

            await adapter.log(1, '\n'.join(log_msg))

            if guardeded:
                for gded in sort_players(guardeded):
                    killed_msg += "**{0}** was attacked last night, but luckily the guardian angel was on duty.\n".format(get_name(gded))

            if protect_totemed:
                for protected in sort_players(protect_totemed):
                    killed_msg += "**{0}** was attacked last night, but their totem emitted a brilliant flash of light, blinding their attacker and allowing them to escape.\n".format(
                                        get_name(protected))
                    
            if death_totemed:
                for ded in sort_players(death_totemed):
                    if session[6] == 'noreveal':
                        killed_msg += "**{0}**'s totem emitted a brilliant flash of light last night. The dead body of **{0}** was found at the scene.\n".format(get_name(ded))
                    else:
                        killed_msg += "**{0}**'s totem emitted a brilliant flash of light last night. The dead body of **{0}**, a **{1}** was found at the scene.\n".format(
                                        get_name(ded), get_role(ded, 'death'))
                    killed_players.remove(ded)

            if revengekill and revengekill in killed_players:
                # retribution totem
                killed_players.remove(revengekill)

            for player in gunner_revenge:
                if player in killed_players:
                    killed_players.remove(player)

            if len(killed_players) == 0:
                if not (guardeded or protect_totemed or death_totemed or [x for x in wolf_killed if get_role(x, 'role') == 'harlot']):
                    killed_msg += random.choice(lang['nokills']) + '\n'
            elif len(killed_players) == 1:
                if session[6] == 'noreveal':
                    killed_msg += "The dead body of **{}** was found. Those remaining mourn the tragedy.\n".format(get_name(killed_players[0]))
                else:
                    killed_msg += "The dead body of **{}**, a **{}**, was found. Those remaining mourn the tragedy.\n".format(get_name(killed_players[0]), get_role(killed_players[0], 'death'))
            else:
                if session[6] == 'noreveal':
                    if len(killed_players) == 2:
                        killed_msg += "The dead bodies of **{0}** and **{1}** were found. Those remaining mourn the tragedy.\n".format(get_name(killed_players[0]), get_name(killed_players[1]))
                    else:
                        killed_msg += "The dead bodies of **{0}**, and **{1}** were found. Those remaining mourn the tragedy.\n".format(('**, **'.join(map(get_name, killed_players[:-1])), get_name(killed_players[-1])))
                else:
                    killed_msg += "The dead bodies of **{}**, and **{}**, a **{}**, were found. Those remaining mourn the tragedy.\n".format(
                    '**, **'.join(get_name(x) + '**, a **' + get_role(x, 'death') for x in killed_players[:-1]), get_name(killed_players[-1]), get_role(killed_players[-1], 'death'))

            if gun_rev:
                if session[6] == 'noreveal':
                    for player in gun_rev:
                        killed_msg += "Fortunately **{}** had bullets and **{}** was shot dead.\n".format(get_name(player), get_name(gun_rev[player]))
                else:
                    for player in gun_rev:
                        killed_msg += "Fortunately **{}** had bullets and **{}**, a **{}**, was shot dead.\n".format(\
                            get_name(player), get_name(gun_rev[player]), get_role(gun_rev[player], 'death'))

            if session[0] and win_condition() == None:
                await adapter.send_lobby("Night lasted **{0:02d}:{1:02d}**. The villagers wake up and search the village.\n\n{2}".format(
                                                                                        night_elapsed.seconds // 60, night_elapsed.seconds % 60, killed_msg))
                for player in session[1]:
                    session[1][player][4] = [o for o in session[1][player][4] if o != "angry"]

            killed_dict = {}
            for player in killed_temp:
                kill_team = "wolf" if player not in gunner_revenge + list(revengekill) + death_totemed and (player in wolf_deaths or player in doom_deaths) else "village"
                killed_dict[player] = ("night kill", kill_team)
            if killed_dict:
                await player_deaths(killed_dict)

            for player in wolf_turn:
                session[1][player][4].append('turned:{}'.format(get_role(player, 'role')))
                session[1][player][1] = 'wolf'

            # Hot potato stuff
            for potato in [x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'hot potato']:
                target = session[1][potato][2]
                if target:
                    if target in [x for x in session[1] if session[1][x][0]]:
                        role = get_role(target, 'role')
                        templates = [x for x in session[1][target][3]]
                        other = [x for x in session[1][target][4]]
                        session[1][target][1] = 'hot potato'
                        session[1][target][3] = [x for x in session[1][potato][3]]
                        session[1][target][4] = [x for x in session[1][potato][4]]
                        session[1][potato][1] = role
                        session[1][potato][3] = templates
                        session[1][potato][4] = other
                        await adapter.send_user(target, 'You are now a **hot potato**!\nYour role is **hot potato**. {}\n'.format(roles['hot potato'][2]))
                        await adapter.send_user(potato, 'You are now a **{0}**!\nYour role is **{0}**. {1}\n'.format(role, roles[role][2]))
                        for player in [x for x in session[1] if session[1][x][0] and session[1][x][4]]:
                            new_other = []
                            for element in session[1][player][4]:
                                if element == 'lover:{}'.format(target):
                                    new_other.append('lover:{}'.format(potato))
                                    await adapter.send_user(player, 'Your lover had their identity swapped, so you are now in love with **{}**!'.format(get_name(potato)))
                                elif element == 'lover:{}'.format(potato):
                                    new_other.append('lover:{}'.format(target))
                                    await adapter.send_user(player, 'Your lover had their identity swapped, so you are now in love with **{}**!'.format(get_name(target)))
                                else:
                                    new_other.append(element)
                            session[1][player][4] = new_other

                        if role == 'hunter':
                            if 'hunterbullet' in session[1][potato][4]:
                                await adapter.send_user(potato, 'You have **not** shot anyone yet.')
                            else:
                                await adapter.send_user(potato, 'You have **already** shot someone this game.')                                  
                        elif role == 'priest':
                            if 'bless' in session[1][potato][4]:
                                await adapter.send_user(potato, 'You have **not** blessed anyone yet.')
                            else:
                                await adapter.send_user(potato, 'You have **already** blessed someone this game.')
                        elif role == 'clone' and session[1][potato][4]:
                            if [x for x in session[1][potato][4] if x.startswith('clone:')]:
                                await adapter.send_user(potato, "You are cloning **{}**. If they die you will take their role.".format(get_name([x for x in session[1][potato][4] if x.startswith('clone:')][0].strip('clone:'))))
                        elif role == 'turncoat':
                            if 'side:villagers' in session[1][potato][4]:
                                await adapter.send_user(potato, 'You are currently siding with the village.')
                            elif 'side:wolves' in session[1][potato][4]:
                                await adapter.send_user(potato, 'You are currently siding with the wolves.')
                            if 'sided2' in session[1][potato][4]:
                                await adapter.send_user(potato, 'You will be able to switch sides in two nights.')
                            else:
                                await adapter.send_user(potato, 'You will be able to switch sides during the upcoming night.')
                        elif role == 'executioner':
                            if [x for x in session[1][potato][4] if x.startswith('execute:')]:
                                exe_target = [x for x in session[1][potato][4] if x.startswith('execute:')][0].strip('execute:')
                                if 'win' in session[1][potato][4]:
                                    await adapter.send_user(potato, 'Your target was **{}**. This player was lynched, so you won.'.format(get_name(exe_target)))
                                else:
                                    await adapter.send_user(potato, 'Your target for lynch is **{}**.'.format(get_name(exe_target)))
                            else:
                                if [x for x in [y for y in session[1] if session[1][y][0]] if get_role(x, 'actualteam') == 'village']:
                                    exe_target = random.choice([x for x in [y for y in session[1] if session[1][y][0]] if get_role(x, 'actualteam') == 'village'])
                                    session[1][potato][4].append('execute:{}'.format(exe_target))
                                    await adapter.send_user(potato, 'Your target for lynch is **{}**.'.format(get_name(exe_target)))
                                else:
                                    session[1][potato][1] = 'jester'
                                    session[1][potato][4].append('executioner')
                                    await adapter.send_user(potato, 'There are no available targets. You have now become a **jester**.\nYour role is **jester**. {}\n'.format(roles['jester'][2]))
                        elif role == 'minion':
                            living_players_string = []
                            for plr in [x for x in session[1] if session[1][x][0]]:
                                temprole = get_role(plr, 'role')
                                role_string = []
                                if roles[temprole][0] == 'wolf' and temprole not in ['minion', 'cultist']:
                                    role_string.append(temprole)
                                living_players_string.append("{} ({}){}".format(get_name(plr), plr,
                                ' ({})'.format(' '.join(role_string)) if role_string else ''))
                            await adapter.send_user(potato, 'Living players: ```basic\n' + '\n'.join(living_players_string) + '\n```')

                        for player in [potato, target]:
                            if 'gunner' in session[1][player][3]:
                                await adapter.send_user(player, 'You have a gun and **{}** bullet{}. Use the command `{}role gunner` for more information.'.format(session[1][player][4].count('bullet'), '' if session[1][player][4].count('bullet') == 1 else 's', BOT_PREFIX))
                            if 'sharpshooter' in session[1][player][3]:
                                await adapter.send_user(player, 'You have a gun and **{}** bullet{}. Use the command `{}role sharpshooter` for more information.'.format(session[1][player][4].count('bullet'), '' if session[1][player][4].count('bullet') == 1 else 's', BOT_PREFIX))
                            if session[1][player][4]:
                                if 'assassin' in session[1][player][3] and [x for x in session[1][player][4] if x.startswith('assassinate:')]:
                                    await adapter.send_user(player, 'Your target is **{0}**. Use the command `{1}role assassin` for more information.'.format(get_name([x for x in session[1][player][4] if x.startswith('assassinate:')][0].strip('assassinate:')), BOT_PREFIX))
                                for element in session[1][player][4]:
                                    if element == 'entranced' and [x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'succubus']:
                                        await adapter.send_user(player, "You have become entranced, and are now on **{}**'s team. From this point on, you must vote along with them or risk dying. You **cannot win with your own team**, but you will win should all alive players become entranced.".format(get_name(random.choice([x for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'succubus']))))
                                    elif element.startswith('lover:'):
                                        await adapter.send_user(player, 'You are in love with **{}**. If that player dies for any reason, the pain will be too much for you to bear and you will commit suicide.'.format(get_name(element.strip('lover:'))))
                        if role in WOLFCHAT_ROLES:
                            try:
                                await wolfchat('**{0}** has replaced **{1}** as a **{2}**!'.format(get_name(potato), get_name(target), role))
                            except discord.Forbidden:
                                pass
                    else:
                        await adapter.send_user(potato, '**{}** died this night, so you are still a **hot potato**.'.format(get_name(target)))

            # Piper stuff
            if session[0]:
                charmed = sort_players([x for x in alive_players if 'charmed' in session[1][x][4]])
                tocharm = sort_players([x for x in alive_players if 'tocharm' in session[1][x][4]])
                for player in tocharm:
                    charmed_total = [x for x in charmed + tocharm if x != player]
                    session[1][player][4].remove('tocharm')
                    session[1][player][4].append('charmed')
                    piper_message = "You hear the sweet tones of a flute coming from outside your window... You inexorably walk outside and find yourself in the village square. "
                    if len(charmed_total) > 2:
                        piper_message += "You find out that **{0}**, and **{1}** are also charmed!".format('**, **'.join(map(get_name, charmed_total[:-1])), get_name(charmed_total[-1]))
                    elif len(charmed_total) == 2:
                        piper_message += "You find out that **{0}** and **{1}** are also charmed!".format(get_name(charmed_total[0]), get_name(charmed_total[1]))
                    elif len(charmed_total) == 1:
                        piper_message += "You find out that **{}** is also charmed!".format(get_name(charmed_total[0]))
        
                    if piper_message:
                        await adapter.send_user(player, piper_message)
                fullcharmed = charmed + tocharm
                for player in charmed:
                    piper_message = ''
                    fullcharmed.remove(player)
                    if len(fullcharmed) > 1:
                        piper_message = "You, **{0}**, and **{1}** are all charmed!".format('**, **'.join(map(get_name, fullcharmed[:-1])), get_name(fullcharmed[-1]))
                    elif len(fullcharmed) == 1:
                        piper_message = "You and **{0}** are now charmed!".format(get_name(fullcharmed[0]))
                    elif len(fullcharmed) == 0:
                        piper_message = "You are the only charmed villager."

                    if piper_message:
                        await adapter.send_user(player, piper_message)
                    fullcharmed.append(player)
                
            # More totem stuff
            if session[0] and win_condition() == None:
                totem_holders = []
                for player in sort_players(session[1]):
                    if [x for x in session[1][player][4] if x in totems]:
                        totem_holders.append(player)
                    other = session[1][player][4][:]
                    for o in other[:]:
                        # hacky way to get specific mechanisms to last 2 nights
                        if o in ['death_totem', 'cursed_totem', 'retribution_totem', 'lycanthropy_totem2',
                                'deceit_totem2', 'silence_totem2', 'luck_totem2', 'misdirection_totem2',
                                'pestilence_totem2', 'consecrated', 'illness', 'disobey', 'lycanthropy2','sided2']:
                            other.remove(o)
                        elif o.startswith('given:'):
                            other.remove(o)
                        elif o == 'protection_totem':
                            other.remove(o)
                            other.append('protection_totem2') # only protects from assassin and mad
                        elif o in ['lycanthropy_totem']:
                            other.remove(o)
                            other.append('lycanthropy_totem2')
                        elif o == 'lycanthropy':
                            other.remove(o)
                            other.append('lycanthropy2')
                        elif o == 'deceit_totem':
                            other.remove(o)
                            other.append('deceit_totem2')
                        elif o in ['silence_totem', 'hex']:
                            other.remove(o)
                            other.append('silence_totem2')
                        elif o == 'misdirection_totem':
                            other.remove(o)
                            other.append('misdirection_totem2')
                        elif o == 'luck_totem':
                            other.remove(o)
                            other.append('luck_totem2')
                        elif o == 'pestilence_totem':
                            other.remove(o)
                            other.append('pestilence_totem2')
                        elif o == 'sick':
                            other.remove(o)
                            other.append('silence_totem2')
                            other.append('illness')
                        elif o == 'sided':
                            other.remove(o)
                            other.append('sided2')
                    session[1][player][4] = other
                totem_holders = sort_players(totem_holders)
                if len(totem_holders) == 0:
                    pass
                elif len(totem_holders) == 1:
                    await adapter.send_lobby(random.choice(lang['hastotem']).format(get_name(totem_holders[0])))
                elif len(totem_holders) == 2:
                    await adapter.send_lobby(random.choice(lang['hastotem2']).format(get_name(totem_holders[0]), get_name(totem_holders[1])))
                else:
                    await adapter.send_lobby(random.choice(lang['hastotems']).format('**, **'.join([get_name(x) for x in totem_holders[:-1]]), get_name(totem_holders[-1])))

            for player in session[1]:
                session[1][player][2] = ''

            if session[0] and win_condition() == None:
                await check_traitor()
                
        else: # DAY
            session[3][1] = datetime.now()
            if session[0] and win_condition() == None:
                for player in session[1]:
                    session[1][player][4] = [x for x in session[1][player][4] if x not in ["guarded", "protection_totem2"] and not x.startswith('bodyguard:')]
                await adapter.send_lobby("It is now **daytime**. Use `{}lynch <player>` to vote to lynch <player>.".format(BOT_PREFIX))

            for player in session[1]:
                if session[1][player][0] and 'blinding_totem' in session[1][player][4]:
                    if 'injured' not in session[1][player][4]:
                        session[1][player][4].append('injured')
                        for i in range(session[1][player][4].count('blinding_totem')):
                            session[1][player][4].remove('blinding_totem')
                        await adapter.send_user(player, "Your totem emits a brilliant flash of light. "
                                                        "It seems like you cannot see anything! Perhaps "
                                                        "you should just rest during the day...")
                if 'illness' in session[1][player][4]:
                    session[1][player][4].append('injured')
                if get_role(player, 'role') == 'doomsayer':
                    session[1][player][4] = [x for x in session[1][player][4] if not x.startswith('doom:')]
            if session[6] != 'mudkip':
                lynched_player = None
                warn = False
                totem_dict = {} # For impatience and pacifism
                # DAY LOOP
                while win_condition() == None and session[2] and lynched_player == None and session[0]:
                    for player in [x for x in session[1]]:
                        totem_dict[player] = session[1][player][4].count('impatience_totem') - session[1][player][4].count('pacifism_totem')
                    vote_dict = get_votes(totem_dict)
                    if vote_dict['abstain'] >= len([x for x in session[1] if session[1][x][0] and 'injured' not in session[1][x][4]]) / 2:
                        lynched_player = 'abstain'
                    max_votes = max([vote_dict[x] for x in vote_dict])
                    max_voted = []
                    if max_votes >= len([x for x in session[1] if session[1][x][0] and 'injured' not in session[1][x][4]]) // 2 + 1:
                        for voted in vote_dict:
                            if vote_dict[voted] == max_votes:
                                max_voted.append(voted)
                        lynched_player = random.choice(max_voted)
                    if (datetime.now() - session[3][1]).total_seconds() > day_timeout:
                        session[3][0] = datetime.now() # hopefully a fix for time being weird
                        session[2] = False
                    if (datetime.now() - session[3][1]).total_seconds() > day_warning and warn == False:
                        warn = True
                        await adapter.send_lobby("**As the sun sinks inexorably toward the horizon, turning the lanky pine "
                                                "trees into fire-edged silhouettes, the villagers are reminded that very little time remains for them to reach a "
                                                "decision; if darkness falls before they have done so, the majority will win the vote. No one will be lynched if "
                                                "there are no votes or an even split.**")
                    await asyncio.sleep(0.1)
                if not lynched_player and win_condition() == None and session[0]:
                    vote_dict = get_votes(totem_dict)
                    max_votes = max([vote_dict[x] for x in vote_dict])
                    max_voted = []
                    for voted in vote_dict:
                        if vote_dict[voted] == max_votes and voted != 'abstain':
                            max_voted.append(voted)
                    if len(max_voted) == 1:
                        lynched_player = max_voted[0]
                if session[0]:
                    session[3][0] = datetime.now() # hopefully a fix for time being weird
                    day_elapsed = datetime.now() - session[3][1]
                    session[4][1] += day_elapsed
                lynched_msg = ""
                if lynched_player and win_condition() == None and session[0]:
                    if lynched_player == 'abstain':
                        for player in [x for x in totem_dict if session[1][x][0] and totem_dict[x] < 0]:
                            lynched_msg += "**{}** meekly votes to not lynch anyone today.\n".format(get_name(player))
                        lynched_msg += "The village has agreed to not lynch anyone today."
                        await adapter.send_lobby(lynched_msg)
                    else:
                        for player in [x for x in totem_dict if session[1][x][0] and totem_dict[x] > 0 and x != lynched_player]:
                            lynched_msg += "**{}** impatiently votes to lynch **{}**.\n".format(get_name(player), get_name(lynched_player))
                        lynched_msg += '\n'
                        if lynched_player in session[1].keys():
                            if 'revealing_totem' in session[1][lynched_player][4]:
                                lynched_msg += 'As the villagers prepare to lynch **{0}**, their totem emits a brilliant flash of light! When the villagers are able to see again, '
                                lynched_msg += 'they discover that {0} has escaped! The left-behind totem seems to have taken on the shape of a **{1}**.'
                                if get_role(lynched_player, 'role') == 'amnesiac':
                                    role = [x.split(':')[1].replace("_", " ") for x in session[1][lynched_player][4] if x.startswith("role:")].pop()
                                    session[1][lynched_player][1] = role
                                    session[1][lynched_player][4] = [x for x in session[1][lynched_player][4] if not x.startswith("role:")]
                                    try:
                                        await adapter.send_user(lynched_player, "Your totem clears your amnesia and you now fully remember who you are!")
                                        await _send_role_info(lynched_player)
                                        if role in WOLFCHAT_ROLES:
                                            await wolfchat("{0} is now a **{1}**!".format(get_name(lynched_player), role))
                                    except discord.Exception:
                                        pass
                                lynched_msg = lynched_msg.format(get_name(lynched_player), get_role(lynched_player, 'role'))
                                await adapter.send_lobby(lynched_msg)
                            elif 'mayor' in get_role(lynched_player, 'templates') and 'unrevealed' in session[1][lynched_player][4]:
                                lynched_msg += "While being dragged to the gallows, **{}** reveals that they are the **mayor**. The village agrees to let them live for now.".format(get_name(lynched_player))
                                session[1][lynched_player][4].remove('unrevealed')
                                await adapter.send_lobby(lynched_msg)
                            else:
                                if 'luck_totem2' in session[1][lynched_player][4]:
                                    lynched_player = misdirect(lynched_player)
                                if session[6] == 'noreveal':
                                    lynched_msg += random.choice(lang['lynchednoreveal']).format(get_name(lynched_player))
                                else:
                                    lynched_msg += random.choice(lang['lynched']).format(get_name(lynched_player), get_role(lynched_player, 'death'))
                                await adapter.send_lobby(lynched_msg)
                                if get_role(lynched_player, 'role') == 'jester':
                                    session[1][lynched_player][4].append('lynched')
                                for player in [x for x in session[1] if session[1][x][0]]:
                                    if get_role(player, 'role') == 'executioner' and 'win' not in session[1][player][4] and [x for x in session[1][player][4] if x.startswith('execute:')]:
                                        if [x for x in session[1][player][4] if x.startswith('execute:')][0].strip('execute:') == lynched_player:
                                            session[1][player][4].append('win')
                                            await adapter.send_user(player, 'Your target was **{}**. This player was lynched, so you won.'.format(get_name(lynched_player)))
                                lynchers_team = [get_role(x, 'actualteam') for x in session[1] if session[1][x][0] and session[1][x][2] == lynched_player]
                                await player_deaths({lynched_player : ('lynch', 'wolf' if lynchers_team.count('wolf') > lynchers_team.count('village') else 'village')})

                            if get_role(lynched_player, 'role') == 'fool' and 'revealing_totem' not in session[1][lynched_player][4]:
                                win_msg = "The fool has been lynched, causing them to win!\n\n" + end_game_stats()
                                lovers = []
                                for n in session[1][lynched_player][4]:
                                    if n.startswith('lover:'):
                                        lover = n.split(':')[1]
                                        if session[1][lover][0]:
                                            lovers.append(lover)

                                await end_game(win_msg, [lynched_player] + (lovers if session[6] == "random" else []) + [x for x in session[1] if get_role(x, "role") == "jester" and "lynched" in session[1][x][4]])
                                return
                elif lynched_player == None and win_condition() == None and session[0]:
                    await adapter.send_lobby("Not enough votes were cast to lynch a player.")
            else:
                lynched_players = []
                warn = False
                totem_dict = {} # For impatience and pacifism, which are not found in mudkip
                # DAY LOOP
                while win_condition() == None and session[2] and not lynched_players and session[0]:
                    for player in [x for x in session[1]]:
                        totem_dict[player] = 0
                    vote_dict = get_votes(totem_dict)
                    max_votes = max([vote_dict[x] for x in vote_dict])
                    if vote_dict['abstain'] >= len([x for x in session[1] if session[1][x][0] and 'injured' not in session[1][x][4]]) / 2:
                        lynched_players = 'abstain'
                    elif max_votes >= len([x for x in session[1] if session[1][x][0] and 'injured' not in session[1][x][4]]) // 2 + 1 or not [x for x in session[1] if not session[1][x][2] and session[1][x][0]]:
                        for voted in vote_dict:
                            if vote_dict[voted] == max_votes:
                                lynched_players.append(voted)
                    if (datetime.now() - session[3][1]).total_seconds() > day_timeout:
                        session[3][0] = datetime.now() # hopefully a fix for time being weird
                        session[2] = False
                    if (datetime.now() - session[3][1]).total_seconds() > day_warning and warn == False:
                        warn = True
                        await adapter.send_lobby("**As the sun sinks inexorably toward the horizon, turning the lanky pine "
                                                "trees into fire-edged silhouettes, the villagers are reminded that very little time remains for them to reach a "
                                                "decision; if darkness falls before they have done so, the majority will win the vote. No one will be lynched if "
                                                "there are no votes or an even split.**")
                    await asyncio.sleep(0.1)
                if not lynched_players and win_condition() == None and session[0]:
                    vote_dict = get_votes(totem_dict)
                    max_votes = max([vote_dict[x] for x in vote_dict])
                    max_voted = []
                    for voted in vote_dict:
                        if vote_dict[voted] == max_votes and voted != 'abstain':
                            max_voted.append(voted)
                    if max_voted:
                        lynched_players = max_voted
                if session[0]:
                    session[3][0] = datetime.now() # hopefully a fix for time being weird
                    day_elapsed = datetime.now() - session[3][1]
                    session[4][1] += day_elapsed
                lynched_msg = ""
                lynch_deaths = {}
                if lynched_players and win_condition() == None and session[0]:
                    if lynched_players == 'abstain':
                        lynched_msg += "The village has agreed to not lynch anyone today."
                    else:
                        for lynched_player in lynched_players:
                            if lynched_player in session[1].keys():
                                lynched_msg += "\n"
                                lynched_msg += random.choice(lang['lynched']).format(get_name(lynched_player), get_role(lynched_player, 'death'))
                                if get_role(lynched_player, 'role') == 'jester':
                                    session[1][lynched_player][4].append('lynched')
                                lynchers_team = [get_role(x, 'actualteam') for x in session[1] if session[1][x][0] and session[1][x][2] == lynched_player]
                                lynch_deaths.update({lynched_player : ('lynch', 'wolf' if lynchers_team.count('wolf') > lynchers_team.count('village') else 'village')})
                    await adapter.send_lobby(lynched_msg)
                    await player_deaths(lynch_deaths)
            # BETWEEN DAY AND NIGHT
            session[2] = False
            night += 1
            if session[0] and win_condition() == None:
                await adapter.send_lobby("Day lasted **{0:02d}:{1:02d}**. The villagers, exhausted from the day's events, go to bed.".format(
                                                                    day_elapsed.seconds // 60, day_elapsed.seconds % 60))
                for player in [x for x in session[1] if session[1][x][0] and 'entranced' in session[1][x][4]]:
                    if session[1][player][2] not in [session[1][x][2] for x in session[1] if session[1][x][0] and get_role(x, 'role') == 'succubus']:
                        session[1][player][4].append('disobey')
                for player in session[1]:
                    session[1][player][4][:] = [x for x in session[1][player][4] if x not in [
                        'revealing_totem', 'influence_totem', 'impatience_totem', 'pacifism_totem', 'injured', 'desperation_totem']]
                    session[1][player][2] = ''
                    session[1][player][4] = [x for x in session[1][player][4] if not x.startswith('vote:')]
                    if get_role(player, 'role') == 'amnesiac' and night == 3 and session[1][player][0]:
                        role = [x.split(':')[1].replace("_", " ") for x in session[1][player][4] if x.startswith("role:")].pop()
                        session[1][player][1] = role
                        session[1][player][4] = [x for x in session[1][player][4] if not x.startswith("role:")]
                        session[1][player][4].append('amnesiac')
                        try:
                            await adapter.send_user(player, "Your amnesia clears and you now remember that you are a{0} **{1}**!".format("n" if role.lower()[0] in ['a', 'e', 'i', 'o', 'u'] else "", role))
                            if role in WOLFCHAT_ROLES:
                                await wolfchat("{0} is now a **{1}**!".format(get_name(player), role))
                        except:
                            pass
            if session[0] and win_condition() == None:
                await check_traitor()
            
    # GAME END
    if session[0]:
        win_msg = win_condition()
        await end_game(win_msg[1], win_msg[2])
        

















async def start_votes(player):
    start = datetime.now()
    while (datetime.now() - start).total_seconds() < 60:
        votes_needed = max(2, min(len(session[1]) // 4 + 1, 4))
        votes = len([x for x in session[1] if session[1][x][1] == 'start'])
        if votes >= votes_needed or session[0] or votes == 0:
            break
        await asyncio.sleep(0.1)
    else:
        for player in session[1]:
            session[1][player][1] = ''
        await adapter.send_lobby("Not enough votes to start, resetting start votes.")

async def rate_limit(message):
    if not (message.channel.is_private or message.content.startswith(BOT_PREFIX)) or message.author.id in ADMINS or message.author.id == OWNER_ID:
        return False
    global ratelimit_dict
    global IGNORE_LIST
    if message.author.id not in ratelimit_dict:
        ratelimit_dict[message.author.id] = 1
    else:
        ratelimit_dict[message.author.id] += 1
    if ratelimit_dict[message.author.id] > IGNORE_THRESHOLD:
        if not message.author.id in IGNORE_LIST:
            IGNORE_LIST.append(message.author.id)
            await adapter.log(2, message.author.name + " (" + message.author.id + ") was added to the ignore list for rate limiting.")
        try:
            await adapter.reply(message, "You've used {0} commands in the last {1} seconds; I will ignore you from now on.".format(IGNORE_THRESHOLD, TOKEN_RESET), mentionauthor=True)
        except discord.Forbidden:
            await adapter.send_lobby(message.author.mention +
                                      " used {0} commands in the last {1} seconds and will be ignored from now on.".format(IGNORE_THRESHOLD, TOKEN_RESET))
        finally:
            return True
    if message.author.id in IGNORE_LIST or ratelimit_dict[message.author.id] > TOKENS_GIVEN:
        if ratelimit_dict[message.author.id] > TOKENS_GIVEN:
            await adapter.log(2, "Ignoring message from " + message.author.name + " (" + message.author.id + "): `" + message.content + "` since no tokens remaining")
        return True
    return False

async def do_rate_limit_loop():
    await client.wait_until_ready()
    global ratelimit_dict
    while not client.is_closed:
        for user in ratelimit_dict:
            ratelimit_dict[user] = 0
        await asyncio.sleep(TOKEN_RESET)

async def game_start_timeout_loop():
    session[5] = datetime.now()
    while not session[0] and len(session[1]) > 0 and datetime.now() - session[5] < timedelta(seconds=GAME_START_TIMEOUT):
        await asyncio.sleep(0.1)
    if not session[0] and len(session[1]) > 0:
        session[0] = True
        await adapter.set_lobby_status(LobbyStatus.READY)
        await adapter.send_lobby("{}, the game has taken too long to start and has been cancelled. "
                          "If you are still here and would like to start a new game, please do `{}join` again.".format(adapter.PLAYERS_ROLE.mention, BOT_PREFIX))
        await adapter.unlock_lobby()
        player_dict = {}
        for player in list(session[1]):
            player_dict[player] = ('game cancel', "bot")
        await player_deaths(player_dict)
        session[0] = False
        session[3] = [datetime.now(), datetime.now()]
        session[4] = [timedelta(0), timedelta(0)]
        session[6] = ''
        session[7] = {}

async def wait_timer_loop():
    global wait_bucket
    timer = datetime.now()
    while not session[0] and len(session[1]) > 0:
        if datetime.now() - timer > timedelta(seconds=WAIT_BUCKET_DELAY):
            timer = datetime.now()
            wait_bucket = min(wait_bucket + 1, WAIT_BUCKET_MAX)
        await asyncio.sleep(0.5)

async def backup_settings_loop():
    while not client.is_closed:
        print("BACKING UP SETTINGS")
        with open(NOTIFY_FILE, 'w') as notify_file:
            notify_file.write(','.join([x for x in notify_me if x != '']))
        with open(STASIS_FILE, 'w') as stasis_file:
            json.dump(stasis, stasis_file)
        await asyncio.sleep(BACKUP_INTERVAL)

############## POST-DECLARATION STUFF ###############
COMMANDS_FOR_ROLE = {'see' : ['seer', 'oracle', 'augur', 'doomsayer'],
                     'kill' : ['wolf', 'werecrow', 'werekitten', 'wolf shaman', 'hunter', 'vengeful ghost', 'doomsayer', 'wolf mystic', 'serial killer'],
                     'give' : ['shaman', 'wolf shaman'],
                     'visit' : ['harlot', 'succubus'],
                     'shoot' : ['gunner', 'sharpshooter'],
                     'observe' : ['werecrow', 'sorcerer'],
                     'pass' : ['harlot', 'hunter', 'guardian angel', 'succubus', 'warlock', 'bodyguard', 'piper', 'turncoat', 'serial killer'],
                     'id' : ['detective'],
                     'choose' : ['matchmaker', 'hot potato'],
                     'guard' : ['guardian angel', 'bodyguard'],
                     'target' : ['assassin'],
                     'bless' : ['priest'],
                     'consecrate' : ['priest'],
                     'entrance' : ['succubus'],
                     'hex' : ['hag'],
                     'curse' : ['warlock'],
                     'charm' : ['piper'],
                     'clone' : ['clone'],
                     'side' : ['turncoat']}
GAMEPLAY_COMMANDS = ['join', 'j', 'start', 'vote', 'lynch', 'v', 'abstain', 'abs', 'nl', 'stats', 'leave', 'q', 'role', 'roles']
GAMEPLAY_COMMANDS += list(COMMANDS_FOR_ROLE)

# {role name : [team, plural, description]}
roles = {'wolf' : ['wolf', 'wolves', "Your job is to kill all of the villagers. Type `kill <player>` in private message to kill them."],
         'werecrow' : ['wolf', 'werecrows', "You are part of the wolfteam. Use `observe <player>` during the night to see if they were in bed or not. "
                                            "You may also use `kill <player>` to kill them."],
         'wolf cub' : ['wolf', 'wolf cubs', "You are part of the wolfteam. While you cannot kill anyone, the other wolves will "
                                            "become enraged if you die and will get two kills the following night."],
         'werekitten' : ['wolf', 'werekittens', "You are like a normal wolf, except due to your cuteness, you are seen as a villager "
                                                "and gunners will always miss when they shoot you. Use `kill <player>` in private message "
                                                "to vote to kill <player>."],
         'wolf shaman' : ['wolf', 'wolf shamans', "You are part of the wolfteam. You may use `kill <player>` to kill a villager. You can also select "
                                                  "a player to receive a totem each night by using `give <player>.` You may give yourself a totem, "
                                                  "but you may not give the same player a totem two nights in a row. If you do not give the totem "
                                                  "to anyone, it will be given to a random player."],
         'traitor' : ['wolf', 'traitors', "You are exactly like a villager, but you are part of the wolf team. Only the detective can reveal your true "
                                          "identity. Once all other wolves die, you will turn into a wolf."],
         'sorcerer' : ['wolf', 'sorcerers', "You may use `observe <player>` in pm during the night to observe someone and determine if they "
                                            "are the seer, oracle, or augur. You are seen as a villager; only detectives can reveal your true identity."],
         'cultist' : ['wolf', 'cultists', "Your job is to help the wolves kill all of the villagers. But you do not know who the wolves are."],
         'seer' : ['village', 'seers', "Your job is to detect the wolves; you may have a vision once per night. Type `see <player>` in private message to see their role."],
         'oracle' : ['village', 'oracles', "Your job is to detect the wolves; you may have a vision once per night. Type `see <player>` in private message to see whether or not they are a wolf."],
         'shaman' : ['village', 'shamans', "You select a player to receive a totem each night by using `give <player>`. You may give a totem to yourself, but you may not give the same"
                                           " person a totem two nights in a row. If you do not give the totem to anyone, it will be given to a random player. "
                                           "To see your current totem, use the command `myrole`."],
         'harlot' : ['village', 'harlots', "You may spend the night with one player each night by using `visit <player>`. If you visit a victim of a wolf, or visit a wolf, "
                                           "you will die. You may visit yourself to stay home."],
         'hunter' : ['village', 'hunters', "Your job is to help kill the wolves. Once per game, you may kill another player using `kill <player>`. "
                                           "If you do not wish to kill anyone tonight, use `pass` instead."],
         'augur' : ['village', 'augurs', "Your job is to detect the wolves; you may have a vision once per night. Type `see <player>` in private message to see the aura they exude."
                                         " Blue is villager, grey is neutral, and red is wolf."],
         'detective' : ['village', 'detectives', "Your job is to determine all of the wolves and traitors. During the day, you may use `id <player>` in private message "
                                                 "to determine their true identity. However you risk a {}% chance of revealing your role to the wolves every time you use your ability.".format(int(DETECTIVE_REVEAL_CHANCE * 100))],
         'villager' : ['village', 'villagers', "Your job is to lynch all of the wolves."],
         'crazed shaman' : ['neutral', 'crazed shamans', "You select a player to receive a random totem each night by using `give <player>`. You may give a totem to yourself, "
                                                         "but you may not give the same person a totem two nights in a row. If you do not give the totem to anyone, "
                                                         "it will be given to a random player. You win if you are alive by the end of the game."],
         'fool' : ['neutral', 'fools', "You become the sole winner if you are lynched during the day. You cannot win otherwise."],
         'cursed villager' : ['template', 'cursed villagers', "This template is hidden and is seen as a wolf by the seer. Roles normally seen as wolf, the seer, and the fool cannot be cursed."],
         'gunner' : ['template', 'gunners', ("This template gives the player a gun. Type `{0}shoot <player>` in channel during the day to shoot <player>."
                                            "If you are a villager and shoot a wolf, they will die. Otherwise, there is a chance of killing them, injuring "
                                            "them, or the gun exploding. If you are a wolf and shoot at a wolf, you will intentionally miss.".format(BOT_PREFIX))],
         'assassin' : ['template', 'assassins', "Choose a target with `target <player>`. If you die you will take out your target with you. If your target dies you may choose another one. "
                                                "Wolves and info-obtaining roles (such as seer and oracle) may not be assassin."],
         'matchmaker' : ['village', 'matchmakers', "You can select two players to be lovers with `choose <player1> and <player2>`."
                                                   " If one lover dies, the other will as well. You may select yourself as one of the lovers."
                                                   " You may only select lovers during the first night."
                                                   " If you do not select lovers, they will be randomly selected and you will not be told who they are (unless you are one of them)."],
         'guardian angel' : ['village', 'guardian angels', "Your job is to protect the villagers. Use `guard <player>` in private message during night to protect "
                                                           "them from dying. You may protect yourself, however you may not guard the same player two nights in a row."],
         'jester' : ['neutral', 'jesters', "You will win alongside the normal winners if you are lynched during the day. You cannot otherwise win this game."],
         'minion' : ['wolf', 'minions', "It is your job to help the wolves kill all of the villagers. You are told who your leaders are on the first night, though they do not know you and you must tell them. Otherwise you have no powers, like a cultist"],
         'amnesiac' : ['neutral', 'amnesiacs', "You have forgotten your original role and need to wait a few nights to let the fog clear. You will win with the default role, until you remember your original role."],
         'blessed villager' : ['template', 'blessed villagers', "You feel incredibly safe. You won't be able to die as a normal villager, unless two players target you, or you are lynched at day."],
         'vengeful ghost' : ['neutral', 'vengeful ghosts', "Your soul will never be at rest. If you are killed during the game, you will swear eternal revenge upon team that killed you."
                                                           " Use `kill <player>` once per night after dying to kill an alive player. You only win if the team you swore revenge upon loses."],
         'priest' : ['village', 'priests', "Once per game during the day, you may bless someone with `bless <player>` to prevent them from being killed. Furthermore, you may consecrate the dead during the day with `consecrate <player>` to settle down restless spirits and prevent the corpse from rising as undead that night; doing so removes your ability to participate in the vote that day."],
         'doomsayer' : ['wolf', 'doomsayers', "You can see how bad luck will befall someone at night by using `see <player>` on them. You may also use `kill <player>` to kill a villager."],
         'succubus' : ['neutral', 'succubi', "You may entrance someone and make them follow you by visiting them at night. If all alive players are entranced, you win. Use `visit <player>` to visit a player or `pass` to stay home. If you visit the victim of the wolves, you will die."],
         'mayor' : ['template', 'mayors', "If the mayor would by lynched during the day, they reveal that they are the mayor and nobody is lynched that day. A mayor that has previously been revealed will be lynched as normal."],
         'monster' : ['neutral', 'monsters', "You cannot be killed by the wolves. If you survive until the end of the game, you win instead of the normal winners."],
         'sharpshooter' : ['template', 'sharpshooters', "This template is like the gunner template but due to it's holder's skills, they may never miss their target."],
         'village drunk': ['village', 'village drunks', "You have been drinking too much!"],
         'hag' : ['wolf', 'hags', "You can hex someone to prevent them from using any special powers they may have during the next day and night. Use `hex <player>` to hex them. Only detectives can reveal your true identity, seers will see you as a regular villager."],
         'bodyguard' : ['village', 'bodyguards', "It is your job to protect the villagers. If you guard a victim, you will sacrifice yourself to save them. Use `guard <player>` to guard a player or `pass` to not guard anyone tonight."],
         'piper' : ['neutral', 'pipers', "You can select up to two players to charm each night. The charmed players will know each other, but not who charmed them. You win when all other players are charmed. Use `charm <player1> and <player2>` to select the players to charm, or `charm <player>` to charm just one player."],
         'warlock' : ['wolf', 'warlocks', "Each night you can curse someone with `curse <player>` to turn them into a cursed villager, so the seer sees them as wolf. Act quickly, as your curse applies as soon as you cast it! Only detectives can reveal your true identity, seers will see you as a regular villager."],
         'mystic' : ['village', 'mystics', "Each night you will sense the number of evil villagers there are."],
         'wolf mystic' : ['wolf', 'wolf mystics', "Each night you will sense the number of villagers with a power that oppose you. You can also use `kill <player>` to kill a villager."],
         'mad scientist' : ['village', 'mad scientists', "You win with the villagers, and should you die, you will let loose a potent chemical concoction that will kill the players next to you if they are still alive."],
         'clone' : ['neutral', 'clones', "You can select someone to clone with `clone <player>`. If that player dies, you become their role. You may only clone someone during the first night."],
         'lycan' : ['neutral', 'lycans', "You are currently on the side of the villagers, but will turn into a wolf instead of dying if you are targeted by the wolves during the night."],
         'time lord' : ['village', 'time lords', "You are a master of time .. but you do not know it. If you are killed, day and night will speed up considerably."],
         'turncoat' : ['neutral', 'turncoats', "You can change the team you side with every other night. Use `side villagers` or `side wolves` to choose your team. If you do not wish to switch sides tonight, then you may use `pass`."],
         'serial killer' : ['neutral', 'serial killers', "You may kill one player each night with `kill <player>`. Your objective is to outnumber the rest of town. If there are any other serial killers, then you do not know who they are, but you win together, provided that the serial killer is alive. The wolves are unable to kill you at night. If you do not wish to kill anyone tonight, then you may use `pass`."],
         'executioner' : ['neutral', 'executioners', "At the start of the game, you will receive a target. This target is on the village team and your goal is to have this player lynched, while you are alive. If your target dies not via lynch, then you will become a jester."],
         'hot potato' : ['neutral', 'hot potatoes', "Under no circumstances may you win the game. You may choose to swap identities with someone else by using `choose <player>` at night."],
         'bishop' : ['template', 'bishops', "Your virtue prevents you from being entranced by the succubus."]}


gamemodes = {
    'default' : {
        'description' : "The default gamemode.",
        'min_players' : 4,
        'max_players' : 24,
        'chance' : 30,
        'roles' : {
            #4, 5, 6, 7, 8, 9, 10,11,12,13,14,15,16,17,18,19,20,21,22,23,24
            'wolf' :
            [1, 1, 1, 1, 1, 1,  1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2],
            'werecrow' :
            [0, 0, 0, 0, 0, 0,  0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'wolf cub' :
            [0, 0, 0, 0, 0, 0,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'werekitten' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1],
            'traitor' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1 ,1 ,1, 1],
            'hag' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
            'warlock' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
            'cultist' :
            [0, 0, 0, 1, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            'seer' :
            [1, 1, 1, 1, 1, 1,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'oracle' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
            'shaman' :
            [0, 0, 0, 1, 1, 1,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'harlot' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'hunter' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'augur' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
            'detective' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0],
            'matchmaker' :
            [0, 0, 0, 0, 0, 0,  0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'bodyguard' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1],
            'villager' :
            [2, 3, 4, 3, 3, 3,  3, 3, 3, 3, 4, 3, 3, 4, 4, 5, 4, 4, 5, 5, 5],
            'crazed shaman' :
            [0, 0, 0, 0, 0, 1,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'monster' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'amnesiac' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1],
            'cursed villager' :
            [0, 0, 1, 1, 1, 1,  1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3],
            'gunner' :
            [0, 0, 0, 0, 0, 0,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'mayor' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1],
            'assassin' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2]
        }
    },
    'foolish' : {
        'description' : "Watch out, because the fool is always there to steal the win.",
        'min_players' : 8,
        'max_players' : 24,
        'chance' : 10,
        'roles' : {
            #4, 5, 6, 7, 8, 9, 10,11,12,13,14,15,16,17,18,19,20,21,22,23,24
            'wolf' :
            [0, 0, 0, 0, 1, 1,  2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 4],
            'wolf cub' :
            [0, 0, 0, 0, 0, 0,  0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'traitor' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2],
            'sorcerer' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'oracle' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'shaman' :
            [0, 0, 0, 0, 0, 0,  0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'harlot' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2],
            'hunter' :
            [0, 0, 0, 0, 0, 1,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'augur' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'bodyguard' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
            'villager' :
            [0, 0, 0, 0, 3, 3,  3, 2, 2, 3, 4, 3, 4, 3, 4, 5, 5, 5, 6, 7, 7],
            'clone' :
            [0, 0, 0, 0, 0, 0,  0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'fool' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'cursed villager' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'gunner' :
            [0, 0, 0, 0, 0, 0,  0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2]
        }
    },
    'chaos' : {
        'description' : "Chaotic and unpredictable. Any role, including wolves, can be a gunner.",
        'min_players' : 4,
        'max_players' : 16,
        'chance' : 0,
        'roles' : {
            #4, 5, 6, 7, 8, 9, 10,11,12,13,14,15,16
            #wolf team
            'wolf' :
            [1, 1, 1, 1, 0, 0,  0, 0, 0, 0, 0, 0, 0],
            'wolf cub' :
            [0, 0, 0, 0, 0, 0,  1, 1, 1, 1, 1, 1, 1],
            'wolf shaman' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 2, 2, 2],
            'werekitten' :
            [0, 0, 0, 0, 0, 0,  0, 0, 1, 1, 1, 1, 1],
            #~~vil~~ shaman team
            'shaman' :
            [3, 4, 4, 5, 5, 5,  5, 6, 6, 6, 6, 6, 7],
            'oracle' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 1, 1, 1],
            #neutrals
            'crazed shaman' :
            [0, 0, 0, 0, 0, 1,  1, 1, 1, 1, 1, 1, 1],
            'jester' :
            [0, 0, 1, 1, 1, 1,  1, 1, 1, 2, 2, 2, 2],
            'vengeful ghost' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 1, 1],
            #templates
            'cursed villager' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 1, 1, 1],
            'gunner' :
            [1, 1, 1, 1, 1, 2,  2, 2, 3, 3, 3, 3, 3],
            'sharpshooter' :
            [1, 1, 1, 1, 1, 1,  1, 1, 1, 1, 1, 1, 1],
            'assassin' :
            [0, 0, 0, 0, 0, 1,  1, 2, 2, 2, 2, 2, 2]
        }
    },
    'orgy' : {
        'description' : "Be careful who you visit! ( ͡° ͜ʖ ͡°)",
        'min_players' : 4,
        'max_players' : 16,
        'chance' : 0,
        'roles' : {
            #4, 5, 6, 7, 8, 9, 10,11,12,13,14,15,16
            'wolf' :
            [1, 1, 1, 1, 1, 1,  2, 2, 2, 3, 3, 3, 3],
            'traitor' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 1, 2, 2],
            'harlot' :
            [3, 4, 4, 4, 3, 4,  3, 4, 5, 3, 4, 4, 4],
            'matchmaker' :
            [0, 0, 1, 1, 1, 1,  2, 2, 2, 3, 3, 3, 4],
            'crazed shaman' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 2, 2, 2, 2],
            'fool' :
            [0, 0, 0, 1, 1, 1,  1, 1, 1, 1, 1, 1, 1],
            'cursed villager' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0]
        }
    },
    'crazy' : {
        'description' : "A free for all with many unstable alignments.",
        'min_players' : 4,
        'max_players' : 16,
        'chance' : 0,
        'roles' : {
            #4, 5, 6, 7, 8, 9, 10,11,12,13,14,15,16
            'wolf' :
            [1, 1, 1, 1, 1, 1, 2, 2, 2, 3, 3, 3, 3],
            'turncoat' :
            [2, 2, 3, 3, 4, 4, 4, 4, 4, 4, 5, 5, 6],
            'crazed shaman' :
            [1, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 4, 4],
            'fool' :
            [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1],
            'clone' :
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
            'shaman' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
        }
    },
    'belunga' : {
        'description' : "Originally an april fool's joke, this gamemode is interesting, to say the least.",
        'min_players' : 4,
        'max_players' : 24,
        'chance' : 0,
        'roles' : {
        }
    },
    'valentines' : {
        'description' : "Love and death are in the air, as the default role is matchmaker.",
        # [8] wolf, wolf(2), matchmaker, matchmaker(2), matchmaker(3), matchmaker(4), matchmaker(5), matchmaker(6)
        # [9] matchmaker(7) [10] matchmaker(8) [11] matchmaker(9) [12] monster [13] wolf(3) [14] matchmaker(10) [15] matchmaker(11)
        # [16] matchmaker(12) [17] wolf(4) [18] mad scientist [19] matchmaker(13) [20] matchmaker(14) [21] wolf(5) [22] matchmaker(15) [23] matchmaker(16) [24] wolf(6)
        'min_players' : 8,
        'max_players' : 24,
        'chance' : 0,
        'roles' : {
            #4, 5, 6, 7, 8, 9, 10,11,12,13,14,15,16,17,18,19,20,21,22,23,24
            'wolf' :
            [0, 0, 0, 0, 2, 2,  2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 6],
            'matchmaker' :
            [0, 0, 0, 0, 6, 7,  8, 9, 9, 9,10,11,12,12,12,13,14,14,15,16,16],
            'mad scientist' :
            [0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1],
            'monster' :
            [0, 0, 0, 0, 0, 0,  0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
        }
    },
    'evilvillage' : {
        'description' : 'Majority of the village is wolf aligned, safes must secretly try to kill the wolves.',
        'min_players' : 6,
        'max_players' : 18,
        'chance' : 5,
        'roles' : {
            #4, 5, 6, 7, 8, 9, 10,11,12,13,14,15,16,17,18
            'wolf' :
            [0, 0, 1, 1, 1, 1,  1, 1, 1, 1, 1, 2, 2, 2, 2],
            'cultist' :
            [0, 0, 4, 5, 5, 6,  4, 5, 5, 6, 7, 6, 7, 8, 9],
            'seer' :
            [0, 0, 0, 0, 1, 1,  1, 1, 1, 1, 1, 1, 1, 1, 1],
            'shaman' :
            [0, 0, 0, 0, 0, 0,  0, 0, 1, 1, 1, 1, 1, 1, 1],
            'hunter' :
            [0, 0, 1, 1, 1, 1,  1, 1, 1, 1, 1, 2, 2, 2, 2],
            'guardian angel' :
            [0, 0, 0, 0, 0, 0,  1, 1, 1, 1, 1, 1, 1, 1, 1],
            'fool' :
            [0, 0, 0, 0, 0, 0,  1, 1, 1, 1, 1, 1, 1, 1, 1],
            'minion' :
            [0, 0, 0, 0, 0, 0,  1, 1, 1, 1, 1, 1, 1, 1, 1],
            'mayor' :
            [0, 0, 0, 0, 0, 0,  0, 0, 1, 1, 1, 1, 1, 1, 1]
        }
    },
    'drunkfire' : {
        'description' : "Most players get a gun, quickly shoot all the wolves!",
        'min_players' : 8,
        'max_players' : 17,
        'chance' : 0,
        'roles' : {
            # 4, 5, 6,7, 8, 9, 10,11,12,13,14,15,16,17
            'wolf' :
            [0, 0, 0, 0, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            'traitor' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2],
            'hag' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
            'seer' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'village drunk' :
            [0, 0, 0, 0, 2, 2, 3, 3, 4, 4, 4, 4, 5, 5],
            'villager' :
            [0, 0, 0, 0, 3, 4, 3, 4, 2, 3, 3, 4, 3, 4],
            'crazed shaman' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
            'cursed villager' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'gunner' :
            [0, 0, 0, 0, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9],
            'sharpshooter' :
            [0, 0, 0, 0, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4],
            'assassin' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1]
        }
    },
    'random' : {
        'description' : "Other than ensuring the game doesn't end immediately, no one knows what roles will appear.",
        'min_players' : 8,
        'max_players' : 20,
        'chance' : 0,
        'roles' : {
        }
    },
    'mudkip' : {
        'description' : "Why are all the professors named after trees?",
        'min_players' : 4,
        'max_players' : 15,
        'chance' : 5,
        'roles' : {
            'wolf' :
            [1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2],
            'wolf shaman' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1],
            'doomsayer' :
            [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1],
            'minion' :
            [0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0],
            'shaman' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1],
            'detective' :
            [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'guardian angel' :
            [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'priest' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1],
            'villager' :
            [2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3],
            'jester' :
            [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'amnesiac' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1],
            'vengeful ghost' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
            'succubus' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
            'assassin' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1]
        }
    },
    'charming' : {
        'description' : "Charmed players must band together to find the piper in this game mode.",
        'min_players' : 6,
        'max_players' : 24,
        'chance' : 10,
         'roles' : {
            'seer' :
            [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'harlot' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'shaman' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2],
            'detective' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'bodyguard' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2],
            'wolf' :
            [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3, 3],
            'traitor' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'werekitten' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'warlock' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'sorcerer' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
            'piper' :
            [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'vengeful ghost' :
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'cursed villager' :
            [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'gunner' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2],
            'mayor' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'assassin' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'villager' :
            [0, 0, 3, 4, 3, 4, 3, 3, 2, 3, 3, 4, 4, 5, 5, 5, 6, 7, 6, 7, 8]
         }
    },
    'mad' : {
        'description' : "This game mode has mad scientist and many things that may kill you.",
        'min_players' : 7,
        'max_players' : 22,
        'chance' : 5,
        'roles' : {
            #         7, 8, 9, 10,11,12,13,14,15,16,17,18,19,20,21,22,
            'villager' :
            [0, 0, 0, 4, 4, 5, 4, 5, 4, 5, 4, 4, 5, 4, 4, 5, 5, 6, 7],
            'seer' :
            [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'mad scientist' :
            [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'village drunk' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'detective' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'harlot' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1],
            'hunter' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
            # wolf team
            'wolf' :
            [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2],
            'traitor' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'werecrow' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'wolf cub' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 2],
            'cultist' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            # neutrals
            'jester' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
            'vengeful ghost' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            # templates
            'cursed villager' : 
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'gunner' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'assassin' : 
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1]
        }
    },
    'lycan' : {
        'description' : "Many lycans will turn into wolves. Hunt them down before the wolves overpower the village.",
        'min_players' : 7,
        'max_players' : 21,
        'chance' : 5,
        'roles' : {
            #         7, 8, 9, 10,11,12,13,14,15,16,17,18,19,20,21
            'villager' :
            [0, 0, 0, 3, 3, 3, 1, 1, 1, 2, 3, 2, 3, 3, 4, 4, 4, 5],
            'seer' :
            [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2],
            'bodyguard' :
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'matchmaker' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1],
            'hunter' :
            [0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2],
            # wolf team
            'wolf' :
            [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'traitor' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'wolf shaman' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            # neutrals
            'clone' :
            [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2],
            'lycan' :
            [0, 0, 0, 1, 1, 1, 2, 2, 3, 3, 3, 4, 4, 4, 4, 4, 5, 5],
            # templates
            'cursed villager' : 
            [0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2],
            'gunner' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
            'mayor' : 
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
        }
    },
    'rapidfire' : {
        'description' : "Many killing roles and roles that cause chain deaths. Living has never been so hard.",
        'min_players' : 6,
        'max_players' : 24,
        'chance' : 0,
        'roles' : {
            #      6, 7, 8, 9, 10,11,12,13,14,15,16,17,18,19,20,21,22,23,24
            'villager' :
            [0, 0, 3, 4, 3, 4, 2, 3, 2, 3, 4, 2, 3, 4, 1, 2, 3, 4, 2, 3, 4],
            'seer' :
            [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'mad scientist' :
            [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2],
            'matchmaker' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2],
            'hunter' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2],
            'augur' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'time lord' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2],
            # wolf team
            'wolf' :
            [0, 0, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4],
            'wolf cub' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2],
            'traitor' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            # neutrals
            'vengeful ghost' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2],
            'amnesiac' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            # templates
            'cursed villager' : 
            [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2],
            'gunner' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'sharpshooter' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'assassin' : 
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
        }
    },
    'noreveal' : {
        'description' : "Roles are not revealed on death.",
        'min_players' : 4,
        'max_players' : 21,
        'chance' : 1,
        'roles' : {
            #4, 5, 6, 7, 8, 9, 10,11,12,13,14,15,16,17,18,19,20,21
            'villager' :
            [2, 3, 4, 5, 4, 5, 4, 5, 4, 5, 6, 4, 5, 4, 5, 5, 6, 7],
            'seer' :
            [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'guardian angel' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'mystic' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'detective' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1],
            'hunter' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            # wolf team
            'wolf' :
            [1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3],
            'wolf mystic' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'traitor' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'werecrow' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1],
            # neutrals
            'clone' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1],
            'lycan' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
            'amnesiac' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
            # templates
            'cursed villager' : 
            [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2]
        }
    },
    'aleatoire' : {
        'description' : "Lots of roles to avoid killing who may not even know it themselves.",
        'min_players' : 8,
        'max_players' : 24,
        'chance' : 10,
        'roles' : {
            #4, 5, 6, 7, 8, 9, 10,11,12,13,14,15,16,17,18,19,20,21,22,23,24
            'villager' :
            [0, 0, 0, 0, 4, 5, 3, 4, 2, 3, 3, 2, 3, 2, 2, 3, 4, 3, 4, 5, 6],
            'seer' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'shaman' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'matchmaker' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'guardian angel' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'hunter' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1],
            'augur' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'time lord' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1],
            # wolf team
            'wolf' :
            [0, 0, 0, 0, 1, 1, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3],
            'wolf cub' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1],
            'traitor' :
            [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'werecrow' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'hag' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            # neutrals
            'vengeful ghost' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2],
            'amnesiac' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'turncoat' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            # templates
            'cursed villager' : 
            [0, 0, 0, 0, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2],
            'assassin' : 
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2],
            'gunner' : 
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'mayor' : 
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
        }
    },
    'bloodbath' : {
        'description' : "A serial killer is on the loose...shall it end up on the noose?",
        'min_players' : 9,
        'max_players' : 24,
        'chance' : 0,
        'roles' : {
            #4, 5, 6, 7, 8, 9, 10,11,12,13,14,15,16,17,18,19,20,21,22,23,24
            # wolf team
            'wolf' :
            [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2],
            'werecrow' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'traitor' :
            [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'hag' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1],
            'cultist' :
            [0, 0, 0, 0, 0, 0, 1, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 1, 1, 1],
            # village team
            'seer' :
            [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'oracle' :
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'shaman' :
            [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2],
            'hunter' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'guardian angel' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1],
            'bodyguard' :
            [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'priest' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
            'villager' :
            [0, 0, 0, 0, 0, 4, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],
            # neutrals
            'amnesiac' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
            'vengeful ghost' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
            'clone' :
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1],
            'turncoat' :
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            'serial killer' :
            [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2],
            # templates
            'gunner' : 
            [0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4]
        }
    }
}
gamemodes['belunga']['roles'] = dict(gamemodes['default']['roles'])

VILLAGE_ROLES_ORDERED = ['seer', 'oracle', 'shaman', 'harlot', 'hunter', 'augur', 'detective', 'matchmaker', 'guardian angel', 'bodyguard', 'priest', 'village drunk', 'mystic', 'mad scientist', 'time lord', 'villager']
WOLF_ROLES_ORDERED = ['wolf', 'werecrow', 'doomsayer', 'wolf cub', 'werekitten', 'wolf shaman', 'wolf mystic', 'traitor', 'hag', 'sorcerer', 'warlock', 'minion', 'cultist']
NEUTRAL_ROLES_ORDERED = ['jester', 'crazed shaman', 'monster', 'piper', 'amnesiac', 'fool', 'vengeful ghost', 'succubus', 'clone', 'lycan', 'turncoat', 'serial killer', 'executioner', 'hot potato']
TEMPLATES_ORDERED = ['cursed villager', 'blessed villager', 'gunner', 'sharpshooter', 'mayor', 'assassin', 'bishop']
totems = {'death_totem' : 'The player who is given this totem will die tonight.',
          'protection_totem': 'The player who is given this totem is protected from dying tonight.',
          'revealing_totem': 'If the player who is given this totem is lynched, their role is revealed to everyone instead of them dying.',
          'influence_totem': 'Votes by the player who is given this totem count twice.',
          'impatience_totem' : 'The player who is given this totem is counted as voting for everyone except themselves, even if they do not lynch.',
          'pacifism_totem' : 'The player who is given this totem is always counted as abstaining, regardless of their vote.',
          'cursed_totem' : 'The player who is given this totem will gain the cursed template if they do not have it.',
          'lycanthropy_totem' : 'If the player who is given this totem is targeted by wolves the following night, they turn into a wolf instead of dying.',
          'retribution_totem' : 'If the player who is given this totem is targeted by wolves during the night, they kill a random wolf in turn.',
          'blinding_totem' : 'The player who is given this totem will be injured and unable to vote the following day.',
          'deceit_totem' : 'If the player who is given this totem is seen by the seer/oracle the following night, the '
                           'vision will return the opposite of what they are. If a seer/oracle is given this totem, '
                           'all of their visions will return the opposite.',
          'misdirection_totem' : 'If the player who is given this totem attempts to use a power the following day or night'
                                 ', they will target a player adjacent to their intended target instead of the player they targeted.',
          'luck_totem' : 'If the player who is given this totem is targeted the next day or night, one of the players adjacent '
                         'to them will be targeted instead.',
          'silence_totem' : 'The player who is given this totem will be unable to use any special powers during the'
                            ' day tomorrow and the night after.',
          'pestilence_totem': 'If the player who is given this totem is killed by wolves tomorrow night,'
                              ' the wolves will not be able to kill the night after.',
          'desperation_totem': 'If the player who is given this totem is lynched, the last player to vote '
                               'them will also die.'}
SHAMAN_TOTEMS = ['death_totem', 'protection_totem', 'revealing_totem', 'influence_totem', 'impatience_totem', 'pacifism_totem', 'silence_totem', 'desperation_totem']
WOLF_SHAMAN_TOTEMS = ['protection_totem', 'impatience_totem', 'pacifism_totem', 'deceit_totem', 'lycanthropy_totem', 'luck_totem', 'misdirection_totem', 'silence_totem']
ROLES_SEEN_VILLAGER = ['werekitten', 'traitor', 'sorcerer', 'warlock', 'minion', 'cultist', 'villager', 'jester', 'fool', 'amnesiac', 'vengeful ghost', 'hag', 'piper', 'clone', 'lycan', 'time lord', 'turncoat', 'executioner']
ROLES_SEEN_WOLF = ['wolf', 'werecrow', 'doomsayer', 'wolf cub', 'wolf shaman', 'wolf mystic', 'cursed', 'monster', 'succubus', 'mad scientist']
ACTUAL_WOLVES = ['wolf', 'werecrow', 'doomsayer', 'wolf cub', 'werekitten', 'wolf shaman', 'wolf mystic']
WOLFCHAT_ROLES = ['wolf', 'werecrow', 'doomsayer', 'wolf cub', 'werekitten', 'wolf shaman', 'wolf mystic', 'traitor', 'sorcerer', 'warlock', 'hag']

########### END POST-DECLARATION STUFF #############
client.loop.create_task(do_rate_limit_loop())
client.loop.create_task(backup_settings_loop())
try:
    client.loop.run_until_complete(client.start(TOKEN))
finally:
    try:
        try:
            client.loop.run_until_complete(client.logout())
        except:
            pass
        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            client.loop.run_until_complete(gathered)
            gathered.exception()
        except:
            pass
    except:
        print("Error in cleanup:\n" + traceback.format_exc())
    client.loop.close()