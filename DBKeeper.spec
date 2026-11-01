# Receita do PyInstaller: inclui só o que o programa usa do Qt, para reduzir o pacote.
# A pasta de saída é passada na linha de comando (veja build-commands.txt).

# Módulos que o PyInstaller incluiria sem necessidade. O mysql-connector é só uma
# segunda opção ao PyMySQL e a versão para Windows 7 já não o tem.
EXCLUDED_MODULES = [
    "mysql", "_mysql_connector", "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtSvg", "PyQt5.QtDBus",
    "PyQt5.QtPrintSupport", "PyQt5.QtOpenGL", "tkinter", "unittest", "pydoc", "test",
]

# Únicos arquivos do Qt levados. A interface é desenhada sem OpenGL; qico lê o ícone do programa.
QT_FILES = {
    "qt5core.dll", "qt5gui.dll", "qt5widgets.dll", "qt5network.dll", "qt5winextras.dll",
    "msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll",
    "qwindows.dll", "qico.dll",
}


def keep(entry):
    path = entry[0].replace("\\", "/").lower()
    return "pyqt5/qt5/" not in path or path.rsplit("/", 1)[-1] in QT_FILES


a = Analysis(["DBKeeper.py"], datas=[("images", "images")], excludes=EXCLUDED_MODULES)
a.binaries = [entry for entry in a.binaries if keep(entry)]
a.datas = [entry for entry in a.datas if keep(entry)]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DBKeeper", icon="images/app_icon.ico", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="DBKeeper")
