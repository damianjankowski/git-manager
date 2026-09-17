import argparse
import os
import re
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Dict, Final, List, Optional, Set, TypeAlias
from urllib.parse import quote

import requests
from app_config import AppConfig, ConfigError, Target, load_config, normalize_gitlab_host
from git import Repo
from loguru_logger import logging

BranchName: TypeAlias = str
UnixTimestamp: TypeAlias = int


@dataclass(frozen=True)
class Config:
    PROTECTED_BRANCHES: Final[frozenset[str]] = frozenset({"master", "main", "develop"})
    DAYS_OLD_THRESHOLD: Final[int] = 0
    DEFAULT_BACKUP_COMMIT_MESSAGE: Final[str] = "pruner: auto backup"
    HEAD_BRANCH_KEYWORD: Final[str] = "HEAD branch"
    GIT_TIMEOUT: Final[int] = 30


config = Config()


class GitLabAPIError(Exception):
    pass


def run_command(
    cmd: List[str] | str,
    path: Optional[Path] = None,
    check: bool = True,
    text: bool = True,
    shell: bool = False,
    capture_output: bool = True,
    timeout: int = config.GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """
    Execute a command in a subprocess with proper error logging.

    This implementation does not wrap exceptions in custom types.

    Args:
        cmd: Command to execute as a list of strings or a single string.
        path: Working directory for command execution.
        check: Raise CalledProcessError if the return code is non-zero.
        text: Return output as text.
        shell: Execute command through shell.
        capture_output: Capture stdout and stderr.
        timeout: Command timeout in seconds.

    Returns:
        A subprocess.CompletedProcess instance.

    Raises:
        subprocess.TimeoutExpired: When the command times out.
        subprocess.CalledProcessError: When the command fails (if check=True).
        OSError: For OS-related errors.
    """
    try:
        env = os.environ.copy()
        env["GIT_HTTP_CONNECT_TIMEOUT"] = str(timeout)

        if shell and isinstance(cmd, list):
            cmd = " ".join(cmd)

        return subprocess.run(
            cmd,
            cwd=str(path) if path else None,
            check=check,
            text=text,
            shell=shell,
            capture_output=capture_output,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logging.error(f"Command timed out after {timeout}s: cmd: {cmd}, path: {path}")
        raise
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed with code {e.returncode}: cmd: {cmd}, path: {path}")
        logging.error(f"Error output: {e.stderr}")
        raise
    except OSError as e:
        logging.error(f"Failed to execute command {path}: cmd: {cmd}, path: {path}")
        logging.error(f"OS error: {e.strerror}")
        raise


def remove_directory(directory: Path):
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError as e:
        logging.error(f"Failed to delete: {directory} - {e}")


class RepositoryGroup:
    def __init__(self, group_directory_path: Path):
        self.group_directory = group_directory_path

    def find_local_repos(self) -> Dict[str, Path]:
        logging.info("Retriving local repositories...")
        git_repos = {}
        search_dirs = [self.group_directory]
        for search_dir in search_dirs:
            if not search_dir.is_dir():
                logging.warning(f"Search directory does not exist: {search_dir}")
                continue
            for root, dirs, files in os.walk(search_dir):
                root_path = Path(root)
                if (root_path / ".git").is_dir():
                    repo_path = root_path.resolve()
                    relative_path = repo_path.relative_to(self.group_directory)
                    git_repos[str(relative_path)] = repo_path
                    dirs[:] = [d for d in dirs if d != ".git"]
        return git_repos


class Repository:
    def __init__(self, repository_path: Path):
        self.path: Path = repository_path
        self._repository: Repo | None = None

    @property
    def repository(self) -> Repo:
        if self._repository is None:
            self._repository = Repo(self.path)
        return self._repository

    def get_branches_with_commit_dates(self) -> Dict[str, int]:
        cmd = [
            "git",
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short) %(committerdate:unix)",
            "refs/heads/",
        ]
        output = run_command(cmd, path=self.path)
        branches = {}
        stdout = output.stdout.strip()

        if not stdout:
            logging.warning(f"No output received from git command in repository {self.path}.")
            return branches

        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) < 2 or not parts[1].isdigit():
                logging.warning(
                    f"Unexpected format for line: '{line}' in repository {self.path}. Skipping."
                )
                continue

            branch_name = parts[0]
            try:
                commit_timestamp = int(parts[1])
            except ValueError:
                logging.warning(
                    f"Unable to convert commit timestamp '{parts[1]}' "
                    f"to int for branch '{branch_name}' in repository {self.path}. Skipping."
                )
                continue

            branches[branch_name] = commit_timestamp

        for protected_branch in config.PROTECTED_BRANCHES:
            branches.pop(protected_branch, None)

        return branches

    def get_active_branch(self) -> str | None:
        try:
            return self.repository.active_branch.name
        except (TypeError, AttributeError) as e:
            logging.warning(f"Detached HEAD in {self.path}: {e}")
            return None

    def has_uncommitted_files(self) -> bool:
        return self.repository.is_dirty(untracked_files=True)

    def get_default_branch_name(self, name: str = "origin") -> str | None:
        try:
            show_result = self.repository.git.remote("show", name)
            matches = re.search(r"\s*HEAD branch:\s*(.*)", show_result)
            if matches:
                return matches.group(1)
        except ValueError as e:
            logging.error(
                f"Not able to determine default branch name for {self.path} due to {e}"
            )

    def safe_checkout(self) -> bool:
        try:
            if not getattr(self.repository.head, "is_valid", lambda: False)():
                logging.warning(f"Repository {self.path} has no commits, skipping checkout.")
                return False

            default_branch = self.get_default_branch_name()
            if not default_branch:
                logging.warning(f"No default branch found for {self.path}. Skipping checkout.")
                return False

            if self.has_uncommitted_files():
                logging.warning(
                    f"Uncommitted changes detected in {self.path}. Creating backup branch."
                )

                backup_branch = f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                self.repository.git.checkout("-b", backup_branch)
                self.repository.git.add(all=True)
                self.repository.git.commit("-m", config.DEFAULT_BACKUP_COMMIT_MESSAGE)

            self.repository.git.checkout(default_branch)
            logging.info(f"Checked out to {default_branch} in {self.path}")
            return True

        except Exception as e:
            logging.error(f"Failed to safely checkout in {self.path}: {e}")
            return False

    def delete_branch(self, branch_name, force: bool = True) -> bool:
        try:
            self.repository.delete_head(branch_name, force=force)
            logging.info(f"Deleted branch: {branch_name} in {self.path}")
            return True
        except Exception as e:
            logging.error(f"Not able to delete a branch {branch_name} in {self.path}: {e}")
            return False


class GitLabClient(ABC):
    def __init__(self, group_id):
        if not group_id:
            raise ValueError("group_id is required for GitLabClient")
        self.group_id = group_id

    @abstractmethod
    def get_json_response(
        self,
        url: str,
        params: Optional[Dict[str, str]] | Optional[Dict[str, bool]] = None,
    ):
        pass


class GitLabRepo(GitLabClient):
    def __init__(
        self,
        group_id: str,
        gitlab_host: str = "gitlab.com",
        include_archived: bool = False,
    ):
        super().__init__(group_id)
        self.gitlab_host = normalize_gitlab_host(gitlab_host)
        self.include_archived = include_archived
        self._token = self._get_token()
        self._headers = {"PRIVATE-TOKEN": self._token}
        self._session = self._get_session()

    def _get_token(self):
        token = os.environ.get("GITLAB_TOKEN")
        if not token:
            logging.error("GITLAB_TOKEN is not set in environment variables.")

            sys.exit(1)

        return token

    def _get_session(self):
        session = requests.Session()
        session.headers.update(self._headers)
        return session

    def get_json_response(
        self,
        url: str,
        params: Optional[Dict[str, str]] | Optional[Dict[str, bool]] = None,
    ) -> List[Dict]:
        results = []
        page = 1
        while True:
            current_params = params.copy() if params else {}
            current_params.update({"page": page, "per_page": 100})
            response = self._session.get(url, params=current_params)
            if response.status_code == 200:
                try:
                    data = response.json()
                    if not isinstance(data, list):
                        logging.error(f"Expected list, recivied:: {type(data)}")
                        raise GitLabAPIError(f"Expected a list, received: {type(data)}")
                except ValueError as e:
                    logging.error(f"Cannon decode JSON response from {url}: {e}")
                    raise GitLabAPIError(f"Cannon decode JSON response from {url}")

                if not data:
                    break

                results.extend(data)
                page += 1
            else:
                logging.error(f"Error {response.status_code} while accessing {url}")
                raise GitLabAPIError(f"Error {response.status_code} while accessing {url}")
        return results

    def get_group_repositories(self) -> Dict[str, str]:
        group_path = quote(str(self.group_id), safe="")
        url = f"https://{self.gitlab_host}/api/v4/groups/{group_path}/projects"

        params: Dict[str, bool] = {"include_subgroups": True}
        if not self.include_archived:
            params["archived"] = False

        try:
            logging.info(
                f"Retriving GitLab group repositories from {self.gitlab_host} "
                f"for group '{self.group_id}'..."
            )
            projects = self.get_json_response(url, params=params)
            return {
                project["path_with_namespace"]: project["http_url_to_repo"]
                for project in projects
            }
        except GitLabAPIError as e:
            logging.error(f"Failed to fetch group repositories: {e}")
            return {}


class RepoManageService:
    def __init__(self, group_directory: Path, repositories: Optional[List[Repository]] = None):
        self.group_directory = group_directory.resolve()
        self.group_repository = RepositoryGroup(self.group_directory)
        self.repositories = (
            repositories
            if repositories
            else [
                Repository(repo_path)
                for repo_path in self.group_repository.find_local_repos().values()
            ]
        )

    def prune(self) -> None:
        abnormal_state = []
        deleted = []
        not_deleted = []

        for repository in self.repositories:
            active_branch = repository.get_active_branch()

            if active_branch not in config.PROTECTED_BRANCHES:
                repository.safe_checkout()

            if active_branch is None:
                abnormal_state.append(" ".join(f"{repository}, {active_branch}"))

            all_branches = repository.get_branches_with_commit_dates()

            for branch_to_delete, commit_timestamp in all_branches.items():
                repository_age = (datetime.now().timestamp() - commit_timestamp) / 86400.0
                if repository_age < config.DAYS_OLD_THRESHOLD:
                    continue

                if repository.delete_branch(branch_to_delete):
                    deleted.append(f"{repository.path} -> {branch_to_delete}")
                else:
                    not_deleted.append(f"{repository.path} -> {branch_to_delete}")

        print("Branch cleanup summary:")
        print(
            f"Successfully deleted branches ({len(deleted)}):\n" + "\n".join(deleted)
            if deleted
            else "Lack of deleted branches"
        )
        print(
            f"Failed to delete branches ({len(not_deleted)}):\n" + "\n".join(not_deleted)
            if not_deleted
            else "No failures!"
        )
        print(
            f"Abnormal state detected in ({len(abnormal_state)} repositories):\n".join(
                abnormal_state
            )
            if abnormal_state
            else "No abnormal state!"
        )


class GitLabService:
    def __init__(
        self,
        base_directory: Path,
        group_id: str,
        gitlab: GitLabRepo,
        include_archived: bool = False,
    ):
        self.base_directory = base_directory.resolve()
        self.group_id = group_id
        self.group_directory = (self.base_directory / self.group_id).resolve()
        self.gitlab = gitlab
        self.include_archived = include_archived

        try:
            self.group_directory.relative_to(self.base_directory)
        except ValueError:
            raise ValueError(
                f"Group directory {self.group_directory} is not "
                f"within base directory {self.base_directory}"
            )

    def _ensure_group_directory_exists(self):
        """Ensure the group directory exists, creating it if necessary."""
        if not self.group_directory.exists():
            logging.info(f"Creating group directory: {self.group_directory}")
            self.group_directory.mkdir(parents=True, exist_ok=True)

    @cached_property
    def repositories(self):
        self._ensure_group_directory_exists()
        group_repository = RepositoryGroup(self.group_directory)
        logging.info(f"Loading local repositories from: {self.group_directory}")
        return [
            Repository(repo_path) for repo_path in group_repository.find_local_repos().values()
        ]

    def _map_gitlab_group_repos_to_absolute_path(
        self, gitlab_repositories: Dict[str, str]
    ) -> Set[Path]:
        mapped_paths = set()
        group_prefix = f"{self.group_id}/"

        for path in gitlab_repositories.keys():
            if path.startswith(group_prefix):
                local_path = path[len(group_prefix) :]
            else:
                local_path = path
            abs_path = (self.group_directory / Path(local_path)).resolve()
            mapped_paths.add(abs_path)
            logging.debug(f"Mapped GitLab path '{path}' -> Local path '{abs_path}'")

        return mapped_paths

    def _identify_repos_to_delete(
        self, local_repositories: Dict[str, Path], mapped_gitlab_repositories: Set[Path]
    ) -> List[Path]:
        repos_to_delete = [
            full_path
            for relative_path, full_path in local_repositories.items()
            if full_path.resolve() not in mapped_gitlab_repositories
        ]
        safe_repos_to_delete = []
        for repo in repos_to_delete:
            try:
                repo.relative_to(self.group_directory)
                safe_repos_to_delete.append(repo)
                relative_path = repo.relative_to(self.group_directory)
                logging.info(f"Repository to delete: {relative_path} (not found on GitLab)")
            except ValueError:
                logging.warning(f"Skipping repository outside group directory: {repo}")
        if safe_repos_to_delete:
            logging.warning(
                f"Found {len(safe_repos_to_delete)} repositories to delete "
                f"that don't exist on GitLab"
            )
        else:
            logging.info("All local repositories are synchronized with GitLab")

        return safe_repos_to_delete

    def sync(self):
        logging.info(
            f"Starting sync for group '{self.group_id}' in directory: {self.group_directory}"
        )

        self.clone_group_repositories()
        self._ensure_group_directory_exists()

        gitlab_repositories = self.gitlab.get_group_repositories()
        logging.info(
            f"Found {len(gitlab_repositories)} repositories "
            f"on GitLab for group '{self.group_id}'"
        )

        mapped_gitlab_repositories = self._map_gitlab_group_repos_to_absolute_path(
            gitlab_repositories
        )
        logging.info(
            f"Mapped {len(mapped_gitlab_repositories)} GitLab repositories to local paths"
        )
        gp = RepositoryGroup(self.group_directory)
        local_repositories = gp.find_local_repos()
        logging.info(f"Found {len(local_repositories)} local repositories in group directory")

        to_delete = self._identify_repos_to_delete(
            local_repositories=local_repositories,
            mapped_gitlab_repositories=mapped_gitlab_repositories,
        )

        if to_delete:
            print("\n" + "=" * 80)
            print(
                f"WARNING: The following repositories "
                f"from group '{self.group_id}' will be DELETED:"
            )
            print(f"Working in: {self.group_directory}")
            print("=" * 80)
            for i, directory in enumerate(to_delete, 1):
                relative_path = directory.relative_to(self.group_directory)
                print(f"{i:2d}. {relative_path}")
            print("=" * 80)
            print(f"Total: {len(to_delete)} repositories will be permanently removed")
            print(f"Group: {self.group_id}")
            print(f"Base directory: {self.group_directory}")
            print("=" * 80)
            print("\nSafety checks:")
            print(f"✓ All repositories are within group directory: {self.group_directory}")
            print(f"✓ Operations limited to group '{self.group_id}' only")
            print(f"✓ Other groups in {self.base_directory} will NOT be affected")

            while True:
                response = input(
                    f"\nType 'DELETE {self.group_id}' to confirm deletion (or 'no' to cancel): "
                ).strip()

                if response.lower() in ["no", "n", "cancel"]:
                    print("Deletion cancelled by user")
                    logging.info("Repository deletion cancelled by user")
                    return
                elif response == f"DELETE {self.group_id}":
                    break
                else:
                    print(
                        f"Please type exactly 'DELETE {self.group_id}' "
                        f"to confirm or 'no' to cancel"
                    )

            print("\nProceeding with deletion...")
            deleted_count = 0
            failed_deletions = []

            for directory in to_delete:
                try:
                    relative_path = directory.relative_to(self.group_directory)
                    remove_directory(directory)
                    deleted_count += 1
                    logging.info(f"Successfully deleted: {relative_path}")
                    print(f"✓ Deleted: {relative_path}")
                except Exception as e:
                    relative_path = directory.relative_to(self.group_directory)
                    failed_deletions.append((relative_path, str(e)))
                    logging.error(f"Failed to delete {relative_path}: {e}")
                    print(f"✗ Failed to delete: {relative_path}")

            print("\nDeletion Summary:")
            print(f"Successfully deleted {deleted_count} repositories")
            if failed_deletions:
                print(f"Failed to delete {len(failed_deletions)} repositories:")
                for dir_path, error in failed_deletions:
                    print(f"   - {dir_path}: {error}")
        else:
            logging.info(
                f"No repositories to delete - all local repositories "
                f"in group '{self.group_id}' exist on GitLab"
            )

    def clone_group_repositories(self):
        logging.info(
            f"Cloning repositories for group '{self.group_id}' into: {self.group_directory}"
        )

        try:
            cmd = [
                "glab",
                "repo",
                "clone",
                "-g",
                self.gitlab.group_id,
                "-p",
                "--paginate",
            ]
            if not self.include_archived:
                cmd.append("--archived=false")
            logging.info(f"Executing command: {' '.join(cmd)}")
            logging.info(f"Working directory: {self.base_directory}")
            logging.info(f"GitLab host: {self.gitlab.gitlab_host}")

            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GLAB_NO_INTERACTIVE"] = "1"
            # glab resolves the instance from GITLAB_HOST; without this it would
            # silently clone from whatever host the ambient environment points at.
            env["GITLAB_HOST"] = self.gitlab.gitlab_host

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd=self.base_directory,
                text=True,
                bufsize=1,
                universal_newlines=True,
                env=env,
            )

            cloned_count = 0
            skipped_count = 0
            error_count = 0
            while True:
                stderr_line = process.stderr.readline()
                if stderr_line == "" and process.poll() is not None:
                    break

                if stderr_line:
                    line = stderr_line.strip()
                    if not line:
                        continue
                    if "Cloning into" in line:
                        cloned_count += 1
                        repo_match = re.search(r"Cloning into '([^']+)'", line)
                        if repo_match:
                            repo_name = repo_match.group(1)
                            progress_msg = f"[{cloned_count}] Cloning: {repo_name}"
                            logging.info(progress_msg)
                        else:
                            logging.info(f"[{cloned_count}] {line}")

                    elif "already exists and is not an empty directory" in line:
                        skipped_count += 1
                        repo_match = re.search(r"'([^']+)'", line)
                        if repo_match:
                            repo_name = repo_match.group(1)
                            logging.info(f"[SKIP] Repository already exists: {repo_name}")
                        else:
                            logging.warning(f"Repository already exists: {line}")

                    elif 'Error: "exit status 128"' in line:
                        logging.debug(f"Clone status: {line}")

                    elif (
                        "remote:" in line
                        or "Receiving objects:" in line
                        or "Resolving deltas:" in line
                    ):
                        logging.debug(f"Git progress: {line}")

                    elif "error:" in line.lower() or "fatal:" in line.lower():
                        error_count += 1
                        logging.error(f"Clone error: {line}")

                    else:
                        logging.debug(f"Clone output: {line}")
            stdout_output = ""
            if process.stdout:
                stdout_output = process.stdout.read()

            if stdout_output:
                logging.info(f"Additional output: {stdout_output}")
            return_code = process.wait()

            logging.info("=" * 60)
            logging.info(f"Clone operation completed for group '{self.group_id}'!")
            logging.info("Summary:")
            logging.info(f"  - Group: {self.group_id}")
            logging.info(f"  - Directory: {self.group_directory}")
            logging.info(f"  - Repositories cloned: {cloned_count}")
            logging.info(f"  - Repositories skipped (already exist): {skipped_count}")
            logging.info(f"  - Errors encountered: {error_count}")
            logging.info(f"  - Exit code: {return_code}")
            logging.info("=" * 60)

            if return_code != 0:
                logging.warning(f"Process completed with non-zero exit code: {return_code}")

        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logging.error(f"Failed to clone group repositories: {e}")
            raise


def check_dependencies(dependency):
    from shutil import which

    if which(dependency) is None:
        logging.error(f"Dependency {dependency} is not installed!")
        raise EnvironmentError


def create_directory(path: Path):
    try:
        os.makedirs(path)
    except OSError as e:
        logging.error(f"Failed to create a directory: {path} due to {e}")
        raise EnvironmentError


def _configure_verbose_logging() -> None:
    from loguru import logger

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> |"
        " <level>{level: <8}</level> |"
        " <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
        "- <level>{message}</level>",
    )
    logging.debug("Debug logging enabled")


def _resolve_targets(
    config: AppConfig, host: Optional[str], group: Optional[str], all_hosts: bool
) -> List[Target]:
    """Pick the host/group pairs for this run, refusing to guess across hosts.

    A single GITLAB_TOKEN is shared by the REST client and `glab`, so a run that
    spans several hosts can only authenticate against one of them. Spanning hosts
    therefore has to be asked for explicitly.
    """
    if not host and not all_hosts and len(config.hosts) > 1:
        known = ", ".join(h.host for h in config.hosts)
        raise ConfigError(
            f"The config defines several hosts ({known}). Pass --host <host> to pick one, "
            f"or --all-hosts to run against every host in one go. "
            f"GITLAB_TOKEN must match the host being processed."
        )

    if all_hosts and len(config.hosts) > 1:
        logging.warning(
            "Running against all hosts with a single GITLAB_TOKEN - "
            "hosts the token does not belong to will fail to authenticate."
        )

    return config.select(host=host, group=group)


def _run_target(target: Target, config: AppConfig, actions: argparse.Namespace) -> None:
    logging.info("=" * 60)
    logging.info(f"Host: {target.host}")
    logging.info(f"Group: {target.group}")
    logging.info(f"Group directory: {target.group_directory}")
    logging.info("=" * 60)

    if actions.sync or actions.clone:
        gitlab_repo = GitLabRepo(
            group_id=target.group,
            gitlab_host=target.host,
            include_archived=config.include_archived,
        )
        gitlab_service = GitLabService(
            base_directory=target.base_directory,
            group_id=target.group,
            gitlab=gitlab_repo,
            include_archived=config.include_archived,
        )

        if actions.sync:
            gitlab_service.sync()

        if actions.clone:
            gitlab_service.clone_group_repositories()

    if actions.cleanup:
        logging.info(f"Running branch cleanup in: {target.group_directory}")
        RepoManageService(group_directory=target.group_directory).prune()


def main():
    args_parser = argparse.ArgumentParser(
        description="Synchronize local Git repositories with GitLab groups "
        "described in a YAML config."
    )
    args_parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ["GIT_MANAGER_CONFIG"])
        if os.environ.get("GIT_MANAGER_CONFIG")
        else None,
        help="Path to the YAML config (default: git-manager.yaml in the current directory).",
    )
    args_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Only act on this host from the config (e.g. gitlab.com).",
    )
    args_parser.add_argument(
        "--group",
        type=str,
        default=None,
        help="Only act on this group from the config (e.g. my-org).",
    )
    args_parser.add_argument(
        "--all-hosts",
        action="store_true",
        help="Act on every host in the config. Requires a token valid for each.",
    )
    args_parser.add_argument("--cleanup", action="store_true", help="Cleanup old branches")
    args_parser.add_argument("--sync", action="store_true", help="Sync repositories")
    args_parser.add_argument("--clone", action="store_true", help="Clone group repositories")
    args_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose (debug) logging",
    )
    parser = args_parser.parse_args()

    if parser.verbose:
        _configure_verbose_logging()

    if not (parser.sync or parser.clone or parser.cleanup):
        args_parser.error("Nothing to do: pass at least one of --sync, --clone, --cleanup.")

    try:
        check_dependencies("glab")

        config = load_config(parser.config)
        targets = _resolve_targets(
            config, host=parser.host, group=parser.group, all_hosts=parser.all_hosts
        )

        for base_directory in {t.base_directory for t in targets}:
            if not base_directory.is_dir():
                create_directory(base_directory)

        logging.info(f"Include archived: {config.include_archived}")
        logging.info(f"Targets: {len(targets)}")
        for target in targets:
            logging.info(f"  {target.host}/{target.group} -> {target.group_directory}")

        for target in targets:
            _run_target(target, config, parser)

    except (EnvironmentError, GitLabAPIError, ConfigError) as e:
        logging.error(f"An error occurred: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
