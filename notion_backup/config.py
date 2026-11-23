"""config.yml 읽기(명세서 7.1절).

파일이 없거나 항목이 빠지면 기본값을 쓰고, 형식이 틀리면 어느 줄이 틀렸는지 알려준다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .ids import normalize_id


class ConfigError(Exception):
    pass


@dataclass
class Config:
    roots: list[str] = field(default_factory=list)
    exclude: set[str] = field(default_factory=set)
    max_delete_ratio: float = 0.3
    min_delete_count: int = 5
    max_image_mb: float = 20
    date_prefix: bool = True  # 데이터베이스 항목 파일 이름 앞에 날짜 붙이기
    date_property: dict[str, str] = field(default_factory=dict)  # 데이터베이스 ID -> 쓸 날짜 속성 이름


def _line_of(node, path: list[str]) -> int | None:
    """YAML 노드 트리에서 키 경로의 줄 번호(1부터)."""
    current = node
    for key in path:
        if not isinstance(current, yaml.MappingNode):
            return None
        for k, v in current.value:
            if getattr(k, "value", None) == key:
                current = v
                break
        else:
            return None
    return current.start_mark.line + 1 if current is not None else None


def _fail(root, path: list[str], message: str):
    line = _line_of(root, path)
    where = f"{line}번째 줄" if line else "/".join(path)
    raise ConfigError(f"config.yml {where}: {message}")


def _id_list(data: dict, root, key: str) -> list[str]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        _fail(root, [key], f"`{key}`는 목록이어야 합니다(각 줄을 `- `로 시작).")
    out = []
    for item in value:
        page_id = normalize_id(str(item)) if item is not None else None
        if not page_id:
            _fail(root, [key], f"`{key}`의 `{item}`에서 노션 페이지 ID를 찾지 못했습니다.")
        out.append(page_id)
    return out


def _number(section: dict, root, path: list[str], default, kind):
    value = section.get(path[-1], default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(root, path, f"`{'.'.join(path)}`는 숫자여야 합니다.")
    if value < 0:
        _fail(root, path, f"`{'.'.join(path)}`는 0 이상이어야 합니다.")
    return kind(value)


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    text = path.read_text(encoding="utf-8")
    try:
        root = yaml.compose(text)
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{mark.line + 1}번째 줄" if mark else "형식"
        raise ConfigError(f"config.yml {where}: YAML 형식이 올바르지 않습니다 ({getattr(exc, 'problem', exc)}).") from exc
    if data is None:
        return Config()
    if not isinstance(data, dict):
        raise ConfigError("config.yml 1번째 줄: 최상위는 `roots:`, `exclude:` 같은 항목이어야 합니다.")

    cfg = Config()
    cfg.roots = _id_list(data, root, "roots")
    cfg.exclude = set(_id_list(data, root, "exclude"))

    safety = data.get("safety") or {}
    if not isinstance(safety, dict):
        _fail(root, ["safety"], "`safety`는 항목 묶음이어야 합니다.")
    cfg.max_delete_ratio = _number(safety, root, ["safety", "max_delete_ratio"], cfg.max_delete_ratio, float)
    cfg.min_delete_count = _number(safety, root, ["safety", "min_delete_count"], cfg.min_delete_count, int)

    images = data.get("images") or {}
    if not isinstance(images, dict):
        _fail(root, ["images"], "`images`는 항목 묶음이어야 합니다.")
    cfg.max_image_mb = _number(images, root, ["images", "max_size_mb"], cfg.max_image_mb, float)

    files = data.get("database_files") or {}
    if not isinstance(files, dict):
        _fail(root, ["database_files"], "`database_files`는 항목 묶음이어야 합니다.")
    prefix = files.get("date_prefix", True)
    if not isinstance(prefix, bool):
        _fail(root, ["database_files", "date_prefix"], "`database_files.date_prefix`는 true 또는 false여야 합니다.")
    cfg.date_prefix = prefix
    props = files.get("date_property") or {}
    if not isinstance(props, dict):
        _fail(root, ["database_files", "date_property"],
              "`database_files.date_property`는 `데이터베이스 링크: 속성 이름` 형식이어야 합니다.")
    for link, name in props.items():
        db_id = normalize_id(str(link))
        if not db_id:
            _fail(root, ["database_files", "date_property"], f"`{link}`에서 노션 데이터베이스 ID를 찾지 못했습니다.")
        if not isinstance(name, str) or not name.strip():
            _fail(root, ["database_files", "date_property"], f"`{link}`의 속성 이름이 비어 있습니다.")
        cfg.date_property[db_id] = name.strip()
    return cfg
