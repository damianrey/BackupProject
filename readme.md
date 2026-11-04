# DBKeeper

> Formerly **BackupProject** / Anteriormente chamado **BackupProject**.

A backup program for scheduling daily backups on **SQL Server** and **MySQL**, running in the background in the Windows system tray.  
Um programa de backup para agendar backups diários no **SQL Server** e **MySQL**, rodando em segundo plano na bandeja do Windows.

![Release Version](https://img.shields.io/badge/Release_Version-2.0.0-brightgreen)
[![Python Version](https://img.shields.io/badge/Python_Version-3.8%2B-blue.svg)](https://www.python.org/downloads/)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D6)

---

## Features (EN)

- **Background execution in the system tray**: a badge on the tray icon shows when a backup is running or needs attention, a click opens a summary menu, and scheduling can be paused until resumed or until the app restarts. Failures always notify; start and success notifications are optional (**Sistema** tab), and clicking a notification opens the app.
- **Automatic Windows startup** (`HKCU\...\Run` via `pythonw.exe`, toggled directly from the **Sistema** tab).
- **Rule-based scheduling with multiple backups per day**: each rule picks weekdays and either fixed times (e.g. `08:00, 12:00, 18:00`) or an interval (e.g. every 3h from 07:00 to 19:00); overlapping rules never duplicate a run. Runs without freezing the UI (`QTimer` scheduler + single-worker `QThreadPool`). Old `[Days]/[Time]` settings are migrated automatically.
- **Home screen and backup history**: the **Início** tab shows at a glance whether backups are up to date and the progress per database (also on the taskbar button); results, including `--backup` runs, are kept in `content/history.jsonl`.
- **Timestamped backups + retention**: files are named `database_YYYY-MM-DD_HHMM.sql/.bak`; after each successful backup, files older than *N* days are deleted (the newest backup of each database is always kept). Shortening the period first tells how many backups will be deleted.
- **Backup verification**: every backup is checked before it counts as successful (`RESTORE VERIFYONLY` on SQL Server, the end-of-dump marker on MySQL), and old backups are only deleted after a verified one.
- **Guided restore** (**Sistema** tab): restores into a new database by default. Replacing an existing one requires typing its name and first takes a verified safety copy. `.sql` files that pick their own database are refused.
- **Guided setup**: on a new installation, three steps cover the databases, schedule and backup folder, and can be reopened from the **Sistema** tab.
- **Headless CLI mode (`--backup`)** to run backups without opening the GUI — ideal for **Windows Task Scheduler** when backups must run even with no user logged in.
- **MySQL & SQL Server support**:
  - **MySQL (5.1 to 8.x)** via `mysqldump` (prioritizes MySQL 5.1 paths or custom path, connects using `utf8` charset for 5.1 compatibility, passes credentials via temporary `--defaults-extra-file`, handles `COLUMN_STATISTICS` / `--routines` version differences, and writes atomically via `.part` before replacing `.sql`).
  - **SQL Server (2008 / 2008 R2 to 2019)** via `pyodbc` (backs up every online user database on the instance, skipping `master`, `model`, `msdb` and `tempdb`, with `BACKUP DATABASE ... WITH INIT, FORMAT`; automatically negotiates installed ODBC Drivers — Driver 17, 18 with `Encrypt=no` fallback for 2008 TLS, 13, 11, Native Client 10/11, or legacy `SQL Server` — supporting SQL or Windows Authentication and named instances).
- **Security & reliability**:
  - Automatic Fernet key generation on first run, protected by Windows **DPAPI** (`content/fernet.key`).
  - User passwords hashed with **PBKDF2-SHA256** + salt (`content/credentials.json`), with automatic migration from legacy credentials.
  - Inter-process locks preventing duplicate app instances (`content/app.lock`) and concurrent backups (`content/backup.lock`).
  - Rotating log files (`logs/main.log`, `logs/error.log`, `logs/exception.log`).

## Funcionalidades (PT-BR)

- **Execução em segundo plano na bandeja do sistema**: um selo no ícone mostra quando há backup em andamento ou algo que precisa de atenção, um clique abre um menu com o resumo, e o agendamento pode ser pausado até retomar ou reiniciar o programa. Falhas sempre notificam; avisos de início e sucesso são opcionais (aba **Sistema**), e clicar na notificação abre o programa.
- **Inicialização automática com o Windows** (`HKCU\...\Run` usando `pythonw.exe`, configurável direto na aba **Sistema**).
- **Agendamento por regras, com vários backups por dia**: cada regra define os dias da semana e horários fixos (ex.: `08:00, 12:00, 18:00`) ou um intervalo (ex.: a cada 3h das 07:00 às 19:00); regras sobrepostas não duplicam backups. Roda sem congelar a interface (agendador via `QTimer` + fila de execução em worker único). Configurações antigas (`[Days]/[Time]`) são migradas automaticamente.
- **Tela inicial e histórico**: a aba **Início** mostra de imediato se os backups estão em dia e o progresso por banco (também no botão da barra de tarefas); os resultados, inclusive do `--backup`, ficam em `content/history.jsonl`.
- **Backups com data/hora + retenção**: arquivos no formato `banco_AAAA-MM-DD_HHMM.sql/.bak`; após cada backup bem-sucedido, os arquivos com mais de *N* dias são apagados (o backup mais recente de cada banco é sempre mantido). Reduzir o prazo avisa antes quantos backups serão apagados.
- **Verificação dos backups**: cada backup é conferido antes de contar como sucesso (`RESTORE VERIFYONLY` no SQL Server, marca de fim do dump no MySQL), e os antigos só são apagados depois de um backup verificado.
- **Restauração guiada** (aba **Sistema**): restaura em um banco novo por padrão. Substituir um banco existente exige digitar o nome dele e antes grava uma cópia de segurança verificada. Arquivos `.sql` que escolhem o próprio banco são recusados.
- **Configuração guiada**: em uma instalação nova, três passos cobrem bancos, agenda e pasta de backups, e podem ser reabertos pela aba **Sistema**.
- **Modo linha de comando (`--backup`)** para rodar backups sem interface gráfica — ideal para o **Agendador de Tarefas do Windows** quando o backup precisa rodar mesmo sem usuário logado.
- **Suporte a MySQL e SQL Server**:
  - **MySQL (5.1 a 8.x)** via `mysqldump` (prioriza instalações 5.1 ou caminho configurável, conecta com charset `utf8` compatível com 5.1, passa credenciais via `--defaults-extra-file` temporário sem expor a senha na linha de comando, trata diferenças de versão como `COLUMN_STATISTICS` / `--routines` e grava em `.part` antes de substituir o `.sql`).
  - **SQL Server (2008 / 2008 R2 a 2019)** via `pyodbc` (copia todos os bancos de usuário online da instância, ignorando `master`, `model`, `msdb` e `tempdb`, com `BACKUP DATABASE ... WITH INIT, FORMAT`; negocia automaticamente entre os drivers ODBC instalados — Driver 17, 18 com fallback `Encrypt=no` para SQL 2008, 13, 11, Native Client 10/11 ou `SQL Server` nativo — com suporte a autenticação SQL ou Windows e instâncias nomeadas).
- **Segurança e confiabilidade**:
  - Chave Fernet gerada automaticamente na primeira execução e protegida com **DPAPI** do Windows (`content/fernet.key`).
  - Senhas de usuários da interface armazenadas com hash **PBKDF2-SHA256** + salt (`content/credentials.json`), com migração automática de senhas antigas.
  - Proteção contra múltiplas instâncias simultâneas (`content/app.lock`) e contra backups concorrentes (`content/backup.lock`).
  - Logs com rotação automática de tamanho (`logs/main.log`, `logs/error.log`, `logs/exception.log`).

---

## Installation / Instalação

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **SQL Server:** install Microsoft **ODBC Driver 17 or 18 for SQL Server**.  
> **MySQL:** requires `mysqldump.exe` installed (MySQL Server / MySQL Workbench / MariaDB).

### Windows 7

Windows 7 requires **Python 3.8** (the last version that supports it) and pinned dependencies.  
O Windows 7 exige o **Python 3.8** (a última versão compatível) e dependências com versões fixas.

1. **Prerequisites / Pré-requisitos:** Windows 7 **SP1** + updates **KB2533623** and **KB2999226** (Universal C Runtime).
2. Install / Instale o **[Python 3.8.10](https://www.python.org/downloads/release/python-3810/)** (last 3.8 release with a Windows installer / última 3.8 com instalador).
3. In `cmd` / No `cmd`:
   ```bat
   py -3.8 -m venv .venv
   .venv\Scripts\activate.bat
   pip install -r requirements-win7.txt
   ```
4. **SQL Server:** ODBC Driver 18 does not run on Windows 7 — use **ODBC Driver 17**, **SQL Server Native Client 11**, or the built-in `SQL Server` driver (the app negotiates them automatically).  
   O ODBC Driver 18 não roda no Windows 7 — use o **ODBC Driver 17**, o **SQL Server Native Client 11** ou o driver `SQL Server` nativo (o programa escolhe automaticamente).

`requirements-win7.txt` keeps `cryptography` below 43 (newer builds use a Rust toolchain without Windows 7 support) and leaves out `mysql-connector-python` (PyMySQL is used instead).  
To build an `.exe` for Windows 7, run PyInstaller **with Python 3.8** (see *Packaging*) and test it on a Windows 7 machine/VM.

> ⚠️ Python 3.8 and Windows 7 no longer receive security updates. / O Python 3.8 e o Windows 7 não recebem mais atualizações de segurança.

---

## Usage / Como usar

1. Start the application / Inicie o programa:
   ```powershell
   python DBKeeper.py
   ```
2. On **first run** (when no users exist yet), click **"Criar primeiro usuário"** on the login screen to register your username and password.  
   No **primeiro acesso** (enquanto não houver usuários cadastrados), clique em **"Criar primeiro usuário"** na tela de login para registrar seu usuário e senha.
3. Log in to open the main window / Faça login para abrir a janela principal:
   - **Início** (home icon): shows whether backups are up to date, the last and next backup, the history, and **Executar agora**.
   - **Conexões** (database icon): configure and test MySQL and/or SQL Server connections and click **Salvar configurações**.
   - **Agendamento** (calendar icon): add scheduling rules (**Adicionar / Editar / Remover**; the checkbox enables/disables a rule), set how many days to keep backups, select `Backup SQL Server` and/or `Backup MySQL`, and click **Salvar configurações**.
   - **Sistema** (gear icon): toggle **"Iniciar com o Windows"** and the optional notifications, choose the backup destination folder, open backup/log folders, or register additional users.
4. Closing the window hides the app in the **system tray** (double-click the tray icon to reopen; click or right-click for the summary, **Abrir**, **Executar backup**, **Pausar agendamento**, or **Sair**).

### Command-Line Options / Parâmetros de linha de comando

| Command / Comando | Description / Descrição |
| --- | --- |
| `pythonw DBKeeper.py` | Opens the app and shows the login screen / Abre o programa exibindo a tela de login |
| `pythonw DBKeeper.py --startup` | Starts hidden in the system tray (used by Windows startup) / Inicia oculto na bandeja do sistema |
| `python DBKeeper.py --backup` | Runs the configured backup immediately without GUI and exits (`0` = success) / Executa o backup sem GUI e encerra |
| `python generate_key.py` | Manually generates `content/fernet.key` (`--plain` to skip DPAPI, `--force` to overwrite) |

---

## Project Structure / Estrutura do projeto

```text
DBKeeper/
├── DBKeeper.py             # Entry point (GUI, --startup, and --backup)
├── BackupProject.py        # Backwards-compatible wrapper (old name) for DBKeeper.py
├── generate_key.py         # Optional CLI utility to regenerate the Fernet key
├── Fernet.py               # Backwards-compatible wrapper for generate_key.py
├── requirements.txt        # Python dependencies
├── requirements-win7.txt   # Pinned dependencies for Windows 7 (Python 3.8)
├── backup_app/
│   ├── paths.py            # Absolute paths and atomic file writes
│   ├── log_config.py       # Rotating loggers (main.log, error.log, exception.log)
│   ├── locks.py            # Inter-process file locks (single instance & single backup)
│   ├── security.py         # Windows DPAPI, Fernet encryption, and PBKDF2-SHA256 hashing
│   ├── users.py            # User authentication and registration
│   ├── settings.py         # Read/write for settings.ini and dbcredentials.ini
│   ├── backup.py           # MySQL and SQL Server backup engine (Qt-free)
│   ├── history.py          # Backup history and the overall status shown on the home screen
│   ├── restore.py          # Backup restore (no Qt)
│   ├── scheduler.py        # Rule-based weekly scheduler + single-worker QThreadPool
│   ├── startup.py          # Windows Run registry integration (HKCU\...\Run)
│   ├── icons.py            # Phosphor icons (QtAwesome) and the dark theme / stylesheet
│   ├── ui.py               # Login, Registration, and Main Window screens
│   └── app.py              # System tray controller and single-instance IPC
├── config/
│   ├── settings.ini
│   └── dbcredentials.ini
├── content/
│   └── credentials.json
├── images/
└── logs/
```

---

## Packaging / Empacotamento (PyInstaller)

```powershell
pip install pyinstaller
pyinstaller --noconfirm --clean DBKeeper.spec
```

`DBKeeper.spec` keeps only the Qt files the app uses and leaves out `mysql-connector-python`, cutting the package roughly in half.  
O `DBKeeper.spec` leva só os arquivos do Qt que o programa usa e deixa de fora o `mysql-connector-python`, reduzindo o pacote a cerca da metade.

---

## Credits / Créditos

- Interface icons: [Phosphor Icons](https://phosphoricons.com/) (MIT) via [QtAwesome](https://github.com/spyder-ide/qtawesome)
- [App icon by Muhammad Ali (Flaticon)](https://www.flaticon.com/authors/muhammad-ali/gradient?author_id=570&type=standard)
