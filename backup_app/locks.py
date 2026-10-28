"""Lock exclusivo entre processos baseado em arquivo.

O sistema operacional libera o lock automaticamente se o processo morrer,
então não existem "locks órfãos" após uma queda de energia ou crash.
"""

import os
from pathlib import Path
from typing import IO, Optional

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - suporte básico a outros sistemas
    import fcntl


class InterProcessLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle: Optional[IO] = None

    @property
    def locked(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Tenta obter o lock sem bloquear. Retorna False se outro processo o detém."""
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "InterProcessLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
