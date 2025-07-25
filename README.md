# Git Manager

Tool for synchronizing local Git repositories with their remote within a GitLab group.

## Features

- Synchronize local repositories with GitLab
- Automatically clone new repositories
- Delete local repositories that do not exist on GitLab
- Prune old branches

## Installation

Install dependencies using Poetry:

```
poetry install
```

## Usage

### Basic Usage

```
poetry run python main.py --sync --group_id your-group-id --group_directory /path/to/group
```

### Parameters

- `--group_id`: GitLab group ID or full path.
- `--group_directory`: Path to the GitLab group directory.
- `--cleanup`: Prune old branches.
- `--sync`: Synchronize local repositories with GitLab.
- `--clone`: Clone all repositories from the GitLab group.

### Requirements

- A valid GitLab access token (`GITLAB_TOKEN`) must be set in the environment variables.
