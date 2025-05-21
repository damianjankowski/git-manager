SHELL := /bin/bash

# Environment Variables
# -----------------------------------------------------------------------------
ENV_FILE := .env

ifneq (,$(wildcard $(ENV_FILE)))
    include $(ENV_FILE)
    export
endif

# User Variables
# -----------------------------------------------------------------------------
GITLAB_TOKEN := ${GITLAB_TOKEN}

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
.PHONY: sync
sync: ## Sync GitLab group repositories with the local machine.
	@echo -e "${COLOR_GREEN}Syncing GitLab group repositories...${COLOR_RESET}"
	poetry run python $(MAIN) --sync --group_id $(GROUP_ID) --group_directory $(GROUP_DIRECTORY)

.PHONY: clone
clone: ## Clone GitLab group repositories.
	@echo -e "${COLOR_GREEN}Cloning GitLab group repositories...${COLOR_RESET}"
	poetry run python $(MAIN) --clone --group_id $(GROUP_ID) --group_directory $(GROUP_DIRECTORY)

.PHONY: cleanup-branches
cleanup-branches: ## Clean up old branches.
	@echo -e "${COLOR_BLUE}Updating (pulling) GitLab group repositories...${COLOR_RESET}"
	poetry run python $(MAIN) --cleanup --group_directory $(GROUP_DIRECTORY)
