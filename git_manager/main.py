import argparse
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Dict, Final, List, Optional, Set, TypeAlias

import requests
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
        self, url: str, params: Optional[Dict[str, str]] | Optional[Dict[str, bool]] = None
    ):
        pass

    @abstractmethod
    def get_group_repositories(self):
        pass


class GitLabRepo(GitLabClient):
    def __init__(self, group_id: str):
        super().__init__(group_id)
        self._token = self._get_token()
        self._headers = {"PRIVATE-TOKEN": self._token}
        self._session = self._get_session()

    def _get_token(self):
        token = os.environ.get("GITLAB_TOKEN")
        if not token:
            logging.error("GITLAB_TOKEN is not set in environment variables.")
            raise EnvironmentError("GITLAB_TOKEN is not set in environment variables.")
        return token

    def _get_session(self):
        session = requests.Session()
        session.headers.update(self._headers)
        return session

    def get_json_response(
        self, url: str, params: Optional[Dict[str, str]] | Optional[Dict[str, bool]] = None
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
        url = f"https://gitlab.com/api/v4/groups/{self.group_id}/projects"

        try:
            logging.info("Retriving GitLab group repositories...")
            projects = self.get_json_response(url, params={"include_subgroups": True})
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
    def __init__(self, group_directory: Path, gitlab: GitLabRepo):
        self.group_directory = group_directory.resolve()
        self.gitlab = gitlab

    @cached_property
    def repositories(self):
        group_repository = RepositoryGroup(self.group_directory)
        logging.info("Loading local repositories...")
        return [
            Repository(repo_path) for repo_path in group_repository.find_local_repos().values()
        ]

    @property
    def base_path(self):
        return Path(
            str(self.group_directory).removesuffix(f"/{self.gitlab.group_id}").rstrip("/")
        )

    def _map_gitlab_group_repos_to_absolute_path(
        self, gitlab_repositories: Dict[str, str]
    ) -> Set[Path]:
        return {
            (
                (self.base_path / Path(path)).resolve()
                if not Path(path).is_absolute()
                else Path(path).resolve()
            )
            for path in gitlab_repositories.keys()
        }

    def _identify_repos_to_delete(
        self, local_repositories: Dict[str, Path], mapped_gitlab_repositories: Set[Path]
    ) -> List[Path]:
        repos_to_delete = [
            full_path
            for relative_path, full_path in local_repositories.items()
            if full_path.resolve() not in mapped_gitlab_repositories
        ]
        for repo in repos_to_delete:
            logging.info(f"Repository to delete: {repo} (not found on GitLab)")
        return repos_to_delete

    def sync(self):
        self.clone_group_repositories()
        gitlab_repositories = self.gitlab.get_group_repositories()
        mapped_gitlab_repositories = self._map_gitlab_group_repos_to_absolute_path(
            gitlab_repositories
        )
        gp = RepositoryGroup(self.group_directory)
        local_repositories = gp.find_local_repos()
        to_delete = self._identify_repos_to_delete(
            local_repositories=local_repositories,
            mapped_gitlab_repositories=mapped_gitlab_repositories,
        )

        if to_delete:
            for directory in to_delete:
                remove_directory(directory)
            logging.info("Repositories were deleted")
        else:
            logging.info("Lack of repositories to delete!")

    def clone_group_repositories(self):
        logging.info("Cloning group repositories from GitLab...")
        try:
            cmd = ["glab", "repo", "clone", "-g", self.gitlab.group_id, "-p", "--paginate"]

            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE, cwd=self.group_directory
            )

            for line in process.stdout:
                print(line, end="")

            stdout, stderr = process.communicate()
            print(stdout)
            import sys

            if stderr:
                print(stderr, file=sys.stderr)

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


def main():
    try:
        args_parser = argparse.ArgumentParser()
        args_parser.add_argument(
            "--group_directory",
            type=Path,
            default=Path(os.getenv("GROUP_DIRECTORY", Path.cwd())),
            help="Base directory for locating repositories (default: current working directory)",
        )
        args_parser.add_argument("--group_id", type=str, default=os.getenv("GROUP_ID", ""))
        args_parser.add_argument("--cleanup", action="store_true", help="Cleanup old branches")
        args_parser.add_argument("--sync", action="store_true", help="Sync repositories")
        args_parser.add_argument("--clone", action="store_true", help="Clone group repository")
        parser = args_parser.parse_args()

        check_dependencies("glab")

        if not os.path.isdir(Path(parser.group_directory)):
            create_directory(Path(parser.group_directory))

        if parser.cleanup:
            repositories = [
                Repository(repo_path)
                for repo_path in RepositoryGroup(parser.group_directory)
                .find_local_repos()
                .values()
            ]
            repo = RepoManageService(
                group_directory=parser.group_directory, repositories=repositories
            )
            repo.prune()

        if parser.sync:
            gitlab = GitLabRepo(parser.group_id)
            repo = GitLabService(gitlab=gitlab, group_directory=parser.group_directory)
            repo.sync()

        if parser.clone:
            gitlab = GitLabRepo(parser.group_id)
            repo = GitLabService(gitlab=gitlab, group_directory=parser.group_directory)
            repo.clone_group_repositories()

    except EnvironmentError:
        exit(1)

    except Exception as e:
        logging.error(f"Unexpected error in main: {e}")
        exit(1)


if __name__ == "__main__":
    main()
