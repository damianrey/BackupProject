"""BackupProject - agendador de backups para SQL Server e MySQL.

Uso:
    pythonw BackupProject.py             abre o programa e mostra a tela de login
    pythonw BackupProject.py --startup   inicia minimizado na bandeja (usado pela inicialização do Windows)
    python  BackupProject.py --backup    executa um backup agora, sem interface, e encerra
                                         (útil no Agendador de Tarefas do Windows)
"""

import argparse
import os
import sys

from backup_app.paths import BASE_DIR, ensure_dirs


def main() -> int:
    parser = argparse.ArgumentParser(description="Agendador de backups para SQL Server e MySQL.")
    parser.add_argument("--startup", action="store_true",
                        help="inicia escondido na bandeja do sistema (inicialização do Windows)")
    parser.add_argument("--backup", action="store_true",
                        help="executa o backup imediatamente, sem interface, e encerra")
    # parse_known_args: com pythonw não há console para exibir erros de argumentos
    args, _unknown = parser.parse_known_args()

    # Garante que tudo funcione mesmo quando o Windows inicia o programa em System32
    os.chdir(BASE_DIR)
    ensure_dirs()

    from backup_app.log_config import setup_logging

    if args.backup:
        setup_logging(console=True)
        from backup_app.backup import BackupAlreadyRunning, run_backup
        try:
            report = run_backup("linha de comando")
        except BackupAlreadyRunning as exc:
            print(exc)
            return 2
        print(report.summary())
        return 0 if report.success else 1

    setup_logging()
    from backup_app.app import run_gui
    return run_gui(start_hidden=args.startup)


if __name__ == "__main__":
    sys.exit(main())
