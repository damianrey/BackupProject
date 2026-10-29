"""Mantido por compatibilidade (nome antigo do programa). Prefira executar `DBKeeper.py`.

Tarefas do Agendador do Windows e atalhos criados antes da troca de nome
continuam funcionando por este arquivo.
"""

import sys

from DBKeeper import main

if __name__ == "__main__":
    sys.exit(main())
