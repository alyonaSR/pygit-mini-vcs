"""
Модуль для работы с ИНДЕКСОМ (staging area).

Индекс — это просто файл, где мы храним информацию
о том, какие файлы уже "подготовлены" для коммита.

Мы будем хранить индекс в формате JSON (список списков):
[
  ["path/relative.txt", "sha1хеш", 33188],
  ...
]

Внутри Python будем работать с ними как с кортежами:
(path: str, sha: str, mode: int)
"""


from pathlib import Path
import json
from .constants import (
    GIT_DIR,
    UTF_8_ENCODING,
    INDEX_FILE,
    JSON_INDENT,
    TREE_DIRECTORY_MODE,
)
from typing import Any, Dict, Iterator, List, Tuple

from .objects import hash_object, Tree, TreeEntry

# Тип для одной записи индекса: (путь, sha, режим)
IndexEntry = Tuple[str, str, int]


def read_index() -> List[IndexEntry]:
    """
    Считываем индекс из файла .pygit/index.

    Если файла нет – возвращаем пустой список (индекс пустой).
    Формат хранения в файле – список троек [path, sha, mode].

    Returns:
        List[IndexEntry]: Список записей индекса в виде кортежей
        (path, sha, mode).
    """
    if not INDEX_FILE.exists():
        return []

    text = INDEX_FILE.read_text(encoding=UTF_8_ENCODING)
    # ожидаем: [["file.txt", "ab12..", 33188], ...]
    raw_list = json.loads(text)

    entries: List[IndexEntry] = []
    for path, sha, mode in raw_list:
        entries.append((path, sha, int(mode)))
    return entries


def write_index(entries: List[IndexEntry]) -> None:
    """
    Записываем список записей индекса в файл .pygit/index в формате JSON.

    Entries — список кортежей (path, sha, mode).
    В файл пишем как список списков, чтобы JSON умел это хранить.

    Args:
        entries (List[IndexEntry]): Список записей индекса для сохранения.
    """
    # Преобразуем в список простых списков для JSON
    raw_list = [[path, sha, mode] for (path, sha, mode) in entries]

    GIT_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(
        json.dumps(raw_list, indent=JSON_INDENT),
        encoding=UTF_8_ENCODING,
    )


def _get_file_mode(path: Path) -> int:
    """
    Определяем режим (mode) файла — это права доступа и тип.

    - обычный файл → берём его st_mode как есть.

    Args:
        path (Path): Путь к файлу.

    Returns:
        int: Режим файла (st_mode).
    """
    st = path.stat()
    # st.st_mode — битовая маска, которая кодирует тип и права.
    return st.st_mode


def add_to_index(path: Path) -> None:
    """
    Добавляет или обновляет запись о файле в индексе.

    Шаги:
      1. прочитать файл → bytes
      2. создать blob-объект: hash_object(data, "blob") → sha
      3. вычислить mode (права/тип файла)
      4. прочитать текущий индекс
      5. заменить запись для этого пути или добавить новую
      6. записать индекс обратно

    Args:
        path (Path): Путь к файлу, который нужно добавить в индекс.

    Raises:
        FileNotFoundError: Если указанный путь не является файлом.
    """
    # Убедимся, что файл существует
    if not path.is_file():
        raise FileNotFoundError(f"cannot add '{path}': not a file")

    # 1. читаем содержимое файла как байты
    data = path.read_bytes()

    # 2. создаём blob и сохраняем его через hash_object
    sha = hash_object(data, "blob")

    # 3. получаем mode файла
    mode = _get_file_mode(path)

    # 4. читаем текущий индекс
    entries = read_index()

    # В индексе храним путь, как его передали
    # (относительный от текущего каталога)
    rel_path = str(path)

    # 5. обновляем список: если запись для этого файла есть — заменим её.
    new_entries: List[IndexEntry] = []
    found = False
    for old_path, old_sha, old_mode in entries:
        if old_path == rel_path:
            # заменяем старые sha/mode на новые
            new_entries.append((rel_path, sha, mode))
            found = True
        else:
            new_entries.append((old_path, old_sha, old_mode))

    if not found:
        # файла ещё не было в индексе — добавляем
        new_entries.append((rel_path, sha, mode))

    # Для аккуратности можно отсортировать по пути,
    # чтобы индекс был стабильным
    new_entries.sort(key=lambda entry: entry[0])

    # 6. записываем обновлённый индекс в файл
    write_index(new_entries)


def _build_tree_dict(entries: List[IndexEntry]) -> Dict[str, Any]:
    """
    Вспомогательная функция.
    Из списка записей индекса (path, sha, mode) строит
    вложенный словарь, похожий на структуру папок.

    Args:
        entries (List[IndexEntry]): Список записей индекса.

    Returns:
        Dict[str, Any]: Вложенный словарь, представляющий структуру
        каталогов и файлов.
    """
    root: Dict[str, Any] = {}

    for path, sha, mode in entries:
        parts = Path(path).parts
        node = root
        # идём по всем частям пути, кроме последней (это имя файла)
        for part in parts[:-1]:
            # создаём под-словарь для каталога, если его ещё нет
            node = node.setdefault(part, {})
            # setdefault — если в текущем словаре node нет ключа part,
            # создаёт запись вида part: {}.
            # возвращает словарь по ключу part.
        # последняя часть — файл
        filename = parts[-1]
        node[filename] = ("file", sha, mode)

    return root


def _walk_dirs(
    node: Dict[str, Any],
    prefix: str = "",
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """
    Генератор, который рекурсивно обходит структуру директорий.

    Идея:
      - сначала обходит все подкаталоги (рекурсивно)
      - потом "yield" текущую директорию

    Таким образом, мы получаем директории в порядке:
      сначала самые глубокие, потом те, что выше.
    Это удобно, потому что родительское tree зависит от SHA поддеревьев.

    Args:
        node (Dict[str, Any]): Текущий узел дерева (каталог).
        prefix (str): Префикс пути для текущего каталога.

    Yields:
        Tuple[str, Dict[str, Any]]: Пара (prefix, node) для каждой
        директории.
    Примечание:
        yield приостанавливает функцию, возвращает значение
        и сохраняет состояние. При следующем вызове функция
        возобновляет свою работу с того места, где остановилась.

    """
    # Сначала найдём все подкаталоги (там, где значение — dict)
    for name, child in sorted(node.items()):
        if isinstance(child, dict):  # если child — dict, значит это подкаталог
            # формируем новый префикс для подкаталога
            new_prefix = f"{prefix}{name}/"
            # рекурсивно обходим подкаталог
            # yield from позволяет "пробросить" все yield'ы
            # внутреннего генератора
            yield from _walk_dirs(child, new_prefix)

    # После всех подкаталогов — отдаём текущую директорию
    # node — это содержимое директории (файлы + подкаталоги)
    # prefix — её путь (например "", "dir/", "dir/sub/")
    yield prefix, node


def write_tree() -> str:
    """
    Строит объекты tree на основе текущего состояния индекса
    и возвращает SHA-1 КОРНЕВОГО tree.

    Шаги:
      1. читаем индекс
      2. строим вложенную структуру каталогов
      3. с помощью генератора _walk_dirs обходим директории
         от самых глубоких к корню
      4. для каждой директории создаём Tree-объект, сохраняем его
         через hash_object и запоминаем его SHA
      5. возвращаем SHA корневого дерева (для пути prefix == "")

    Returns:
        str: SHA-1 корневого tree-объекта.
    """
    entries = read_index()

    # Если индекс пустой — создадим пустое дерево
    if not entries:
        empty_tree = Tree(entries=[])
        tree_sha = hash_object(empty_tree.serialize(), "tree")
        return tree_sha

    # 2. Строим вложенную структуру (как дерево словарей)
    tree_dict = _build_tree_dict(entries)

    # Сюда будем записывать: "путь директории" → "sha её tree-объекта"
    # Примеры ключей:
    #   ""          → корневая директория
    #   "dir/"      → подкаталог dir
    #   "dir/sub/"  → подкаталог dir/sub
    dir_to_sha: Dict[str, str] = {}

    # 3–4. Обходим все директории от глубоких к верхним
    for dirpath, node in _walk_dirs(tree_dict, prefix=""):
        tree_entries: List[TreeEntry] = []

        # Сначала добавляем записи для подкаталогов
        for name, child in sorted(node.items()):
            if isinstance(child, dict):
                # для подкаталога мы уже посчитали SHA его tree ранее,
                # потому что генератор выдаёт "детей" раньше "родителя"
                subdir_prefix = f"{dirpath}{name}/"  # пути к подкаталогу
                subdir_sha = dir_to_sha[subdir_prefix]
                tree_entries.append(
                    TreeEntry(
                        mode=TREE_DIRECTORY_MODE,  # стандартный режим
                        # для директории (tree)
                        name=name,
                        sha=subdir_sha,
                    )
                )

        # Затем добавляем записи для файлов
        for name, child in sorted(node.items()):
            if not isinstance(child, dict):
                kind, sha, mode = child  # kind="file", sha=..., mode=...
                tree_entries.append(
                    TreeEntry(
                        mode=mode,
                        name=name,
                        sha=sha,
                    )
                )

        # Создаём объект Tree для текущей директории
        tree_obj = Tree(entries=tree_entries)
        tree_data = tree_obj.serialize()

        # Сохраняем tree как Git-объект, получаем его SHA
        tree_sha = hash_object(tree_data, "tree")

        # Запоминаем для этой директории
        dir_to_sha[dirpath] = tree_sha

    # 5. Корневая директория имеет путь "" (пустая строка)
    return dir_to_sha[""]
