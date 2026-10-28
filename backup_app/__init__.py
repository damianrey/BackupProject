"""Pacote principal do BackupProject.

Módulos:
    paths       -> caminhos absolutos (independentes do diretório de trabalho)
    log_config  -> configuração dos loggers e captura de exceções
    locks       -> lock entre processos (instância única / backup único)
    security    -> chave Fernet (protegida por DPAPI) e hash de senhas
    users       -> cadastro e login de usuários
    settings    -> leitura/gravação de settings.ini e dbcredentials.ini
    backup      -> execução dos backups (sem Qt, pode rodar sem interface)
    scheduler   -> agendamento + worker em segundo plano (Qt)
    startup     -> inicialização automática com o Windows
    ui          -> telas (login, registro, janela principal)
    app         -> controlador da aplicação (bandeja, instância única)
"""

__version__ = "2.0.0"
