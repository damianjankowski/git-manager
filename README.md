# Git Manager

Tool for synchronizing local Git repositories with their remote within a GitLab group.
Supports several GitLab instances — a self-managed host and gitlab.com — from one
YAML configuration.

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

## Configuration

Hosts, their groups and their target directories live in `git-manager.yaml` (gitignored).
Copy the template and adjust:

```
cp git-manager.example.yaml git-manager.yaml
```

```yaml
defaults:
  include_archived: false
  # Optional fallback for hosts without their own base_directory
  # base_directory: /Users/user/repo

hosts:
  - host: gitlab.example.com
    base_directory: /Users/user/repo
    groups:
      - platform
      - backend

  - host: gitlab.com
    base_directory: /Users/user/gitlab.com
    groups:
      - my-org
```

Each host has its own `base_directory`; repositories land in
`<base_directory>/<group>`. `~` is expanded. Two hosts may reuse a group name as
long as their base directories differ — the config is rejected if they collide.

`GITLAB_TOKEN` is read from the environment — export it in your shell (for example
via the 1Password CLI) — and is used by both the REST API client and `glab`. It
therefore has to belong to the host being processed, so run one host at a time
unless the same token is valid everywhere.

## Usage

```
make sync HOST=gitlab.com                      # every group of one host
make sync HOST=gitlab.com GROUP=my-org         # a single group
make clone HOST=gitlab.example.com             # clone only, no deletions
make cleanup-branches HOST=gitlab.example.com  # prune old branches
make sync-all                                  # every host in the config
make config                                    # show the config in use
```

### Direct invocation

```
poetry run python git_manager/main.py --sync --host gitlab.com --group my-org
```

### Parameters

- `--config`: Path to the YAML config (default: `git-manager.yaml` in the current
  directory, or `$GIT_MANAGER_CONFIG`).
- `--host`: Only act on this host from the config. Required when the config lists
  more than one host, unless `--all-hosts` is given.
- `--group`: Only act on this group from the config.
- `--all-hosts`: Act on every host in the config. Needs a token valid for each.
- `--sync`: Synchronize local repositories with GitLab (clone + confirm deletions).
- `--clone`: Clone all repositories from the group.
- `--cleanup`: Prune old branches.
- `--verbose` / `-v`: Debug logging.

### Requirements

- A valid GitLab access token in `GITLAB_TOKEN`.
- `glab` installed and able to authenticate against the host being processed.
