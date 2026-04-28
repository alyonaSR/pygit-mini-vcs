"""
Модуль описывает классы и функции для работы с внутренними объектами Git:

- GitObject (абстрактный базовый класс)
- Blob      (содержимое файла)
- Tree      (дерево — как директория с записями)
- Commit    (снимок состояния + метаданные)
- CommitHistoryIterator (итератор по истории коммитов)
- hash_object / read_object (работа с .pygit/objects)

Каждый Git-объект обязан уметь:

* serialize()   -> bytes     — как он хранится в .pygit/objects
* deserialize() -> GitObject — как восстановить объект из «сырых» байт
"""
# Это позволяет использовать классы,
# которые объявлены ниже в файле, без кавычек
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from .constants import (
    SHA_RAW_BYTES_LENGTH,
    OBJECTS_DIR,
    OID_DIR_PREFIX_LENGTH,
    VALID_OBJECT_TYPES,
    UTF_8_ENCODING,
)
import hashlib
import zlib


class GitObject(ABC):
    """
    Абстрактный базовый класс для всех Git-объектов.

    Любой наследник должен уметь сериализоваться в байты и
    восстанавливаться из байтов.
    """

    type_name: str  # 'blob' | 'tree' | 'commit'

    # @abstractmethod - этот метод обязаны реализовать конкретные подклассы
    @abstractmethod
    def serialize(self) -> bytes:
        """
        Преобразует внутреннее содержимое объекта в байты.

        Returns:
            bytes: Сериализованное содержимое объекта.
        """
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def deserialize(cls, data: bytes) -> GitObject:
        """
        Восстанавливает объект из байтов.

        Args:
            data (bytes): Сырые байты объекта без заголовка Git.

        Returns:
            GitObject: Восстановленный объект нужного типа.
        """
        raise NotImplementedError


@dataclass
class Blob(GitObject):
    """
    Самый простой объект: хранит ровно байты файла.

    Git не интересует имя файла — только его содержимое.
    """

    data: bytes
    type_name: str = "blob"

    def serialize(self) -> bytes:
        """
        Возвращает данные blob как есть.

        Returns:
            bytes: Содержимое файла.
        """
        return self.data

    @classmethod
    def deserialize(cls, data: bytes) -> Blob:
        """
        Восстанавливает blob из байтов.

        Args:
            data (bytes): Сырые байты содержимого файла.

        Returns:
            Blob: Новый blob с переданными данными.
        """
        # Восстановить blob — значит просто положить байты обратно
        return cls(data=data)  # сопоставление имени


@dataclass
class TreeEntry:
    """
    Одна запись дерева.

    Атрибуты:
        mode (int): Числа вида 0o100644 (файл), 0o100755 (исполняемый),
            0o040000 (директория/tree).
        name (str): Имя файла/поддиректории (строка без '/').
        sha (str): 40-символьный hex SHA-1 указываемого объекта
            (blob или tree).
    """

    mode: int
    name: str
    sha: str  # 40 hex chars


class Tree(GitObject):
    """
    Tree — это список записей (файлы и поддиректории).

    Внутренний бинарный формат Git для каждой записи:

        b"{mode:o} {name}\\0{sha_raw}"

    где:
        * {mode:o} — строка восьмеричного числа прав (без префикса 0o)
        * пробел
        * {name}   — имя
        * \\0      — нулевой байт
        * {sha_raw} — 20 сырых байт SHA-1 (НЕ текст '40 hex', а именно
          20 байт).
    """

    entries: List[TreeEntry]
    type_name: str = "tree"

    def __init__(self, entries: Optional[List[TreeEntry]] = None) -> None:
        """
        Создаёт объект дерева.

        Если список записей не передан, дерево будет пустым.

        Args:
            entries (Optional[List[TreeEntry]]): Список записей дерева
                (mode, name, sha). По умолчанию пустой список.
        """
        if entries is None:
            self.entries = []
        else:
            self.entries = list(entries)

    def serialize(self) -> bytes:
        """
        Сериализует дерево в бинарный формат Git.

        Returns:
            bytes: Бинарное представление дерева в формате Git.
        """
        parts: List[bytes] = []

        # Сортируем по имени
        for entry in sorted(
            self.entries,
            key=lambda tree_entry: tree_entry.name,
        ):
            # mode записывается как текст восьмеричного числа
            mode_text = f"{entry.mode:o}".encode("ascii")
            name_bytes = entry.name.encode(UTF_8_ENCODING)
            # sha у нас хранится как 40 hex-символов → нужно превратить
            # в 20 сырых байт
            sha_raw = bytes.fromhex(entry.sha)
            # Склеиваем запись: <mode> <name>\0<sha_raw>
            parts.append(mode_text + b" " + name_bytes + b"\x00" + sha_raw)

        return b"".join(parts)

    @classmethod
    def deserialize(cls, data: bytes) -> Tree:
        """
        Разбирает бинарный поток дерева Git.

        Схема:
        * читаем до пробела → mode (текст в восьмеричном формате)
        * читаем до \\0     → name
        * читаем следующие 20 байт → sha_raw
        Повторяем, пока не кончатся байты.

        Args:
            data (bytes): Бинарное содержимое tree-объекта без заголовка.

        Returns:
            Tree: Восстановленный объект дерева.

        Raises:
            ValueError: Если формат данных некорректен.
        """
        idx = 0
        data_length = len(data)
        entries: List[TreeEntry] = []

        while idx < data_length:
            # 1) MODE (до пробела)
            space_index = data.find(b" ", idx)
            if space_index == -1:
                raise ValueError("invalid tree: cannot find space after mode")
            mode_text = data[idx:space_index].decode("ascii")
            mode = int(mode_text, 8)
            idx = space_index + 1

            # 2) NAME (до \0)
            nul_index = data.find(b"\x00", idx)
            if nul_index == -1:
                raise ValueError("invalid tree: cannot find NUL after name")
            name = data[idx:nul_index].decode(UTF_8_ENCODING)
            idx = nul_index + 1

            # 3) SHA (20 сырых байт)
            if idx + SHA_RAW_BYTES_LENGTH > data_length:
                raise ValueError("invalid tree: truncated sha")
            sha_raw = data[idx: idx + SHA_RAW_BYTES_LENGTH]
            sha_hex = sha_raw.hex()
            idx += SHA_RAW_BYTES_LENGTH

            entries.append(TreeEntry(mode=mode, name=name, sha=sha_hex))

        return cls(entries=entries)


@dataclass
class Commit(GitObject):
    """
    Commit — текстовый объект.

    В простейшем виде он содержит:
      - tree <sha>
      - parent <sha>        (опционально; у первого коммита нет parent)
      - author <строка>
      - committer <строка>  (обычно совпадает с author)
      - пустая строка
      - сообщение (может быть многострочным)
      - завершающая пустая строка

    Пример author:
        "User <u@e> 1730910000 +0000".
    """

    tree: str  # 40 hex SHA дерева
    parent: Optional[str]  # 40 hex SHA родителя или None
    author: str  # строка "Имя <email> ts tz"
    message: str  # текст сообщения коммита

    type_name: str = "commit"

    def serialize(self) -> bytes:
        """
        Сериализует commit в текстовый формат Git.

        Returns:
            bytes: Байт-строка с заголовками и сообщением коммита.
        """
        lines: List[str] = [f"tree {self.tree}"]
        if self.parent:
            lines.append(f"parent {self.parent}")
        # committer = author; для простоты используем одно и то же значение
        lines.append(f"author {self.author}")
        lines.append(f"committer {self.author}")
        # Пустая строка отделяет заголовки от тела сообщения
        lines.append("")
        lines.append(self.message)
        # (Опционально) завершающая пустая строка — удобно при чтении
        lines.append("")
        return "\n".join(lines).encode(UTF_8_ENCODING)

    @classmethod
    def deserialize(cls, data: bytes) -> Commit:
        """
        Восстанавливает commit из байтов.

        Args:
            data (bytes): Байт-строка с содержимым commit-объекта
                без заголовка Git.

        Returns:
            Commit: Восстановленный объект коммита.

        Raises:
            ValueError: Если обязательные поля (tree/author) отсутствуют.
        """
        text = data.decode(UTF_8_ENCODING, errors="replace")

        # Делим заголовки и тело по первой пустой строке
        if "\n\n" in text:
            headers_text, body = text.split("\n\n", 1)
        else:
            headers_text, body = text, ""

        headers: Dict[str, str] = {}
        parent: Optional[str] = None
        tree: Optional[str] = None
        author: Optional[str] = None

        for line in headers_text.splitlines():
            if not line.strip():
                continue
            if line.startswith("tree "):
                tree = line[5:].strip()
                headers["tree"] = tree
            elif line.startswith("parent "):
                parent = line[7:].strip()
                headers["parent"] = parent
            elif line.startswith("author "):
                author = line[7:].strip()
                headers["author"] = author
            # строку committer можно игнорировать (мы ставим = author
            # при сериализации)

        if not tree or not author:
            raise ValueError(
                "invalid commit: missing required fields (tree/author)",
            )

        # Уберём возможную финальную пустую строку из body
        # (коммит может оканчиваться \n\n)
        if body.endswith("\n"):
            body = body[:-1]

        return cls(tree=tree, parent=parent, author=author, message=body)


class CommitHistoryIterator:
    """
    Итератор по истории коммитов.

    Идея:
      - в __init__ получаем SHA «текущего» коммита (обычно тот, на который
        указывает HEAD)
      - в __next__:
          * читаем объект по SHA
          * десериализуем как Commit
          * возвращаем информацию о нём
          * сдвигаем «текущий SHA» на его parent
          * если parent = None → истории больше нет → StopIteration
    """

    def __init__(self, start_oid: str) -> None:
        """
        Инициализирует итератор с начального SHA коммита.

        Args:
            start_oid (str): SHA-1 коммита, с которого начинается обход.
        """
        # текущий SHA коммита, на котором мы «стоим»
        self.current_oid: str | None = start_oid

    def __iter__(self) -> CommitHistoryIterator:
        """
        Возвращает сам итератор.

        Returns:
            CommitHistoryIterator: Текущий объект итератора.
        """
        return self

    def __next__(self) -> Tuple[str, str, str]:
        """
        Возвращает следующую запись в истории коммитов.

        Returns:
            Tuple[str, str, str]: Кортеж вида
                (oid, author, message).

        Raises:
            StopIteration: Если история коммитов закончилась.
            ValueError: Если объект по SHA не является коммитом.
        """
        # если SHA больше нет — история закончилась
        if self.current_oid is None:
            raise StopIteration

        oid = self.current_oid

        # читаем объект по SHA
        obj_type, data = read_object(oid)
        if obj_type != "commit":
            # на всякий случай проверяем тип
            raise ValueError(
                f"Object {oid} is not a commit (type={obj_type})",
            )

        # превращаем байты в объект Commit
        commit = Commit.deserialize(data)

        # подготовим данные для возврата
        info = (oid, commit.author, commit.message)

        # сдвигаем указатель на родителя
        # parent у нас либо строка SHA, либо None
        self.current_oid = commit.parent

        return info


def hash_object(data: bytes, obj_type: str) -> str:
    """
    Создаёт «сырой объект» Git-формата, считает его SHA-1 и сохраняет
    (в сжатом виде) в .pygit/objects/xx/xxxx....

    Args:
        data (bytes): Байты полезной нагрузки (payload),
            например содержимое файла.
        obj_type (str): Тип объекта: 'blob', 'tree' или 'commit'.

    Returns:
        str: 40-символьный hex-хеш объекта.

    Raises:
        ValueError: Если передан неизвестный тип объекта.
    """
    if obj_type not in VALID_OBJECT_TYPES:
        raise ValueError(f"unknown obj_type: {obj_type}")

    # 1–2) Заголовок вида: b"{type} {len(data)}\\0"
    header = f"{obj_type} {len(data)}".encode(UTF_8_ENCODING) + b"\x00"

    # 3) Полные данные объекта = заголовок + payload
    full = header + data

    # 4) SHA-1 от «полных данных» (именно так делает Git)
    oid = hashlib.sha1(full).hexdigest()

    # 5) Сжимаем — сделаем как в Git
    compressed = zlib.compress(full)

    # 6) Сохраняем по пути .pygit/objects/aa/bbbbb...
    # Первые 2 символа — это имя каталога.
    dirpath = OBJECTS_DIR / oid[:OID_DIR_PREFIX_LENGTH]
    dirpath.mkdir(parents=True, exist_ok=True)
    objpath = dirpath / oid[OID_DIR_PREFIX_LENGTH:]

    # Не перезаписываем, если уже есть (идемпотентно, как Git)
    if not objpath.exists():
        objpath.write_bytes(compressed)

    # 7) Возвращаем хеш
    return oid


def read_object(oid: str) -> Tuple[str, bytes]:
    """
    Читает объект из .pygit/objects по его SHA-1 (oid).

    Args:
        oid (str): 40-символьный hex SHA-1 объекта.

    Returns:
        Tuple[str, bytes]: Пара (obj_type, data), где:
            obj_type (str): Строка 'blob', 'tree' или 'commit'.
            data (bytes): «Полезные» байты (то, что вернёт serialize()).

    Raises:
        ValueError: Если формат объекта некорректен или длина данных
            не совпадает с указанной в заголовке.
    """
    objpath = (
        OBJECTS_DIR
        / oid[:OID_DIR_PREFIX_LENGTH]
        / oid[OID_DIR_PREFIX_LENGTH:]
    )
    compressed = objpath.read_bytes()
    full = zlib.decompress(compressed)

    # full = b"{type} {size}\0" + data
    nul_pos = full.find(b"\x00")
    if nul_pos == -1:
        raise ValueError("invalid object: no NUL in header")

    header = full[:nul_pos]  # b"commit 123"
    data = full[nul_pos + 1:]  # сами данные

    header_text = header.decode(UTF_8_ENCODING)
    # разбиваем "commit 123" → ["commit", "123"]
    obj_type, size_str = header_text.split(" ", 1)
    size = int(size_str)

    # маленькая проверка: длина данных должна совпадать с размером в заголовке
    if len(data) != size:
        raise ValueError(
            f"invalid object {oid}: bad length {len(data)} != {size}",
        )

    return obj_type, data
