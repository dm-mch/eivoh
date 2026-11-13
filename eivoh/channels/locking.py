"""Same-user, same-host token lock. The lock inode is never unlinked."""
import fcntl
import hashlib
import os
from pathlib import Path
import stat


class ConsumerConflict(RuntimeError):
    pass


class TokenLock:
    def __init__(self, token):
        self.token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.fd = None

    def __enter__(self):
        directory = Path('/tmp') / f'eivoh-pollers-{os.getuid()}'
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ConsumerConflict('Unsafe Telegram consumer lock directory')
        dirfd = os.open(directory, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(dirfd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ConsumerConflict('Unsafe Telegram consumer lock directory')
            self.fd = os.open(self.token_hash + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                raise ConsumerConflict('Unsafe Telegram consumer lock file')
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConsumerConflict('Another local Eivoh process is already consuming this bot token') from None
        except BaseException:
            self.__exit__(None, None, None)
            raise
        finally:
            os.close(dirfd)
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
