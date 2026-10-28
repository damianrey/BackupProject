"""Mantido por compatibilidade. Prefira executar `python generate_key.py`.

Observação: o BackupProject gera a chave automaticamente na primeira execução.
"""

import sys

from generate_key import main

if __name__ == "__main__":
    sys.exit(main())
