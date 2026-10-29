# BackupProject

A backup program for scheduling daily backups on **SQL Server** and **MySQL**, running in the background in the Windows system tray.  
Um programa de backup para agendar backups diários no **SQL Server** e **MySQL**, rodando em segundo plano na bandeja do Windows.

![Release Version](https://img.shields.io/badge/Release_Version-2.0.0-brightgreen)
[![Python Version](https://img.shields.io/badge/Python_Version-3.8%2B-blue.svg)](https://www.python.org/downloads/)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D6)

---

## Features (EN)

- **Background execution in the system tray** with start, completion, and failure notifications.
- **Automatic Windows startup** (`HKCU\...\Run` via `pythonw.exe`, toggled directly from the **Sistema** tab).
- **Per-weekday & time scheduling** without freezing the UI (`QTimer` scheduler + single-worker `QThreadPool`).
- **Headless CLI mode (`--backup`)** to run backups without opening the GUI — ideal for **Windows Task Scheduler** when backups must run even with no user logged in.
- **MySQL & SQL Server support**:
  - **MySQL (5.1 to 8.x)** via `mysqldump` (prioritizes MySQL 5.1 paths or custom path, connects using `utf8` charset for 5.1 compatibility, passes credentials via temporary `--defaults-extra-file`, handles `COLUMN_STATISTICS` / `--routines` version differences, and writes atomically via `.part` before replacing `.sql`).
  - **SQL Server (2008 / 2008 R2 to 2019)** via `pyodbc` (`BACKUP DATABASE ... WITH INIT, FORMAT`, automatically negotiates installed ODBC Drivers — Driver 17, 18 with `Encrypt=no` fallback for 2008 TLS, 13, 11, Native Client 10/11, or legacy `SQL Server` — supporting SQL or Windows Authentication and named instances).
- **Security & reliability**:
  - Automatic Fernet key generation on first run, protected by Windows **DPAPI** (`content/fernet.key`).
  - User passwords hashed with **PBKDF2-SHA256** + salt (`content/credentials.json`), with automatic migration from legacy credentials.
  - Inter-process locks preventing duplicate app instances (`content/app.lock`) and concurrent backups (`content/backup.lock`).
  - Rotating log files (`logs/main.log`, `logs/error.log`, `logs/exception.log`).

## Funcionalidades (PT-BR)

- **Execução em segundo plano na bandeja do sistema** com notificações de início, conclusão e falha.
- **Inicialização automática com o Windows** (`HKCU\...\Run` usando `pythonw.exe`, configurável direto na aba **Sistema**).
- **Agendamento por dia da semana e horário**, sem congelar a interface (agendador via `QTimer` + fila de execução em worker único).
- **Modo linha de comando (`--backup`)** para rodar backups sem interface gráfica — ideal para o **Agendador de Tarefas do Windows** quando o backup precisa rodar mesmo sem usuário logado.
- **Suporte a MySQL e SQL Server**:
  - **MySQL (5.1 a 8.x)** via `mysqldump` (prioriza instalações 5.1 ou caminho configurável, conecta com charset `utf8` compatível com 5.1, passa credenciais via `--defaults-extra-file` temporário sem expor a senha na linha de comando, trata diferenças de versão como `COLUMN_STATISTICS` / `--routines` e grava em `.part` antes de substituir o `.sql`).
  - **SQL Server (2008 / 2008 R2 a 2019)** via `pyodbc` (`BACKUP DATABASE ... WITH INIT, FORMAT`, negocia automaticamente entre os drivers ODBC instalados — Driver 17, 18 com fallback `Encrypt=no` para SQL 2008, 13, 11, Native Client 10/11 ou `SQL Server` nativo — com suporte a autenticação SQL ou Windows e instâncias nomeadas).
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

---

## Usage / Como usar

1. Start the application / Inicie o programa:
   ```powershell
   python BackupProject.py
   ```
2. On **first run** (when no users exist yet), click **"Criar primeiro usuário"** on the login screen to register your username and password.  
   No **primeiro acesso** (enquanto não houver usuários cadastrados), clique em **"Criar primeiro usuário"** na tela de login para registrar seu usuário e senha.
3. Log in to open the main window / Faça login para abrir a janela principal:
   - **Conexões** (`config.png`): configure and test MySQL and/or SQL Server connections and click **Salvar Configurações**.
   - **Agendamento** (`schedule.png`): check the days of the week, set the times, select `Backup SQL Server` and/or `Backup MySQL`, and click **Salvar Configurações** (or **Executar backup agora**).
   - **Sistema** (`monitor.png`): toggle **"Iniciar com o Windows"**, choose the backup destination folder, open backup/log folders, or register additional users.
4. Closing the window hides the app in the **system tray** (double-click the tray icon to reopen; right-click for **Abrir**, **Backup**, or **Sair**).

### Command-Line Options / Parâmetros de linha de comando

| Command / Comando | Description / Descrição |
| --- | --- |
| `pythonw BackupProject.py` | Opens the app and shows the login screen / Abre o programa exibindo a tela de login |
| `pythonw BackupProject.py --startup` | Starts hidden in the system tray (used by Windows startup) / Inicia oculto na bandeja do sistema |
| `python BackupProject.py --backup` | Runs the configured backup immediately without GUI and exits (`0` = success) / Executa o backup sem GUI e encerra |
| `python generate_key.py` | Manually generates `content/fernet.key` (`--plain` to skip DPAPI, `--force` to overwrite) |

---

## Project Structure / Estrutura do projeto

```text
BackupProject/
├── BackupProject.py        # Entry point (GUI, --startup, and --backup)
├── generate_key.py         # Optional CLI utility to regenerate the Fernet key
├── Fernet.py               # Backwards-compatible wrapper for generate_key.py
├── requirements.txt        # Python dependencies
├── backup_app/
│   ├── paths.py            # Absolute paths and atomic file writes
│   ├── log_config.py       # Rotating loggers (main.log, error.log, exception.log)
│   ├── locks.py            # Inter-process file locks (single instance & single backup)
│   ├── security.py         # Windows DPAPI, Fernet encryption, and PBKDF2-SHA256 hashing
│   ├── users.py            # User authentication and registration
│   ├── settings.py         # Read/write for settings.ini and dbcredentials.ini
│   ├── backup.py           # MySQL and SQL Server backup engine (Qt-free)
│   ├── scheduler.py        # Weekday scheduler + single-worker QThreadPool
│   ├── startup.py          # Windows Run registry integration (HKCU\...\Run)
│   ├── ui.py               # Login, Registration, and Main Window screens
│   └── app.py              # System tray controller, single-instance IPC, and dark theme
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
pyinstaller --noconsole --onedir --name BackupProject --icon images/system.ico --add-data "images;images" BackupProject.py
```

---

## Credits / Créditos

- [Icons by Muhammad Ali (Flaticon)](https://www.flaticon.com/authors/muhammad-ali/gradient?author_id=570&type=standard)
