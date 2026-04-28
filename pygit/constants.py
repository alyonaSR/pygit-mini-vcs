from pathlib import Path

# Путь к корневой директории репозитория.
# pygit хранит все свои служебные данные в .pygit/
GIT_DIR = Path(".pygit")

# Директория для хранения объектов Git-модели: blob, tree, commit
# Каждый объект хранится по пути .pygit/objects/xx/yyyyy...
OBJECTS_DIR = GIT_DIR / "objects"

# Путь к файлу индекса (.pygit/index),
# где хранятся сведения о файлах, добавленных через pygit add.
INDEX_FILE = GIT_DIR / "index"

# Кодировка, используемая при чтении/записи текстовых файлов.
UTF_8_ENCODING = "utf-8"

# Красивое форматирование JSON-файла индекса при записи.
# JSON_INDENT = 2 означает отступ в 2 пробела.
JSON_INDENT = 2

# Восьмеричный режим (permissions) для объекта tree.
# В Git все каталоги имеют режим 040000 (восьмеричная запись).
# 0o — префикс Python для восьмеричной системы.
TREE_DIRECTORY_MODE = 0o040000

# Длина SHA-1 хеша в "сырых" байтах — 20.
SHA_RAW_BYTES_LENGTH = 20

# Git использует первые два hex-символа SHA как имя подкаталога,
# а остальные 38 символов — как имя файла.
OID_DIR_PREFIX_LENGTH = 2

# Какие типы объектов допускает pygit.
VALID_OBJECT_TYPES = ("blob", "tree", "commit")
