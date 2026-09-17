"""YAML configuration for the GitLab hosts and groups this tool manages."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Final, List, Optional, Tuple

import yaml
from loguru_logger import logging

DEFAULT_CONFIG_FILENAMES: Final[Tuple[str, ...]] = (
    "git-manager.yaml",
    "git-manager.yml",
)


class ConfigError(Exception):
    """Raised when the configuration file is missing, malformed or incomplete."""


def normalize_gitlab_host(gitlab_host: str) -> str:
    """Reduce a host to the bare form both the REST API and `glab` expect."""
    host = gitlab_host.strip()
    host = host.replace("https://", "").replace("http://", "")
    return host.rstrip("/")


def _parse_base_directory(value: Any, where: str) -> Optional[Path]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.base_directory must be a non-empty string")
    return Path(value.strip()).expanduser()


@dataclass(frozen=True)
class HostConfig:
    host: str
    base_directory: Path
    groups: Tuple[str, ...]

    @classmethod
    def from_dict(
        cls, raw: Any, index: int, default_base_directory: Optional[Path]
    ) -> "HostConfig":
        where = f"hosts[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be a mapping, got {type(raw).__name__}")

        unknown = set(raw) - {"host", "base_directory", "groups"}
        if unknown:
            raise ConfigError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")

        host = raw.get("host")
        if not isinstance(host, str) or not host.strip():
            raise ConfigError(f"{where}.host is required and must be a non-empty string")

        base_directory = _parse_base_directory(raw.get("base_directory"), where)
        if base_directory is None:
            base_directory = default_base_directory
        if base_directory is None:
            raise ConfigError(
                f"{where}.base_directory is required "
                f"(or set defaults.base_directory for all hosts)"
            )

        groups = raw.get("groups")
        if not isinstance(groups, list) or not groups:
            raise ConfigError(f"{where}.groups is required and must be a non-empty list")
        for group in groups:
            if not isinstance(group, str) or not group.strip():
                raise ConfigError(f"{where}.groups entries must be non-empty strings")

        return cls(
            host=normalize_gitlab_host(host),
            base_directory=base_directory,
            groups=tuple(group.strip() for group in groups),
        )


@dataclass(frozen=True)
class Target:
    """A single host/group pair to operate on."""

    host: str
    group: str
    base_directory: Path

    @property
    def group_directory(self) -> Path:
        return self.base_directory / self.group


@dataclass(frozen=True)
class AppConfig:
    include_archived: bool
    hosts: Tuple[HostConfig, ...]

    @property
    def base_directories(self) -> Tuple[Path, ...]:
        """Every distinct directory this config writes into."""
        return tuple(dict.fromkeys(h.base_directory for h in self.hosts))

    @classmethod
    def from_dict(cls, raw: Any, source: Path) -> "AppConfig":
        if not isinstance(raw, dict):
            raise ConfigError(f"{source}: top level must be a mapping")

        unknown = set(raw) - {"defaults", "hosts"}
        if unknown:
            raise ConfigError(f"{source}: unknown keys: {', '.join(sorted(unknown))}")

        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise ConfigError(f"{source}: defaults must be a mapping")
        unknown_defaults = set(defaults) - {"base_directory", "include_archived"}
        if unknown_defaults:
            raise ConfigError(
                f"{source}: defaults has unknown keys: {', '.join(sorted(unknown_defaults))}"
            )

        default_base_directory = _parse_base_directory(
            defaults.get("base_directory"), f"{source}: defaults"
        )
        include_archived = defaults.get("include_archived", False)
        if not isinstance(include_archived, bool):
            raise ConfigError(f"{source}: defaults.include_archived must be a boolean")

        hosts_raw = raw.get("hosts")
        if not isinstance(hosts_raw, list) or not hosts_raw:
            raise ConfigError(f"{source}: hosts is required and must be a non-empty list")

        hosts = tuple(
            HostConfig.from_dict(host_raw, index, default_base_directory)
            for index, host_raw in enumerate(hosts_raw)
        )

        duplicates = {h.host for h in hosts if [x.host for x in hosts].count(h.host) > 1}
        if duplicates:
            raise ConfigError(f"{source}: duplicate hosts: {', '.join(sorted(duplicates))}")

        for host_config in hosts:
            collisions = [
                other
                for other in hosts
                if other.host != host_config.host
                and other.base_directory == host_config.base_directory
                and set(other.groups) & set(host_config.groups)
            ]
            for other in collisions:
                shared = ", ".join(sorted(set(other.groups) & set(host_config.groups)))
                raise ConfigError(
                    f"{source}: hosts '{host_config.host}' and '{other.host}' share "
                    f"base_directory {host_config.base_directory} and group(s) {shared}. "
                    f"Give them different base_directory values."
                )

        return cls(include_archived=include_archived, hosts=hosts)

    def select(self, host: Optional[str] = None, group: Optional[str] = None) -> List[Target]:
        """Resolve the host/group pairs to act on, narrowed by the given filters."""
        hosts = self.hosts
        if host:
            wanted = normalize_gitlab_host(host)
            hosts = tuple(h for h in self.hosts if h.host == wanted)
            if not hosts:
                known = ", ".join(h.host for h in self.hosts)
                raise ConfigError(f"Host '{wanted}' is not in the config. Known hosts: {known}")

        targets = [
            Target(host=h.host, group=g, base_directory=h.base_directory)
            for h in hosts
            for g in h.groups
        ]

        if group:
            targets = [t for t in targets if t.group == group]
            if not targets:
                scope = f"host '{host}'" if host else "the config"
                raise ConfigError(f"Group '{group}' is not listed for {scope}")

        return targets


def find_config(explicit_path: Optional[Path] = None) -> Path:
    """Locate the config file: an explicit path, else a default name in the cwd."""
    if explicit_path:
        if not explicit_path.is_file():
            raise ConfigError(f"Config file not found: {explicit_path}")
        return explicit_path

    for name in DEFAULT_CONFIG_FILENAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate

    raise ConfigError(
        f"No config file found. Expected one of {', '.join(DEFAULT_CONFIG_FILENAMES)} "
        f"in {Path.cwd()}, or pass --config. "
        f"See git-manager.example.yaml for the format."
    )


def load_config(explicit_path: Optional[Path] = None) -> AppConfig:
    path = find_config(explicit_path)
    logging.info(f"Loading configuration from: {path}")

    try:
        raw: Dict[str, Any] = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    except OSError as e:
        raise ConfigError(f"Cannot read config file {path}: {e}") from e

    if raw is None:
        raise ConfigError(f"{path}: config file is empty")

    return AppConfig.from_dict(raw, source=path)
