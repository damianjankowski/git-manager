SHELL := /bin/bash

# Environment Variables
# -----------------------------------------------------------------------------
# Optional: keep GITLAB_TOKEN in .env instead of exporting it in the shell.
ENV_FILE := .env

ifneq (,$(wildcard $(ENV_FILE)))
    include $(ENV_FILE)
    export
endif

# User Variables
# -----------------------------------------------------------------------------
# Hosts, groups and base directories live in the YAML config.
CONFIG ?= $(if ${GIT_MANAGER_CONFIG},${GIT_MANAGER_CONFIG},git-manager.yaml)

# Narrow a run down. HOST is required when the config holds several hosts,
# because GITLAB_TOKEN can only authenticate against one of them.
HOST ?=
GROUP ?=

FILTERS := --config $(CONFIG) $(if $(HOST),--host $(HOST),) $(if $(GROUP),--group $(GROUP),)

# Colored Output
# -----------------------------------------------------------------------------
COLOR_RESET := \033[0m
COLOR_RED   := \033[0;31m
COLOR_GREEN := \033[0;32m
COLOR_BLUE  := \033[0;34m
COLOR_CYAN  := \033[36m

# Application Configuration
# -----------------------------------------------------------------------------
PACKAGE_NAME := git_manager
MAIN		 := $(PACKAGE_NAME)/main.py

# Default Goal
# -----------------------------------------------------------------------------
.DEFAULT_GOAL := help

# Help
# -----------------------------------------------------------------------------
.PHONY: help
help:  ## Display this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} \
		/^[a-zA-Z_-]+:.*?##/ { \
			printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2 \
		} \
		/^##@/ { \
			printf "\n%s\n", substr($$0, 5) \
		} ' $(MAKEFILE_LIST)

##@ Development
# -----------------------------------------------------------------------------
.PHONY: venv
venv: ## Create a Poetry virtual environment in the project.
	@echo -e "${COLOR_GREEN}Configuring Poetry virtual environment in project...${COLOR_RESET}"
	poetry config virtualenvs.in-project true
	poetry env use python3

.PHONY: install
install: venv ## Install dependencies.
	@echo -e "${COLOR_GREEN}Installing dependencies from pyproject.toml...${COLOR_RESET}"
	poetry install

.PHONY: clean
clean: ## Clean environment by removing specific files and directories.
	@echo -e "${COLOR_RED}Removing Python cache files and virtual environment...${COLOR_RESET}"
	@find . -name '__pycache__' -exec rm -rf {} +
	@find . -name '*.pyc' -exec rm -rf {} +
	@rm -rf .venv .mypy_cache .pytest_cache dist build
	@echo -e "${COLOR_RED}Removing other unwanted files...${COLOR_RESET}"
	@find . -name 'Thumbs.db' -exec rm -rf {} +
	@find . -name '*~' -exec rm -rf {} +

.PHONY: pre-commit
pre-commit: ## Run pre-commit checks on all files.
	@echo -e "${COLOR_RED}Running pre-commit checks...${COLOR_RESET}"
	poetry run pre-commit run --all-files

##@ Ops
# -----------------------------------------------------------------------------
.PHONY: config
config: ## Show the resolved config targets without touching any repository.
	@echo -e "${COLOR_CYAN}Config: $(CONFIG)${COLOR_RESET}"
	@cat $(CONFIG)

.PHONY: sync
sync: ## Sync repositories. Usage: make sync HOST=gitlab.com [GROUP=my-org]
	@echo -e "${COLOR_GREEN}Syncing GitLab group repositories...${COLOR_RESET}"
	poetry run python $(MAIN) --sync $(FILTERS)

.PHONY: sync-all
sync-all: ## Sync every host in the config. GITLAB_TOKEN must be valid for each.
	@echo -e "${COLOR_GREEN}Syncing all hosts from $(CONFIG)...${COLOR_RESET}"
	@echo -e "${COLOR_RED}GITLAB_TOKEN must be valid for every host in the config.${COLOR_RESET}"
	poetry run python $(MAIN) --sync --all-hosts --config $(CONFIG)

.PHONY: clone
clone: ## Clone repositories. Usage: make clone HOST=gitlab.com [GROUP=my-org]
	@echo -e "${COLOR_GREEN}Cloning GitLab group repositories...${COLOR_RESET}"
	poetry run python $(MAIN) --clone $(FILTERS)

.PHONY: cleanup-branches
cleanup-branches: ## Clean up old branches. Usage: make cleanup-branches HOST=... [GROUP=...]
	@echo -e "${COLOR_BLUE}Cleaning up old branches in group repositories...${COLOR_RESET}"
	poetry run python $(MAIN) --cleanup $(FILTERS)
