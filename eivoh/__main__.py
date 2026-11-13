import asyncio
import logging
import os
import signal

from .channels.telegram import TelegramChannel
from .config import Config
from .executors.claude import ClaudeExecutor
from .gateway import Gateway
from .storage import Storage


async def serve(config: Config):
    os.umask(0o077)
    storage = Storage(config.state_dir / "eivoh.sqlite3")
    executor = ClaudeExecutor(config.workdir, config.claude_bin, config.claude_model, config.permission_mode)
    channel = TelegramChannel(config.telegram_token, config.owner_id)
    gateway = Gateway(storage, executor, {"telegram": channel})
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
                    await poll  # Configuration/conflict failures trigger systemd restart.
            finally:
                # Stop polling before children; running tasks stay recoverable.
                gateway.begin_shutdown()
                poll.cancel()
                stop.cancel()
                await asyncio.gather(poll, stop, return_exceptions=True)
                await gateway.shutdown()
    finally:
        await gateway.shutdown()
        storage.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP URLs contain the bot token. Never log request URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(serve(Config.from_env()))


if __name__ == "__main__":
    main()
