import asyncio
import logging
import os
import signal
from pathlib import Path
from tempfile import TemporaryDirectory

from .channels.telegram import TelegramChannel
from .channels.locking import ConsumerConflict, TokenLock
from .config import Config
from .executors.claude import ClaudeStreamExecutor
from .executors.codex_app import CodexAppExecutor
from .gateway import Gateway
from .speech import make_transcriber
from .storage import Storage
from .titles import ClaudeTitleExecutor, CodexTitleExecutor


async def serve(config: Config):
    # Across distinct state directories too. No database, executor or Bot exists
    # until the process owns this token's canonical same-user lock.
    with TokenLock(config.telegram_token):
        await _serve_locked(config)


async def _serve_locked(config: Config):
    os.umask(0o077)
    # New tasks work in the home; tasks created before V3.1 keep the old folder.
    storage = Storage(config.state_dir / "eivoh.sqlite3", config.default_executor,
                      task_workdir=config.home, legacy_workdir=config.workdir)
    executors = {
        "claude": ClaudeStreamExecutor(config.home, config.claude_bin, config.claude_model,
                                 config.permission_mode, extra_args=config.claude_args),
        "codex": CodexAppExecutor(config.home, config.codex_bin, config.codex_model,
                               extra_args=config.codex_args),
    }
    executor = executors.pop(config.default_executor)
    transcriber = make_transcriber(config.stt_provider, os.environ)
    channel = TelegramChannel(config.telegram_token, config.owner_id, transcriber=transcriber)
    # An empty working directory prevents task/repository instructions from
    # becoming naming instructions. Auth remains with each CLI's subscription.
    title_directory = TemporaryDirectory(prefix='eivoh-titles-')
    title_workdir = Path(title_directory.name)
    title_executors = {'claude': ClaudeTitleExecutor(title_workdir, config.claude_bin),
                       'codex': CodexTitleExecutor(title_workdir, config.codex_bin)}
    gateway = Gateway(storage, executor, {"telegram": channel}, executors=executors,
                      max_parallel=config.max_parallel, title_executors=title_executors)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    def stop_requested():
        gateway.begin_shutdown()
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_requested)
    try:
        async with channel.bot:
            await gateway.start()
            poll = asyncio.create_task(channel.poll(gateway.receive))
            stop = asyncio.create_task(stopping.wait())
            try:
                done, _ = await asyncio.wait([poll, stop], return_when=asyncio.FIRST_COMPLETED)
                if poll in done:
                    await poll
            finally:
                # Stop polling before children; running tasks stay recoverable.
                gateway.begin_shutdown()
                poll.cancel()
                stop.cancel()
                await asyncio.gather(poll, stop, return_exceptions=True)
                await gateway.shutdown()
    finally:
        await gateway.shutdown()
        title_directory.cleanup()
        storage.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP URLs contain the bot token. Never log request URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(serve(Config.from_env()))
    except ConsumerConflict as exc:
        logging.getLogger(__name__).error('%s', exc)
        raise SystemExit(78) from None


if __name__ == "__main__":
    main()
