"""Gera a chave Fernet usada para cifrar as credenciais dos bancos.

Normalmente NÃO é necessário: o programa gera a chave sozinho na primeira
execução. Use este script apenas para recriar a chave manualmente.

    python generate_key.py            # gera content/fernet.key protegida por DPAPI
    python generate_key.py --plain    # gera em texto puro (portável entre usuários/máquinas)
    python generate_key.py --force    # sobrescreve uma chave existente
"""

import argparse
import sys

from cryptography.fernet import Fernet

from backup_app.paths import KEY_FILE
from backup_app.security import dpapi_available, write_key


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera a chave Fernet do DBKeeper.")
    parser.add_argument("--plain", action="store_true", help="não proteger a chave com DPAPI")
    parser.add_argument("--force", action="store_true", help="sobrescrever a chave existente")
    args = parser.parse_args()

    if KEY_FILE.exists() and not args.force:
        print(f"A chave '{KEY_FILE}' já existe. Uma nova chave tornaria ilegíveis as credenciais "
              "já salvas. Use --force se tiver certeza.")
        return 1

    use_dpapi = not args.plain and dpapi_available()
    write_key(Fernet.generate_key(), KEY_FILE, use_dpapi=use_dpapi)
    print(f"Chave Fernet gerada e salva em '{KEY_FILE}'" + (" (protegida por DPAPI)." if use_dpapi else "."))
    if args.force:
        print("Informe novamente as credenciais dos bancos na aba Conexões.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
