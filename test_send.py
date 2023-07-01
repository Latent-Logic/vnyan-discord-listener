import asyncio
import logging
from typing import Dict, List, NamedTuple, Optional, Union

import aiohttp
import discord
import toml
from discord.ext.commands import Bot, CommandError, Context

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s\t%(levelname)-7s\t%(name)s\t%(message)s")
discord_logger = logging.getLogger("discord")
discord_logger.setLevel(logging.INFO)

log = logging.getLogger(__name__)

description = """A bot to translate Discord commands to vnyan websocket commands."""

intents = discord.Intents.default()
intents.message_content = True
intents.bans = False
intents.voice_states = False
intents.typing = False

bot = Bot(command_prefix="?", description=description, intents=intents)


class Command(NamedTuple):
    name: str
    help: str
    ws_cmd: str
    arg: Optional[str] = None

    @classmethod
    def create(cls, name: str, help_or_blob: Union[str, Dict[str, str]]):
        """Create a Command object from either a help string or a dict"""
        if isinstance(help_or_blob, str):
            return cls(name, help_or_blob, name)
        elif isinstance(help_or_blob, dict):
            arg = help_or_blob.get("arg")
            if arg:
                assert arg in {"<int>", "<str>"}, f"command {name} has a non-recognized arg {arg}"
            return cls(
                name,
                help_or_blob.get("help", "No Help Provided"),
                help_or_blob.get("ws", name),
                arg,
            )

    def discord_help(self, verbose: bool = False):
        """Returns a pretty print string for Discord"""
        ret_str = f"`={self.name} {self.arg}`" if self.arg else f"`={self.name}`"
        if verbose:
            ret_str += f" sends `{self.ws_cmd}`"
        ret_str += f": {self.help}"
        return ret_str

    def to_send(self, items: List[str]):
        if self.arg == "<int>":
            if not items:
                raise ValueError(f"No argument found for {self.name} (should be a number)")
            try:
                int(items[0])
            except ValueError:
                raise ValueError(f"Argument for {self.name} must be an number")
            return f"{self.ws_cmd} {items[0]}"
        elif self.arg == "<str>":
            if not items:
                raise ValueError(f"No argument found for {self.name}")
            return f"{self.ws_cmd} {items[0]}"
        else:
            if self.arg is not None:
                ValueError(f"Command {self.name} has unknown arg {self.arg}")
        return f"{self.ws_cmd}"


COMMANDS: Dict[str, Command] = {}  # Dict of Command objects


@bot.event
async def on_ready():
    log.debug(f"Logged in as {bot.user.name}, {bot.user.id}")
    log.debug("------")
    for guild in bot.guilds:
        guild: discord.Guild
        log.debug(f"\tGuild {guild.name}, {guild.id} with {guild.member_count} members")
        if str(guild.id) not in SETTINGS["guilds"]:
            log.info(f"\t Not listening in {guild.name} as I don't have a config for it")
            continue
        guild_settings = SETTINGS["guilds"][str(guild.id)]
        for channel_id in guild_settings["channels"]:
            channel = guild.get_channel(channel_id)
            if channel is None:
                raise ValueError(f"Unexpected channel id {channel_id} not in the guild")
            if not isinstance(channel, discord.TextChannel):
                raise ValueError(f"channel {channel} is not a Text Channel!")
        for role_id in guild_settings["roles"]:
            role: discord.Role = guild.get_role(role_id)
            if role is None:
                raise ValueError(f"Unexpected role id {role_id} not in the guild")
    log.debug("\n".join([repr(g) for g in bot.guilds]))


@bot.event
async def on_command_error(ctx: Context, exception: Exception):
    """Run whenever a CommandError is thrown"""
    user_msg = f"{ctx.author.mention}, `{ctx.message.content}` gave error \n> {exception}"
    await ctx.send(user_msg, delete_after=30)
    reason = exception.__cause__
    if not reason and not exception.__suppress_context__:
        reason = exception.__context__
    if reason:
        log.exception(
            f"`{ctx.author}` sent `{ctx.message.content}` which raised error `{exception}`",
            exc_info=reason,
        )
    else:
        log.error(f"`{ctx.author}` sent `{ctx.message.content}` which raised error `{exception}`")


def perm_check(guild: discord.Guild, channel: discord.TextChannel, author: discord.Member):
    if not guild:
        raise CommandError(f"This command has to run in a server context")
    if str(guild.id) not in SETTINGS["guilds"]:
        raise CommandError("No config known for this server")
    guild_settings = SETTINGS["guilds"][str(guild.id)]
    if not set(guild_settings["roles"]) & set(r.id for r in author.roles):
        raise CommandError("This command only for approved roles")
    if channel.id not in guild_settings["channels"]:
        raise CommandError("This command only works in specific channels")


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot or not message.content.startswith("="):
        return  # Only listening to non-bot users and =commands
    try:
        perm_check(message.guild, message.channel, message.author)
    except CommandError as err:
        log.debug(f"Ignoring {message} because {err}")
        return
    cmd, *rest = message.content[1:].split(maxsplit=1)  # Strip the = from the start of the message
    if cmd not in COMMANDS:
        await message.channel.send(f"Unknown command {cmd}, check `?ws_list`", delete_after=30)
        return
    try:
        to_send = COMMANDS[cmd].to_send(rest)
    except ValueError as err:
        await message.channel.send(f"{err}", delete_after=30)
        return
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(SETTINGS["bot"]["vnyan_socket"]) as ws:
            await ws.send_str(to_send)
    await message.add_reaction("✅")


@bot.command()
async def ws_list(ctx: Context, verbose: bool = False):
    """List all configured websocket commands"""
    out_str = "\n".join(COMMANDS[k].discord_help(verbose) for k in sorted(COMMANDS))
    await ctx.send(out_str)


@bot.command()
async def ws_del(ctx: Context, cmd: str):
    """Temporarily remove a =command in the currently running program

    Any changes will need to be manually updated in settings.toml before next run"""
    perm_check(ctx.guild, ctx.channel, ctx.author)
    if cmd not in COMMANDS:
        raise CommandError(f"Failed to delete as {cmd} not found in commands")
    del COMMANDS[cmd]
    log.info(f"User {ctx.author} has removed ={cmd} from the running instance")
    await ctx.message.add_reaction("✅")


@bot.command()
async def ws_add(ctx: Context, cmd: str, ws_str: str):
    """Temporarily add a =command in the currently running program

    Any changes will need to be manually updated in settings.toml before next run"""
    perm_check(ctx.guild, ctx.channel, ctx.author)
    if cmd in COMMANDS:
        raise CommandError(f"Can't add {cmd} as it already exists, use `?ws_del {cmd}` if you want to change it")
    COMMANDS[cmd] = Command(cmd, f"Temporarily added command `={cmd}` which sends `{ws_str}` to the web socket", ws_str)
    log.info(f"User {ctx.author} has added ={cmd} to send {ws_str} to the running instance")
    await ctx.message.add_reaction("✅")


@bot.command()
async def ws_cat(ctx: Context, data: str):
    """Echo message out to the websocket"""
    perm_check(ctx.guild, ctx.channel, ctx.author)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(SETTINGS["bot"]["vnyan_socket"]) as ws:
            await ws.send_str(data)
    await ctx.message.add_reaction("✅")


async def main(settings: dict):
    try:
        async with bot:
            await bot.start(settings["bot"]["token"])
    finally:
        log.debug("Shutting down bot")


if __name__ == "__main__":
    SETTINGS = toml.load("settings.toml")
    COMMANDS = {k: Command.create(k, v) for k, v in SETTINGS["commands"].items()}

    asyncio.run(main(SETTINGS))
